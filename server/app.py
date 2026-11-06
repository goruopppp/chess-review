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


@app.get("/api/repertoire/list")
def api_rep_list():
    try:
        return jsonify({"repertoires": repstore.list_repertoires()})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.post("/api/repertoire/save")
def api_rep_save():
    try:
        return jsonify(repstore.save_repertoire(request.get_json(force=True, silent=True) or {}))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.get("/api/repertoire/get/<rid>")
def api_rep_get(rid):
    try:
        return jsonify(repstore.get_repertoire(rid))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 404


@app.post("/api/repertoire/delete")
def api_rep_delete():
    data = request.get_json(force=True, silent=True) or {}
    try:
        repstore.delete_repertoire(data.get("id") or "")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 404


@app.post("/api/repertoire/rename")
def api_rep_rename():
    data = request.get_json(force=True, silent=True) or {}
    try:
        repstore.rename_repertoire(data.get("id") or "", data.get("name") or "")
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
    data = request.get_json(force=True, silent=True) or {}
    threads = max(1, min(16, int(data.get("threads", 1))))
    hash_mb = max(16, min(2048, int(data.get("hash", 256))))
    manager.set_defaults(threads, hash_mb)
    return jsonify({"ok": True, "threads": threads, "hash": hash_mb})


@app.post("/api/review")
def api_review():
    data = request.get_json(force=True, silent=True) or {}
    pgn = (data.get("pgn") or "").strip()
    if not pgn:
        return jsonify({"error": "Missing PGN text."}), 400
    engine_id = data.get("engine") or "stockfish"
    engine = manager.get(engine_id)
    depth = max(4, min(22, int(data.get("depth", 12))))
    multipv = max(1, min(5, int(data.get("multipv", 3))))
    movetime = max(200, min(20000, int(data.get("movetime", 900))))

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        # keep only the 6 most recent jobs
        for old in sorted(_jobs, key=lambda k: _jobs[k].get("startedAt", 0))[:-6]:
            if _jobs[old].get("status") in ("done", "error"):
                _jobs.pop(old, None)
        _jobs[job_id] = {"status": "running", "stage": "starting", "progress": 0.0,
                         "startedAt": time.time(), "result": None, "error": None,
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
    return jsonify({"jobId": job_id, "engine": getattr(engine, "name", engine_id)})


@app.get("/api/review/status/<job_id>")
def api_review_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        return jsonify({k: job.get(k) for k in ("status", "stage", "stageDetail", "progress", "error", "engine")})


@app.get("/api/review/result/<job_id>")
def api_review_result(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
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
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip().lstrip("@").lower()
    max_games = max(1, min(20, int(data.get("max", 8))))
    time_class = (data.get("timeClass") or "").strip().lower()
    if not username:
        return jsonify({"error": "Missing username."}), 400
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
    from server import openings
    data = request.get_json(force=True, silent=True) or {}
    openings.set_token(data.get("token"))
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
        counts = trainer.sync_exercises(exercises)
        return jsonify({"ok": True, "candidates": len(exercises), **counts})
    except Exception as e:
        return jsonify({"error": f"trainer extract failed: {e}"}), 500


@app.get("/api/trainer/dashboard")
def api_trainer_dashboard():
    """Aggregated stats: totals, due/new/mastered, streak, accuracy, weak spots."""
    from server import trainer
    try:
        return jsonify(trainer.dashboard(trainer._parse_filters(request.args)))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"dashboard failed: {e}"}), 500


@app.get("/api/trainer/list")
def api_trainer_list():
    from server import trainer
    try:
        limit = max(1, min(500, int(request.args.get("limit") or 200)))
        return jsonify({"exercises": trainer.list_exercises(trainer._parse_filters(request.args), limit)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/trainer/queue")
def api_trainer_queue():
    """Next N exercises for a session (due first, then new, capped)."""
    from server import trainer
    try:
        limit = max(1, min(60, int(request.args.get("limit") or 8)))
        queue = trainer.next_queue(limit, trainer._parse_filters(request.args))
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
        return jsonify(trainer.record_attempt(ex_id, quality, data.get("playedUci") or "", ms))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"record failed: {e}"}), 500


@app.get("/api/trainer/history")
def api_trainer_history():
    from server import trainer
    try:
        limit = max(1, min(200, int(request.args.get("limit") or 50)))
        return jsonify({"history": trainer.attempt_history(limit)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/trainer/sources")
def api_trainer_sources():
    from server import trainer
    return jsonify({"sources": trainer.sources_list()})


@app.post("/api/trainer/detach")
def api_trainer_detach():
    """Drop all exercises exclusively sourced from a deleted library game."""
    from server import trainer
    data = request.get_json(force=True, silent=True) or {}
    source_id = (data.get("sourceId") or "").strip()
    if not source_id:
        return jsonify({"error": "missing sourceId"}), 400
    try:
        return jsonify(trainer.detach_source(source_id))
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
        counts = perfdb.import_games(parsed, source="import")
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
        res = perfdb.attach_review(data.get("review"))
        return jsonify(res)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"review import failed: {e}"}), 500


@app.get("/api/perf/overall")
def api_perf_overall():
    from server import perfdb
    return jsonify(perfdb.overall())


@app.get("/api/perf/openings")
def api_perf_openings():
    from server import perfdb
    return jsonify(perfdb.openings_report())


@app.get("/api/perf/weaknesses")
def api_perf_weaknesses():
    from server import perfdb
    try:
        return jsonify(perfdb.weaknesses())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.get("/api/perf/recommendations")
def api_perf_recommendations():
    from server import perfdb
    try:
        return jsonify(perfdb.recommendations())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.get("/api/perf/timeline")
def api_perf_timeline():
    from server import perfdb
    return jsonify(perfdb.timeline())


@app.get("/api/perf/games")
def api_perf_games():
    from server import perfdb
    limit = max(1, min(500, int(request.args.get("limit") or 100)))
    return jsonify({"games": perfdb.list_games(limit), "total": perfdb.games_total()})


@app.post("/api/perf/games/delete")
def api_perf_games_delete():
    from server import perfdb
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify({"ok": perfdb.delete_game((data.get("id") or "").strip())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/perf/aliases")
def api_perf_aliases():
    from server import perfdb
    data = request.get_json(force=True, silent=True) or {}
    a, c = (data.get("alias") or "").strip(), (data.get("canonical") or "").strip()
    if not a or not c:
        return jsonify({"error": "alias and canonical required"}), 400
    perfdb.add_alias(a, c)
    perfdb.normalize_aliases()
    return jsonify({"ok": True})


@app.get("/api/perf/aliases")
def api_perf_aliases_get():
    from server import perfdb
    return jsonify({"aliases": perfdb.list_aliases()})


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
    use_engine = bool(data.get("useEngine")) or mode == "strongest"
    with _rjobs_lock:
        _rjobs[job_id] = {"status": "running", "stage": "starting", "result": None, "error": None,
                          "startedAt": time.time()}

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
    return jsonify({"jobId": job_id})


@app.get("/api/repertoire/status/<job_id>")
def api_rep_status(job_id):
    with _rjobs_lock:
        job = _rjobs.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        return jsonify({k: job.get(k) for k in ("status", "stage", "error")})


@app.get("/api/repertoire/result/<job_id>")
def api_rep_result(job_id):
    with _rjobs_lock:
        job = _rjobs.get(job_id)
        if not job:
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
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    max_games = max(1, min(10, int(data.get("max", 5))))
    speeds = data.get("speeds") or []
    if not username:
        return jsonify({"error": "Missing username."}), 400
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
