"""UCI engine management: process lifecycle, analysis, and discovery of engine binaries.

I/O uses a dedicated reader thread + queue so timeouts are always honored on Windows
(blocking readline on a pipe cannot be interrupted there).
"""
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

ENGINES_DIR = Path(__file__).resolve().parent.parent / "engines"

MATE_CP = 100000

# Per-engine UCI defaults (applied at startup when the option exists).
ENGINE_DEFAULT_OPTIONS = {
    "rubichess": {"Use_NNUE": "false"},  # its net file isn't distributable; classical eval
}


def safe_cwd() -> str:
    """Engine processes should not get a cwd containing non-ANSI characters — some
    engines scan their working directory and crash on unicode paths. Prefer the
    Windows 8.3 short path; fall back to a relative path from the project root."""
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(600)
            if ctypes.windll.kernel32.GetShortPathNameW(str(ENGINES_DIR), buf, 600) and buf.value.isascii():
                return buf.value
        except Exception:
            pass
        try:
            rel = os.path.relpath(ENGINES_DIR, os.getcwd())
            if rel.isascii():
                return rel
        except Exception:
            pass
    return str(ENGINES_DIR)


class UciEngine:
    """Wraps a single UCI engine process. All communication is guarded by a lock so
    concurrent Flask threads queue up instead of corrupting the stream."""

    def __init__(self, path: Path, threads: int = 2, hash_mb: int = 256):
        self.path = path
        self.id = path.stem.lower()
        self.lock = threading.RLock()  # reentrant: restart() -> _start() -> configure()
        self.threads = threads
        self.hash_mb = hash_mb
        self.name = path.stem
        self.author = ""
        self.options = {}
        self.warnings = []
        self.proc = None
        self.alive = False
        self._q = queue.Queue()
        self._reader = None
        self.search_info = {"searching": False, "depth": 0, "nodes": 0, "nps": 0, "time": 0, "fen": ""}
        self._start()

    # ---------- process lifecycle ----------
    def _start(self):
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [str(self.path)],
            cwd=safe_cwd(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self.alive = True
        self._q = queue.Queue()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        self._send("uci")
        info = self._read_until("uciok", timeout=20)
        for line in info:
            if line.startswith("id name"):
                self.name = line[8:].strip()
            elif line.startswith("id author"):
                self.author = line[10:].strip()
            elif line.startswith("info string") and ("Cannot open" in line or "Failed" in line):
                self.warnings.append(line)
            m = re.match(r"option name (\S+(?: \S+)*?) type (\w+)", line)
            if m:
                self.options[m.group(1)] = {"type": m.group(2)}
        defaults = dict(ENGINE_DEFAULT_OPTIONS.get(self.id, {}))
        self.configure(self.threads, self.hash_mb, extra=defaults)

    def _reader_loop(self):
        try:
            for line in self.proc.stdout:
                self._q.put(line.rstrip("\r\n"))
        except Exception:
            pass
        self._q.put(None)  # EOF marker
        self.alive = False

    def configure(self, threads: int, hash_mb: int, extra: dict | None = None):
        with self.lock:
            self.threads, self.hash_mb = threads, hash_mb
            if "Threads" in self.options:
                self._send(f"setoption name Threads value {threads}")
            if "Hash" in self.options:
                self._send(f"setoption name Hash value {hash_mb}")
            for k, v in (extra or {}).items():
                if k in self.options:
                    self._send(f"setoption name {k} value {v}")
            self._sync()

    def _send(self, cmd: str):
        try:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
        except Exception:
            self.alive = False
            raise RuntimeError(f"engine {self.id} stdin closed")

    def _read_until(self, token: str, timeout: float):
        lines = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self._q.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if line is None:
                self.alive = False
                raise RuntimeError(f"engine {self.id} died (stdout closed)")
            if line:
                lines.append(line)
            if line.startswith(token):
                return lines
        raise TimeoutError(f"engine {self.id} did not send {token!r} within {timeout}s")

    def _sync(self):
        self._send("isready")
        self._read_until("readyok", timeout=25)

    def restart(self):
        with self.lock:
            try:
                self.proc.kill()
            except Exception:
                pass
            self._start()

    def quit(self):
        with self.lock:
            try:
                self._send("quit")
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.alive = False

    # ---------- analysis ----------
    def analyse(self, fen: str, depth: int = 14, multipv: int = 3, timeout: float = 120.0,
                movetime: int | None = None, options: dict | None = None):
        """Returns list of dicts: {'cp': mover-pov cp (mates encoded), 'mate': plies or None,
        'pv': [uci moves]} ordered best-first. Search stops at depth OR movetime, whichever
        comes first (movetime keeps reviews predictable on slow machines).
        `options`: extra UCI setoptions for this search only (e.g. a strength limit).
        Callers that pass none get full strength, so a limited 'vs engine' session
        never leaks into reviews or live analysis."""
        with self.lock:
            if not self.alive:
                try:
                    self.proc.kill()
                except Exception:
                    pass
                self._start()
            self._send(f"setoption name MultiPV value {multipv}")
            for k, v in (options or {"UCI_LimitStrength": "false"}).items():
                self._send(f"setoption name {k} value {v}")
            self._send(f"position fen {fen}")
            self.search_info.update({"searching": True, "depth": 0, "nodes": 0, "nps": 0,
                                     "time": 0, "fen": fen})
            if movetime:
                self._send(f"go depth {depth} movetime {int(movetime)}")
            else:
                self._send(f"go depth {depth}")
            return self._collect_go(multipv, timeout)

    def best_move(self, fen: str, depth: int = 12, timeout: float = 60.0):
        lines = self.analyse(fen, depth=depth, multipv=1, timeout=timeout)
        return lines[0]["pv"][0] if lines and lines[0]["pv"] else None

    def _collect_go(self, multipv: int, timeout: float):
        pvs = {}
        deadline = time.time() + timeout
        bestmove = None
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                line = self._q.get(timeout=max(0.05, remaining))
            except queue.Empty:
                break
            if line is None:
                self.alive = False
                raise RuntimeError(f"engine {self.id} died during search")
            if not line:
                continue
            if line.startswith("bestmove"):
                self.search_info["searching"] = False
                parts = line.split()
                bestmove = parts[1] if len(parts) > 1 else "(none)"
                break
            if not line.startswith("info"):
                continue
            # live search tracking: depth/nodes/nps arrive on every info line
            toks_all = line.split()

            def _ival(key):
                try:
                    return int(toks_all[toks_all.index(key) + 1])
                except (ValueError, IndexError):
                    return None
            d = _ival("depth")
            if d is not None:
                self.search_info["depth"] = d
            for k in ("nodes", "nps", "time"):
                v = _ival(k)
                if v is not None:
                    self.search_info[k] = v
            if " pv " not in line or " score " not in line:
                continue
            if " lowerbound" in line or " upperbound" in line:
                continue
            toks = toks_all

            def tokval(key, cast=str, default=None):
                try:
                    return cast(toks[toks.index(key) + 1])
                except (ValueError, IndexError):
                    return default

            idx = tokval("multipv", int, 1)
            if idx > multipv:
                continue
            pv = toks[toks.index("pv") + 1:]
            if not pv:
                continue
            score, mate = None, None
            if " mate " in line:
                mate = tokval("mate", int, 0)
                score = (MATE_CP - abs(mate)) * (1 if mate > 0 else -1)
            else:
                cp = tokval("cp", int, 0)
                score = max(-4000, min(4000, cp))
            pvs[idx] = {"cp": score, "mate": mate, "pv": pv}
        if bestmove is None:  # safety stop, then drain the bestmove
            self.search_info["searching"] = False
            try:
                self._send("stop")
                stop_deadline = time.time() + 10
                while time.time() < stop_deadline:
                    try:
                        line = self._q.get(timeout=0.5)
                    except queue.Empty:
                        break
                    if line is None:
                        self.alive = False
                        break
                    if line and line.startswith("bestmove"):
                        break
            except Exception:
                pass
        if not pvs:
            return [{"cp": 0, "mate": None, "pv": [bestmove] if bestmove and bestmove != "(none)" else []}]
        return [pvs[i] for i in sorted(pvs)]


class EngineManager:
    """Discovers engines in engines/ (any *.exe on Windows, any executable file elsewhere),
    plus the built-in Python fallback engine. UCI processes start lazily on first use."""

    def __init__(self):
        self._engines = {}
        self._paths = {}
        self._order = []
        self._lock = threading.Lock()
        self._defaults = {"threads": 1, "hash_mb": 128}
        self.refresh()

    def refresh(self):
        found = []
        if ENGINES_DIR.exists():
            for p in sorted(ENGINES_DIR.iterdir()):
                if p.is_file() and (p.suffix.lower() == ".exe" or (os.name != "nt" and os.access(p, os.X_OK))):
                    found.append(p)
        for path in found:
            eid = path.stem.lower()
            if eid in self._paths:
                continue
            self._paths[eid] = path
            self._order.append(eid)
        if "builtin" not in self._engines:
            from server.fallback import FallbackEngine
            self._engines["builtin"] = FallbackEngine()
            self._order.append("builtin")

    def _ensure(self, eid: str):
        if eid in self._engines:
            return self._engines[eid]
        path = self._paths.get(eid)
        if not path:
            return None
        with self._lock:
            if eid in self._engines:
                return self._engines[eid]
            try:
                eng = UciEngine(path, **self._defaults)
                self._engines[eid] = eng
                return eng
            except Exception as e:
                print(f"[engines] failed to start {path.name}: {e}")
                return None

    def list(self):
        out = []
        for eid in self._order:
            e = self._engines.get(eid)
            if eid == "builtin":
                out.append({"id": "builtin", "name": e.name, "author": e.author, "kind": "builtin",
                            "threads": None, "hash": None, "warnings": [], "options": [], "started": True})
                continue
            out.append({
                "id": eid,
                "name": e.name if e else eid.capitalize() + " (lazy)",
                "author": getattr(e, "author", "") if e else "",
                "kind": "uci",
                "threads": getattr(e, "threads", None),
                "hash": getattr(e, "hash_mb", None),
                "warnings": [w for w in getattr(e, "warnings", [])][:3] if e else [],
                "options": sorted(getattr(e, "options", {}).keys()) if e else [],
                "started": e is not None,
            })
        return out

    def get(self, engine_id: str):
        eng = self._engines.get(engine_id) or self._ensure(engine_id)
        if eng is None:
            # engine binary broken/missing: fall back to stockfish, then anything, then builtin
            for fallback in ("stockfish", "builtin"):
                eng = self._engines.get(fallback) or self._ensure(fallback)
                if eng:
                    break
        return eng

    def engine_state(self):
        """Live per-engine search state (depth/nodes/nps) for real-time UI."""
        out = {}
        for eid, e in self._engines.items():
            if hasattr(e, "search_info"):
                info = dict(e.search_info)
                info["name"] = e.name
                out[eid] = info
        return out

    def set_defaults(self, threads: int, hash_mb: int):
        self._defaults = {"threads": threads, "hash_mb": hash_mb}
        for e in self._engines.values():
            try:
                e.configure(threads, hash_mb)
            except Exception:
                pass

    def quit_all(self):
        for e in self._engines.values():
            try:
                e.quit()
            except Exception:
                pass
