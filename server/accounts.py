"""Accounts: register/login sessions + per-user progress library.

Users, sessions and each account's saved-game library live in the same backend
the other stores use (local SQLite file by default, Turso/libSQL when
TURSO_DATABASE_URL is set - see server/db.py).

Security model
--------------
* Passwords are hashed with Argon2id (or PBKDF2-HMAC-SHA256 when argon2-cffi
  is not installed). The algorithm is recorded inside the stored hash, so old
  hashes keep working and are upgraded on the next successful login.
* Session tokens are opaque random strings. Only their SHA-256 hash is stored,
  so a leak of the database does not hand out live sessions.
* Sessions expire (absolute + idle), can be revoked one at a time or all at
  once, and are revoked when the password changes.
* Login answers are generic: a caller cannot tell an unknown account from a
  wrong password, and a failed attempt is rate limited per account and per
  client address.
* Password resets use single-use, expiring tokens that are stored hashed.
"""
import json
import re
import secrets
import threading
import time
from pathlib import Path

try:
    from server import db
    from server import security
except ImportError:
    import db  # type: ignore
    import security  # type: ignore

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "accounts.sqlite"
_lock = threading.RLock()
_conn = None

# session lifetimes
SESSION_ABSOLUTE_TTL = 30 * 24 * 3600      # 30 days
SESSION_IDLE_TTL = 7 * 24 * 3600          # 7 days of inactivity
RESET_TTL = 30 * 60                       # 30 minutes
MAX_FAILED_LOGINS = 5
LOCKOUT_STEP = 120                        # seconds, doubles per extra failure
LOCKOUT_MAX = 3600                        # never longer than an hour

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,24}$")

# in-process session cache: token hash -> (user_id, cached_at)
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
            created_at REAL NOT NULL,
            email TEXT UNIQUE COLLATE NOCASE,
            failed_logins INTEGER NOT NULL DEFAULT 0,
            locked_until REAL NOT NULL DEFAULT 0,
            password_changed_at REAL NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_seen REAL NOT NULL,
            expires_at REAL NOT NULL,
            ip TEXT DEFAULT '',
            user_agent TEXT DEFAULT '')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS user_library (
            user_id TEXT NOT NULL,
            game_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            saved_at REAL NOT NULL,
            PRIMARY KEY (user_id, game_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS password_resets (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            used INTEGER NOT NULL DEFAULT 0)""")
        # tolerant migration for databases created before accounts hardening
        for tbl, col, decl in (("users", "email", "TEXT"),
                               ("users", "failed_logins", "INTEGER NOT NULL DEFAULT 0"),
                               ("users", "locked_until", "REAL NOT NULL DEFAULT 0"),
                               ("users", "password_changed_at", "REAL NOT NULL DEFAULT 0"),
                               ("sessions", "last_seen", "REAL NOT NULL DEFAULT 0"),
                               ("sessions", "expires_at", "REAL NOT NULL DEFAULT 0"),
                               ("sessions", "ip", "TEXT DEFAULT ''"),
                               ("sessions", "user_agent", "TEXT DEFAULT ''")):
            cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % tbl)}
            if col not in cols:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (tbl, col, decl))
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
def _hash_pw(pw):
    return security.hash_password(pw)


def _verify_pw(pw, stored):
    return security.verify_password(pw, stored)


# ---------------------------------------------------------------- auth
class AuthError(Exception):
    """An authentication problem with a safe, generic message."""

    def __init__(self, message, status=400, retry_after=0):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after


def register(username, password, email=None):
    """Create an account. Raises AuthError with a safe message on failure."""
    username = (username or "").strip()
    if not security.valid_username(username):
        raise AuthError("Username must be 3-24 characters (letters, digits, _ . -)")
    problem = security.password_problem(password)
    if problem:
        raise AuthError(problem)
    email_norm = ""
    if email:
        email_norm = security.normalize_email(email)
        if not email_norm:
            raise AuthError("That email address does not look valid")
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE",
                           (username,)).fetchone()
        if row:
            raise AuthError("That username is already taken")
        if email_norm:
            row = conn.execute("SELECT id FROM users WHERE email = ? COLLATE NOCASE",
                               (email_norm,)).fetchone()
            if row:
                # do not reveal that the address is registered
                raise AuthError("That account could not be created")
        uid = secrets.token_hex(8)
        now = time.time()
        conn.execute(
            "INSERT INTO users(id, username, pw_hash, created_at, email,"
            " failed_logins, locked_until, password_changed_at) VALUES (?,?,?,?,?,0,0,?)",
            (uid, username, _hash_pw(password), now, email_norm or None, now))
        conn.commit()
    security.log_event("register", user=username, uid=uid, has_email=bool(email_norm))
    return {"token": _new_session(uid), "user": {"id": uid, "username": username,
                                                 "createdAt": now}}


def login(identifier, password, ip="", user_agent=""):
    """Verify credentials. Every failure looks the same to the caller."""
    identifier = (identifier or "").strip()
    generic = AuthError("Wrong username or password", 401)
    if not identifier or not password:
        raise generic
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT id, username, pw_hash, email, failed_logins, locked_until"
            " FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE",
            (identifier, identifier)).fetchone()
        if not row:
            # spend the same time as a real verification (no user enumeration)
            security.verify_password(password, "pbkdf2$1$00$00")
            security.log_event("login_failed", reason="unknown_user", ip=ip)
            raise generic
        uid, username, pw_hash, email, failed, locked_until = row
        now = time.time()
        if locked_until and now < locked_until:
            security.log_event("login_locked", user=username, ip=ip)
            raise AuthError("Too many failed attempts - try again later", 429,
                            retry_after=int(locked_until - now))
        if security.verify_password(password, pw_hash):
            conn.execute("UPDATE users SET failed_logins = 0, locked_until = 0 WHERE id = ?",
                         (uid,))
            conn.commit()
            if security.needs_rehash(pw_hash):
                try:
                    conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?",
                                 (_hash_pw(password), uid))
                    conn.commit()
                    security.log_event("password_upgraded", user=username)
                except Exception:
                    pass
            security.log_event("login_ok", user=username, uid=uid, ip=ip)
            return {"token": _new_session(uid, ip, user_agent),
                    "user": {"id": uid, "username": username, "email": email or "",
                             "createdAt": now}}
        # wrong password: count it and apply a growing lockout
        failed = int(failed or 0) + 1
        lock = 0
        if failed >= MAX_FAILED_LOGINS:
            lock = min(LOCKOUT_MAX, LOCKOUT_STEP * (2 ** (failed - MAX_FAILED_LOGINS)))
            lock = int(lock)
        conn.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                     (failed, now + lock if lock else 0, uid))
        conn.commit()
        security.log_event("login_failed", reason="bad_password", user=username,
                           ip=ip, failures=failed, lockout=lock)
        raise generic


def _new_session(uid, ip="", user_agent=""):
    token = "c" + secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at, last_seen, expires_at, ip, user_agent)"
            " VALUES (?,?,?,?,?,?,?)",
            (security.token_hash(token), uid, now, now, now + SESSION_ABSOLUTE_TTL,
             (ip or "")[:64], (user_agent or "")[:200]))
        conn.commit()
    _sess_cache[security.token_hash(token)] = (uid, now, now + SESSION_ABSOLUTE_TTL, now)
    return token


def logout(token):
    """Revoke one session."""
    if not token:
        return {"ok": True}
    th = security.token_hash(token)
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM sessions WHERE token = ?", (th,))
        conn.commit()
    _sess_cache.pop(th, None)
    security.log_event("logout")
    return {"ok": True}


def logout_all(uid):
    """Revoke every session of an account (password change / reset)."""
    if not uid:
        return {"ok": True}
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        conn.commit()
    _sess_cache.clear()
    security.log_event("logout_all", uid=uid)
    return {"ok": True}


def me(token):
    uid = uid_from_token(token)
    if not uid:
        return None
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT id, username, email, created_at FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "email": row[2] or "", "createdAt": row[3]}


def uid_from_token(token):
    """Bearer-token lookup with expiry handling and a tiny in-process cache.

    The cache stores the expiry and the last-seen time as well, so a cached
    entry can never outlive the session it represents - a revoked or expired
    session stops working at once, not after the cache window.
    """
    if not token:
        return ""
    th = security.token_hash(token)
    hit = _sess_cache.get(th)
    now = time.time()
    if hit and now - hit[1] < _SESS_TTL:
        uid, cached_at, expires_at, last_seen = hit
        if expires_at and now > expires_at:
            _expire(th)
            return ""
        if last_seen and now - last_seen > SESSION_IDLE_TTL:
            _expire(th)
            security.log_event("session_idle_expired", uid=uid)
            return ""
        return uid
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT user_id, last_seen, expires_at FROM sessions WHERE token = ?", (th,)).fetchone()
    if not row:
        _sess_cache.pop(th, None)
        return ""
    uid, last_seen, expires_at = row
    if expires_at and now > expires_at:
        _expire(th)
        security.log_event("session_expired", uid=uid)
        return ""
    if last_seen and now - last_seen > SESSION_IDLE_TTL:
        _expire(th)
        security.log_event("session_idle_expired", uid=uid)
        return ""
    try:
        with _lock:
            conn = _connect()
            conn.execute("UPDATE sessions SET last_seen = ? WHERE token = ?", (now, th))
            conn.commit()
    except Exception:
        pass
    _sess_cache[th] = (uid, now, expires_at, now)
    return uid


def _expire(token_hash):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM sessions WHERE token = ?", (token_hash,))
        conn.commit()
    _sess_cache.pop(token_hash, None)


def uid_from_request(req):
    auth = req.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return uid_from_token(auth[7:].strip())
    return ""


# ---------------------------------------------------------------- passwords
def change_password(uid, current_password, new_password, keep_token=None):
    """Change the password of an account; revoke every other session."""
    if not uid:
        raise AuthError("not signed in", 401)
    problem = security.password_problem(new_password)
    if problem:
        raise AuthError(problem)
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT pw_hash FROM users WHERE id = ?", (uid,)).fetchone()
        if not row:
            raise AuthError("not signed in", 401)
        if not security.verify_password(current_password or "", row[0]):
            security.log_event("password_change_failed", uid=uid)
            raise AuthError("Current password is incorrect", 403)
        now = time.time()
        conn.execute("UPDATE users SET pw_hash = ?, password_changed_at = ?,"
                     " failed_logins = 0, locked_until = 0 WHERE id = ?",
                     (_hash_pw(new_password), now, uid))
        conn.commit()
        # every other session is invalidated; the current one survives
        keep_hash = security.token_hash(keep_token) if keep_token else None
        if keep_hash:
            conn.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?", (uid, keep_hash))
        else:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        conn.commit()
    _sess_cache.clear()
    security.log_event("password_changed", uid=uid)
    return {"ok": True}


def request_password_reset(identifier):
    """Start a password reset.

    Always answers the same way, so it cannot be used to find out which
    accounts exist. The reset token is single-use, expires, and only its
    hash is stored. The caller is responsible for delivering it.
    """
    identifier = (identifier or "").strip()
    token = ""
    uid = ""
    if identifier:
        with _lock:
            conn = _connect()
            row = conn.execute(
                "SELECT id, email FROM users WHERE username = ? COLLATE NOCASE"
                " OR email = ? COLLATE NOCASE", (identifier, identifier)).fetchone()
        if row:
            uid, email = row[0], row[1] or ""
            if email:
                token = security.random_token(32)
                now = time.time()
                with _lock:
                    conn = _connect()
                    conn.execute(
                        "INSERT INTO password_resets(token, user_id, created_at, expires_at, used)"
                        " VALUES (?,?,?,?,0)",
                        (security.token_hash(token), uid, now, now + RESET_TTL))
                    conn.commit()
                security.log_event("password_reset_requested", uid=uid)
    return {"ok": True, "token": token, "uid": uid}


def reset_password(token, new_password):
    """Consume a reset token and set a new password. Revokes all sessions."""
    problem = security.password_problem(new_password)
    if problem:
        raise AuthError(problem)
    th = security.token_hash(token or "")
    now = time.time()
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT user_id, expires_at, used FROM password_resets WHERE token = ?",
            (th,)).fetchone()
        if not row or row[2] or now > row[1]:
            raise AuthError("This reset link is invalid or has expired", 400)
        uid = row[0]
        conn.execute("UPDATE users SET pw_hash = ?, password_changed_at = ?,"
                     " failed_logins = 0, locked_until = 0 WHERE id = ?",
                     (_hash_pw(new_password), now, uid))
        conn.execute("UPDATE password_resets SET used = 1 WHERE token = ?", (th,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM password_resets WHERE user_id = ? AND token != ?", (uid, th))
        conn.commit()
    _sess_cache.clear()
    security.log_event("password_reset_completed", uid=uid)
    return {"ok": True}


def purge_expired():
    """Housekeeping: drop expired sessions and reset tokens."""
    now = time.time()
    with _lock:
        conn = _connect()
        s = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,)).rowcount
        r = conn.execute("DELETE FROM password_resets WHERE expires_at < ? OR used = 1",
                         (now,)).rowcount
        conn.commit()
    return {"sessions": s, "resets": r}


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


def library_meta(uid):
    """Just the ids and timestamps - the full reviews are large, so a client
    that already has most games only needs to know what changed."""
    if not uid:
        return []
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT game_id, saved_at FROM user_library WHERE user_id = ? ORDER BY saved_at DESC",
            (uid,)).fetchall()
    return [{"id": r[0], "savedAt": r[1]} for r in rows]


def library_get(uid, ids):
    """The full entries for the given game ids only."""
    if not uid or not ids:
        return []
    ids = [str(i) for i in ids if i][:500]
    if not ids:
        return []
    placeholders = ",".join(["?"] * len(ids))
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT payload FROM user_library WHERE user_id = ? AND game_id IN (" + placeholders + ")",
            [uid] + ids).fetchall()
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
