"""Server-side Chess.com auto-sync.

The browser only turns the feature on and polls its status. The actual work runs
in daemon threads on the server:

  1. fetch the newest public games from chess.com's archive API
  2. import them into the account-scoped performance database (content-hash
     dedupe, exactly like a manual import)
  3. queue the newest unreviewed games and analyse them one at a time with the
     same review pipeline the manual reviewer uses
  4. attach each result to the performance database, which makes Performance,
     Rivals, the personal explorer and the Lab recommendations update together

The queue and its settings live in the durable database, so a restart resumes
instead of losing work. Only signed-in accounts are allowed to use it: the
guest pool is shared by every visitor, and an automatic background job must not
write somebody else's games into it.
"""
import re
import threading
import time
import traceback
from pathlib import Path

try:
    from server import chesscom, db, perfdb, review, security
except ImportError:  # pragma: no cover - flat layout
    import chesscom  # type: ignore
    import db  # type: ignore
    import perfdb  # type: ignore
    import review  # type: ignore
    import security  # type: ignore

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "autosync.sqlite"

# How often the supervisor looks for abandoned work and for accounts that are
# due a background check while the instance happens to be awake.
SUPERVISOR_INTERVAL = 300.0
BACKGROUND_INTERVAL = 1800.0        # 30 min between automatic checks per account
FETCH_COOLDOWN = 600.0              # opening the app repeatedly must not hammer chess.com
STALE_RUNNING = 2700.0              # a "running" item older than this is retried
QUEUE_CAP = 12                      # never let one account build an endless queue
RETRY_LIMIT = 2

STRENGTHS = {
    "quick": (10, 300),
    "balanced": (12, 500),
    "deep": (14, 900),
}
TIME_CLASSES = ("", "bullet", "blitz", "rapid", "daily")

DEFAULTS = {
    "user_id": "",
    "username": "",
    "time_class": "",
    "max_games": 3,
    "depth": 12,
    "movetime": 500,
    "enabled": 0,
    "last_fetch": 0.0,
    "last_status": "",
    "last_error": "",
    "last_game": "",
    "last_done": 0.0,
    "updated_at": 0.0,
}
_COLUMNS = tuple(DEFAULTS.keys())

_lock = threading.RLock()
_conn = None
_started = False
_engine_provider = None
_workers = {}
_workers_lock = threading.Lock()
_fetchers = set()
_fetchers_lock = threading.Lock()
_cancel = set()
_review_gate = threading.Lock()     # one automatic review at a time
_last_prune = 0.0


# ---------------------------------------------------------------- storage
def _connect():
    global _conn
    if _conn is None:
        conn = db.connect("autosync", DB_PATH)
        conn.execute("""CREATE TABLE IF NOT EXISTS sync_settings (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            time_class TEXT NOT NULL DEFAULT '',
            max_games INTEGER NOT NULL DEFAULT 3,
            depth INTEGER NOT NULL DEFAULT 12,
            movetime INTEGER NOT NULL DEFAULT 500,
            enabled INTEGER NOT NULL DEFAULT 0,
            last_fetch REAL NOT NULL DEFAULT 0,
            last_status TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            last_game TEXT NOT NULL DEFAULT '',
            last_done REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sync_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            game_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            progress REAL NOT NULL DEFAULT 0,
            stage TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL)""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_queue_game"
                     " ON sync_queue(user_id, game_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_queue_state"
                     " ON sync_queue(status, user_id)")
        conn.commit()
        _conn = conn
    return _conn


def init(engine_provider=None):
    """Create the tables and start the supervisor (idempotent)."""
    global _engine_provider, _started
    if engine_provider is not None:
        _engine_provider = engine_provider
    try:
        _connect()
    except Exception:
        # A database hiccup must not stop the app from serving reviews: the
        # request paths and the supervisor both retry the connection lazily.
        traceback.print_exc()
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_supervisor, daemon=True).start()


# ---------------------------------------------------------------- settings
def _settings_from_row(row):
    if not row:
        return dict(DEFAULTS)
    return dict(zip(_COLUMNS, row))


def get_settings(user_id):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT " + ", ".join(_COLUMNS) + " FROM sync_settings WHERE user_id = ?",
            (user_id,)).fetchone()
    return _settings_from_row(row)


def _truthy(value):
    return value in (True, 1, "1", "true", "on", "yes")


def _int_in(value, default, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _pick(data, snake, camel, default):
    """Accept both the API's camelCase and the stored snake_case spelling."""
    if snake in data:
        return data[snake]
    if camel in data:
        return data[camel]
    return default


def _strength_for(depth, movetime):
    for name, (d, m) in STRENGTHS.items():
        if int(depth) == d and int(movetime) == m:
            return name
    return "custom"


def public_settings(s):
    return {
        "username": s.get("username") or "",
        "timeClass": s.get("time_class") or "",
        "maxGames": int(s.get("max_games") or 3),
        "strength": _strength_for(s.get("depth") or 12, s.get("movetime") or 500),
        "depth": int(s.get("depth") or 12),
        "movetime": int(s.get("movetime") or 500),
        "enabled": bool(s.get("enabled")),
        "lastFetch": float(s.get("last_fetch") or 0),
        "lastDone": float(s.get("last_done") or 0),
        "lastStatus": s.get("last_status") or "",
        "lastError": s.get("last_error") or "",
        "lastGame": s.get("last_game") or "",
    }


def save_settings(user_id, data):
    """Validate + persist the per-account sync settings.

    Raises ValueError with a user-facing message for invalid input.
    """
    if not user_id:
        raise ValueError("Sign in to use auto-sync.")
    data = data or {}
    old = get_settings(user_id)

    username = str(data.get("username", old["username"]) or "").strip().lstrip("@")
    if username and not security.valid_external_username(username):
        raise ValueError("That is not a valid chess.com username.")

    time_class = str(_pick(data, "time_class", "timeClass",
                           old["time_class"]) or "").strip().lower()
    if time_class not in TIME_CLASSES:
        time_class = ""

    max_games = _int_in(_pick(data, "max_games", "maxGames", old["max_games"]),
                        3, 1, 10)

    depth = int(old["depth"] or 12)
    movetime = int(old["movetime"] or 500)
    strength = str(data.get("strength", "") or "").strip().lower()
    if strength in STRENGTHS:
        depth, movetime = STRENGTHS[strength]
    else:
        if "depth" in data:
            depth = _int_in(data.get("depth"), depth, 8, 16)
        if "movetime" in data:
            movetime = _int_in(data.get("movetime"), movetime, 200, 1500)

    enabled = 1 if _truthy(data.get("enabled", old["enabled"])) else 0
    if enabled and not username:
        raise ValueError("Add your chess.com username before turning on auto-sync.")

    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO sync_settings (user_id, username, time_class, max_games, depth,"
            " movetime, enabled, last_fetch, last_status, last_error, last_game,"
            " last_done, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            " username=excluded.username, time_class=excluded.time_class,"
            " max_games=excluded.max_games, depth=excluded.depth,"
            " movetime=excluded.movetime, enabled=excluded.enabled,"
            " updated_at=excluded.updated_at",
            (user_id, username, time_class, max_games, depth, movetime, enabled,
             old["last_fetch"], old["last_status"], old["last_error"], old["last_game"],
             old["last_done"], now))
        conn.commit()

    # Changing the chess.com identity or switching the feature off must not
    # leave another player's games queued for analysis.
    if (old["username"] or "").lower() != username.lower():
        _cancel_queued(user_id, "chess.com username changed")
    if not enabled:
        _cancel_queued(user_id, "auto-sync is off")

    return get_settings(user_id)


def _cancel_queued(user_id, reason):
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE sync_queue SET status='skipped', error=?, updated_at=?"
            " WHERE user_id = ? AND status = 'queued'",
            (reason, now, user_id))
        conn.commit()


# ---------------------------------------------------------------- status
def _queue_rows(user_id):
    with _lock:
        conn = _connect()
        active = conn.execute(
            "SELECT id, game_id, status, attempts, progress, stage, error,"
            " created_at, updated_at FROM sync_queue"
            " WHERE user_id = ? AND status IN ('queued','running')"
            " ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id LIMIT 8",
            (user_id,)).fetchall()
        recent = conn.execute(
            "SELECT id, game_id, status, attempts, progress, stage, error,"
            " created_at, updated_at FROM sync_queue"
            " WHERE user_id = ? AND status IN ('done','skipped','error')"
            " ORDER BY updated_at DESC LIMIT 6",
            (user_id,)).fetchall()
    return list(active) + list(recent)


def _item(row, games):
    gid = row[1]
    game = games.get(gid) or {}
    return {
        "queueId": row[0],
        "gameId": gid,
        "status": row[2],
        "attempts": int(row[3] or 0),
        "progress": round(float(row[4] or 0), 3),
        "stage": row[5] or "",
        "error": row[6] or "",
        "createdAt": float(row[7] or 0),
        "updatedAt": float(row[8] or 0),
        "white": game.get("white") or "",
        "black": game.get("black") or "",
        "result": game.get("result") or "",
        "date": game.get("date") or "",
        "opening": game.get("opening") or "",
        "reviewed": bool(game.get("reviewed")),
    }


def status(user_id):
    s = get_settings(user_id)
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM sync_queue WHERE user_id = ? GROUP BY status",
            (user_id,)).fetchall()
    counts = {"queued": 0, "running": 0, "done": 0, "error": 0, "skipped": 0}
    for state, n in rows:
        counts[state] = int(n or 0)

    queue_rows = _queue_rows(user_id)
    ids = [r[1] for r in queue_rows]
    games = {}
    if ids:
        for g in perfdb.games_by_id(ids, user_id):
            games[g["id"]] = g
    items = [_item(r, games) for r in queue_rows]
    active = next((i for i in items if i["status"] == "running"), None)
    return {
        "signedIn": True,
        "settings": public_settings(s),
        "counts": counts,
        "active": active,
        "items": items,
        "serverTime": time.time(),
    }


# ---------------------------------------------------------------- fetch + queue
def request_sync(user_id, manual=False):
    """Start a background fetch. Returns immediately; poll status() for progress."""
    s = get_settings(user_id)
    if not s.get("username"):
        return {"ok": False, "started": False, "reason": "no-username"}
    if not manual and not s.get("enabled"):
        return {"ok": False, "started": False, "reason": "disabled"}
    now = time.time()
    if not manual and now - float(s.get("last_fetch") or 0) < FETCH_COOLDOWN:
        return {"ok": True, "started": False, "reason": "cooldown",
                "nextIn": int(FETCH_COOLDOWN - (now - float(s.get("last_fetch") or 0)))}
    with _fetchers_lock:
        if user_id in _fetchers:
            return {"ok": True, "started": False, "reason": "already-running"}
        _fetchers.add(user_id)
    threading.Thread(target=_fetch_worker, args=(user_id, bool(manual)),
                     daemon=True).start()
    return {"ok": True, "started": True}


def _fetch_worker(user_id, manual):
    try:
        s = get_settings(user_id)
        if not s.get("username"):
            return
        want = min(20, max(8, int(s.get("max_games") or 3) * 3))
        fetched = chesscom.fetch_games(s["username"], max_games=want,
                                       time_class=s.get("time_class") or "",
                                       months=6)
        parsed = []
        for g in fetched:
            try:
                parsed.extend(perfdb.parse_pgn_text(g.get("pgn") or ""))
            except Exception:
                continue
        parsed.sort(key=lambda p: (p.get("dateTs") or 0, p.get("date") or ""), reverse=True)
        counts = perfdb.import_games(parsed, source="chesscom-auto", user_id=user_id)

        ids = [perfdb.scoped_game_id(p["id"], user_id)
               for p in parsed[:max(6, int(s.get("max_games") or 3) * 2)]]
        queued = enqueue(user_id, ids, limit=int(s.get("max_games") or 3),
                         force=manual)
        if queued:
            message = "Fetched %d game(s), queued %d for review" % (len(parsed), queued)
        elif counts.get("inserted"):
            message = "Fetched %d new game(s); nothing needed review" % counts["inserted"]
        else:
            message = "Checked chess.com - no new games"
        _touch(user_id, status=message, error="", fetched=True)
        if queued:
            kick(user_id)
    except chesscom.ChessComError as e:
        # An empty archive (new account, or no games in the selected speed) is
        # a normal state, not a failure users should see in red.
        if e.status == 404 and e.message.startswith("No games found"):
            _touch(user_id, status="Checked chess.com - no games found",
                   error="", fetched=True)
        else:
            _touch(user_id, status="Chess.com fetch failed", error=e.message, fetched=True)
    except Exception as e:
        traceback.print_exc()
        _touch(user_id, status="Auto-sync failed",
               error="%s: %s" % (type(e).__name__, e), fetched=True)
    finally:
        with _fetchers_lock:
            _fetchers.discard(user_id)


def enqueue(user_id, game_ids, limit=3, force=False):
    """Queue unreviewed games, newest first, without duplicating queue rows."""
    s = get_settings(user_id)
    if not force and not s.get("enabled"):
        return 0
    ids = [g for g in (game_ids or []) if g]
    if not ids:
        return 0
    metas = {g["id"]: g for g in perfdb.games_by_id(ids, user_id)}
    now = time.time()
    added = 0
    with _lock:
        conn = _connect()
        active = conn.execute(
            "SELECT COUNT(*) FROM sync_queue WHERE user_id = ?"
            " AND status IN ('queued','running')", (user_id,)).fetchone()[0]
        for gid in ids:
            if added >= max(0, int(limit)) or int(active or 0) >= QUEUE_CAP:
                break
            game = metas.get(gid)
            if not game or game.get("reviewed"):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO sync_queue"
                " (user_id, game_id, status, attempts, progress, stage, error,"
                " created_at, updated_at) VALUES (?,?,'queued',0,0,'','',?,?)",
                (user_id, gid, now, now))
            if cur.rowcount and cur.rowcount > 0:
                added += 1
                active = int(active or 0) + 1
        conn.commit()
    return added


# ---------------------------------------------------------------- review worker
def kick(user_id):
    """Ensure one review worker is running for this account."""
    if not user_id:
        return False
    with _workers_lock:
        t = _workers.get(user_id)
        if t is not None and t.is_alive():
            return False
        t = threading.Thread(target=_review_worker, args=(user_id,), daemon=True)
        _workers[user_id] = t
        t.start()
    return True


def _claim_next(user_id):
    now = time.time()
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT id, game_id, attempts FROM sync_queue"
            " WHERE user_id = ? AND status = 'queued' ORDER BY id LIMIT 1",
            (user_id,)).fetchone()
        if not row:
            return None
        cur = conn.execute(
            "UPDATE sync_queue SET status='running', attempts=attempts+1,"
            " progress=0, stage='starting', error='', updated_at=?"
            " WHERE id = ? AND status = 'queued'", (now, row[0]))
        conn.commit()
        if cur.rowcount == 0:
            return None
        return {"id": row[0], "game_id": row[1], "attempts": int(row[2] or 0) + 1}


def _finish(item_id, user_id, state, error="", progress=0.0, stage=""):
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE sync_queue SET status=?, error=?, progress=?, stage=?, updated_at=?"
            " WHERE id = ? AND user_id = ?",
            (state, error or "", progress, stage or state, time.time(), item_id, user_id))
        conn.commit()


def _requeue(item_id, user_id, error):
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE sync_queue SET status='queued', error=?, stage='', updated_at=?"
            " WHERE id = ? AND user_id = ?",
            (error or "", time.time(), item_id, user_id))
        conn.commit()


def _update_progress(item_id, user_id, progress, stage):
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE sync_queue SET progress=?, stage=?, updated_at=?"
            " WHERE id = ? AND user_id = ?",
            (progress, stage or "", time.time(), item_id, user_id))
        conn.commit()


def _touch(user_id, status=None, error=None, fetched=False, done=False, game=None):
    s = get_settings(user_id)
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE sync_settings SET last_fetch=?, last_status=?, last_error=?,"
            " last_game=?, last_done=?, updated_at=? WHERE user_id=?",
            (now if fetched else s["last_fetch"],
             s["last_status"] if status is None else status,
             s["last_error"] if error is None else error,
             s["last_game"] if game is None else game,
             now if done else s["last_done"],
             now, user_id))
        conn.commit()


def _review_worker(user_id):
    try:
        while user_id not in _cancel:
            item = _claim_next(user_id)
            if not item:
                break
            s = get_settings(user_id)
            game = perfdb.game_for_review(item["game_id"], user_id)
            if not game or not game.get("pgn"):
                _finish(item["id"], user_id, "skipped", "game no longer stored")
                continue
            if game.get("reviewed"):
                _finish(item["id"], user_id, "skipped", "already reviewed")
                continue
            try:
                engine = _engine_provider() if _engine_provider else None
                if engine is None:
                    raise RuntimeError("no chess engine available")
                last_write = [0.0, -1.0]

                def progress(msg):
                    if user_id in _cancel:
                        raise RuntimeError("cancelled")
                    text = msg or ""
                    m = re.match(r"engine (\d+)/(\d+)", text)
                    frac = None
                    if m:
                        frac = int(m.group(1)) / max(1, int(m.group(2)))
                    elif text == "classify":
                        frac = 0.97
                    if frac is None:
                        return
                    now = time.time()
                    if now - last_write[0] >= 3.0 and abs(frac - last_write[1]) >= 0.04:
                        last_write[0] = now
                        last_write[1] = frac
                        _update_progress(item["id"], user_id, round(frac, 3), text)

                with _review_gate:
                    if user_id in _cancel:
                        raise RuntimeError("cancelled")
                    result = review.run_review(
                        game["pgn"], engine,
                        depth=int(s.get("depth") or 12),
                        multipv=2,
                        movetime=int(s.get("movetime") or 500),
                        progress=progress)

                # Pin the result to the exact stored row. Without this,
                # attach_review falls back to fen + player names and could
                # update a different game between the same two players.
                if isinstance(result, dict):
                    raw_id = item["game_id"].split(":", 1)[1] if ":" in item["game_id"] else item["game_id"]
                    result.setdefault("game", {})["id"] = raw_id

                # A manual review might have landed while the engine was
                # thinking. Automatic analysis must never overwrite it.
                fresh = perfdb.game_for_review(item["game_id"], user_id)
                if fresh and fresh.get("reviewed"):
                    _finish(item["id"], user_id, "skipped", "reviewed manually")
                    continue
                attached = perfdb.attach_review(result, user_id)
                if not attached.get("ok"):
                    raise RuntimeError(attached.get("reason") or "review could not be stored")
                _finish(item["id"], user_id, "done", "", 1.0, "done")
                _touch(user_id,
                       status="Reviewed %s vs %s" % (game.get("white") or "?", game.get("black") or "?"),
                       error="", done=True,
                       game="%s vs %s" % (game.get("white") or "?", game.get("black") or "?"))
            except Exception as e:
                message = "%s: %s" % (type(e).__name__, e)
                if "cancelled" in str(e).lower():
                    _finish(item["id"], user_id, "skipped", "cancelled")
                    break
                traceback.print_exc()
                if item["attempts"] >= RETRY_LIMIT:
                    _finish(item["id"], user_id, "error", message)
                    _touch(user_id, status="Review failed", error=message)
                else:
                    _requeue(item["id"], user_id, message)
            time.sleep(0.4)
    finally:
        with _workers_lock:
            _workers.pop(user_id, None)
        # A game can be queued while this worker is winding down; without this
        # hand-off it would wait for the next supervisor tick.
        try:
            with _lock:
                conn = _connect()
                pending = conn.execute(
                    "SELECT 1 FROM sync_queue WHERE user_id = ? AND status = 'queued'"
                    " LIMIT 1", (user_id,)).fetchone()
            if pending and user_id not in _cancel:
                kick(user_id)
        except Exception:
            traceback.print_exc()
        _cancel.discard(user_id)


# ---------------------------------------------------------------- lifecycle
def cancel(user_id):
    _cancel.add(user_id)
    _cancel_queued(user_id, "cancelled")
    with _workers_lock:
        t = _workers.get(user_id)
        alive = bool(t is not None and t.is_alive())
    if not alive:
        _cancel.discard(user_id)
    return {"ok": True}


def _prune():
    global _last_prune
    now = time.time()
    if now - _last_prune < 86400:
        return
    _last_prune = now
    try:
        with _lock:
            conn = _connect()
            conn.execute("DELETE FROM sync_queue WHERE status IN ('done','skipped')"
                         " AND updated_at < ?", (now - 30 * 86400,))
            conn.execute("DELETE FROM sync_queue WHERE status = 'error'"
                         " AND updated_at < ?", (now - 90 * 86400,))
            conn.commit()
    except Exception:
        traceback.print_exc()


def resume():
    """Recover interrupted work and check accounts that are due a sync."""
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE sync_queue SET status='queued', stage='', updated_at=?"
            " WHERE status='running' AND updated_at < ?",
            (now, now - STALE_RUNNING))
        conn.commit()
        queued_users = [r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM sync_queue WHERE status='queued'").fetchall()]
        due_users = [r[0] for r in conn.execute(
            "SELECT user_id FROM sync_settings WHERE enabled=1 AND username <> ''"
            " AND last_fetch < ?", (now - BACKGROUND_INTERVAL,)).fetchall()]
    for uid in queued_users:
        if uid:
            kick(uid)
    for uid in due_users:
        if uid:
            request_sync(uid)
    _prune()


def _supervisor():
    time.sleep(5)
    while True:
        try:
            resume()
        except Exception:
            traceback.print_exc()
        time.sleep(SUPERVISOR_INTERVAL)
