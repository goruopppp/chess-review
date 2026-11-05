"""Entry point: python run.py  → serves the app on http://127.0.0.1:8543"""
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from server.app import app, manager  # noqa: E402

PORT = int(os.environ.get("PORT", "8543"))
HOST = os.environ.get("HOST", "127.0.0.1")
URL = f"http://127.0.0.1:{PORT}"


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main():
    if port_in_use():
        # already running (desktop shortcut double-clicked again, autostart, etc.)
        print(f"already running - opening {URL}")
        webbrowser.open(URL)
        return
    names = ", ".join(e["name"] for e in manager.list())
    print(f"chess-review: engines available -> {names}")
    print(f"Serving on {URL}")
    if HOST == "127.0.0.1":
        threading.Timer(1.2, lambda: webbrowser.open(URL)).start()
    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
