"""Quick sanity probe for every engine binary: UCI handshake + depth-8 search from startpos."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.uci import EngineManager, UciEngine, ENGINES_DIR

import chess


def main():
    print(f"engines dir: {ENGINES_DIR}")
    exes = [p for p in sorted(ENGINES_DIR.iterdir()) if p.suffix.lower() == ".exe"]
    if not exes:
        print("no engine binaries found")
        return
    for p in exes:
        try:
            t0 = time.time()
            e = UciEngine(p, threads=1, hash_mb=64)
            lines = e.analyse(chess.STARTING_FEN, depth=8, multipv=2, timeout=60)
            dt = time.time() - t0
            top = lines[0]
            print(f"OK   {p.name:16s} -> {e.name!r} by {e.author!r} | best={top['pv'][:3]} cp={top['cp']} | {dt:.1f}s")
            e.quit()
        except Exception as ex:
            print(f"FAIL {p.name:16s} -> {type(ex).__name__}: {ex}")


if __name__ == "__main__":
    main()
