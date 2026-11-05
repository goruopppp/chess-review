"""Persistent repertoire storage (SQLite).

Every repertoire build is auto-saved here; entries survive refreshes, browser
restarts and server restarts. All mutating operations are serialized through a
lock and written transactionally.
"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "repertoire.db"
_lock = threading.Lock()
_conn = None


def _connect():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute("""CREATE TABLE IF NOT EXISTS repertoires (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'white',
            mode TEXT NOT NULL DEFAULT 'mainline',
            root_fen TEXT NOT NULL DEFAULT '',
            data TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
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


def _row_meta(row):
    return {
        "id": row[0], "name": row[1], "side": row[2], "mode": row[3],
        "rootFen": row[4], "lines": _line_count(row[5]),
        "createdAt": row[6], "updatedAt": row[7],
    }


def _line_count(data_json):
    try:
        return len(json.loads(data_json).get("lines", []))
    except Exception:
        return 0


def list_repertoires():
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, name, side, mode, root_fen, data, created_at, updated_at "
            "FROM repertoires ORDER BY updated_at DESC").fetchall()
    return [_row_meta(r) for r in rows]


def save_repertoire(payload):
    """Upsert. payload: {id?, name, side, mode, rootFen, data:{lines,pgn,stats}}."""
    if not isinstance(payload, dict) or not payload.get("data"):
        raise ValueError("repertoire data missing")
    data = payload["data"]
    if not isinstance(data.get("lines"), list):
        raise ValueError("repertoire lines missing")
    now = time.time()
    rid = (payload.get("id") or "").strip() or uuid.uuid4().hex[:12]
    name = (payload.get("name") or "").strip() or "Unnamed repertoire"
    side = payload.get("side") if payload.get("side") in ("white", "black") else "white"
    mode = payload.get("mode") or "mainline"
    root_fen = payload.get("rootFen") or ""
    data_json = json.dumps(data)
    with _lock:
        conn = _connect()
        cur = conn.execute("SELECT created_at FROM repertoires WHERE id = ?", (rid,)).fetchone()
        created = cur[0] if cur else now
        conn.execute(
            "INSERT INTO repertoires (id, name, side, mode, root_fen, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, side=excluded.side, "
            "mode=excluded.mode, root_fen=excluded.root_fen, data=excluded.data, "
            "updated_at=excluded.updated_at",
            (rid, name, side, mode, root_fen, data_json, created, now))
        conn.commit()
    return {"id": rid, "name": name}


def get_repertoire(rid):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT id, name, side, mode, root_fen, data, created_at, updated_at "
            "FROM repertoires WHERE id = ?", (rid,)).fetchone()
    if not row:
        raise ValueError("repertoire not found")
    meta = _row_meta(row)
    meta["data"] = json.loads(row[5])
    return meta


def delete_repertoire(rid):
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM repertoires WHERE id = ?", (rid,))
        conn.commit()
    if cur.rowcount == 0:
        raise ValueError("repertoire not found")
    return True


def rename_repertoire(rid, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    with _lock:
        conn = _connect()
        cur = conn.execute("UPDATE repertoires SET name = ?, updated_at = ? WHERE id = ?",
                           (name, time.time(), rid))
        conn.commit()
    if cur.rowcount == 0:
        raise ValueError("repertoire not found")
    return True
