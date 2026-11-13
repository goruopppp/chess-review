"""Persistent repertoire storage (SQLite).

Every repertoire build is auto-saved here; entries survive refreshes, browser
restarts and server restarts. All mutating operations are serialized through a
lock and written transactionally.

Repertoires are per-account: the browser derives a deterministic id from
(side, mode, root FEN), so two accounts building the same repertoire would
collide on the primary key. Ids are therefore stored prefixed with the account
id and the client keeps seeing the plain id it generated.
"""
import json
import threading
import time
import uuid
from pathlib import Path

try:
    from server import db
except ImportError:
    import db  # type: ignore

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "repertoire.db"
_lock = threading.Lock()
_conn = None


def _connect():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = db.connect("repstore", DB_PATH)
        conn.execute("""CREATE TABLE IF NOT EXISTS repertoires (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'white',
            mode TEXT NOT NULL DEFAULT 'mainline',
            root_fen TEXT NOT NULL DEFAULT '',
            data TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            user_id TEXT NOT NULL DEFAULT ''
        )""")
        # tolerant migration: pre-accounts databases gain the user column
        cols = {r[1] for r in conn.execute("PRAGMA table_info(repertoires)")}
        if "user_id" not in cols:
            conn.execute("ALTER TABLE repertoires ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        conn.commit()
        _conn = conn
    return _conn


def init_db():
    try:
        _connect()
        return True
    except Exception as e:
        print(f"[repstore] init failed: {e}")
        return False


# ---------------------------------------------------------------- id scoping
def _rid(raw_id, user_id=""):
    raw = (raw_id or "").strip()
    return f"{user_id}:{raw}" if user_id else raw


def _public(db_id, user_id=""):
    s = db_id or ""
    if user_id and s.startswith(user_id + ":"):
        return s[len(user_id) + 1:]
    return s


def _row_meta(row, user_id=""):
    return {
        "id": _public(row[0], user_id), "name": row[1], "side": row[2], "mode": row[3],
        "rootFen": row[4], "lines": _line_count(row[5]),
        "createdAt": row[6], "updatedAt": row[7],
    }


def _line_count(data_json):
    try:
        return len(json.loads(data_json).get("lines", []))
    except Exception:
        return 0


def list_repertoires(user_id=""):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, name, side, mode, root_fen, data, created_at, updated_at "
            "FROM repertoires WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)).fetchall()
    return [_row_meta(r, user_id) for r in rows]


def save_repertoire(payload, user_id=""):
    """Upsert. payload: {id?, name, side, mode, rootFen, data:{lines,pgn,stats}}."""
    if not isinstance(payload, dict) or not payload.get("data"):
        raise ValueError("repertoire data missing")
    data = payload["data"]
    if not isinstance(data.get("lines"), list):
        raise ValueError("repertoire lines missing")
    now = time.time()
    public_id = (payload.get("id") or "").strip() or uuid.uuid4().hex[:12]
    rid = _rid(public_id, user_id)
    name = (payload.get("name") or "").strip() or "Unnamed repertoire"
    side = payload.get("side") if payload.get("side") in ("white", "black") else "white"
    mode = payload.get("mode") or "mainline"
    root_fen = payload.get("rootFen") or ""
    data_json = json.dumps(data)
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "SELECT created_at FROM repertoires WHERE id = ? AND user_id = ?",
            (rid, user_id)).fetchone()
        created = cur[0] if cur else now
        conn.execute(
            "INSERT INTO repertoires (id, name, side, mode, root_fen, data, created_at, updated_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, side=excluded.side, "
            "mode=excluded.mode, root_fen=excluded.root_fen, data=excluded.data, "
            "updated_at=excluded.updated_at "
            "WHERE repertoires.user_id = excluded.user_id",
            (rid, name, side, mode, root_fen, data_json, created, now, user_id))
        conn.commit()
    return {"id": public_id, "name": name}


def get_repertoire(rid, user_id=""):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT id, name, side, mode, root_fen, data, created_at, updated_at "
            "FROM repertoires WHERE id = ? AND user_id = ?", (_rid(rid, user_id), user_id)).fetchone()
    if not row:
        raise ValueError("repertoire not found")
    meta = _row_meta(row, user_id)
    meta["data"] = json.loads(row[5])
    return meta


def delete_repertoire(rid, user_id=""):
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM repertoires WHERE id = ? AND user_id = ?",
                           (_rid(rid, user_id), user_id))
        conn.commit()
    if cur.rowcount == 0:
        raise ValueError("repertoire not found")
    return True


def rename_repertoire(rid, name, user_id=""):
    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE repertoires SET name = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (name, time.time(), _rid(rid, user_id), user_id))
        conn.commit()
    if cur.rowcount == 0:
        raise ValueError("repertoire not found")
    return True
