"""Flask app: serves the web UI and the review/engine JSON API."""
import os
import threading
import time
import traceback
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from server.uci import EngineManager
from server import review

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

app = Flask(__name__, static_folder=None)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # always revalidate assets (no stale JS/CSS)
manager = EngineManager()

from server import repstore  # noqa: E402
repstore.init_db()

from server import accounts  # noqa: E402
from server import db as _db  # noqa: E402
from server import mailer  # noqa: E402
from server import security  # noqa: E402
accounts.init_db()

# The app loads nothing from another origin (every fetch is same-origin), so a
# strict policy costs nothing and neutralises any HTML injection that slips
# through: no inline script, no framing, no form posts elsewhere.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")

# largest PGN we accept in one request (characters) - the engine work scales
# with the game, so an unbounded body is a denial-of-service lever
MAX_PGN_CHARS = 400_000
MAX_REVIEW_JOBS = 24


@app.before_request
def _reject_invalid_token():
    """A request that presents a token the server does not accept is refused
    outright.

    Without this, an expired or revoked token silently fell back to the
    shared guest pool, so signing out still showed data - and a stale token
    looked like it had worked. No Authorization header at all still means
    "guest", which is the public, account-less mode.
    """
    if not request.path.startswith("/api/"):
        return None
    auth = request.headers.get("Authorization", "")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    if not accounts.uid_from_token(auth[7:].strip()):
        security.log_event("invalid_token", ip=_client_ip())
        return jsonify({"error": "Session expired - please sign in again"}), 401
    return None


@app.after_request
def add_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy",
                            "geolocation=(), microphone=(), camera=(), interest-cohort=()")
    resp.headers.setdefault("Content-Security-Policy", CSP)
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    if request.path.startswith("/api/"):
        # authenticated responses must never be stored by a shared cache
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        resp.headers["Pragma"] = "no-cache"
    return resp


def _bearer(req):
    """Bearer token from the Authorization header ('' when signed out)."""
    auth = req.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _uid():
    """Account id for this request ('' = the shared guest pool)."""
    return accounts.uid_from_request(request)


def _client_ip():
    """Best-effort client address for rate limiting.

    X-Forwarded-For is only trusted when the app is behind a proxy that sets
    it (Render does). A client can spoof it, which only weakens the limiter
    - it never grants access - so it is combined with the account lockout.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.remote_addr or "unknown")[:64]


def _rate(key, limit, window):
    """A 429 response with a Retry-After header (never a bare bool)."""
    resp = jsonify({"error": "Too many requests - please slow down"})
    resp.status_code = 429
    retry = security.limiter.retry_after(key, window)
    if retry:
        resp.headers["Retry-After"] = str(retry)
    return resp


def _job_owner_ok(job, key):
    """A job result is private to whoever started it.

    The job carries a random 'key'; a signed-in caller must ALSO be the
    account that started it. Guests are protected by the key alone (the
    review data of a signed-out visitor is still their own PGN).
    """
    if not job:
        return False
    if not hmac_compare(job.get("key"), key or ""):
        return False
    owner = job.get("owner")
    if owner:
        return accounts.uid_from_request(request) == owner
    return True


def hmac_compare(a, b):
    import hmac
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


def _housekeeping_loop():
    """Drop expired sessions and reset tokens every 10 minutes."""
    while True:
        time.sleep(600)
        try:
            res = accounts.purge_expired()
            if res.get("sessions") or res.get("resets"):
                security.log_event("purged_expired", sessions=res.get("sessions"),
                                   resets=res.get("resets"))
        except Exception:
            pass


threading.Thread(target=_housekeeping_loop, daemon=True).start()


def _is_chesscom_archive(url):
    """Only chess.com's own HTTPS archive endpoints may be followed.

    The URL comes from an API response, not from the caller - but following a
    URL that a (compromised or spoofed) API hands us would turn the server
    into a request proxy, so the host is pinned.
    """
    try:
        from urllib.parse import urlsplit
        u = urlsplit(url or "")
        return (u.scheme == "https" and u.hostname == "api.chess.com"
                and u.path.startswith("/pub/archives"))
    except Exception:
        return False


# ---------------- accounts ----------------
@app.get("/api/auth/status")
def api_auth_status():
    """Remote (external) database configured? Tells the client where data lives."""
    return jsonify({"remote": bool(_db.REMOTE_ENABLED)})


@app.post("/api/auth/register")
def api_auth_register():
    ip = _client_ip()
    if not security.limiter.allow("register:" + ip, 10, 3600):
        security.log_event("register_rate_limited", ip=ip)
        return _rate("register:" + ip, 10, 3600)
    data = request.get_json(force=True, silent=True) or {}
    try:
        res = accounts.register(data.get("username") or "", data.get("password") or "",
                                data.get("email") or "")
        return jsonify(res)
    except accounts.AuthError as e:
        security.log_event("register_failed", ip=ip, reason=str(e)[:60])
        return jsonify({"error": e.message}), e.status
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"register failed: {e}"}), 500


@app.post("/api/auth/login")
def api_auth_login():
    ip = _client_ip()
    ident = (request.get_json(force=True, silent=True) or {}).get("identifier") or ""
    ident = str(ident)
    # per-client throttle + per-account lockout (in the accounts table)
    if not security.limiter.allow("login-ip:" + ip, 30, 300):
        security.log_event("login_rate_limited", ip=ip)
        return _rate("login-ip:" + ip, 30, 300)
    if ident:
        if not security.limiter.allow("login-user:" + ident.lower(), 10, 300):
            security.log_event("login_rate_limited", user=ident[:24], ip=ip)
            return _rate("login-user:" + ident.lower(), 10, 300)
    data = request.get_json(force=True, silent=True) or {}
    ident = data.get("identifier") or data.get("username") or data.get("email") or ""
    try:
        res = accounts.login(str(ident), data.get("password") or "", ip=ip,
                              user_agent=request.headers.get("User-Agent", ""))
        return jsonify(res)
    except accounts.AuthError as e:
        return jsonify({"error": e.message}), e.status
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "login failed"}), 500


@app.post("/api/auth/logout")
def api_auth_logout():
    accounts.logout(_bearer(request))
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def api_auth_me():
    token = _bearer(request)
    user = accounts.me(token)
    return jsonify({"user": user, "remote": bool(_db.REMOTE_ENABLED)})


@app.post("/api/auth/password")
def api_auth_password():
    """Change the password of the signed-in account (reauthentication)."""
    token = _bearer(request)
    uid = accounts.uid_from_token(token)
    if not uid:
        return jsonify({"error": "not signed in"}), 401
    ip = _client_ip()
    if not security.limiter.allow("pwchange:" + ip, 10, 3600):
        return _rate("pwchange:" + ip, 10, 3600)
    data = request.get_json(force=True, silent=True) or {}
    try:
        res = accounts.change_password(uid, data.get("currentPassword") or "",
                                       data.get("newPassword") or "", keep_token=token)
        return jsonify(res)
    except accounts.AuthError as e:
        return jsonify({"error": e.message}), e.status
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "password change failed"}), 500


@app.post("/api/auth/password-reset")
def api_auth_password_reset():
    """Start a password reset. Always the same answer, whatever the input."""
    ip = _client_ip()
    if not security.limiter.allow("pwreset:" + ip, 5, 3600):
        return _rate("pwreset:" + ip, 5, 3600)
    data = request.get_json(force=True, silent=True) or {}
    ident = str(data.get("identifier") or data.get("email") or data.get("username") or "")
    try:
        res = accounts.request_password_reset(ident)
    except Exception:
        traceback.print_exc()
        res = {"ok": True}
    token = res.get("token") or ""
    if token:
        # deliver the single-use link (only when mail is configured)
        row = None
        try:
            conn = accounts._connect()
            row = conn.execute("SELECT email FROM users WHERE id = ?",
                               (res.get("uid"),)).fetchone()
        except Exception:
            row = None
        email = (row[0] if row else "") or ""
        if email and mailer.configured():
            base = os.environ.get("PUBLIC_URL") or "https://chess-review-1.onrender.com"
            link = "%s/?reset=%s" % (base.rstrip("/"), token)
            mailer.send(email, "Reset your chess-review password",
                        "Use this link to choose a new password (valid 30 minutes):\n\n%s\n\n"
                        "If you did not ask for this, you can ignore this email." % link)
    return jsonify({"ok": True,
                    "message": "If that account exists, a reset link has been sent."})


@app.post("/api/auth/password-reset/confirm")
def api_auth_password_reset_confirm():
    ip = _client_ip()
    if not security.limiter.allow("pwresetc:" + ip, 10, 3600):
        return _rate("pwresetc:" + ip, 10, 3600)
    data = request.get_json(force=True, silent=True) or {}
    try:
        res = accounts.reset_password(str(data.get("token") or ""),
                                      data.get("newPassword") or "")
        return jsonify(res)
    except accounts.AuthError as e:
        return jsonify({"error": e.message}), e.status
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "reset failed"}), 500


# ---------------- per-account game library ----------------
@app.get("/api/account/library")
def api_account_library():
    uid = _uid()
    if not uid:
        return jsonify({"error": "not signed in"}), 401
    try:
        # ?meta=1 -> ids + timestamps only (the client then pulls what it lacks)
        if (request.args.get("meta") or "").strip() in ("1", "true", "yes"):
            return jsonify({"entries": accounts.library_meta(uid)})
        return jsonify({"entries": accounts.library_all(uid)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"library load failed: {e}"}), 500


@app.post("/api/account/library/pull")
def api_account_library_pull():
    """Fetch the full entries for specific ids (keeps the sync small)."""
    uid = _uid()
    if not uid:
        return jsonify({"error": "not signed in"}), 401
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get("ids")
    if not isinstance(ids, list):
        return jsonify({"error": "ids must be a list"}), 400
    try:
        return jsonify({"entries": accounts.library_get(uid, ids[:500])})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"library pull failed: {e}"}), 500


@app.post("/api/account/library")
def api_account_library_push():
    """Merge-safe upsert of the client's saved games."""
    uid = _uid()
    if not uid:
        return jsonify({"error": "not signed in"}), 401
    data = request.get_json(force=True, silent=True) or {}
    entries = data.get("entries")
    if not isinstance(entries, list):
        return jsonify({"error": "entries must be a list"}), 400
    if len(entries) > 200:
        entries = entries[:200]
    try:
        return jsonify(accounts.library_replace(uid, entries))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"library save failed: {e}"}), 500


@app.post("/api/account/library/delete")
def api_account_library_delete():
    """Propagate a deletion from a synced device."""
    uid = _uid()
    if not uid:
        return jsonify({"error": "not signed in"}), 401
    data = request.get_json(force=True, silent=True) or {}
    game_id = (data.get("id") or "").strip()
    if not game_id:
        return jsonify({"error": "missing id"}), 400
    try:
        return jsonify(accounts.library_delete(uid, game_id))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"library delete failed: {e}"}), 500


@app.get("/api/repertoire/list")
def api_rep_list():
    try:
        return jsonify({"repertoires": repstore.list_repertoires(_uid())})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.post("/api/repertoire/save")
def api_rep_save():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        return jsonify(repstore.save_repertoire(payload, _uid()))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.get("/api/repertoire/get/<rid>")
def api_rep_get(rid):
    try:
        return jsonify(repstore.get_repertoire(rid, _uid()))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 404


@app.post("/api/repertoire/delete")
def api_rep_delete():
    data = request.get_json(force=True, silent=True) or {}
    try:
        repstore.delete_repertoire(data.get("id") or "", _uid())
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 404


@app.post("/api/repertoire/rename")
def api_rep_rename():
    data = request.get_json(force=True, silent=True) or {}
    try:
        repstore.rename_repertoire(data.get("id") or "", data.get("name") or "", _uid())
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 404

# in-memory review jobs: id -> {"status", "progress", "stage", "result", "error", "startedAt"}
_jobs = {}
_jobs_lock = threading.Lock()


@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(WEB, filename)


@app.get("/api/engines")
def api_engines():
    return jsonify({"engines": manager.list()})


@app.post("/api/settings")
def api_settings():
    """Engine resources are shared by everyone, so the values are clamped to
    what one instance can afford and the endpoint is rate limited."""
    ip = _client_ip()
    if not security.limiter.allow("settings:" + ip, 20, 300):
        return _rate("settings:" + ip, 20, 300)
    data = request.get_json(force=True, silent=True) or {}
    threads = max(1, min(4, int(data.get("threads", 1))))
    hash_mb = max(16, min(512, int(data.get("hash", 256))))
    manager.set_defaults(threads, hash_mb)
    return jsonify({"ok": True, "threads": threads, "hash": hash_mb})


@app.post("/api/review")
def api_review():
    ip = _client_ip()
    if not security.limiter.allow("review:" + ip, 30, 300):
        return _rate("review:" + ip, 30, 300)
    data = request.get_json(force=True, silent=True) or {}
    pgn = (data.get("pgn") or "").strip()
    if not pgn:
        return jsonify({"error": "Missing PGN text."}), 400
    if len(pgn) > MAX_PGN_CHARS:
        return jsonify({"error": "That PGN is too large to analyse."}), 413
    engine_id = data.get("engine") or "stockfish"
    engine = manager.get(engine_id)
    depth = max(4, min(22, int(data.get("depth", 12))))
    multipv = max(1, min(5, int(data.get("multipv", 3))))
    movetime = max(200, min(20000, int(data.get("movetime", 900))))

    job_id = uuid.uuid4().hex[:12]
    job_key = security.random_token(24)
    owner = _uid()          # '' for a guest - the key is then the only guard
    with _jobs_lock:
        # keep only the most recent jobs
        for old in sorted(_jobs, key=lambda k: _jobs[k].get("startedAt", 0))[:-MAX_REVIEW_JOBS]:
            if _jobs[old].get("status") in ("done", "error"):
                _jobs.pop(old, None)
        _jobs[job_id] = {"status": "running", "stage": "starting", "progress": 0.0,
                         "startedAt": time.time(), "result": None, "error": None,
                         "key": job_key, "owner": owner,
                         "engine": getattr(engine, "name", engine_id)}

    def progress(msg):
        import re
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job:
                return
            job["stage"] = msg.split(" ")[0] if msg else msg
            m = re.match(r"(engine|book) (\d+)/(\d+)", msg or "")
            if m:
                frac = int(m.group(2)) / max(1, int(m.group(3)))
                job["progress"] = round(float(frac), 4)
                job["stageDetail"] = msg
            if msg == "classify":
                job["progress"] = 0.97

    def worker():
        try:
            t0 = time.time()
            result = review.run_review(pgn, engine, depth=depth, multipv=multipv,
                                       movetime=movetime,
                                       progress=lambda m: progress(m))
            result["analysis"]["seconds"] = round(time.time() - t0, 1)
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id].update(status="done", progress=1.0, stage="done", result=result)
        except Exception as e:
            traceback.print_exc()
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id].update(status="error", error=f"{type(e).__name__}: {e}")

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"jobId": job_id, "jobKey": job_key,
                    "engine": getattr(engine, "name", engine_id)})


@app.get("/api/review/status/<job_id>")
def api_review_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not _job_owner_ok(job, request.args.get("k")):
            return jsonify({"error": "unknown job"}), 404
        return jsonify({k: job.get(k) for k in ("status", "stage", "stageDetail", "progress", "error", "engine")})


@app.get("/api/review/result/<job_id>")
def api_review_result(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not _job_owner_ok(job, request.args.get("k")):
            return jsonify({"error": "unknown job"}), 404
        if job["status"] == "error":
            return jsonify({"error": job["error"]}), 500
        if job["status"] != "done":
            return jsonify({"error": "not finished", "stage": job["stage"], "progress": job["progress"]}), 202
        return jsonify(job["result"])


@app.post("/api/eval")
def api_eval():
    """Live single-position analysis for the engine panel."""
    data = request.get_json(force=True, silent=True) or {}
    fen = data.get("fen") or ""
    engine = manager.get(data.get("engine") or "stockfish")
    depth = max(4, min(22, int(data.get("depth", 16))))
    multipv = max(1, min(5, int(data.get("multipv", 3))))
    movetime = max(200, min(20000, int(data.get("movetime", 800))))
    try:
        import chess
        board = chess.Board(fen)
    except Exception:
        return jsonify({"error": "invalid FEN"}), 400
    if not any(board.legal_moves):
        return jsonify({"gameover": True})
    eff_depth = min(depth, 3) if engine.id == "builtin" else depth
    eff_mt = None if engine.id == "builtin" else movetime
    # optional engine-strength limit for "vs engine" play (UCI standard options;
    # the old server quietly ignores the unknown `elo` field, so the new client
    # stays compatible with it - it just plays full strength there)
    try:
        elo = int(data.get("elo") or 0)
    except (TypeError, ValueError):
        elo = 0
    options = None
    if engine.id != "builtin":
        if elo:
            elo = max(1320, min(3190, elo))
            options = {"UCI_LimitStrength": "true", "UCI_Elo": str(elo)}
        else:
            options = {"UCI_LimitStrength": "false"}   # un-limit after a limited game
    try:
        if options:
            lines = engine.analyse(board.fen(), depth=eff_depth, multipv=multipv,
                                   timeout=180, movetime=eff_mt, options=options)
        else:
            lines = engine.analyse(board.fen(), depth=eff_depth, multipv=multipv,
                                   timeout=180, movetime=eff_mt)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    white_to_move = board.turn == chess.WHITE
    out = []
    for ln in lines:
        cp_white = ln["cp"] if white_to_move else -ln["cp"]
        mate_white = (ln["mate"] or 0) * (1 if white_to_move else -1) if ln.get("mate") is not None else None
        out.append({
            "cpWhite": cp_white, "mateWhite": mate_white,
            "depth": depth,
            "pv": ln["pv"][:12],
            "san": review.uci_pv_to_san(board, ln["pv"], 12),
        })
    return jsonify({"lines": out, "engine": getattr(engine, "name", "?")})


@app.post("/api/chesscom/fetch")
def api_chesscom_fetch():
    """Fetch a user's recent games from chess.com's public API (monthly archives)."""
    ip = _client_ip()
    if not security.limiter.allow("ccfetch:" + ip, 20, 300):
        return _rate("ccfetch:" + ip, 20, 300)
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip().lstrip("@").lower()
    max_games = max(1, min(20, int(data.get("max", 8))))
    time_class = (data.get("timeClass") or "").strip().lower()
    if not username:
        return jsonify({"error": "Missing username."}), 400
    if not security.valid_external_username(username):
        return jsonify({"error": "That is not a valid chess.com username."}), 400
    import datetime as _dt

    import requests as rq
    headers = {"User-Agent": "chess-review-local-app/1.0 (local game review tool)"}
    try:
        arch = rq.get(f"https://api.chess.com/pub/player/{username}/games/archives",
                      headers=headers, timeout=30)
        arch.raise_for_status()
        games = []
        for url in reversed(arch.json().get("archives", [])):
            if len(games) >= max_games:
                break
            # only follow chess.com's own archive URLs - never an arbitrary host
            if not _is_chesscom_archive(url):
                continue
            month = rq.get(url, headers=headers, timeout=30)
            month.raise_for_status()
            for g in reversed(month.json().get("games", [])):
                pgn = g.get("pgn")
                if not pgn:
                    continue
                if time_class and g.get("time_class") != time_class:
                    continue
                w, b = g.get("white") or {}, g.get("black") or {}
                wres, bres = w.get("result"), b.get("result")
                result = "1-0" if wres == "win" else ("0-1" if bres == "win" else "1/2-1/2")
                end = g.get("end_time")
                date = _dt.datetime.fromtimestamp(end).strftime("%Y-%m-%d") if end else ""
                games.append({
                    "pgn": pgn,
                    "white": w.get("username", "?"),
                    "black": b.get("username", "?"),
                    "whiteRating": w.get("rating"),
                    "blackRating": b.get("rating"),
                    "result": result,
                    "timeClass": g.get("time_class", ""),
                    "date": date,
                })
                if len(games) >= max_games:
                    break
    except rq.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        msg = "User not found on chess.com." if code == 404 else f"chess.com API error (HTTP {code})"
        return jsonify({"error": msg}), 502
    except Exception as e:
        return jsonify({"error": f"chess.com fetch failed: {e}"}), 502
    if not games:
        return jsonify({"error": "No games found for that username / speed filter."}), 404
    return jsonify({"games": games})


@app.get("/api/enginestate")
def api_enginestate():
    """Live search state (depth/nodes/nps) per engine, for real-time UI."""
    return jsonify({"engines": manager.engine_state()})


@app.get("/api/explorer")
def api_explorer():
    """Lichess masters explorer for a position: continuations + w/d/l stats."""
    from server import openings
    fen = request.args.get("fen") or ""
    if not fen:
        return jsonify({"error": "missing fen"}), 400
    data = openings.query_position(fen)
    if data is None:
        return jsonify({"error": "explorer needs a free Lichess API token (see settings) or network access",
                        "needsToken": not openings.has_token()}), 502
    moves = data.get("moveList")
    if moves is None:  # legacy cache entries: rebuild from shares
        moves = [{"uci": u, "san": u, "games": int(round(s * 1000)), "share": s,
                  "white": None, "draws": None, "black": None}
                 for u, s in sorted(data.get("moves", {}).items(), key=lambda kv: -kv[1])][:12]
    return jsonify({"total": data.get("total", 0), "opening": data.get("opening"), "moves": moves})


@app.post("/api/lichess_token")
def api_lichess_token():
    """Store the shared Lichess API token (used by the opening explorer).

    The token is a server-wide resource, so it is only accepted in the
    exact format Lichess issues and writes are rate limited. The token
    itself is never returned by the API.
    """
    ip = _client_ip()
    if not security.limiter.allow("ltoken:" + ip, 10, 3600):
        return _rate("ltoken:" + ip, 10, 3600)
    from server import openings
    data = request.get_json(force=True, silent=True) or {}
    tok = (data.get("token") or "").strip()
    if tok:
        if len(tok) != 32 or any(c not in "0123456789abcdefABCDEF" for c in tok):
            return jsonify({"error": "That does not look like a Lichess API token"}), 400
        openings.set_token(tok)
        security.log_event("lichess_token_set", ip=ip)
    else:
        openings.set_token("")
        security.log_event("lichess_token_cleared", ip=ip)
    return jsonify({"ok": True, "set": openings.has_token()})


@app.get("/api/lichess_token")
def api_lichess_token_get():
    from server import openings
    return jsonify({"set": openings.has_token()})


@app.get("/api/puzzle")
def api_puzzle():
    """One fresh Lichess puzzle (id, rating, themes, solution, FEN)."""
    from server import puzzles
    try:
        return jsonify(puzzles.get_puzzle(request.args.get("theme") or ""))
    except Exception as e:
        return jsonify({"error": f"puzzle fetch failed: {e}"}), 502


# ---------------- trainer (spaced repetition) ----------------
@app.post("/api/trainer/extract")
def api_trainer_extract():
    """Import exercises from a finished review payload; idempotent dedupe."""
    from server import trainer
    data = request.get_json(force=True, silent=True) or {}
    res = data.get("review")
    if not isinstance(res, dict):
        return jsonify({"error": "missing review payload"}), 400
    if not isinstance(res.get("moves"), list) or not isinstance(res.get("positions"), list):
        return jsonify({"error": "review payload missing moves/positions"}), 400
    try:
        exercises = trainer.extract_from_review(res)
        counts = trainer.sync_exercises(exercises, _uid())
        return jsonify({"ok": True, "candidates": len(exercises), **counts})
    except Exception as e:
        return jsonify({"error": f"trainer extract failed: {e}"}), 500


@app.get("/api/trainer/dashboard")
def api_trainer_dashboard():
    """Aggregated stats: totals, due/new/mastered, streak, accuracy, weak spots."""
    from server import trainer
    try:
        return jsonify(trainer.dashboard(trainer._parse_filters(request.args), _uid()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"dashboard failed: {e}"}), 500


@app.get("/api/trainer/list")
def api_trainer_list():
    from server import trainer
    try:
        limit = max(1, min(500, int(request.args.get("limit") or 200)))
        return jsonify({"exercises": trainer.list_exercises(
            trainer._parse_filters(request.args), limit, _uid())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/trainer/queue")
def api_trainer_queue():
    """Next N exercises for a session (due first, then new, capped)."""
    from server import trainer
    try:
        limit = max(1, min(60, int(request.args.get("limit") or 8)))
        queue = trainer.next_queue(limit, trainer._parse_filters(request.args), True, _uid())
        return jsonify({"queue": queue})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/trainer/result")
def api_trainer_result():
    """Grade an attempt (Again=0/Hard=2/Good=4/Easy=5) and update the schedule."""
    from server import trainer
    data = request.get_json(force=True, silent=True) or {}
    ex_id = (data.get("exerciseId") or data.get("id") or "").strip()
    if not ex_id:
        return jsonify({"error": "missing exerciseId"}), 400
    try:
        quality = int(data.get("quality", 0))
        ms = max(0, int(data.get("ms", 0) or 0))
        return jsonify(trainer.record_attempt(
            ex_id, quality, data.get("playedUci") or "", ms, _uid()))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"record failed: {e}"}), 500


@app.get("/api/trainer/history")
def api_trainer_history():
    from server import trainer
    try:
        limit = max(1, min(200, int(request.args.get("limit") or 50)))
        return jsonify({"history": trainer.attempt_history(limit, _uid())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/trainer/sources")
def api_trainer_sources():
    from server import trainer
    return jsonify({"sources": trainer.sources_list(_uid())})


@app.post("/api/trainer/detach")
def api_trainer_detach():
    """Drop all exercises exclusively sourced from a deleted library game."""
    from server import trainer
    data = request.get_json(force=True, silent=True) or {}
    source_id = (data.get("sourceId") or "").strip()
    if not source_id:
        return jsonify({"error": "missing sourceId"}), 400
    try:
        return jsonify(trainer.detach_source(source_id, _uid()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------- performance lab ----------------
@app.post("/api/perf/import_pgn")
def api_perf_import_pgn():
    """Bulk import a PGN blob (single or multi-game). Dedups content-hashed games."""
    from server import perfdb
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("pgn") or "").strip()
    if not text:
        return jsonify({"error": "missing pgn"}), 400
    if len(text) > 20_000_000:
        return jsonify({"error": "file too large (20 MB limit)"}), 400
    try:
        parsed = perfdb.parse_pgn_text(text)
        counts = perfdb.import_games(parsed, source="import", user_id=_uid())
        return jsonify({"ok": True, "parsed": len(parsed), **counts})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"import failed: {e}"}), 500


@app.post("/api/perf/import_review")
def api_perf_import_review():
    """Attach (or overwrite) a review for a game."""
    from server import perfdb
    data = request.get_json(force=True, silent=True) or {}
    try:
        res = perfdb.attach_review(data.get("review"), _uid())
        return jsonify(res)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"review import failed: {e}"}), 500


@app.get("/api/perf/overall")
def api_perf_overall():
    from server import perfdb
    return jsonify(perfdb.overall(_uid()))


@app.get("/api/perf/openings")
def api_perf_openings():
    from server import perfdb
    return jsonify(perfdb.openings_report(_uid()))


@app.get("/api/perf/weaknesses")
def api_perf_weaknesses():
    from server import perfdb
    try:
        return jsonify(perfdb.weaknesses(_uid()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.get("/api/perf/recommendations")
def api_perf_recommendations():
    from server import perfdb
    try:
        return jsonify(perfdb.recommendations(_uid()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.get("/api/perf/timeline")
def api_perf_timeline():
    from server import perfdb
    return jsonify(perfdb.timeline(_uid()))


@app.get("/api/perf/games")
def api_perf_games():
    from server import perfdb
    limit = max(1, min(500, int(request.args.get("limit") or 100)))
    uid = _uid()
    return jsonify({"games": perfdb.list_games(limit, uid), "total": perfdb.games_total(uid)})


@app.get("/api/perf/metrics")
def api_perf_metrics():
    """Behavior metrics for one side (aimchess-style stats), ?color=white|black."""
    from server import perfdb
    color = (request.args.get("color") or "white").strip().lower()
    if color not in ("white", "black"):
        color = "white"
    return jsonify(perfdb.metrics(color, _uid()))


@app.get("/api/perf/series")
def api_perf_series():
    """Per-game skill series for the Lab tiles, ?color=white|black."""
    from server import perfdb
    color = (request.args.get("color") or "white").strip().lower()
    if color not in ("white", "black"):
        color = "white"
    return jsonify(perfdb.skill_series(color, _uid()))


@app.post("/api/perf/games/delete")
def api_perf_games_delete():
    from server import perfdb
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify({"ok": perfdb.delete_game((data.get("id") or "").strip(), _uid())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/perf/aliases")
def api_perf_aliases():
    from server import perfdb
    data = request.get_json(force=True, silent=True) or {}
    a, c = (data.get("alias") or "").strip(), (data.get("canonical") or "").strip()
    if not a or not c:
        return jsonify({"error": "alias and canonical required"}), 400
    uid = _uid()
    perfdb.add_alias(a, c, uid)
    perfdb.normalize_aliases(uid)
    return jsonify({"ok": True})


@app.get("/api/perf/aliases")
def api_perf_aliases_get():
    from server import perfdb
    return jsonify({"aliases": perfdb.list_aliases(_uid())})


_rjobs = {}
_rjobs_lock = threading.Lock()


@app.post("/api/repertoire/build")
def api_rep_build():
    import chess as chess_mod
    from server.repertoire import build_repertoire, MODES
    data = request.get_json(force=True, silent=True) or {}
    fen = data.get("fen") or chess_mod.STARTING_FEN
    side = data.get("side") if data.get("side") in ("white", "black") else "white"
    mode = data.get("mode") if data.get("mode") in MODES else "mainline"
    try:
        chess_mod.Board(fen)
    except Exception:
        return jsonify({"error": "invalid FEN"}), 400
    job_id = uuid.uuid4().hex[:12]
    job_key = security.random_token(24)
    owner = _uid()
    use_engine = bool(data.get("useEngine")) or mode == "strongest"
    with _rjobs_lock:
        for old in sorted(_rjobs, key=lambda k: _rjobs[k].get("startedAt", 0))[:-MAX_REVIEW_JOBS]:
            if _rjobs[old].get("status") in ("done", "error"):
                _rjobs.pop(old, None)
        _rjobs[job_id] = {"status": "running", "stage": "starting", "result": None, "error": None,
                          "startedAt": time.time(), "key": job_key, "owner": owner}

    def progress(stage):
        with _rjobs_lock:
            job = _rjobs.get(job_id)
            if job:
                job["stage"] = stage

    def worker():
        try:
            engine = manager.get("stockfish") if use_engine else None
            result = build_repertoire(fen, side, mode,
                                      your_moves=data.get("yourMoves", 5),
                                      breadth=data.get("breadth", 2),
                                      use_engine=use_engine,
                                      engine=engine, progress=progress)
            with _rjobs_lock:
                if job_id in _rjobs:
                    _rjobs[job_id].update(status="done", stage="done", result=result)
        except Exception as e:
            traceback.print_exc()
            with _rjobs_lock:
                if job_id in _rjobs:
                    _rjobs[job_id].update(status="error", error=f"{type(e).__name__}: {e}")

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"jobId": job_id, "jobKey": job_key})


@app.get("/api/repertoire/status/<job_id>")
def api_rep_status(job_id):
    with _rjobs_lock:
        job = _rjobs.get(job_id)
        if not job or not _job_owner_ok(job, request.args.get("k")):
            return jsonify({"error": "unknown job"}), 404
        return jsonify({k: job.get(k) for k in ("status", "stage", "error")})


@app.get("/api/repertoire/result/<job_id>")
def api_rep_result(job_id):
    with _rjobs_lock:
        job = _rjobs.get(job_id)
        if not job or not _job_owner_ok(job, request.args.get("k")):
            return jsonify({"error": "unknown job"}), 404
        if job["status"] == "error":
            return jsonify({"error": job["error"]}), 500
        if job["status"] != "done":
            return jsonify({"stage": job["stage"]}), 202
        return jsonify(job["result"])


@app.get("/api/samples")
def api_samples():
    samples = []
    sdir = ROOT / "samples"
    if sdir.exists():
        for p in sorted(sdir.glob("*.pgn")):
            try:
                samples.append({"id": p.stem, "name": p.stem.replace("_", " "), "file": p.name})
            except Exception:
                pass
    return jsonify({"samples": samples})


@app.get("/api/samples/<name>")
def api_sample(name):
    safe = Path(name).name
    p = ROOT / "samples" / (safe if safe.endswith(".pgn") else safe + ".pgn")
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    return jsonify({"pgn": p.read_text(encoding="utf-8", errors="replace")})


@app.post("/api/lichess/fetch")
def api_lichess_fetch():
    """Fetch a user's latest game PGN from Lichess."""
    ip = _client_ip()
    if not security.limiter.allow("lcfetch:" + ip, 20, 300):
        return _rate("lcfetch:" + ip, 20, 300)
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    max_games = max(1, min(10, int(data.get("max", 5))))
    speeds = data.get("speeds") or []
    if not username:
        return jsonify({"error": "Missing username."}), 400
    if not security.valid_external_username(username):
        return jsonify({"error": "That is not a valid Lichess username."}), 400
    if not isinstance(speeds, list):
        speeds = []
    speeds = [str(s) for s in speeds if str(s) in ("bullet", "blitz", "rapid", "classical")][:4]
    import requests as rq
    try:
        params = {"max": max_games, "moves": "false", "tags": "true", "clocks": "true", "evals": "false"}
        if speeds:
            params["perf"] = ",".join(speeds)
        r = rq.get(f"https://lichess.org/api/games/user/{username}",
                   params=params, timeout=30,
                   headers={"Accept": "application/x-chess-pgn",
                            "User-Agent": "chess-review-local/1.0"})
        r.raise_for_status()
        pgn_text = r.text
        games = [g for g in pgn_text.split("\n\n[") if g.strip()]
        return jsonify({"pgn": pgn_text, "count": pgn_text.count("[Event ")})
    except Exception as e:
        return jsonify({"error": f"Lichess fetch failed: {e}"}), 502


@app.post("/api/move")
def api_move():
    """Apply a move to a FEN, returning the new position (for explore mode)."""
    import chess
    data = request.get_json(force=True, silent=True) or {}
    try:
        board = chess.Board(data.get("fen") or "")
        mv = chess.Move.from_uci(data.get("uci") or "")
    except Exception:
        return jsonify({"error": "invalid input"}), 400
    if mv not in board.legal_moves:
        return jsonify({"error": "illegal move"}), 400
    san = board.san(mv)
    board.push(mv)
    return jsonify({
        "fen": board.fen(),
        "san": san,
        "check": board.is_check(),
        "checkmate": board.is_checkmate(),
        "stalemate": board.is_stalemate(),
        "draw": board.is_insufficient_material() or board.can_claim_fifty_moves(),
    })


@app.post("/api/move/chain")
def api_move_chain():
    """Apply several moves at once, returning every intermediate FEN (repertoire preview)."""
    import chess
    data = request.get_json(force=True, silent=True) or {}
    ucis = data.get("ucis")
    if not isinstance(ucis, list) or not ucis or len(ucis) > 40:
        return jsonify({"error": "ucis must be a list of 1..40 moves"}), 400
    try:
        board = chess.Board(data.get("fen") or "")
    except Exception:
        return jsonify({"error": "invalid input"}), 400
    fens = [board.fen()]
    try:
        for uci in ucis:
            mv = chess.Move.from_uci(uci or "")
            if mv not in board.legal_moves:
                raise ValueError("illegal move")
            board.push(mv)
            fens.append(board.fen())
    except Exception:
        return jsonify({"error": "invalid input"}), 400
    return jsonify({"fens": fens})


@app.get("/api/legal")
def api_legal():
    """Legal moves (uci + san) for a FEN — used by the board UI for click-to-move."""
    import chess
    fen = request.args.get("fen") or ""
    try:
        board = chess.Board(fen)
    except Exception:
        return jsonify({"error": "invalid FEN"}), 400
    moves = []
    for mv in board.legal_moves:
        moves.append({"uci": mv.uci(), "san": board.san(mv)})
    return jsonify({
        "moves": moves,
        "turn": "white" if board.turn == chess.WHITE else "black",
        "check": board.is_check(),
        "checkmate": board.is_checkmate(),
        "stalemate": board.is_stalemate(),
    })


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(WEB, "index.html")


def create_app():
    return app
