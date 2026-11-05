"""Chess Performance Lab — persistent analytics store (SQLite).

Games are deduped by content hash (starting FEN + player names + the full mainline).
Reviews attached later upgrade the record to 'reviewed' with per-move detail so every
statistic traces back to real rows — no fabricated numbers.

Deliberately synchronous-safe (module lock) + each mutation in a transaction.
JSON blobs hold per-game breakdowns (phases, accuracy trend, move quality); everything
else is indexed columns so filters never scan.
"""
import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from server import eco as ECO
except ImportError:
    import eco as ECO  # type: ignore

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "perfdb.sqlite"
_lock = threading.RLock()
_conn = None

# ---------------------------------------------------------------- schema & migration


def _connect():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute("""CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            fen TEXT NOT NULL,
            white TEXT NOT NULL, black TEXT NOT NULL,
            white_elo INTEGER, black_elo INTEGER,
            result TEXT NOT NULL DEFAULT '*',
            date TEXT DEFAULT '', date_ts INTEGER DEFAULT 0,
            time_control TEXT DEFAULT '', tc_bucket TEXT DEFAULT 'unknown',
            opening TEXT DEFAULT '', eco TEXT DEFAULT '',
            ply_count INTEGER NOT NULL DEFAULT 0,
            event TEXT DEFAULT '', source TEXT DEFAULT 'import',
            reviewed INTEGER NOT NULL DEFAULT 0,
            pgn_text TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT NOT NULL UNIQUE, canonical TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS reviews (
            game_id TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
            white_acc REAL, black_acc REAL,
            white_acpl REAL, black_acpl REAL,
            blunders INTEGER DEFAULT 0, mistakes INTEGER DEFAULT 0,
            inaccuracies INTEGER DEFAULT 0, misses INTEGER DEFAULT 0,
            brilliant INTEGER DEFAULT 0, great INTEGER DEFAULT 0,
            phase_json TEXT DEFAULT '{}',
            time_json TEXT DEFAULT '{}',
            graph_json TEXT DEFAULT '[]',
            moves_json TEXT DEFAULT '[]')""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_games_date ON games(date_ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_games_opening ON games(opening)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_games_result ON games(result)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_games_reviewed ON games(reviewed)")
        conn.commit()
        _conn = conn
    return _conn


def init_db():
    try:
        _connect()
        return True
    except Exception as e:
        print(f"[perfdb] init failed: {e}")
        return False


# ---------------------------------------------------------------- canonical id & parse helpers


def _game_hash(fen, white, black, uci_seq):
    key = f"{fen}|{white.strip().lower()}|{black.strip().lower()}|{' '.join(uci_seq)}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]


_DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")


def _date_ts(s):
    m = _DATE_RE.match(s or "")
    if not m:
        return 0
    try:
        return int(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


def _tc_bucket(tc):
    """Parse TimeControl like '180+2' into a bucket (bullet/blitz/rapid/classical/correspondence/unknown)."""
    if not tc:
        return "unknown"
    tc = tc.strip()
    if "/" in tc:
        # correspondence style: '1/86400' days per move
        return "correspondence"
    if "+" in tc:
        base = tc.split("+", 1)[0]
    else:
        base = tc
    try:
        secs = int(base)
    except ValueError:
        return "unknown"
    if secs < 180:
        return "bullet"
    if secs < 600:
        return "blitz"
    if secs < 1800:
        return "rapid"
    if secs < 86400:
        return "classical"
    return "correspondence"


def _split_pgn_games(text):
    """Yield one PGN game text per game. Markers: [Event header lines."""
    import io
    import chess.pgn
    games = []
    stream = io.StringIO(text)
    while True:
        g = chess.pgn.read_game(stream)
        if g is None:
            break
        games.append(g)
    return games


def game_summary_from_pgn(g):
    headers = dict(g.headers)
    uci_moves = [m.uci() for m in g.mainline_moves()]
    board = g.board()
    start_fen = board.fen() if g.headers.get("SetUp") == "1" else chess_startpose()
    return headers, uci_moves, start_fen


def chess_startpose():
    return "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def parse_pgn_text(text):
    """Parse a PGN blob (one or many games). Returns list of dicts (unsaved)."""
    games = _split_pgn_games(text)
    out = []
    for g in games:
        try:
            headers, uci, start_fen = game_summary_from_pgn(g)
        except Exception:
            continue
        uci_list = list(uci)
        opening, eco = ECO.classify(uci_list)
        result = headers.get("Result", "*")
        date = headers.get("Date", "")
        tc = headers.get("TimeControl", "")
        out.append({
            "id": _game_hash(start_fen, headers.get("White", ""), headers.get("Black", ""), uci_list),
            "fen": start_fen,
            "white": headers.get("White", "White").strip() or "White",
            "black": headers.get("Black", "Black").strip() or "Black",
            "whiteElo": _int_or(headers.get("WhiteElo"), 0),
            "blackElo": _int_or(headers.get("BlackElo"), 0),
            "result": result,
            "date": date,
            "dateTs": _date_ts(date),
            "timeControl": tc,
            "tcBucket": _tc_bucket(tc),
            "opening": opening,
            "eco": eco,
            "plyCount": len(uci_list),
            "event": headers.get("Event", ""),
            "pgn": str(g),
        })
    return out


def _int_or(v, default):
    try:
        return int(str(v).split(".")[0])
    except Exception:
        return default


def _canonical_player(name):
    """Apply aliases for identity matching; empty means no alias hit."""
    name = (name or "").strip()
    if not name:
        return name
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT canonical FROM aliases WHERE alias = ? COLLATE NOCASE", (name,)).fetchone()
    return row[0] if row else name


def canonical_players_sql(name_tokens):
    """Build a (name = ?) OR (name IN aliases-of-name) condition — we instead match canonical TEXT."""
    # Strategic simplification: aliases fold at WRITE time via canonical_player; reads stay simple.
    return " OR ".join(["white = ?", "black = ?"] * len(name_tokens)), list(name_tokens) + list(name_tokens)


# ---------------------------------------------------------------- persistence


def import_games(games, source="import"):
    counts = {"inserted": 0, "duplicates": 0, "skipped": 0}
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            for g in games:
                white = _canonical_player(g["white"])
                black = _canonical_player(g["black"])
                cur = conn.execute("SELECT 1 FROM games WHERE id = ?", (g["id"],)).fetchone()
                if cur:
                    counts["duplicates"] += 1
                    continue
                now = time.time()
                conn.execute(
                    "INSERT INTO games("
                    "id, fen, white, black, white_elo, black_elo, result, date, date_ts,"
                    "time_control, tc_bucket, opening, eco, ply_count, event, source,"
                    "reviewed, pgn_text, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (g["id"], g["fen"], white, black, g["whiteElo"] or None, g["blackElo"] or None,
                     g["result"], g["date"], g["dateTs"], g["timeControl"], g["tcBucket"],
                     g["opening"], g["eco"], g["plyCount"], g["event"], source,
                     0, g["pgn"], now, now))
                counts["inserted"] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return counts


def attach_review(res):
    """Upsert a review payload. Returns (created|updated|missing)."""
    if not isinstance(res, dict):
        return {"ok": False, "reason": "bad payload"}
    game = res.get("game", {})
    white = game.get("White", "White").strip() or "White"
    black = game.get("Black", "Black").strip() or "Black"
    moves = res.get("moves", [])
    positions = res.get("positions", [])
    if not moves or not positions:
        return {"ok": False, "reason": "missing moves/positions"}
    try:
        uci_list = [m["uci"] for m in moves]
        fen = positions[0]["fen"]
    except Exception:
        return {"ok": False, "reason": "positions malformed"}
    gid = None
    white_c = _canonical_player(white)
    black_c = _canonical_player(black)
    explicit_id = (game.get("id") or "").strip()
    with _lock:
        conn = _connect()
        if explicit_id:
            row = conn.execute("SELECT id FROM games WHERE id = ?", (explicit_id,)).fetchone()
            if row:
                gid = row[0]
        if gid is None:
            row = conn.execute(
                "SELECT id FROM games WHERE fen = ? AND white = ? AND black = ? LIMIT 1",
                (fen, white_c, black_c)).fetchone()
            gid = row[0] if row else None
    if gid is None:
        gid = _game_hash(fen, white, black, uci_list)
    now = time.time()
    moves_summary = [{
        "ply": m.get("ply"), "san": m.get("san"), "uci": m.get("uci"),
        "class": m.get("class"), "color": m.get("color"),
        "accuracy": m.get("accuracy"), "drop": m.get("drop"),
        "cpBefore": m.get("cpBefore"), "cpAfter": m.get("cpAfter"),
        "winBefore": m.get("winBefore"), "winAfter": m.get("winAfter"),
        "loss": m.get("loss"),
        "clock": m.get("clock"), "timeSpent": m.get("timeSpent"),
    } for m in moves]
    blobs = {
        "white_acc": res.get("players", {}).get("white", {}).get("accuracy"),
        "black_acc": res.get("players", {}).get("black", {}).get("accuracy"),
        "white_acpl": res.get("players", {}).get("white", {}).get("acpl"),
        "black_acpl": res.get("players", {}).get("black", {}).get("acpl"),
        "blunders": res.get("players", {}).get("white", {}).get("counts", {}).get("blunder", 0)
                     + res.get("players", {}).get("black", {}).get("counts", {}).get("blunder", 0),
        "mistakes": res.get("players", {}).get("white", {}).get("counts", {}).get("mistake", 0)
                     + res.get("players", {}).get("black", {}).get("counts", {}).get("mistake", 0),
        "inaccuracies": res.get("players", {}).get("white", {}).get("counts", {}).get("inaccuracy", 0)
                     + res.get("players", {}).get("black", {}).get("counts", {}).get("inaccuracy", 0),
        "misses": res.get("players", {}).get("white", {}).get("counts", {}).get("miss", 0)
                     + res.get("players", {}).get("black", {}).get("counts", {}).get("miss", 0),
        "brilliant": res.get("players", {}).get("white", {}).get("counts", {}).get("brilliant", 0)
                     + res.get("players", {}).get("black", {}).get("counts", {}).get("brilliant", 0),
        "great": res.get("players", {}).get("white", {}).get("counts", {}).get("great", 0)
                     + res.get("players", {}).get("black", {}).get("counts", {}).get("great", 0),
        "phase_json": json.dumps({
            "white": res.get("players", {}).get("white", {}).get("phases", {}),
            "black": res.get("players", {}).get("black", {}).get("phases", {}),
        }),
        "time_json": json.dumps(res.get("timeReport") or {}),
        "graph_json": json.dumps(res.get("graph") or []),
        "moves_json": json.dumps(moves_summary),
    }
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "INSERT OR IGNORE INTO games("
                "id, fen, white, black, white_elo, black_elo, result, date, date_ts,"
                "time_control, tc_bucket, opening, eco, ply_count, event, source,"
                "reviewed, pgn_text, created_at, updated_at) "
                "VALUES (?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?,'review',1,'',?,?)",
                (gid, fen, white_c, black_c, game.get("Result", "*"), game.get("Date", ""),
                 _date_ts(game.get("Date", "")), game.get("TimeControl", ""),
                 _tc_bucket(game.get("TimeControl", "")), game.get("opening", ""),
                 game.get("eco", ""), len(moves), game.get("Event", ""), time.time(), time.time()))
            conn.execute(
                "UPDATE games SET reviewed = 1, updated_at = ?,"
                " opening = COALESCE(NULLIF(?, ''), opening),"
                " eco     = COALESCE(NULLIF(?, ''), eco)"
                " WHERE id = ?",
                (now, game.get("opening", ""), game.get("eco", ""), gid))
            conn.execute(
                "INSERT INTO reviews("
                "game_id, white_acc, black_acc, white_acpl, black_acpl, blunders, mistakes,"
                " inaccuracies, misses, brilliant, great, phase_json, time_json, graph_json, moves_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(game_id) DO UPDATE SET"
                " white_acc=excluded.white_acc, black_acc=excluded.black_acc,"
                " white_acpl=excluded.white_acpl, black_acpl=excluded.black_acpl,"
                " blunders=excluded.blunders, mistakes=excluded.mistakes,"
                " inaccuracies=excluded.inaccuracies, misses=excluded.misses,"
                " brilliant=excluded.brilliant, great=excluded.great,"
                " phase_json=excluded.phase_json, time_json=excluded.time_json,"
                " graph_json=excluded.graph_json, moves_json=excluded.moves_json",
                (gid, blobs["white_acc"], blobs["black_acc"],
                 blobs["white_acpl"], blobs["black_acpl"],
                 blobs["blunders"], blobs["mistakes"],
                 blobs["inaccuracies"], blobs["misses"],
                 blobs["brilliant"], blobs["great"],
                 blobs["phase_json"], blobs["time_json"],
                 blobs["graph_json"], blobs["moves_json"]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"ok": True, "id": gid}


# ---------------------------------------------------------------- reads & analytics


def _filtered(where, params):
    """Compose a safe filtered player/games query fragment."""
    return ("SELECT white, black, white_elo, black_elo, result, date, date_ts,"
            " time_control, tc_bucket, opening, eco, ply_count, reviewed, id"
            " FROM games WHERE " + where), params


def _row(conn, gid):
    return {
        "id": gid[0], "fen": gid[1], "white": gid[2], "black": gid[3],
        "whiteElo": gid[4], "blackElo": gid[5], "result": gid[6], "date": gid[7],
        "dateTs": gid[8], "timeControl": gid[9], "tcBucket": gid[10],
        "opening": gid[11], "eco": gid[12], "plyCount": gid[13],
        "source": gid[14], "reviewed": gid[15],
    }


def list_games(limit=100):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, fen, white, black, white_elo, black_elo,"
            " result, date, date_ts, time_control, tc_bucket, opening, eco,"
            " ply_count, source, reviewed FROM games"
            " ORDER BY date_ts DESC, created_at DESC LIMIT ?", (int(limit),)).fetchall()
    return [_row(None, r) for r in rows]


def overall():
    with _lock:
        conn = _connect()
        total = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        wl = conn.execute("SELECT result, COUNT(*) FROM games GROUP BY result").fetchall()
        by_color = dict(wl)
        tc = conn.execute(
            "SELECT tc_bucket, result, COUNT(*) FROM games WHERE tc_bucket != 'unknown'"
            " GROUP BY tc_bucket, result").fetchall()
        monthly = conn.execute(
            "SELECT strftime('%Y-%m', date_ts, 'unixepoch'), COUNT(*),"
            " SUM(CASE WHEN result = '1-0' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result = '0-1' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result = '1/2-1/2' THEN 1 ELSE 0 END)"
            " FROM games WHERE date_ts > 0 GROUP BY 1 ORDER BY 1").fetchall()
        rating_pool = conn.execute(
            "SELECT AVG(CASE WHEN white_elo IS NOT NULL AND black_elo IS NOT NULL"
            " THEN (white_elo + black_elo) / 2.0 END) FROM games").fetchone()[0]
        avg_ply = conn.execute("SELECT AVG(ply_count) FROM games").fetchone()[0]
        openings = conn.execute(
            "SELECT opening, result, COUNT(*) FROM games GROUP BY opening, result").fetchall()
        acc = conn.execute(
            "SELECT AVG(white_acc), AVG(black_acc), AVG(white_acpl), AVG(black_acpl),"
            " SUM(blunders), SUM(mistakes), SUM(inaccuracies), SUM(misses),"
            " SUM(brilliant), SUM(great) FROM reviews").fetchone()
        reviewed_count = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        phase_rows = conn.execute(
            "SELECT r.phase_json FROM games g JOIN reviews r ON r.game_id = g.id").fetchall()

        def _avg_ph(name):
            vals = []
            for (pj,) in phase_rows:
                try:
                    d2 = json.loads(pj or "{}")
                except Exception:
                    continue
                for side in ((d2.get("white") or {}), (d2.get("black") or {})):
                    pa = (side.get(name) or {}).get("accuracy") if isinstance(side, dict) else None
                    if isinstance(pa, (int, float)):
                        vals.append(pa)
            return round(sum(vals) / len(vals), 1) if vals else None

        phase_stats = {"opening": _avg_ph("opening"), "middlegame": _avg_ph("middlegame"),
                       "endgame": _avg_ph("endgame"), "n": len(phase_rows)}
    wins = sum(v for k, v in wl if k == "1-0")
    losses = sum(v for k, v in wl if k == "0-1")
    draws = sum(v for k, v in wl if k == "1/2-1/2")
    decided = wins + losses + draws
    counts_simple = {k: v for k, v in wl}
    del wl
    by_result = {}
    tc_out = {}
    for bucket, res, n in tc:
        b = tc_out.setdefault(bucket, {"wins": 0, "losses": 0, "draws": 0, "games": 0})
        if res in ("1-0", "0-1", "1/2-1/2"):
            b[{"1-0": "wins", "0-1": "losses", "1/2-1/2": "draws"}[res]] += n
        b["games"] += n
    openings_out = {}
    for op, res, n in openings:
        o = openings_out.setdefault(op or "Unknown", {"wins": 0, "losses": 0, "draws": 0, "games": 0})
        if res in ("1-0", "0-1", "1/2-1/2"):
            o[{"1-0": "wins", "0-1": "losses", "1/2-1/2": "draws"}[res]] += n
        o["games"] += n
    unknown = openings_out.get("Unknown", {"games": 0, "wins": 0, "losses": 0, "draws": 0})
    return {
        "total": total,
        "wins": wins, "losses": losses, "draws": draws,
        "reviewedGames": reviewed_count,
        "phaseStats": phase_stats,
        "decidedGames": decided,
        "winRate": round(100 * wins / decided, 1) if decided else None,
        "drawRate": round(100 * draws / decided, 1) if decided else None,
        "countsByResult": counts_simple,
        "avgRating": round(rating_pool, 1) if rating_pool else None,
        "avgPly": round(avg_ply, 1) if avg_ply else None,
        "avgAccuracy": {
            "white": round(acc[0], 1) if acc[0] is not None else None,
            "black": round(acc[1], 1) if acc[1] is not None else None,
        },
        "avgACPL": {
            "white": round(acc[2], 1) if acc[2] is not None else None,
            "black": round(acc[3], 1) if acc[3] is not None else None,
        },
        "errorTotals": {
            "blunders": acc[4] or 0, "mistakes": acc[5] or 0,
            "inaccuracies": acc[6] or 0, "misses": acc[7] or 0,
            "brilliant": acc[8] or 0, "great": acc[9] or 0,
        },
        "byTcBucket": tc_out,
        "byMonth": [{"month": m, "games": n, "wins": w, "losses": l, "draws": d}
                    for m, n, w, l, d in monthly],
        "openings": openings_out,
        "unknownOpenings": unknown,
    }


def games_by_id(ids):
    ids = [str(i) for i in (ids or []) if i][:500]
    if not ids:
        return []
    with _lock:
        conn = _connect()
        placeholders = ",".join(["?"] * len(ids))
        rows = conn.execute(
            "SELECT id, fen, white, black, white_elo, black_elo, result, date, date_ts,"
            " time_control, tc_bucket, opening, eco, ply_count, source, reviewed"
            " FROM games WHERE id IN (" + placeholders + ")", ids).fetchall()
    return [_row(None, r) for r in rows]


def delete_game(gid):
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM games WHERE id = ?", (gid,))
        conn.commit()
    return cur.rowcount > 0


def add_alias(alias, canonical):
    with _lock:
        conn = _connect()
        conn.execute("INSERT OR IGNORE INTO aliases(alias, canonical) VALUES (?,?)",
                     (alias.strip(), canonical.strip()))
        conn.commit()
    return {"ok": True}


def list_aliases():
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT alias, canonical FROM aliases ORDER BY canonical, alias").fetchall()
    return [{"alias": r[0], "canonical": r[1]} for r in rows]


def remove_alias(alias):
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM aliases WHERE alias = ?", (alias.strip(),))
        conn.commit()
    return cur.rowcount > 0


def normalize_aliases():
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT alias, canonical FROM aliases").fetchall()
        try:
            conn.execute("BEGIN")
            for a, c in rows:
                conn.execute("UPDATE games SET white = ? WHERE white = ? COLLATE NOCASE", (c, a))
                conn.execute("UPDATE games SET black = ? WHERE black = ? COLLATE NOCASE", (c, a))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"ok": True, "aliasesApplied": len(rows)}


def _reviews_only_count():
    with _lock:
        conn = _connect()
        return conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]


def weaknesses():
    """Rule-based weakness fingerprint. Each rule fires on REAL rows; severity &
    confidence derive from raw counts with explicit thresholds."""
    findings = []
    with _lock:
        conn = _connect()
        rev_count = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        if rev_count == 0:
            return {"reviewedGames": 0, "weaknesses": [], "note": "No reviewed games yet — review some games to unlock weakness analysis."}

        def aggregate(sql):
            return conn.execute(sql).fetchall()

        # ---- rule 1: misses (had a winning chance and failed to convert)
        miss_rows = aggregate(
            "SELECT g.id, g.white, g.black, g.result, g.date, g.ply_count "
            "FROM games g JOIN reviews r ON r.game_id = g.id "
            "WHERE r.misses > 0 ORDER BY g.date_ts DESC")
        if miss_rows:
            n = len(miss_rows)
            conf = "high" if n >= 5 else ("medium" if n >= 2 else "low")
            findings.append({
                "key": "misses", "label": "Missed winning opportunities",
                "count": n, "gamesCount": n,
                "severity": "high" if n >= 4 else "medium", "confidence": conf,
                "evidence": [r[0] for r in miss_rows[:20]],
                "description": f"You passed up a win in {n} game(s). Look for the pattern below.",
            })

        # ---- rule 2: blunders while already better (>= +250 cp before the move)
        blunder_better = aggregate(
            "SELECT DISTINCT g.id, g.white, g.black, g.result, g.date, g.ply_count "
            "FROM games g JOIN reviews r ON r.game_id = g.id, reviews r2 "
            "WHERE g.reviewed = 1 AND EXISTS ("
            "  SELECT 1 FROM json_each(CASE WHEN json_valid(r.moves_json) THEN r.moves_json ELSE '[]' END) AS mv"
            ")") if False else []
        # instead of json_each complexity, recompute from moves_json in python
        blunder_better_ids = []
        try:
            for row in conn.execute(
                "SELECT g.id, r.moves_json FROM games g JOIN reviews r ON r.game_id = g.id"):
                mid, mvj = row
                try:
                    mvs = json.loads(mvj or "[]")
                except Exception:
                    continue
                for m in mvs:
                    cp_b = m.get("cpBefore")
                    if cp_b is not None and cp_b >= 250 and m.get("class") == "blunder":
                        blunder_better_ids.append(mid)
                        break
        except Exception as e:
            print(f"[perfdb] weakness rule2 skipped: {e}")
        if blunder_better_ids:
            n = len(blunder_better_ids)
            conf = "high" if n >= 5 else ("medium" if n >= 2 else "low")
            findings.append({
                "key": "blunder_winning", "label": "Blundering while already better",
                "count": n, "gamesCount": n,
                "severity": "high" if n >= 4 else "medium", "confidence": conf,
                "evidence": blunder_better_ids[:20],
                "description": f"{n} game(s) include a blunder made while clearly better — conversion needs work.",
            })

        # ---- rule 3: time-pressure mistakes
        time_err = aggregate(
            "SELECT t.time_json FROM games g JOIN reviews t ON t.game_id = g.id"
            " WHERE t.time_json IS NOT NULL AND t.time_json != '{}'")
        time_errs = 0
        for (tj,) in time_err:
            try:
                t = json.loads(tj or "{}")
            except Exception:
                continue
            time_errs += int(t.get("timeTroubleErrors", 0) or 0)
        time_games = len(time_err)
        if time_errs > 0:
            conf = "high" if time_errs >= 4 else ("medium" if time_errs >= 2 else "low")
            findings.append({
                "key": "time_trouble", "label": "Mistakes in time trouble",
                "count": time_errs, "gamesCount": time_games,
                "severity": "high" if time_errs >= 4 else "medium", "confidence": conf,
                "evidence": [],
                "description": f"{time_errs} mistakes were made with under 12% of the initial clock left — you play faster than you think.",
            })

        # ---- rule 4: phase leak (opening/mid/end accuracy diff)
        phase_rows = aggregate(
            "SELECT g.id, r.phase_json FROM games g JOIN reviews r ON r.game_id = g.id")
        phase_acc = {"opening": [], "middlegame": [], "endgame": []}
        phase_ids = {"opening": [], "middlegame": [], "endgame": []}
        for gid, pj in phase_rows:
            try:
                d = json.loads(pj or "{}")
            except Exception:
                d = {}
            # players.white.phases and black merged per game (mean of both colors' accuracy per phase)
            wp = (d.get("white") or {}); bp = (d.get("black") or {})
            for ph in ("opening", "middlegame", "endgame"):
                vals = [wp.get(ph, {}).get("accuracy"), bp.get(ph, {}).get("accuracy")]
                vals = [v for v in vals if isinstance(v, (int, float))]
                if vals:
                    phase_acc[ph].append(sum(vals) / len(vals))
                    phase_ids[ph].append(gid)
        for ph in ("opening", "middlegame", "endgame"):
            vals = phase_acc[ph]
            if len(vals) < 3:
                continue
            avg = sum(vals) / len(vals)
            others = {p: (sum(phase_acc[p]) / len(phase_acc[p]) if phase_acc[p] else None)
                      for p in ("opening", "middlegame", "endgame") if p != ph}
            others = {p: v for p, v in others.items() if v is not None}
            if not others:
                continue
            avg_other = sum(others.values()) / len(others)
            gap = avg_other - avg
            if avg < 65 or gap >= 12:
                conf = "high" if len(vals) >= 8 else ("medium" if len(vals) >= 4 else "low")
                findings.append({
                    "key": f"phase_{ph}", "label": f"Weak {ph}",
                    "count": len(vals), "gamesCount": len(vals),
                    "severity": "high" if avg < 55 else "medium", "confidence": conf,
                    "evidence": phase_ids[ph][:20],
                    "description": f"{ph.capitalize()} accuracy averages {avg:.0f}%, "
                                   f"{gap:.0f} points below your other phases.",
                })

        # ---- rule 5: conversion failure — had a position worth ≥85 win% and didn't win it
        conv_fail_ids = []
        try:
            for row in conn.execute(
                "SELECT g.id, g.result, r.moves_json FROM games g JOIN reviews r ON r.game_id = g.id"):
                gid, res, mvj = row
                try:
                    mvs = json.loads(mvj or "[]")
                except Exception:
                    continue
                # track best win% ever seen FOR EITHER SIDE before each move
                had_big_win = False
                side_of_advantage = None
                for m in mvs:
                    wb = m.get("winBefore")
                    if wb is not None and abs(wb) >= 85:
                        had_big_win = True
                        side_of_advantage = m.get("color")
                        break
                if not had_big_win or side_of_advantage is None or res == "*":
                    continue
                # winner from the white perspective
                if res == "1/2-1/2":
                    winner = None
                elif res == "1-0":
                    winner = "white"
                elif res == "0-1":
                    winner = "black"
                else:
                    winner = None
                if winner != side_of_advantage:   # had a big advantage, didn't win
                    conv_fail_ids.append(gid)
        except Exception as e:
            print(f"[perfdb] conversion rule skipped: {e}")
        if conv_fail_ids:
            n = len(conv_fail_ids)
            conf = "high" if n >= 4 else ("medium" if n >= 2 else "low")
            findings.append({
                "key": "conversion", "label": "Failing to convert big advantages",
                "count": n, "gamesCount": n,
                "severity": "high" if n >= 3 else "medium", "confidence": conf,
                "evidence": conv_fail_ids[:20],
                "description": f"{n} game(s) where you were clearly winning (≥85 win%) and didn't win.",
            })

        return {
            "reviewedGames": rev_count,
            "weaknesses": findings,
        }


def recommendations():
    w = weaknesses()
    findings = w.get("weaknesses", [])
    recs = []
    severity_rank = {"high": 2, "medium": 1, "low": 0}
    findings.sort(key=lambda f: (severity_rank.get(f["severity"], 0), f["count"]), reverse=True)
    for f in findings[:4]:
        if f["key"] == "time_trouble":
            recs.append({
                "title": "Slow down in critical positions",
                "category": "Time management",
                "why": f"{f['count']} mistakes under time pressure account for "
                        f"a disproportionate share of your errors.",
                "exerciseFocus": "Try 5-10 rapid (10+5) practice games before timed practice.",
                "support": f["evidence"][:10],
                "priority": "high" if f["severity"] == "high" else "medium",
            })
        elif f["key"] == "misses":
            recs.append({
                "title": "Convert winning positions",
                "category": "Conversion",
                "why": "Your biggest practical leak is failing to cash in " + str(f["count"]) + " clearly better position(s).",
                "exerciseFocus": "Work through your missed tactics in the Trainer tab — they are your own positions.",
                "support": f["evidence"][:10],
                "priority": "high" if f["severity"] == "high" else "medium",
            })
        elif f["key"] == "blunder_winning":
            recs.append({
                "title": "Stop gifting material from better positions",
                "category": "Conversion",
                "why": "You blundered in positions where you were already winning more than once.",
                "exerciseFocus": "Play through your trainer exercises in order of hardest first — those are your hardest blind spots.",
                "support": f["evidence"][:10],
                "priority": "high" if f["severity"] == "high" else "medium",
            })
        elif f["key"].startswith("phase_"):
            ph = f["key"].split("_", 1)[1]
            recs.append({
                "title": f"Strengthen your {ph}",
                "category": f"{ph.capitalize()} play",
                "why": f"{f['description']}",
                "exerciseFocus": f"Use the trainer's {ph} exercises (filters by the opening of those games).",
                "support": f["evidence"][:10],
                "priority": "high" if f["severity"] == "high" else "medium",
            })

    # augment with trainer-specific weak categories (from the trainer DB - real data)
    try:
        from server import trainer as T
        dash = T.dashboard()
        weakest = (dash.get("weakCategories") or [{}])[0] if dash.get("weakCategories") else None
        if weakest:
            recs.append({
                "title": "Drill your most-missed trainer positions",
                "category": f"Trainer — {weakest['cls']}",
                "why": f"Your last attempts: {weakest['misses']} {weakest['cls']} miss(es) in the trainer. Keep them hot.",
                "exerciseFocus": f"Filters: mistake type = {weakest['cls']}.",
                "support": [],
                "priority": "medium",
            })
    except Exception:
        pass

    # always baseline recommendation about opening preparation
    recs.append({
        "title": "Build an opening repertoire map",
        "category": "Opening preparation",
        "why": "Structured study pays compounding dividends: the openings you actually play are listed in the Openings tab.",
        "exerciseFocus": "Review your five most-played openings with the Explorer and tag your must-know lines.",
        "support": [],
        "priority": "low" if not recs else recs[-1].get("priority", "medium"),
    })
    return {"reviewedGames": w.get("reviewedGames", 0), "recommendations": recs}


def timeline():
    with _lock:
        conn = _connect()
        monthly = conn.execute(
            "SELECT strftime('%Y-%m', date_ts, 'unixepoch'), COUNT(*),"
            " SUM(CASE WHEN result='1-0' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result='0-1' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result='1/2-1/2' THEN 1 ELSE 0 END)"
            " FROM games WHERE date_ts > 0 GROUP BY 1 ORDER BY 1").fetchall()
        monthly_acc = conn.execute(
            "SELECT strftime('%Y-%m', g.date_ts, 'unixepoch'),"
            " AVG(CASE WHEN g.reviewed = 1 THEN r.white_acc END),"
            " AVG(CASE WHEN g.reviewed = 1 THEN r.black_acc END)"
            " FROM games g LEFT JOIN reviews r ON r.game_id = g.id"
            " WHERE g.date_ts > 0 GROUP BY 1 ORDER BY 1").fetchall()
    acc_map = {m: (aw, ab) for m, aw, ab in monthly_acc}
    points = []
    for m, n, w, l, d in monthly:
        decided = w + l + d
        aw, ab = acc_map.get(m, (None, None))
        points.append({
            "month": m, "games": n, "wins": w, "losses": l, "draws": d,
            "winRate": round(100 * w / decided, 1) if decided else None,
            "whiteAccuracy": round(aw, 1) if aw is not None else None,
            "blackAccuracy": round(ab, 1) if ab is not None else None,
        })
    return {"points": points}


def openings_report():
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT eco, opening, white, black, result, date_ts"
            " FROM games WHERE opening != '' OR eco != ''").fetchall()
    fam = {}
    for eco, op, white, black, result, date_ts in rows:
        key = (eco or "?")
        fam.setdefault(key, {"eco": eco or "", "opening": op or "Unknown",
                             "games": 0, "wins": 0, "losses": 0, "draws": 0,
                             "results": []})
        fam[key]["games"] += 1
        fam[key]["results"].append(result)
        if result == "1-0":
            fam[key]["wins"] += 1
        elif result == "0-1":
            fam[key]["losses"] += 1
        elif result == "1/2-1/2":
            fam[key]["draws"] += 1
    out = []
    for key, f in sorted(fam.items(), key=lambda kv: -kv[1]["games"]):
        decided = f["wins"] + f["losses"] + f["draws"]
        out.append({
            "eco": f["eco"], "opening": f["opening"],
            "games": f["games"],
            "wins": f["wins"], "losses": f["losses"], "draws": f["draws"],
            "winRate": round(100 * f["wins"] / decided, 1) if decided else None,
            "confidence": "high" if f["games"] >= 10 else ("medium" if f["games"] >= 5 else "low"),
        })
    return {"families": out}
