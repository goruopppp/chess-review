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
    from server import db
    from server import eco as ECO
except ImportError:
    import db  # type: ignore
    import eco as ECO  # type: ignore

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "perfdb.sqlite"
_lock = threading.RLock()
_conn = None

# --- short-lived cache for the read-only analytics ------------------------
# With an external database every query is a round trip, and the Lab asks for
# eight reports at once - each one several statements. The results change
# only when a game is added, reviewed or deleted, so the analytics are cached
# for a few seconds and every write clears the cache. Same data, far fewer
# round trips.
_CACHE_TTL = 10.0
_cache = {}


def _cache_get(key):
    hit = _cache.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return val


def _cache_set(key, value):
    _cache[key] = (time.time(), value)
    return value


def cache_clear():
    """Called by every mutating path so the analytics never go stale."""
    _cache.clear()


def _cached(fn):
    """Memoize a read-only analytics function for a few seconds."""
    def wrapper(*args, **kwargs):
        key = (fn.__name__, args, tuple(sorted(kwargs.items())))
        hit = _cache_get(key)
        if hit is not None:
            return hit
        return _cache_set(key, fn(*args, **kwargs))
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper

# ---------------------------------------------------------------- schema & migration


def _connect():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = db.connect("perfdb", DB_PATH)
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
            user_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT NOT NULL, canonical TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '')""")
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
            moves_json TEXT DEFAULT '[]',
            user_id TEXT NOT NULL DEFAULT '')""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_games_date ON games(date_ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_games_opening ON games(opening)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_games_result ON games(result)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_games_reviewed ON games(reviewed)")
        # tolerant migration: pre-accounts databases gain the user column;
        # old rows stay in the shared guest pool ('') and never get dropped
        gcols = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
        if "user_id" not in gcols:
            conn.execute("ALTER TABLE games ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        rcols = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
        if "user_id" not in rcols:
            conn.execute("ALTER TABLE reviews ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        acols = {r[1] for r in conn.execute("PRAGMA table_info(aliases)")}
        if "user_id" not in acols:
            conn.execute("ALTER TABLE aliases ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        # the per-user index comes AFTER the migration: on an older database the
        # column does not exist yet and CREATE INDEX would fail
        conn.execute("CREATE INDEX IF NOT EXISTS idx_games_user ON games(user_id)")
        conn.commit()
        # review rows whose games row is missing are pure corruption (an older
        # attach_review left them behind); drop so analytics stay consistent
        orphans = conn.execute(
            "DELETE FROM reviews WHERE game_id NOT IN (SELECT id FROM games)").rowcount
        if orphans:
            print(f"[perfdb] removed {orphans} orphan review row(s)")
        # FK enforcement per connection so ON DELETE CASCADE actually runs
        conn.execute("PRAGMA foreign_keys=ON")
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


def _row_id(raw_id, user_id):
    """Content-hash ids are scoped per account by prefixing the user id (guest
    pool keeps bare ids). All id-based lookups go through the same prefix, so
    two accounts can hold the same game without colliding on the PK."""
    raw = (raw_id or "").strip()
    return f"{user_id}:{raw}" if user_id else raw


def _canonical_player(name, user_id=""):
    """Apply per-user aliases for identity matching; empty means no alias hit."""
    name = (name or "").strip()
    if not name:
        return name
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT canonical FROM aliases WHERE alias = ? COLLATE NOCASE AND user_id = ?",
            (name, user_id)).fetchone()
    return row[0] if row else name


def canonical_players_sql(name_tokens):
    """Build a (name = ?) OR (name IN aliases-of-name) condition — we instead match canonical TEXT."""
    # Strategic simplification: aliases fold at WRITE time via canonical_player; reads stay simple.
    return " OR ".join(["white = ?", "black = ?"] * len(name_tokens)), list(name_tokens) + list(name_tokens)


# ---------------------------------------------------------------- persistence


def import_games(games, source="import", user_id=""):
    cache_clear()
    counts = {"inserted": 0, "duplicates": 0, "skipped": 0}
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            for g in games:
                white = _canonical_player(g["white"], user_id)
                black = _canonical_player(g["black"], user_id)
                rid = _row_id(g["id"], user_id)
                cur = conn.execute("SELECT 1 FROM games WHERE id = ? AND user_id = ?",
                                   (rid, user_id)).fetchone()
                if cur:
                    counts["duplicates"] += 1
                    continue
                now = time.time()
                conn.execute(
                    "INSERT INTO games("
                    "id, fen, white, black, white_elo, black_elo, result, date, date_ts,"
                    "time_control, tc_bucket, opening, eco, ply_count, event, source,"
                    "reviewed, pgn_text, user_id, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, g["fen"], white, black, g["whiteElo"] or None, g["blackElo"] or None,
                     g["result"], g["date"], g["dateTs"], g["timeControl"], g["tcBucket"],
                     g["opening"], g["eco"], g["plyCount"], g["event"], source,
                     0, g["pgn"], user_id, now, now))
                counts["inserted"] += 1
            conn.commit()
            cache_clear()      # the analytics changed
        except Exception:
            conn.rollback()
            raise
    return counts


def attach_review(res, user_id=""):
    """Upsert a review payload. Returns (created|updated|missing)."""
    cache_clear()
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
    white_c = _canonical_player(white, user_id)
    black_c = _canonical_player(black, user_id)
    explicit_id = (game.get("id") or "").strip()
    with _lock:
        conn = _connect()
        if explicit_id:
            row = conn.execute("SELECT id FROM games WHERE id = ? AND user_id = ?",
                               (_row_id(explicit_id, user_id), user_id)).fetchone()
            if row:
                gid = row[0]
        if gid is None:
            row = conn.execute(
                "SELECT id FROM games WHERE fen = ? AND white = ? AND black = ? AND user_id = ? LIMIT 1",
                (fen, white_c, black_c, user_id)).fetchone()
            gid = row[0] if row else None
    if gid is None:
        gid = _row_id(_game_hash(fen, white, black, uci_list), user_id)
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
                "reviewed, pgn_text, user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'review', 1, ?, ?, ?, ?)",
                (gid, fen, white_c, black_c, game.get("Result", "*"), game.get("Date", ""),
                 _date_ts(game.get("Date", "")), game.get("TimeControl", ""),
                 _tc_bucket(game.get("TimeControl", "")), game.get("opening", ""),
                 game.get("eco", ""), len(moves), game.get("Event", ""),
                 str(res.get("pgn") or ""), user_id, time.time(), time.time()))
            conn.execute(
                "UPDATE games SET reviewed = 1, updated_at = ?,"
                " opening = COALESCE(NULLIF(?, ''), opening),"
                " eco     = COALESCE(NULLIF(?, ''), eco)"
                " WHERE id = ? AND user_id = ?",
                (now, game.get("opening", ""), game.get("eco", ""), gid, user_id))
            conn.execute(
                "INSERT INTO reviews("
                "game_id, white_acc, black_acc, white_acpl, black_acpl, blunders, mistakes,"
                " inaccuracies, misses, brilliant, great, phase_json, time_json, graph_json, moves_json, user_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
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
                 blobs["graph_json"], blobs["moves_json"], user_id))
            conn.commit()
            cache_clear()      # the analytics changed
            if user_id:
                # The game now belongs to an account, so its copy in the
                # shared guest pool goes away - that pool is visible to every
                # visitor, and the owner has it safely in their account.
                plain = gid.split(":", 1)[1] if ":" in gid else gid
                try:
                    conn.execute("DELETE FROM reviews WHERE game_id = ? AND user_id = ''",
                                 (plain,))
                    conn.execute("DELETE FROM games WHERE id = ? AND user_id = ''", (plain,))
                    conn.commit()
                except Exception:
                    pass
        except Exception:
            conn.rollback()
            raise
    return {"ok": True, "id": gid}


# ---------------------------------------------------------------- reads & analytics


def _filtered(where, params, user_id=""):
    """Compose a safe filtered player/games query fragment (account-scoped)."""
    return ("SELECT white, black, white_elo, black_elo, result, date, date_ts,"
            " time_control, tc_bucket, opening, eco, ply_count, reviewed, id"
            " FROM games WHERE user_id = ? AND (" + where + ")"), [user_id] + list(params)


def _row(conn, gid):
    return {
        "id": gid[0], "fen": gid[1], "white": gid[2], "black": gid[3],
        "whiteElo": gid[4], "blackElo": gid[5], "result": gid[6], "date": gid[7],
        "dateTs": gid[8], "timeControl": gid[9], "tcBucket": gid[10],
        "opening": gid[11], "eco": gid[12], "plyCount": gid[13],
        "source": gid[14], "reviewed": gid[15],
    }


@_cached
def list_games(limit=100, user_id=""):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, fen, white, black, white_elo, black_elo,"
            " result, date, date_ts, time_control, tc_bucket, opening, eco,"
            " ply_count, source, reviewed FROM games"
            " WHERE user_id = ?"
            " ORDER BY date_ts DESC, created_at DESC LIMIT ?", (user_id, int(limit))).fetchall()
    return [_row(None, r) for r in rows]


@_cached
def games_total(user_id=""):
    """Real games count for the Lab list header ('N shown of M imported')."""
    with _lock:
        conn = _connect()
        return conn.execute("SELECT COUNT(*) FROM games WHERE user_id = ?",
                            (user_id,)).fetchone()[0]


# ---------------------------------------------------------------- aimchess-style behavior metrics
# All scores are deterministic maps defined here (nothing empirical/fabricated):
#   speed        = avg seconds/move  (4s -> 100, 30s -> 0, linear)
#   time_mgmt    = bell around the 8-12 s sweet spot (<8 s impulsive, >12 s slow)
#   aggression   = (captures + checks) / own moves   (8% -> 0, 40% -> 100)
#   blunderless  = blunders per 100 own moves        (0 -> 100, 5 -> 0)
#   consistency  = stddev of per-game accuracy       (6% -> 100, 20% -> 0)
# metrics() reads stored moves_json (per-move san/clock/timeSpent/class/accuracy),
# which attach_review already persists for every reviewed game.


def _speed_score(tpm):
    if tpm is None:
        return None
    return max(0, min(100, round(100 - (tpm - 4) * (100 / 26))))


def _time_mgmt_score(tpm):
    if tpm is None:
        return None
    if tpm < 8:
        return round(max(0.0, 100 - (8 - tpm) * 9))
    if tpm <= 12:
        return 100
    return round(max(0.0, 100 - (tpm - 12) * 4.5))


def _aggression_score(share):
    if share is None:
        return None
    return max(0, min(100, round((share - 0.08) / 0.32 * 100)))


def _blunderless_score(rate_per_100):
    if rate_per_100 is None:
        return None
    return max(0, min(100, round(100 - rate_per_100 * 20)))


def _consistency_score(sd):
    if sd is None:
        return None
    return max(0, min(100, round(100 - (sd - 6) * (100 / 14))))


@_cached
def metrics(color="white", user_id=""):
    """Per-side behavior metrics over all reviewed games (aimchess-style stats)."""
    me = "white" if (color or "white").lower() != "black" else "black"
    opp = "black" if me == "white" else "white"
    cap_rng = {"captures": 0, "checks": 0, "moves": 0, "blunders": 0,
               "mistakes": 0, "fast": 0, "fastAccSum": 0.0, "accSum": 0.0,
               "accN": 0, "tpmSum": 0.0, "tpmN": 0, "maxClockHigh": 0, "clockMoves": 0}
    acc_values = []
    clock_moves_total = 0
    games_count = 0
    need_clock_total = 0   # games whose moves carry ANY clock info
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT g.result, r.moves_json, r.white_acc, r.black_acc FROM games g"
            " JOIN reviews r ON r.game_id = g.id WHERE g.user_id = ?",
            (user_id,)).fetchall()
    for result, mvj, wacc, bacc in rows:
        games_count += 1
        try:
            mvs = json.loads(mvj or "[]")
        except Exception:
            mvs = []
        if not mvs:
            continue
        own = [m for m in mvs if (m.get("color") or opp) == me]
        if own:
            acc_values.append(wacc if me == "white" else bacc)
        has_clock = any(m.get("timeSpent") is not None for m in own)
        if has_clock:
            need_clock_total += 1
        for m in own:
            cap_rng["moves"] += 1
            san = (m.get("san") or "").lower()
            if "x" in san:
                cap_rng["captures"] += 1
            if "+" in san or "#" in san:
                cap_rng["checks"] += 1
            cls = m.get("class")
            if cls == "blunder":
                cap_rng["blunders"] += 1
            elif cls == "mistake":
                cap_rng["mistakes"] += 1
            acc = m.get("accuracy")
            if isinstance(acc, (int, float)):
                cap_rng["accN"] += 1
                cap_rng["accSum"] += acc
                ts = m.get("timeSpent")
                if ts is not None:
                    clock_moves_total += 1
                if ts is not None and ts <= 3:
                    cap_rng["fast"] += 1
                    cap_rng["fastAccSum"] += acc
            ts_b = m.get("timeSpent")
            if ts_b is not None:
                cap_rng["tpmN"] += 1
                cap_rng["tpmSum"] += ts_b
    wins = sum(1 for result, _mvj, _w, _b in rows
               if result == ("1-0" if me == "white" else "0-1"))
    losses = sum(1 for result, _mvj, _w, _b in rows
                 if result == ("0-1" if me == "white" else "1-0"))
    draws = games_count - wins - losses
    decided = wins + losses + draws
    n = cap_rng["moves"]
    moves_total = max(n, clock_moves_total)
    attack_share = (cap_rng["captures"] + cap_rng["checks"]) / n if n else None
    blunder_rate = (cap_rng["blunders"] / n * 100) if n else None
    avg_acc = cap_rng["accSum"] / cap_rng["accN"] if cap_rng["accN"] else None
    fast_acc = cap_rng["fastAccSum"] / cap_rng["fast"] if cap_rng["fast"] else None
    tpm = cap_rng["tpmSum"] / cap_rng["tpmN"] if cap_rng["tpmN"] else None
    sd = None
    if len(acc_values) >= 2:
        avgs = [a for a in acc_values if a is not None]
        if len(avgs) >= 2:
            mu = sum(avgs) / len(avgs)
            sd = (sum((a - mu) ** 2 for a in avgs) / len(avgs)) ** 0.5
    phase_acc = {"opening": [], "middlegame": [], "endgame": []}
    phase = None
    with _lock:
        conn = _connect()
        p_rows = conn.execute("SELECT phase_json FROM reviews WHERE user_id = ?",
                              (user_id,)).fetchall()
    for (pj,) in p_rows:
        try:
            d = json.loads(pj or "{}")
        except Exception:
            continue
        side = d.get(me) or {}
        for ph in phase_acc:
            a = (side.get(ph) or {}).get("accuracy")
            if isinstance(a, (int, float)):
                phase_acc[ph].append(a)
    opening_acc = round(sum(phase_acc["opening"]) / len(phase_acc["opening"]), 1) \
        if phase_acc["opening"] else None
    radar = [{"axis": "Accuracy", "v": max(0, min(100, round(avg_acc))) if avg_acc is not None else None},
             {"axis": "Opening", "v": max(0, min(100, round(opening_acc))) if opening_acc is not None else None},
             {"axis": "Speed", "v": _speed_score(tpm)},
             {"axis": "Aggression", "v": _aggression_score(attack_share)},
             {"axis": "Time mgmt", "v": _time_mgmt_score(tpm)},
             {"axis": "Consistency", "v": _consistency_score(sd)}]
    axes_have_data = [x for x in radar if x["v"] is not None]
    win_rate = round(100 * wins / decided, 1) if decided else None
    return {
        "color": me,
        "gamesCount": games_count,
        "movesCount": n,
        "wins": wins, "losses": losses, "draws": draws,
        "winRate": win_rate,
        "avgAccuracy": round(avg_acc, 1) if avg_acc is not None else None,
        "acpl": None,
        "tpm": round(tpm, 1) if tpm is not None else None,
        "speed": _speed_score(tpm),
        "timeMgmt": _time_mgmt_score(tpm),
        "captureCount": cap_rng["captures"],
        "checkCount": cap_rng["checks"],
        "attackShare": round(attack_share * 100, 1) if attack_share is not None else None,
        "aggression": _aggression_score(attack_share),
        "blunders": cap_rng["blunders"],
        "mistakes": cap_rng["mistakes"],
        "blunderRate": round(blunder_rate, 1) if blunder_rate is not None else None,
        "blunderless": _blunderless_score(blunder_rate),
        "fastMoves": cap_rng["fast"],
        "fastAccuracy": round(fast_acc, 1) if fast_acc is not None else None,
        "accuracyStdDev": round(sd, 1) if sd is not None else None,
        "consistency": _consistency_score(sd),
        "openingAccuracy": opening_acc,
        "accuracyN": cap_rng["accN"],
        "clockGames": need_clock_total,
        "conversion": win_rate,
        "radar": radar,
        "note": None if axes_have_data else "No scored moves yet — review games to unlock behavior metrics.",
    }


@_cached
def skill_series(color="white", user_id=""):
    """Per-game skill series for the Lab tiles (aimchess-style line charts).

    Each skill is one value per reviewed game (date-sorted), so the tiles draw
    the same jagged per-game progression the report cards need. Delta = second
    half average vs first half average (trend), computed honestly; None when
    there are fewer than 2 points on either side.
    """
    me = "white" if (color or "white").lower() != "black" else "black"
    opp = "black" if me == "white" else "white"
    target = {"rating": 85, "opening": 85, "middlegame": 85, "endgame": 85,
              "tactics": 80, "time_mgmt": 80, "resourcefulness": 80}
    series = {k: [] for k in target}   # key -> list of {"t","date","v"}
    wdl = {"wins": 0, "losses": 0, "draws": 0, "total": 0}
    win_key = "1-0" if me == "white" else "0-1"
    loss_key = "0-1" if me == "white" else "1-0"
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT g.date_ts, g.date, g.result, r.phase_json, r.moves_json,"
            " r.white_acc, r.black_acc FROM games g JOIN reviews r ON r.game_id = g.id"
            " WHERE g.user_id = ?"
            " GROUP BY g.id ORDER BY g.date_ts, g.created_at", (user_id,)).fetchall()
    for date_ts, date, result, pjk, mvj, wacc, bacc in rows:
        wdl["total"] += 1
        if result == win_key:
            wdl["wins"] += 1
        elif result == loss_key:
            wdl["losses"] += 1
        elif result == "1/2-1/2":
            wdl["draws"] += 1
        try:
            mvs = json.loads(mvj or "[]")
        except Exception:
            mvs = []
        own = [m for m in mvs if (m.get("color") or opp) == me]
        add = series
        acc_list = [m.get("accuracy") for m in own if isinstance(m.get("accuracy"), (int, float))]
        if acc_list:
            add["rating"].append({"t": date_ts, "date": date, "v": round(sum(acc_list) / len(acc_list), 1)})
        try:
            ph = json.loads(pjk or "{}").get(me) or {}
        except Exception:
            ph = {}
        for ph_key, sk_key in (("opening", "opening"), ("middlegame", "middlegame"), ("endgame", "endgame")):
            a = (ph.get(ph_key) or {}).get("accuracy")
            if isinstance(a, (int, float)):
                add[sk_key].append({"t": date_ts, "date": date, "v": round(a, 1)})
        if own:
            bls = sum(1 for m in own if m.get("class") == "blunder")
            mN = len(own)
            add["tactics"].append({"t": date_ts, "date": date,
                                   "v": _blunderless_score(bls / mN * 100)})
            tps = [m.get("timeSpent") for m in own if isinstance(m.get("timeSpent"), (int, float))]
            if tps:
                add["time_mgmt"].append({"t": date_ts, "date": date,
                                         "v": _time_mgmt_score(sum(tps) / len(tps))})
            big = any(isinstance(m.get("winBefore"), (int, float)) and abs(m["winBefore"]) >= 85 for m in own)
            if big:
                won = result == ("1-0" if me == "white" else "0-1")
                add["resourcefulness"].append({"t": date_ts, "date": date, "v": 100 if won else 0})
    out = {}
    for key, pts in series.items():
        pts.sort(key=lambda p: p["t"])
        vals = [p["v"] for p in pts]
        avg = round(sum(vals) / len(vals), 1) if vals else None
        half = len(pts) // 2
        delta = None
        if half >= 1 and len(pts) - half >= 1 and half != len(pts):
            first = sum(p["v"] for p in pts[:half]) / half
            second = sum(p["v"] for p in pts[half:]) / (len(pts) - half)
            if abs(first) > 0.01:
                delta = round(100 * (second - first) / first)
        out[key] = {"points": pts, "avg": avg, "target": target[key], "delta": delta}
    return {"color": me, "wdl": wdl, "skills": out}


@_cached
def overall(user_id=""):
    with _lock:
        conn = _connect()
        total = conn.execute("SELECT COUNT(*) FROM games WHERE user_id = ?", (user_id,)).fetchone()[0]
        wl = conn.execute("SELECT result, COUNT(*) FROM games WHERE user_id = ? GROUP BY result",
                          (user_id,)).fetchall()
        by_color = dict(wl)
        tc = conn.execute(
            "SELECT tc_bucket, result, COUNT(*) FROM games WHERE user_id = ?"
            " AND tc_bucket != 'unknown'"
            " GROUP BY tc_bucket, result", (user_id,)).fetchall()
        monthly = conn.execute(
            "SELECT strftime('%Y-%m', date_ts, 'unixepoch'), COUNT(*),"
            " SUM(CASE WHEN result = '1-0' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result = '0-1' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result = '1/2-1/2' THEN 1 ELSE 0 END)"
            " FROM games WHERE user_id = ? AND date_ts > 0 GROUP BY 1 ORDER BY 1",
            (user_id,)).fetchall()
        rating_pool = conn.execute(
            "SELECT AVG(CASE WHEN white_elo IS NOT NULL AND black_elo IS NOT NULL"
            " THEN (white_elo + black_elo) / 2.0 END) FROM games WHERE user_id = ?",
            (user_id,)).fetchone()[0]
        avg_ply = conn.execute("SELECT AVG(ply_count) FROM games WHERE user_id = ?",
                               (user_id,)).fetchone()[0]
        openings = conn.execute(
            "SELECT opening, result, COUNT(*) FROM games WHERE user_id = ? GROUP BY opening, result",
            (user_id,)).fetchall()
        acc = conn.execute(
            "SELECT AVG(white_acc), AVG(black_acc), AVG(white_acpl), AVG(black_acpl),"
            " SUM(blunders), SUM(mistakes), SUM(inaccuracies), SUM(misses),"
            " SUM(brilliant), SUM(great) FROM reviews WHERE user_id = ?",
            (user_id,)).fetchone()
        reviewed_count = conn.execute("SELECT COUNT(*) FROM reviews WHERE user_id = ?",
                                      (user_id,)).fetchone()[0]
        phase_rows = conn.execute(
            "SELECT r.phase_json FROM games g JOIN reviews r ON r.game_id = g.id"
            " WHERE g.user_id = ?", (user_id,)).fetchall()

        def _avg_ph(name):
            vals = []
            for (pj,) in phase_rows:
                try:
                    d2 = json.loads(pj or "{}")
                except Exception:
                    continue
                for side in ((d2.get("white") or {}), (d2.get("black") or {})):
                    if not isinstance(side, dict):
                        continue
                    ph = side.get(name)
                    pa = ph.get("accuracy") if isinstance(ph, dict) else None
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


@_cached
def games_by_id(ids, user_id=""):
    ids = [str(i) for i in (ids or []) if i][:500]
    if not ids:
        return []
    with _lock:
        conn = _connect()
        placeholders = ",".join(["?"] * len(ids))
        rows = conn.execute(
            "SELECT id, fen, white, black, white_elo, black_elo, result, date, date_ts,"
            " time_control, tc_bucket, opening, eco, ply_count, source, reviewed"
            " FROM games WHERE user_id = ? AND id IN (" + placeholders + ")",
            [user_id] + ids).fetchall()
    return [_row(None, r) for r in rows]


def delete_game(gid, user_id=""):
    """Remove a game AND its review (cascade is enforced by FK pragma + explicit delete)."""
    cache_clear()
    with _lock:
        conn = _connect()
        conn.execute("BEGIN")
        conn.execute("DELETE FROM reviews WHERE game_id = ? AND user_id = ?", (gid, user_id))
        cur = conn.execute("DELETE FROM games WHERE id = ? AND user_id = ?", (gid, user_id))
        conn.commit()
    cache_clear()
    return cur.rowcount > 0


def add_alias(alias, canonical, user_id=""):
    cache_clear()
    alias, canonical = alias.strip(), canonical.strip()
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT 1 FROM aliases WHERE alias = ? COLLATE NOCASE AND user_id = ?",
                           (alias, user_id)).fetchone()
        if row:
            conn.execute("UPDATE aliases SET canonical = ? WHERE alias = ? COLLATE NOCASE AND user_id = ?",
                         (canonical, alias, user_id))
        else:
            try:
                conn.execute("INSERT INTO aliases(alias, canonical, user_id) VALUES (?,?,?)",
                             (alias, canonical, user_id))
            except sqlite3.IntegrityError:
                # pre-accounts databases still carry UNIQUE(alias); another
                # account owns that name — leave their mapping untouched
                pass
        conn.commit()
    cache_clear()
    return {"ok": True}


@_cached
def list_aliases(user_id=""):
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT alias, canonical FROM aliases WHERE user_id = ?"
                            " ORDER BY canonical, alias", (user_id,)).fetchall()
    return [{"alias": r[0], "canonical": r[1]} for r in rows]


def remove_alias(alias, user_id=""):
    cache_clear()
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM aliases WHERE alias = ? AND user_id = ?",
                           (alias.strip(), user_id))
        conn.commit()
    cache_clear()
    return cur.rowcount > 0


def normalize_aliases(user_id=""):
    cache_clear()
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT alias, canonical FROM aliases WHERE user_id = ?",
                            (user_id,)).fetchall()
        try:
            conn.execute("BEGIN")
            for a, c in rows:
                conn.execute("UPDATE games SET white = ? WHERE white = ? COLLATE NOCASE AND user_id = ?",
                             (c, a, user_id))
                conn.execute("UPDATE games SET black = ? WHERE black = ? COLLATE NOCASE AND user_id = ?",
                             (c, a, user_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    cache_clear()
    return {"ok": True, "aliasesApplied": len(rows)}


def _reviews_only_count(user_id=""):
    with _lock:
        conn = _connect()
        return conn.execute("SELECT COUNT(*) FROM reviews WHERE user_id = ?",
                            (user_id,)).fetchone()[0]


@_cached
def weaknesses(user_id=""):
    """Rule-based weakness fingerprint. Each rule fires on REAL rows; severity &
    confidence derive from raw counts with explicit thresholds."""
    findings = []
    with _lock:
        conn = _connect()
        rev_count = conn.execute("SELECT COUNT(*) FROM reviews WHERE user_id = ?",
                                 (user_id,)).fetchone()[0]
        if rev_count == 0:
            return {"reviewedGames": 0, "weaknesses": [], "note": "No reviewed games yet — review some games to unlock weakness analysis."}

        def aggregate(sql):
            return conn.execute(sql, (user_id,)).fetchall()

        # ---- rule 1: misses (had a winning chance and failed to convert)
        miss_rows = aggregate(
            "SELECT g.id, g.white, g.black, g.result, g.date, g.ply_count "
            "FROM games g JOIN reviews r ON r.game_id = g.id "
            "WHERE g.user_id = ? AND r.misses > 0 ORDER BY g.date_ts DESC")
        if miss_rows:
            n = len(miss_rows)
            conf = "high" if n >= 5 else ("medium" if n >= 2 else "low")
            findings.append({
                "key": "misses", "label": "Missed winning opportunities",
                "count": n, "gamesCount": n,
                "severity": "high" if n >= 4 else "medium", "confidence": conf,
                "evidence": [r[0] for r in miss_rows[:20]],
                "description": f"You passed up a winning chance in {n} game(s) — the Trainer stores those critical positions as exercises.",
            })

        # ---- rule 2: blunders while already better (>= +250 cp before the move)
        # recomputed from moves_json in python (no json_each — clearer + testable)
        blunder_better_ids = []
        try:
            for row in conn.execute(
                "SELECT g.id, r.moves_json FROM games g JOIN reviews r ON r.game_id = g.id"
                " WHERE g.user_id = ?", (user_id,)):
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
            " WHERE g.user_id = ? AND t.time_json IS NOT NULL AND t.time_json != '{}'")
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
            "SELECT g.id, r.phase_json FROM games g JOIN reviews r ON r.game_id = g.id"
            " WHERE g.user_id = ?")
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
                if gap >= 12:
                    detail = f"{gap:.0f} points below your other phases"
                elif gap < 0:
                    detail = "your weakest phase relative to the others right now"
                else:
                    detail = "below your usual level in this phase"
                findings.append({
                    "key": f"phase_{ph}", "label": f"Weak {ph}",
                    "count": len(vals), "gamesCount": len(vals),
                    "severity": "high" if avg < 55 else "medium", "confidence": conf,
                    "evidence": phase_ids[ph][:20],
                    "description": f"{ph.capitalize()} accuracy averages {avg:.0f}% — {detail}.",
                })

        # ---- rule 5: conversion failure — had a position worth ≥85 win% and didn't win it
        conv_fail_ids = []
        try:
            for row in conn.execute(
                "SELECT g.id, g.result, r.moves_json FROM games g JOIN reviews r ON r.game_id = g.id"
                " WHERE g.user_id = ?", (user_id,)):
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


@_cached
def recommendations(user_id=""):
    w = weaknesses(user_id)
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
        elif f["key"] == "conversion":
            recs.append({
                "title": "Cash in your winning positions",
                "category": "Conversion",
                "why": f"{f['count']} game(s) were clearly winning (85%+ win chance) and finished without the point.",
                "exerciseFocus": "Open those games from your library, reload the winning position on the board, and practice converting against the engine.",
                "support": f["evidence"][:10],
                "priority": "high" if f["severity"] == "high" else "medium",
            })
        elif f["key"].startswith("phase_"):
            ph = f["key"].split("_", 1)[1]
            recs.append({
                "title": f"Strengthen your {ph}",
                "category": f"{ph.capitalize()} play",
                "why": f"{f['description']}",
                "exerciseFocus": f"Open the games that drove this from your library and re-watch how the {ph} slipped.",
                "support": f["evidence"][:10],
                "priority": "high" if f["severity"] == "high" else "medium",
            })

    # augment with trainer-specific weak categories (from the trainer DB - real data)
    try:
        from server import trainer as T
        dash = T.dashboard(user_id=user_id)
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
        "why": "Structured study pays compounding dividends: the openings you actually play are listed in the Explorer tab.",
        "exerciseFocus": "Review your five most-played openings with the Explorer and tag your must-know lines.",
        "support": [],
        "priority": "low" if recs else "medium",
    })
    return {"reviewedGames": w.get("reviewedGames", 0), "recommendations": recs}


@_cached
def timeline(user_id=""):
    with _lock:
        conn = _connect()
        monthly = conn.execute(
            "SELECT strftime('%Y-%m', date_ts, 'unixepoch'), COUNT(*),"
            " SUM(CASE WHEN result='1-0' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result='0-1' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result='1/2-1/2' THEN 1 ELSE 0 END)"
            " FROM games WHERE user_id = ? AND date_ts > 0 GROUP BY 1 ORDER BY 1",
            (user_id,)).fetchall()
        monthly_acc = conn.execute(
            "SELECT strftime('%Y-%m', g.date_ts, 'unixepoch'),"
            " AVG(CASE WHEN g.reviewed = 1 THEN r.white_acc END),"
            " AVG(CASE WHEN g.reviewed = 1 THEN r.black_acc END)"
            " FROM games g LEFT JOIN reviews r ON r.game_id = g.id"
            " WHERE g.user_id = ? AND g.date_ts > 0 GROUP BY 1 ORDER BY 1",
            (user_id,)).fetchall()
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


@_cached
def openings_report(user_id=""):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT eco, opening, white, black, result, date_ts"
            " FROM games WHERE user_id = ? AND (opening != '' OR eco != '')",
            (user_id,)).fetchall()
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
