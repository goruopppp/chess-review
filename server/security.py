"""Security helpers: password hashing, rate limiting, response hardening.

Password hashing
----------------
New passwords use Argon2id (OWASP baseline: m=19 MiB, t=2, p=1) when the
`argon2-cffi` package is installed; PBKDF2-HMAC-SHA256 (600k rounds) is the
fallback. The algorithm is recorded in the stored hash, so every hash can
always be verified by the code that created it - upgrading the algorithm
never locks an existing user out, and a successful PBKDF2 login is silently
re-hashed with Argon2id.

Rate limiting
-------------
A small in-process sliding window keyed by action + client identity. It is
deliberately simple: the app runs on a single instance, and the durable
brute-force protection for a single account lives in the accounts table
(failed logins + lockout), not here.
"""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time

# ---------------------------------------------------------------- passwords
try:                                    # preferred
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
    _ARGON2 = True
except Exception:                       # pragma: no cover - depends on the env
    PasswordHasher = None
    InvalidHashError = VerificationError = VerifyMismatchError = Exception
    _ARGON2 = False

try:
    import bcrypt
    _BCRYPT = True
except Exception:                       # pragma: no cover
    bcrypt = None
    _BCRYPT = False

PBKDF2_ITERS = 600_000
PBKDF2_PREFIX = "pbkdf2$"
ARGON2_PREFIX = "argon2id$"
BCRYPT_PREFIX = "bcrypt$"

# Argon2id parameters (OWASP Password Storage Cheat Sheet baseline)
ARGON2_TIME_COST = 2
ARGON2_MEMORY_COST = 19456      # 19 MiB
ARGON2_PARALLELISM = 1

_hasher = None
if _ARGON2:
    _hasher = PasswordHasher(time_cost=ARGON2_TIME_COST,
                             memory_cost=ARGON2_MEMORY_COST,
                             parallelism=ARGON2_PARALLELISM)


def hashing_backend():
    """Which algorithm new passwords get ('argon2id' | 'bcrypt' | 'pbkdf2')."""
    if _hasher is not None:
        return "argon2id"
    if _BCRYPT:
        return "bcrypt"
    return "pbkdf2"


def hash_password(password):
    """Hash a password with the strongest algorithm available."""
    pw = (password or "").encode("utf-8")
    if _hasher is not None:
        try:
            out = _hasher.hash(pw)
            if isinstance(out, bytes):      # argon2-cffi returns str
                out = out.decode("ascii")
            return ARGON2_PREFIX + out
        except Exception:
            pass
    if _BCRYPT:
        try:
            return BCRYPT_PREFIX + bcrypt.hashpw(pw, bcrypt.gensalt()).decode("ascii")
        except Exception:
            pass
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw, bytes.fromhex(salt), PBKDF2_ITERS)
    return "%s%d$%s$%s" % (PBKDF2_PREFIX, PBKDF2_ITERS, salt, dk.hex())


def verify_password(password, stored):
    """Constant-time verification. Returns True/False, never raises."""
    if not stored or not isinstance(stored, str):
        return False
    pw = (password or "").encode("utf-8")
    try:
        if stored.startswith(ARGON2_PREFIX):
            if _hasher is None:
                return False
            try:
                return bool(_hasher.verify(stored[len(ARGON2_PREFIX):], pw))
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                return False
            except Exception:
                return False
        if stored.startswith(BCRYPT_PREFIX):
            if not _BCRYPT:
                return False
            try:
                return bool(bcrypt.checkpw(pw, stored[len(BCRYPT_PREFIX):].encode("ascii")))
            except Exception:
                return False
        if stored.startswith(PBKDF2_PREFIX):
            parts = stored.split("$")
            if len(parts) != 4:
                return False
            _, iters, salt, hexd = parts
            dk = hashlib.pbkdf2_hmac("sha256", pw, bytes.fromhex(salt), int(iters))
            return hmac.compare_digest(dk.hex(), hexd)
    except Exception:
        return False
    return False


def needs_rehash(stored):
    """True when a stored hash should be upgraded to the current algorithm
    (or to the current parameters)."""
    backend = hashing_backend()
    if backend == "argon2id":
        return not (isinstance(stored, str) and stored.startswith(ARGON2_PREFIX))
    if backend == "bcrypt":
        return not (isinstance(stored, str) and stored.startswith(BCRYPT_PREFIX))
    if not (isinstance(stored, str) and stored.startswith(PBKDF2_PREFIX)):
        return True
    # a PBKDF2 hash made with fewer rounds than we use now is worth upgrading
    try:
        return int(stored.split("$")[1]) < PBKDF2_ITERS
    except Exception:
        return True


# ---------------------------------------------------------------- tokens
def random_token(nbytes=32):
    return secrets.token_urlsafe(nbytes)


def token_hash(token):
    """Store only a hash of a reset/session token, never the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- validation
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,24}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}$")
# external account names we forward to chess.com / lichess
EXTERNAL_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def valid_username(name):
    return bool(USERNAME_RE.match(name or ""))


def normalize_email(email):
    """Lower-case + trim; returns '' for anything that is not an email."""
    e = (email or "").strip().lower()
    if len(e) > 254 or not EMAIL_RE.match(e):
        return ""
    return e


def valid_external_username(name):
    """chess.com / lichess handles - strict, so they cannot be used to
    smuggle a path or a URL into an outbound request."""
    return bool(EXTERNAL_USER_RE.match(name or ""))


def password_problem(password):
    """Returns a human-readable problem with the password, or '' if fine."""
    pw = password or ""
    if len(pw) < 10:
        return "Password must be at least 10 characters"
    if len(pw) > 200:
        return "Password must be at most 200 characters"
    if pw.lower() in ("password123", "1234567890", "qwertyuiop", "letmein123"):
        return "That password is too common"
    if pw.isdigit() or pw.isalpha():
        return "Use a mix of letters, numbers and symbols"
    return ""


# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """In-process sliding-window limiter: allow(key, limit, window_seconds)."""

    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    def _prune(self, now):
        for k in list(self._hits):
            hits = [t for t in self._hits[k] if now - t < 3600]
            if hits:
                self._hits[k] = hits
            else:
                del self._hits[k]

    def allow(self, key, limit, window):
        """True when the call is allowed; False when the caller is over the limit."""
        now = time.time()
        with self._lock:
            self._prune(now)
            hits = [t for t in self._hits.get(key, []) if now - t < window]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True

    def retry_after(self, key, window):
        """Seconds until the oldest hit falls out of the window."""
        now = time.time()
        with self._lock:
            hits = sorted(t for t in self._hits.get(key, []) if now - t < window)
            if not hits:
                return 0
            return max(0, int(window - (now - hits[0])))

    def clear(self, key):
        with self._lock:
            self._hits.pop(key, None)


limiter = RateLimiter()


# ---------------------------------------------------------------- logging
def log_event(kind, **fields):
    """Security event log. Never receives passwords, tokens or PGN text."""
    parts = ["[security] %s" % kind]
    for k, v in fields.items():
        if v is None:
            continue
        parts.append("%s=%s" % (k, v))
    print(" ".join(parts), flush=True)
