"""Chess Performance Lab — persistent analytics store (SQLite).

Games are deduped by content hash (starting FEN + player names + the full mainline).
Reviews attached later upgrade the record to 'reviewed' with per-move detail so every
statistic traces back to real rows — no fabricated numbers.

Deliberately synchronous-safe (module lock) + each mutation in a transaction.
JSON blobs hold per-game breakdowns (phases, accuracy trend, move quality); everything
else is indexed columns so filters never scan.
"""
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import chess
import chess.pgn

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

# --- personal position index (v87) ---------------------------------------
# Replaying every stored PGN on each explorer move would be wasteful, so the
# full position -> results/continuations index is built once per account and
# kept until a write clears it (cache_clear). Bounded so many accounts cannot
# grow the process without limit.
_POS_INDEX = {}
_POS_LOCK = threading.RLock()
_POS_MAX = 8


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
    with _POS_LOCK:
        _POS_INDEX.clear()


def _freeze(value):
    """Make list/dict arguments hashable for the short-lived read cache."""
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, tuple):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    return value


def _cached(fn):
    """Memoize a read-only analytics function for a few seconds."""
    def wrapper(*args, **kwargs):
        key = (fn.__name__, tuple(_freeze(a) for a in args),
               tuple(sorted((k, _freeze(v)) for k, v in kwargs.items())))
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


def scoped_game_id(raw_id, user_id=""):
    """Public wrapper for the account-scoped row id used by import/queue code."""
    return _row_id(raw_id, user_id)


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


def game_for_review(gid, user_id=""):
    """Load the stored PGN + reviewed flag for one account-scoped game.

    Deliberately uncached: the auto-sync worker uses it right before analysing
    and again right before writing, so a manual review that landed in between
    is always visible.
    """
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT id, white, black, result, date, reviewed, pgn_text, opening"
            " FROM games WHERE id = ? AND user_id = ?", (gid, user_id)).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "white": row[1], "black": row[2], "result": row[3],
        "date": row[4], "reviewed": bool(row[5]), "pgn": row[6] or "",
        "opening": row[7] or "",
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


# ---------------------------------------------------------------- personal position explorer


def _position_key(board):
    """Identity of a position for the personal opening explorer.

    Move counters are irrelevant (transpositions must merge), while a legal
    en passant target is part of the position. python-chess only reports the
    ep square when a capture is actually available, so no ghost-ep merges.
    """
    fen = board.fen().split()
    ep = board.ep_square if board.has_legal_en_passant() else None
    fen[3] = chess.square_name(ep) if ep is not None else "-"
    return " ".join(fen[:4])


def _fen_key(fen):
    """Normalise a client FEN to the same key the index uses."""
    try:
        return _position_key(chess.Board(fen))
    except Exception:
        return " ".join((fen or "").split()[:4])


def _pgn_result_score(result):
    r = (result or "").strip()
    if r == "1-0":
        return 1.0
    if r == "0-1":
        return 0.0
    if r in ("1/2-1/2", "0.5-0.5"):
        return 0.5
    return None


def _build_position_index(user_id=""):
    """Replay every stored PGN once: position -> record, continuations, refs.

    Results and per-move accuracy come from the real games/reviews rows. No
    engine work and no inferred numbers: a node exists only because a game
    actually reached it.
    """
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT g.id, g.white, g.black, g.result, g.date, g.date_ts, g.opening,"
            " g.pgn_text, r.moves_json"
            " FROM games g LEFT JOIN reviews r"
            "   ON r.game_id = g.id AND r.user_id = g.user_id"
            " WHERE g.user_id = ?", (user_id,)).fetchall()
    positions = {}
    games = []
    for gid, white, black, result, date, date_ts, opening, pgn_text, moves_json in rows:
        if not pgn_text:
            continue
        try:
            game = chess.pgn.read_game(io.StringIO(pgn_text))
            if game is None:
                continue
            board = game.board()
            mainline = list(game.mainline_moves())
        except Exception:
            continue
        try:
            detail = json.loads(moves_json) if moves_json else []
            if not isinstance(detail, list):
                detail = []
        except Exception:
            detail = []
        score = _pgn_result_score(result)
        gi = len(games)
        games.append({"id": gid, "white": white, "black": black,
                      "result": result, "date": date, "dateTs": date_ts or 0,
                      "opening": opening or ""})
        last_key = None
        for i, move in enumerate(mainline):
            key = _position_key(board)
            last_key = key
            node = positions.get(key)
            if node is None:
                node = positions[key] = {"n": 0, "w": 0, "d": 0, "l": 0, "u": 0,
                                         "moves": {}, "refs": []}
            node["n"] += 1
            if score is None:
                node["u"] += 1
            elif score == 1.0:
                node["w"] += 1
            elif score == 0.5:
                node["d"] += 1
            else:
                node["l"] += 1
            try:
                san = board.san(move)
                uci = move.uci()
            except Exception:
                break
            mv = node["moves"].get(san)
            if mv is None:
                mv = node["moves"][san] = {"san": san, "uci": uci, "n": 0,
                                           "w": 0, "d": 0, "l": 0, "u": 0,
                                           "accSum": 0.0, "accN": 0}
            mv["n"] += 1
            if score is None:
                mv["u"] += 1
            elif score == 1.0:
                mv["w"] += 1
            elif score == 0.5:
                mv["d"] += 1
            else:
                mv["l"] += 1
            info = detail[i] if i < len(detail) and isinstance(detail[i], dict) else None
            if info:
                acc = info.get("accuracy")
                if isinstance(acc, (int, float)):
                    mv["accSum"] += float(acc)
                    mv["accN"] += 1
            node["refs"].append((gi, i))
            try:
                board.push(move)
            except Exception:
                break
        # The position after the last move is a real node too (typically an
        # endgame). Its continuation list is empty, but its record is not.
        final_key = _position_key(board)
        if final_key != last_key:
            node = positions.get(final_key)
            if node is None:
                node = positions[final_key] = {"n": 0, "w": 0, "d": 0, "l": 0, "u": 0,
                                               "moves": {}, "refs": []}
            node["n"] += 1
            if score is None:
                node["u"] += 1
            elif score == 1.0:
                node["w"] += 1
            elif score == 0.5:
                node["d"] += 1
            else:
                node["l"] += 1
            node["refs"].append((gi, len(mainline)))
    return {"positions": positions, "games": games, "total": len(games)}


def _position_index(user_id=""):
    key = user_id or ""
    with _POS_LOCK:
        hit = _POS_INDEX.pop(key, None)
        if hit is not None:
            _POS_INDEX[key] = hit       # refresh LRU order
            return hit
    idx = _build_position_index(key)
    with _POS_LOCK:
        _POS_INDEX[key] = idx
        while len(_POS_INDEX) > _POS_MAX:
            _POS_INDEX.pop(next(iter(_POS_INDEX)))
    return idx


def position_report(fen, color="white", user_id=""):
    """Your results and continuations from one position (personal explorer)."""
    me = "black" if (color or "").strip().lower() == "black" else "white"
    idx = _position_index(user_id or "")
    try:
        turn = "white" if chess.Board(fen).turn else "black"
    except Exception:
        turn = "white"
    node = idx["positions"].get(_fen_key(fen))
    base = {"positionGames": 0, "totalGames": idx["total"], "me": me,
            "turn": turn, "score": None, "wins": 0, "draws": 0, "losses": 0,
            "unscored": 0, "moves": [], "recent": []}
    if not node:
        return base

    def mine(wins, losses):
        return (wins, losses) if me == "white" else (losses, wins)

    wins, losses = mine(node["w"], node["l"])
    draws = node["d"]
    decided = wins + draws + losses
    out = dict(base)
    out.update({
        "positionGames": node["n"],
        "score": round((wins + 0.5 * draws) / decided * 100, 1) if decided else None,
        "wins": wins, "draws": draws, "losses": losses, "unscored": node["u"],
    })
    rows = []
    for mv in node["moves"].values():
        w, l = mine(mv["w"], mv["l"])
        dec = w + mv["d"] + l
        rows.append({
            "san": mv["san"], "uci": mv["uci"], "games": mv["n"],
            "share": round(100 * mv["n"] / node["n"], 1) if node["n"] else 0,
            "wins": w, "draws": mv["d"], "losses": l, "unscored": mv["u"],
            "score": round((w + 0.5 * mv["d"]) / dec * 100, 1) if dec else None,
            "accuracy": (round(mv["accSum"] / mv["accN"], 1)
                         if (turn == me and mv["accN"]) else None),
        })
    rows.sort(key=lambda r: (-r["games"], r["san"]))
    out["moves"] = rows[:12]
    refs = {}
    for gi, ply in node["refs"]:
        refs.setdefault(gi, ply)
    recent = []
    for gi, ply in sorted(refs.items(),
                          key=lambda kv: -(idx["games"][kv[0]]["dateTs"] or 0))[:5]:
        g = dict(idx["games"][gi])
        g["ply"] = ply
        recent.append(g)
    out["recent"] = recent
    return out


# ---------------------------------------------------------------- opponent scouting (v89)


def _person_key(name):
    """Case/spacing-insensitive identity for a player name."""
    return " ".join((name or "").split()).strip().casefold()


def _wdl_bucket():
    return {"games": 0, "wins": 0, "draws": 0, "losses": 0,
            "unscored": 0, "score": None}


def _wdl_add(bucket, score):
    bucket["games"] += 1
    if score is None:
        bucket["unscored"] += 1
    elif score == 1.0:
        bucket["wins"] += 1
    elif score == 0.5:
        bucket["draws"] += 1
    else:
        bucket["losses"] += 1


def _wdl_done(bucket):
    decided = bucket["wins"] + bucket["draws"] + bucket["losses"]
    bucket["score"] = (round(100.0 * (bucket["wins"] + 0.5 * bucket["draws"]) / decided, 1)
                       if decided else None)
    return bucket


def _score_for(result, color):
    s = _pgn_result_score(result)
    if s is None:
        return None
    return s if color == "white" else 1.0 - s


def _plan_ref(op):
    if not op:
        return None
    return {"name": op["name"], "eco": op["eco"], "games": op["games"],
            "score": op["score"], "theirColor": op["theirColor"]}


def _scout_plan(rec):
    """Deterministic match plan from repeated, real results only."""
    repeated = [o for o in rec["openings"]
                if o["games"] >= 2 and o["score"] is not None]
    weapon = avoid = None
    if repeated:
        best = sorted(repeated, key=lambda o: (-o["score"], -o["games"],
                                               o["name"].casefold()))[0]
        if best["score"] >= 60:
            weapon = best
        worst = sorted(repeated, key=lambda o: (o["score"], -o["games"],
                                               o["name"].casefold()))[0]
        if worst["score"] <= 40:
            avoid = worst
    favorite = None
    if rec["openings"]:
        favorite = sorted(rec["openings"],
                          key=lambda o: (-o["games"], o["name"].casefold()))[0]
    phases = [(ph, acc) for ph, acc in rec["opp"]["phase"].items()
              if acc is not None]
    weak = min(phases, key=lambda kv: kv[1]) if (phases and rec["opp"]["reviewed"] >= 2) else None
    colors = [(c, b) for c, b in rec["colors"].items()
              if b["games"] >= 2 and b["score"] is not None]
    best_color = max(colors, key=lambda kv: (kv[1]["score"], kv[1]["games"]))[0] if colors else None

    notes = []
    if weapon:
        notes.append({"kind": "weapon",
                      "text": f"Steer into {weapon['name']} - you score "
                              f"{weapon['score']:.0f}% in {weapon['games']} games."})
    if avoid:
        notes.append({"kind": "avoid",
                      "text": f"Avoid {avoid['name']} - only "
                              f"{avoid['score']:.0f}% in {avoid['games']} games."})
    if favorite:
        notes.append({"kind": "info",
                      "text": f"Most-faced opening: {favorite['name']} "
                              f"({favorite['games']} game{'s' if favorite['games'] != 1 else ''})."})
    if weak:
        if weak[1] <= 80:
            notes.append({"kind": "info",
                          "text": f"Attack their {weak[0]} - they average "
                                  f"{weak[1]:.0f}% accuracy there."})
        else:
            notes.append({"kind": "info",
                          "text": f"Lowest phase: {weak[0]} at {weak[1]:.0f}% accuracy."})
    if not rec["opp"]["reviewed"]:
        notes.append({"kind": "info",
                      "text": "Review these games to unlock their accuracy and phase tendencies."})
    if not notes:
        notes.append({"kind": "info",
                      "text": "Play them again to unlock a match plan."})
    return {
        "weapon": _plan_ref(weapon),
        "avoid": _plan_ref(avoid),
        "favorite": _plan_ref(favorite),
        "weakPhase": weak[0] if weak else None,
        "weakPhaseAcc": weak[1] if weak else None,
        "bestColor": best_color,
        "notes": notes[:4],
    }


@_cached
def rivals(user_id="", me=None):
    """Opponent scouting from stored games, auto-detecting which name is you.

    `me` is an optional explicit name; when it does not match a stored player
    the most frequent name wins and `detected` stays true so the UI can ask.
    W/D/L and score are always from the scouted player's side, never the
    opponent's. All numbers come from stored results and reviews only.
    """
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT g.id, g.white, g.black, g.white_elo, g.black_elo, g.result,"
            " g.date, g.date_ts, g.tc_bucket, g.opening, g.eco, g.ply_count,"
            " r.white_acc, r.black_acc, r.white_acpl, r.black_acpl,"
            " r.phase_json, r.moves_json"
            " FROM games g LEFT JOIN reviews r"
            "   ON r.game_id = g.id AND r.user_id = g.user_id"
            " WHERE g.user_id = ? ORDER BY g.date_ts DESC, g.id DESC",
            (user_id,)).fetchall()

    counts, display = {}, {}
    for row in rows:
        for name in (row[1], row[2]):
            key = _person_key(name)
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
            display.setdefault(key, " ".join((name or "").split()))
    ranked = sorted(counts, key=lambda k: (-counts[k], display[k].casefold()))
    candidates = [{"name": display[k], "games": counts[k]} for k in ranked[:50]]

    me_name, detected = "", True
    if me:
        key = _person_key(_canonical_player(me.strip(), user_id))
        if key in counts:
            me_name, detected = display[key], False
    if not me_name and ranked:
        me_name = display[ranked[0]]

    if not me_name:
        return {"me": "", "detected": True, "candidates": [],
                "totalGames": len(rows), "matchedGames": 0,
                "skipped": len(rows), "rivals": []}

    me_key = _person_key(me_name)
    table = {}
    matched = skipped = 0
    for row in rows:
        (gid, white, black, welo, belo, result, date, date_ts, tc,
         opening, eco, plies, wacc, bacc, wacpl, bacpl,
         phase_json, moves_json) = row
        my_white = _person_key(white) == me_key
        my_black = _person_key(black) == me_key
        if my_white == my_black:
            skipped += 1
            continue
        my_color = "white" if my_white else "black"
        opp_color = "black" if my_color == "white" else "white"
        opp_name = black if my_color == "white" else white
        okey = _person_key(opp_name)
        if not okey:
            skipped += 1
            continue
        matched += 1
        rec = table.get(okey)
        if rec is None:
            rec = table[okey] = {
                "name": " ".join((opp_name or "").split()),
                **_wdl_bucket(),
                "colors": {"white": _wdl_bucket(), "black": _wdl_bucket()},
                "openings": {},
                "opp": {"reviewed": 0, "accSum": 0.0, "accN": 0,
                        "acplSum": 0.0, "acplN": 0, "moves": 0,
                        "blunders": 0, "mistakes": 0, "inaccuracies": 0,
                        "misses": 0,
                        "phase": {ph: {"sum": 0.0, "n": 0}
                                  for ph in ("opening", "middlegame", "endgame")}},
                "my": {"reviewed": 0, "accSum": 0.0, "accN": 0,
                       "acplSum": 0.0, "acplN": 0},
                "recent": [], "lastTs": -1, "lastDate": "",
                "firstTs": None, "firstDate": "", "eloSum": 0.0, "eloN": 0,
            }

        score = _score_for(result, my_color)
        _wdl_add(rec, score)
        _wdl_add(rec["colors"][my_color], score)

        opkey = (opening or "Unknown", eco or "", opp_color)
        op = rec["openings"].get(opkey)
        if op is None:
            op = rec["openings"][opkey] = {
                "name": opening or "Unknown", "eco": eco or "",
                "theirColor": opp_color, **_wdl_bucket(),
            }
        _wdl_add(op, score)

        my_acc = opp_acc = my_acpl = opp_acpl = None
        if moves_json or phase_json:
            rec["opp"]["reviewed"] += 1
            rec["my"]["reviewed"] += 1
            opp_acc = bacc if opp_color == "black" else wacc
            opp_acpl = bacpl if opp_color == "black" else wacpl
            my_acc = wacc if my_color == "white" else bacc
            my_acpl = wacpl if my_color == "white" else bacpl
            for bucket, acc, acpl in ((rec["opp"], opp_acc, opp_acpl),
                                       (rec["my"], my_acc, my_acpl)):
                if isinstance(acc, (int, float)):
                    bucket["accSum"] += float(acc)
                    bucket["accN"] += 1
                if isinstance(acpl, (int, float)):
                    bucket["acplSum"] += float(acpl)
                    bucket["acplN"] += 1
            try:
                mvs = json.loads(moves_json or "[]")
            except Exception:
                mvs = []
            if isinstance(mvs, list):
                for m in mvs:
                    if not isinstance(m, dict) or m.get("color") != opp_color:
                        continue
                    if m.get("class") == "book":
                        continue
                    rec["opp"]["moves"] += 1
                    cls = m.get("class")
                    if cls == "blunder":
                        rec["opp"]["blunders"] += 1
                    elif cls == "mistake":
                        rec["opp"]["mistakes"] += 1
                    elif cls == "inaccuracy":
                        rec["opp"]["inaccuracies"] += 1
                    elif cls == "miss":
                        rec["opp"]["misses"] += 1
            try:
                phases = json.loads(phase_json or "{}")
            except Exception:
                phases = {}
            side = phases.get(opp_color) if isinstance(phases, dict) else None
            if isinstance(side, dict):
                for ph in ("opening", "middlegame", "endgame"):
                    cell = side.get(ph)
                    acc = cell.get("accuracy") if isinstance(cell, dict) else None
                    if isinstance(acc, (int, float)):
                        rec["opp"]["phase"][ph]["sum"] += float(acc)
                        rec["opp"]["phase"][ph]["n"] += 1

        ts = int(date_ts or 0)
        if ts >= rec["lastTs"]:
            rec["lastTs"], rec["lastDate"] = ts, date or ""
        if rec["firstTs"] is None or ts < rec["firstTs"]:
            rec["firstTs"], rec["firstDate"] = ts, date or ""
        opp_elo = belo if my_color == "white" else welo
        if isinstance(opp_elo, int) and opp_elo > 0:
            rec["eloSum"] += opp_elo
            rec["eloN"] += 1
        rec["recent"].append({
            "id": gid, "date": date or "", "dateTs": ts, "result": result or "*",
            "myColor": my_color, "opening": opening or "", "eco": eco or "",
            "plyCount": plies or 0,
            "myAcc": round(my_acc, 1) if isinstance(my_acc, (int, float)) else None,
            "oppAcc": round(opp_acc, 1) if isinstance(opp_acc, (int, float)) else None,
        })

    out = []
    for rec in table.values():
        _wdl_done(rec)
        for b in rec["colors"].values():
            _wdl_done(b)
        openings = []
        for op in rec["openings"].values():
            _wdl_done(op)
            openings.append(op)
        openings.sort(key=lambda o: (-o["games"], o["name"].casefold()))
        rec["openings"] = openings[:12]
        rec["recent"] = sorted(rec["recent"],
                               key=lambda g: (-(g["dateTs"] or 0), g["id"]))[:5]
        opp = rec["opp"]
        rec["opp"] = {
            "reviewed": opp["reviewed"],
            "accuracy": round(opp["accSum"] / opp["accN"], 1) if opp["accN"] else None,
            "acpl": round(opp["acplSum"] / opp["acplN"], 1) if opp["acplN"] else None,
            "moves": opp["moves"],
            "blunders": opp["blunders"], "mistakes": opp["mistakes"],
            "inaccuracies": opp["inaccuracies"], "misses": opp["misses"],
            "errorRate": (round(100.0 * (opp["blunders"] + opp["mistakes"]
                                        + opp["inaccuracies"] + opp["misses"])
                               / opp["moves"], 1) if opp["moves"] else None),
            "phase": {ph: (round(v["sum"] / v["n"], 1) if v["n"] else None)
                      for ph, v in opp["phase"].items()},
        }
        my = rec["my"]
        rec["my"] = {
            "reviewed": my["reviewed"],
            "accuracy": round(my["accSum"] / my["accN"], 1) if my["accN"] else None,
            "acpl": round(my["acplSum"] / my["acplN"], 1) if my["acplN"] else None,
        }
        rec["oppElo"] = (round(rec["eloSum"] / rec["eloN"])
                         if rec["eloN"] else None)
        rec["confidence"] = ("low" if rec["games"] < 4
                             else ("medium" if rec["games"] < 10 else "high"))
        rec["plan"] = _scout_plan(rec)
        rec.pop("eloSum", None)
        rec.pop("eloN", None)
        out.append(rec)

    out.sort(key=lambda r: (-r["games"],
                            (r["score"] if r["score"] is not None else 101),
                            r["name"].casefold()))
    return {"me": me_name, "detected": detected, "candidates": candidates,
            "totalGames": len(rows), "matchedGames": matched,
            "skipped": skipped, "rivals": out}


def rival_report(name, user_id="", me=None):
    """One rival from the cached scouting table (None when not found)."""
    data = rivals(user_id, me=me)
    key = _person_key(name)
    found = next((r for r in data["rivals"]
                  if _person_key(r["name"]) == key), None)
    out = {k: data[k] for k in ("me", "detected", "candidates",
                                "totalGames", "matchedGames", "skipped")}
    out["rival"] = found
    return out
