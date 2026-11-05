"""Personalized mistake trainer with SM-2 spaced repetition.

Every blunder/mistake/missed-win produced by a review becomes a permanent
training exercise, stored server-side (survives refreshes, container wipes are
noted separately - the database lives in data/). Multiple best moves are
accepted where the saved analysis shows them; the fixed accuracy calculation is
never re-run here.
"""
import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "training.db"
_lock = threading.Lock()   # every mutation is serialized through here
_conn = None

EXTRACT_CLASSES = {"blunder": 3, "miss": 2, "mistake": 1}
SEV = {"blunder": 3, "miss": 2, "mistake": 1}

# SM-2 parameters (chess variant, 4-point scale)
EASE_MIN, EASE_DEFAULT = 1.3, 2.5
AGAIN_INTERVAL_DAYS = 10 / 1440.0   # ~10 minutes: "Again" = learn it again, right away
EASY_BONUS = 1.3

EAGER = 86400.0


# ---------------------------------------------------------------- storage
def _connect():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute("""CREATE TABLE IF NOT EXISTS exercises (
            id TEXT PRIMARY KEY,
            fen TEXT NOT NULL,
            turn TEXT NOT NULL,
            color_side TEXT NOT NULL,
            played_uci TEXT NOT NULL,
            played_san TEXT NOT NULL,
            best_uci TEXT NOT NULL,
            best_san TEXT NOT NULL,
            cls TEXT NOT NULL,
            label TEXT,
            cp_before INTEGER, cp_after INTEGER,
            win_before REAL, win_after REAL,
            loss REAL, accuracy_cp REAL,
            pv_san TEXT,
            best_cp INTEGER, best_mate INTEGER,
            source_id TEXT NOT NULL, source_name TEXT DEFAULT '',
            source_date TEXT DEFAULT '', opening TEXT DEFAULT '', eco TEXT DEFAULT '',
            move_number INTEGER, ply INTEGER,
            first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
            removed INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS exercise_sources (
            ex_id TEXT NOT NULL, source_id TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (ex_id, source_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ex_id TEXT NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
            quality INTEGER NOT NULL,
            played_uci TEXT,
            solved INTEGER NOT NULL,
            auto INTEGER NOT NULL DEFAULT 0,
            ms INTEGER DEFAULT 0,
            attempted_at INTEGER NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule (
            ex_id TEXT PRIMARY KEY REFERENCES exercises(id) ON DELETE CASCADE,
            ease REAL NOT NULL DEFAULT 2.5,
            interval_days REAL NOT NULL DEFAULT 0,
            due_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            reps INTEGER NOT NULL DEFAULT 0,
            lapses INTEGER NOT NULL DEFAULT 0,
            total_correct INTEGER NOT NULL DEFAULT 0,
            total_attempts INTEGER NOT NULL DEFAULT 0,
            last_grade INTEGER, last_at INTEGER,
            suspended INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_due ON schedule(due_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ex_cls ON exercises(cls)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ex_src ON exercises(source_id)")
        # tolerant migration: older DBs get the lines column added, never dropped
        cols = {r[1] for r in conn.execute("PRAGMA table_info(exercises)")}
        if "lines_json" not in cols:
            conn.execute("ALTER TABLE exercises ADD COLUMN lines_json TEXT DEFAULT '[]'")
        conn.commit()
        _conn = conn
    return _conn


def init_db():
    try:
        _connect()
        return True
    except Exception as e:
        print(f"[trainer] init failed: {e}")
        return False


# ---------------------------------------------------------------- extraction
def _source_id(res):
    g = res.get("game", {})
    sid = g.get("id") or ""
    if sid:
        return sid
    return hashlib.sha1(f"{g.get('White','')}|{g.get('Black','')}|{g.get('Date','')}|{g.get('plyCount','')}".encode()).hexdigest()[:16]


def _uci_legal(fen, uci):
    try:
        import chess
        return chess.Move.from_uci(uci) in chess.Board(fen).legal_moves
    except Exception:
        return False


def extract_from_review(res):
    """Pull exercises out of a finished review payload (server-side, no engine)."""
    out = []
    moves = res.get("moves", [])
    positions = res.get("positions", [])
    if not moves or not positions:
        return out
    src_id = _source_id(res)
    src = res.get("game", {})
    src_name = f"{src.get('White','White')} vs {src.get('Black','Black')}"
    now = int(time.time())
    for i, mv in enumerate(moves):
        cls = mv.get("class") or ""
        if cls not in EXTRACT_CLASSES:
            continue
        if i >= len(positions):
            continue
        pos = positions[i]
        fen = pos.get("fen")
        best = mv.get("best") or {}
        best_uci, best_san = best.get("uci") or "", best.get("san") or "?"
        if not fen or not best_uci:
            continue
        try:
            board = __import__("chess").Board(fen)
        except Exception:
            continue
        if not _uci_legal(fen, best_uci):   # corrupted analysis - skip silently
            continue
        played_uci = mv.get("uci")
        if not _uci_legal(fen, played_uci):
            continue
        lines = pos.get("lines") or []
        lines0 = lines[0] if lines else {}
        # compact alternative-line representation for client-side solution grading
        alt_lines = []
        for ln in lines[:3]:
            pv = ln.get("pv") or []
            san = ln.get("san") or []
            alt_lines.append({
                "uci": pv[0] if pv else "", "san": san[0] if san else "?",
                "cp": ln.get("cp"), "mate": ln.get("mate"),
            })
        ex_id = hashlib.sha1(f"{fen}|{best_uci}".encode()).hexdigest()[:20]
        out.append({
            "id": ex_id, "fen": fen, "turn": fen.split()[1],
            "color_side": "white" if fen.split()[1] == "w" else "black",
            "played_uci": played_uci, "played_san": mv.get("san") or "?",
            "best_uci": best_uci, "best_san": best_san,
            "cls": cls, "label": mv.get("label") or cls.title(),
            "cpBefore": mv.get("cpBefore"), "cpAfter": mv.get("cpAfter"),
            "winBefore": mv.get("winBefore"), "winAfter": mv.get("winAfter"),
            "loss": mv.get("loss"), "accuracyCp": mv.get("accuracy"),
            "pvSan": lines0.get("san") or [], "bestCp": lines0.get("cp"), "bestMate": lines0.get("mate"),
            "lines": alt_lines,
            "sourceId": src_id, "sourceName": src_name, "sourceDate": src.get("Date", ""),
            "opening": src.get("opening", ""), "eco": src.get("eco", ""),
            "moveNumber": mv.get("moveNumber"), "ply": mv.get("ply"),
            "firstSeen": now, "lastSeen": now,
        })
    return out


# ---------------------------------------------------------------- upsert / dedupe
def sync_exercises(exercise_list):
    """Idempotent insert-or-get. Worst severity class wins on conflicts."""
    counts = {"created": 0, "updated": 0, "skipped": 0}
    now = int(time.time())
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            for ex in exercise_list:
                try:
                    cur = conn.execute(
                        "SELECT cls FROM exercises WHERE id = ? AND removed = 0",
                        (ex["id"],)).fetchone()
                    if cur:
                        sev_saved, sev_new = SEV.get(cur[0], 0), SEV.get(ex["cls"], 0)
                        if sev_new > sev_saved:
                            conn.execute(
                                "UPDATE exercises SET cls=?, label=?, last_seen=? WHERE id=?",
                                (ex["cls"], ex["label"], now, ex["id"]))
                        else:
                            conn.execute("UPDATE exercises SET last_seen=? WHERE id=?",
                                         (now, ex["id"]))
                        conn.execute(
                            "INSERT INTO exercise_sources(ex_id, source_id, occurrences) VALUES (?,?,1) "
                            "ON CONFLICT(ex_id, source_id) DO NOTHING",
                            (ex["id"], ex["sourceId"]))
                        counts["updated"] += 1
                        continue
                    src = {k: v for k, v in ex.items() if k in (
                        "cpBefore", "cpAfter", "winBefore", "winAfter", "loss", "accuracyCp")}
                    conn.execute(
                        "INSERT INTO exercises("
                        "id, fen, turn, color_side, played_uci, played_san, best_uci, best_san,"
                        "cls, label, cp_before, cp_after, win_before, win_after, loss, accuracy_cp,"
                        "pv_san, best_cp, best_mate, lines_json,"
                        "source_id, source_name, source_date, opening, eco,"
                        "move_number, ply, first_seen, last_seen) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ex["id"], ex["fen"], ex["turn"], ex["color_side"],
                         ex["played_uci"], ex["played_san"], ex["best_uci"], ex["best_san"],
                         ex["cls"], ex["label"],
                         src.get("cpBefore"), src.get("cpAfter"), src.get("winBefore"),
                         src.get("winAfter"), src.get("loss"), src.get("accuracyCp"),
                         json.dumps(ex.get("pvSan") or []),
                         ex.get("bestCp"), ex.get("bestMate"),
                         json.dumps(ex.get("lines") or []),
                         ex["sourceId"], ex["sourceName"], ex["sourceDate"],
                         ex["opening"], ex["eco"],
                         ex.get("moveNumber"), ex.get("ply"),
                         ex["firstSeen"], ex["lastSeen"]))
                    conn.execute(
                        "INSERT INTO exercise_sources(ex_id, source_id, occurrences) VALUES (?,?,1) "
                        "ON CONFLICT(ex_id, source_id) DO NOTHING",
                        (ex["id"], ex["sourceId"]))
                    conn.execute(
                        "INSERT OR IGNORE INTO schedule(ex_id, due_at) VALUES (?, ?)",
                        (ex["id"], now))
                    counts["created"] += 1
                except Exception as e:
                    print(f"[trainer] exercise skipped ({ex.get('id')}): {e}")
                    counts["skipped"] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return counts


# ---------------------------------------------------------------- SM-2
def _grade_interval(ease, interval, reps, quality):
    """Returns (new_ease, new_interval_days, new_reps, new_status)."""
    q = quality
    if q == 0:  # again
        ease = max(EASE_MIN, ease - 0.2)
        return ease, AGAIN_INTERVAL_DAYS, 0, "learning"
    if q == 2:  # hard
        ease = max(EASE_MIN, ease - 0.15)
        reps = reps + 1
        interval = max(float(AGAIN_INTERVAL_DAYS), min(interval * 1.2, max(interval + 1, 2)))
        return ease, max(1.0, interval), reps, ("review" if reps >= 2 else "learning")
    if q == 4:  # good
        reps = reps + 1
        if interval < 1:
            interval = 1.0
        elif interval < 6:
            interval = 6.0
        else:
            interval = round(interval * ease, 2)
        return ease, interval, reps, ("review" if reps >= 2 else "learning")
    # easy (q == 5)
    ease = min(3.0, ease + 0.15)
    reps = reps + 1
    if interval < 1:
        interval = 2.0
    elif interval < 8:
        interval = 8.0
    else:
        interval = round(interval * ease * EASY_BONUS, 2)
    status = "mastered" if reps >= 5 and interval >= 21 else "review"
    return ease, interval, reps, status


def _parse_filters(args):
    f = {}
    cls = (args.get("cls") or "").strip()
    if cls in SEV:
        f["cls"] = cls
    side = (args.get("side") or "").strip()
    if side in ("white", "black"):
        f["color_side"] = side
    status = (args.get("status") or "").strip()
    if status in ("new", "learning", "review", "mastered", "due"):
        f["status"] = status
    src = (args.get("source") or "").strip()
    if src:
        f["source_id"] = src
    return f


def _filters_sql(f, table="exercises"):
    """Returns (sql_fragments, params) - safe parameter binding only."""
    parts, params = ["removed = 0"], []
    if "cls" in f:
        parts.append("cls = ?"); params.append(f["cls"])
    if "color_side" in f:
        parts.append("color_side = ?"); params.append(f["color_side"])
    if "source_id" in f:
        parts.append(f"EXISTS (SELECT 1 FROM exercise_sources s WHERE s.ex_id = {table}.id AND s.source_id = ?)")
        params.append(f["source_id"])
    if "status" in f:
        if f["status"] == "due":
            parts.append("schedule.due_at <= ? AND schedule.status != 'new'")
            params.append(int(time.time()))
        else:  # new|learning|review|mastered
            parts.append("schedule.status = ?")
            params.append(f["status"])
    return " AND ".join(parts), params


def next_queue(limit=8, filters=None, include_new=True):
    """Pick ORDER: overdue (oldest first), then new (bounded)."""
    filters = filters or {}
    now = int(time.time())
    stats = counts = 0
    result = []
    with _lock:
        conn = _connect()
        wsql, wparams = _filters_sql(filters, "exercises")
        # 1) due learning/review/mastered
        rows = conn.execute(
            f"SELECT exercises.id, exercises.fen, exercises.turn, exercises.color_side,"
            f" exercises.played_uci, exercises.played_san, exercises.best_uci, exercises.best_san,"
            f" exercises.cls, exercises.label, exercises.cp_before, exercises.cp_after,"
            f" exercises.win_before, exercises.win_after, exercises.loss, exercises.pv_san,"
            f" exercises.best_cp, exercises.best_mate, exercises.move_number, exercises.ply, exercises.lines_json,"
            f" exercises.source_name, exercises.source_date, exercises.opening, exercises.eco,"
            f" schedule.status, schedule.interval_days, schedule.due_at"
            f" FROM exercises JOIN schedule ON schedule.ex_id = exercises.id"
            f" WHERE {wsql} AND schedule.status != 'new' AND schedule.due_at <= ? AND schedule.suspended = 0"
            f" ORDER BY schedule.due_at ASC LIMIT ?",
            (*wparams, now, limit)).fetchall()
        result += [_row_to_card(r) for r in rows]
        if len(result) < limit and include_new:
            new_cap = max(0, min(6, limit - len(result)))
            rows2 = conn.execute(
                f"SELECT exercises.id, exercises.fen, exercises.turn, exercises.color_side,"
                f" exercises.played_uci, exercises.played_san, exercises.best_uci, exercises.best_san,"
                f" exercises.cls, exercises.label, exercises.cp_before, exercises.cp_after,"
                f" exercises.win_before, exercises.win_after, exercises.loss, exercises.pv_san,"
                f" exercises.best_cp, exercises.best_mate, exercises.move_number, exercises.ply, exercises.lines_json,"
                f" exercises.source_name, exercises.source_date, exercises.opening, exercises.eco,"
                f" schedule.status, schedule.interval_days, schedule.due_at"
                f" FROM exercises JOIN schedule ON schedule.ex_id = exercises.id"
                f" WHERE {wsql} AND schedule.status = 'new' AND schedule.suspended = 0"
                f" ORDER BY exercises.first_seen ASC LIMIT ?",
                (*wparams, new_cap)).fetchall()
            result += [_row_to_card(r) for r in rows2]
    return result


def _row_to_card(r):
    return {
        "id": r[0], "fen": r[1], "turn": r[2], "colorSide": r[3],
        "playedUci": r[4], "playedSan": r[5], "bestUci": r[6], "bestSan": r[7],
        "cls": r[8], "label": r[9], "cpBefore": r[10], "cpAfter": r[11],
        "winBefore": r[12], "winAfter": r[13], "loss": r[14],
        "pvSan": json.loads(r[15] or "[]"),
        "bestCp": r[16], "bestMate": r[17],
        "moveNumber": r[18], "ply": r[19],
        "status": r[25], "intervalDays": r[26], "dueAt": r[27],
        "lines": json.loads(r[20] or "[]"),
        "sourceName": r[21], "sourceDate": r[22],
        "opening": r[23], "eco": r[24],
    }


def record_attempt(ex_id, quality, played_uci="", ms=0):
    """Validate + grade an attempt atomically. quality in 0|2|4|5."""
    quality = int(quality)
    if quality not in (0, 2, 4, 5):
        raise ValueError("quality must be one of 0,2,4,5")
    solved = 1 if quality > 0 else 0
    now = int(time.time())
    with _lock:
        conn = _connect()
        cur = conn.execute("SELECT 1 FROM exercises WHERE id = ? AND removed = 0", (ex_id,)).fetchone()
        if not cur:
            raise ValueError("unknown exercise")
        try:
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO attempts(ex_id, quality, played_uci, solved, ms, attempted_at)"
                " VALUES (?,?,?,?,?,?)",
                (ex_id, quality, played_uci or "", solved, int(ms or 0), now))
            row = conn.execute(
                "SELECT ease, interval_days, reps, lapses, total_correct, total_attempts"
                " FROM schedule WHERE ex_id = ?", (ex_id,)).fetchone()
            ease, interval, reps, lapses, tcorr, ttot = row if row else (EASE_DEFAULT, 0, 0, 0, 0, 0)
            ease2, interval2, reps2, status2 = _grade_interval(ease, interval, reps, quality)
            lapses2 = lapses + (1 if quality == 0 else 0)
            due_at = int(now + max(float(interval2), 0.0) * EAGER)
            conn.execute(
                "UPDATE schedule SET ease=?, interval_days=?, due_at=?, status=?, reps=?, lapses=?,"
                " total_correct=?, total_attempts=?, last_grade=?, last_at=? WHERE ex_id=?",
                (ease2, interval2, due_at, status2, reps2, lapses2,
                 tcorr + (1 if solved else 0), ttot + 1, quality, now, ex_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"intervalDays": round(interval2, 2), "ease": round(ease2, 3),
            "status": status2, "reps": reps2, "lapses": lapses2, "dueAt": due_at}


def dashboard(filters=None):
    filters = filters or {}
    now = int(time.time())
    with _lock:
        conn = _connect()
        wsql, wparams = _filters_sql(filters, "exercises")
        base_sql = f"FROM exercises JOIN schedule ON schedule.ex_id = exercises.id WHERE {wsql}"

        def one(sql, p=()):
            return conn.execute("SELECT " + sql + " " + base_sql, (*p,) if p else tuple(wparams)).fetchone()[0]

        total = one("COUNT(*)")
        due = conn.execute(
            f"SELECT COUNT(*) {base_sql} AND schedule.status != 'new'"
            f" AND schedule.due_at <= ? AND schedule.suspended = 0",
            (*wparams, now)).fetchone()[0]
        learning = conn.execute(f"SELECT COUNT(*) {base_sql} AND schedule.status = 'learning'", wparams).fetchone()[0]
        review = conn.execute(f"SELECT COUNT(*) {base_sql} AND schedule.status = 'review'", wparams).fetchone()[0]
        mastered = conn.execute(f"SELECT COUNT(*) {base_sql} AND schedule.status = 'mastered'", wparams).fetchone()[0]
        new_count = conn.execute(f"SELECT COUNT(*) {base_sql} AND schedule.status = 'new'", wparams).fetchone()[0]

        att = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN solved = 1 THEN 1 ELSE 0 END), 0), COUNT(*),"
            " COALESCE(SUM(ms), 0) FROM attempts").fetchone()
        solved_all, att_all, time_all = att

        att50 = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN solved = 1 THEN 1 ELSE 0 END), 0), COUNT(*)"
            " FROM (SELECT solved FROM attempts ORDER BY attempted_at DESC, id DESC LIMIT 50)").fetchone()
        solved50, total50 = att50

        weak = conn.execute(
            "SELECT exercises.cls, COUNT(*) FROM attempts JOIN exercises ON exercises.id = attempts.ex_id"
            " WHERE attempts.solved = 0 GROUP BY exercises.cls ORDER BY 2 DESC").fetchall()

        day_rows = conn.execute(
            "SELECT DISTINCT strftime('%Y-%m-%d', attempted_at, 'unixepoch') FROM attempts"
            " ORDER BY 1 DESC").fetchall()
    streak = 0
    from datetime import date, timedelta
    today = date.today()
    days = {r[0] for r in day_rows}
    cur = today
    if today.isoformat() not in days and (today - timedelta(days=1)).isoformat() in days:
        cur = today - timedelta(days=1)   # allow today or yesterday as anchor
    while cur.isoformat() in days:
        streak += 1
        cur = cur - timedelta(days=1)
    return {
        "filters": filters,
        "total": total, "due": due, "new": new_count,
        "learning": learning, "review": review, "mastered": mastered,
        "solvedAll": solved_all, "attemptsAll": att_all,
        "accuracy": round(100 * solved_all / att_all, 1) if att_all else None,
        "recentAccuracy": round(100 * solved50 / total50, 1) if total50 else None,
        "streakDays": streak,
        "studyTimeMs": time_all,
        "weakCategories": [{"cls": c, "misses": n} for c, n in weak],
    }


def attempt_history(limit=50):
    limit = max(1, min(200, int(limit or 50)))
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT a.id, a.ex_id, a.quality, a.played_uci, a.solved, a.ms, a.attempted_at,"
            " e.cls, e.label, e.best_san, e.played_san, e.move_number, e.color_side"
            " FROM attempts a JOIN exercises e ON e.id = a.ex_id"
            " ORDER BY a.attempted_at DESC, a.id DESC LIMIT ?", (limit,)).fetchall()
    return [{
        "id": r[0], "exerciseId": r[1], "quality": r[2], "playedUci": r[3],
        "solved": r[4], "ms": r[5], "attemptedAt": r[6],
        "cls": r[7], "label": r[8], "bestSan": r[9], "playedSan": r[10],
        "moveNumber": r[11], "colorSide": r[12],
    } for r in rows]


def detach_source(source_id):
    """Used when a library game is deleted: loses the ref, orphans the exercise."""
    removed = 0
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "SELECT DISTINCT ex_id FROM exercise_sources WHERE source_id = ?", (source_id,)).fetchall()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM exercise_sources WHERE source_id = ?", (source_id,))
            for (ex_id,) in cur:
                refs = conn.execute(
                    "SELECT COUNT(*) FROM exercise_sources WHERE ex_id = ?", (ex_id,)).fetchone()[0]
                if refs == 0:
                    conn.execute("UPDATE exercises SET removed = 1 WHERE id = ?", (ex_id,))
                    removed += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"detachedRefs": len(cur), "exercisesRemoved": removed}


def list_exercises(filters=None, limit=200):
    filters = filters or {}
    limit = max(1, min(500, int(limit or 200)))
    now = int(time.time())
    with _lock:
        conn = _connect()
        wsql, wparams = _filters_sql(filters, "exercises")
        rows = conn.execute(
            f"SELECT exercises.id, exercises.fen, exercises.color_side, exercises.cls, exercises.label,"
            f" exercises.best_san, exercises.played_san, exercises.move_number,"
            f" exercises.source_name, exercises.opening,"
            f" schedule.status, schedule.interval_days, schedule.reps, schedule.due_at,"
            f" (SELECT COALESCE(SUM(occurrences), 1) FROM exercise_sources s WHERE s.ex_id = exercises.id)"
            f" FROM exercises JOIN schedule ON schedule.ex_id = exercises.id"
            f" WHERE {wsql}"
            f" ORDER BY schedule.due_at ASC LIMIT ?", (*wparams, limit)).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "fen": r[1], "colorSide": r[2], "cls": r[3], "label": r[4],
            "bestSan": r[5], "playedSan": r[6], "moveNumber": r[7],
            "sourceName": r[8], "opening": r[9],
            "status": r[10], "intervalDays": r[11], "reps": r[12], "dueAt": r[13],
            "occurrences": r[14],
        })
    return out


def sources_list():
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT exercises.source_id, MAX(exercises.source_name), COUNT(*)"
            " FROM exercises WHERE exercises.removed = 0 GROUP BY exercises.source_id"
            " ORDER BY 2").fetchall()
    return [{"id": r[0], "name": r[1], "count": r[2]} for r in rows]
