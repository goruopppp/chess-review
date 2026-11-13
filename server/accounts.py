"""Accounts: register/login sessions + per-user progress library.

Users, sessions and each account's saved-game library live in the same backend
the other stores use (local SQLite file by default, Turso/libSQL when
TURSO_DATABASE_URL is set — see server/db.py). Passwords are PBKDF2-HMAC-SHA256
hashed; sessions are opaque bearer tokens in the sessions table, so signing out
on one device does not affect other devices and tokens survive restarts.

The saved-game library mirrors exactly what the browser keeps (entries shaped
{id, savedAt, review}); the client is the source of truth and pushes/pulls the
full list, so the server copy always matches the newest device that synced.
"""
import hashlib
import json
import re
import secrets
import threading
import time
from pathlib import Path

try:
    from server import db
except ImportError:
    import db  # type: ignore

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "accounts.sqlite"
_lock = threading.RLock()
_conn = None
PBKDF_ITERS = 200_000

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,24}$")

# in-process session cache: token -> (user_id, cached_at); sessions are stable,
# so a short TTL only guards against a token revoked by logout on THIS server
_sess_cache = {}
_SESS_TTL = 120.0


# ---------------------------------------------------------------- schema
def _connect():
    global _conn
    if _conn is None:
        conn = db.connect("accounts", DB_PATH)
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            pw_hash TEXT NOT NULL,
            created_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS user_library (
            user_id TEXT NOT NULL,
            game_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            saved_at REAL NOT NULL,
            PRIMARY KEY (user_id, game_id))""")
        conn.commit()
        _conn = conn
    return _conn


def init_db():
    try:
        _connect()
        return True
    except Exception as e:
        print(f"[accounts] init failed: {e}")
        return False


# ---------------------------------------------------------------- passwords
def _hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), PBKDF_ITERS)
    return f"pbkdf2${PBKDF_ITERS}${salt}${h.hex()}"


def _verify_pw(pw, stored):
    try:
        scheme, iters, salt, hexd = (stored or "").split("$")
        if scheme != "pbkdf2":
            return False
        h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return secrets.compare_digest(h.hex(), hexd)
    except Exception:
        return False


# ---------------------------------------------------------------- auth
def register(username, password):
    username = (username or "").strip()
    password = password or ""
    if not _NAME_RE.match(username):
        raise ValueError("Username must be 3-24 characters (letters, digits, _ . -)")
    if len(password) < 6 or len(password) > 200:
        raise ValueError("Password must be at least 6 characters")
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE",
                           (username,)).fetchone()
        if row:
            raise ValueError("That username is already taken")
        uid = secrets.token_hex(8)
        now = time.time()
        conn.execute("INSERT INTO users(id, username, pw_hash, created_at) VALUES (?,?,?,?)",
                     (uid, username, _hash_pw(password), now))
        conn.commit()
    return {"token": _new_session(uid),
            "user": {"id": uid, "username": username, "createdAt": now}}


def login(username, password):
    username = (username or "").strip()
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT id, username, pw_hash, created_at FROM users WHERE username = ? COLLATE NOCASE",
            (username,)).fetchone()
    if not row or not _verify_pw(password or "", row[2]):
        raise ValueError("Wrong username or password")
    return {"token": _new_session(row[0]),
            "user": {"id": row[0], "username": row[1], "createdAt": row[3]}}


def _new_session(uid):
    token = "c" + secrets.token_urlsafe(32)
    with _lock:
        conn = _connect()
        conn.execute("INSERT INTO sessions(token, user_id, created_at) VALUES (?,?,?)",
                     (token, uid, time.time()))
        conn.commit()
    _sess_cache[token] = (uid, time.time())
    return token


def logout(token):
    if not token:
        return {"ok": True}
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    _sess_cache.pop(token, None)
    return {"ok": True}


def me(token):
    uid = uid_from_token(token)
    if not uid:
        return None
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT id, username, created_at FROM users WHERE id = ?",
                           (uid,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "createdAt": row[2]}


def uid_from_token(token):
    """Bearer-token lookup with a tiny in-process cache (one RTT saved per call)."""
    if not token:
        return ""
    hit = _sess_cache.get(token)
    if hit and time.time() - hit[1] < _SESS_TTL:
        return hit[0]
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT user_id FROM sessions WHERE token = ?", (token,)).fetchone()
    uid = row[0] if row else ""
    if uid:
        _sess_cache[token] = (uid, time.time())
    return uid


def uid_from_request(req):
    auth = req.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return uid_from_token(auth[7:].strip())
    return ""


# ---------------------------------------------------------------- per-user library
def library_all(uid):
    if not uid:
        return []
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT payload FROM user_library WHERE user_id = ? ORDER BY saved_at DESC",
            (uid,)).fetchall()
    out = []
    for (pj,) in rows:
        try:
            out.append(json.loads(pj))
        except Exception:
            continue
    return out


def library_delete(uid, game_id):
    """Remove one entry (propagates a delete from a synced device)."""
    if not uid:
        raise ValueError("not signed in")
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM user_library WHERE user_id = ? AND game_id = ?",
                           (uid, str(game_id)))
        conn.commit()
    return {"ok": True, "removed": cur.rowcount > 0}


def library_replace(uid, entries):
    """Upsert each entry the client sent; entries it dropped stay (merge-safe,
    deletes propagate explicitly via library_delete)."""
    if not uid:
        raise ValueError("not signed in")
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute("BEGIN")
        for g in (entries or []):
            if not isinstance(g, dict) or not g.get("id"):
                continue
            payload = json.dumps(g, separators=(",", ":"))
            saved_at = float(g.get("savedAt") or 0) or now
            conn.execute(
                "INSERT INTO user_library(user_id, game_id, payload, saved_at) "
                "VALUES (?,?,?,?) ON CONFLICT(user_id, game_id) DO UPDATE SET "
                "payload = excluded.payload, saved_at = MAX(saved_at, excluded.saved_at)",
                (uid, str(g["id"]), payload, saved_at))
        conn.commit()
    return {"ok": True, "count": len(entries or [])}
