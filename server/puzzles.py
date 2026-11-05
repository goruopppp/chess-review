"""Fetch Lichess puzzles (lichess.org/training) for the in-app Puzzle tab.

The public endpoint GET https://lichess.org/api/puzzle/next accepts an optional
`angle` query param (our `theme`, e.g. mateIn1, mateIn2, fork, pin, endgame...) —
unlike a plain `theme`, which lichess silently ignores. Returns a puzzle with
rating / popularity / themes / solution / board FEN. No token required; outbound
traffic is cached on disk briefly so the tab still opens when the network is flaky.
"""
import json
import threading
import time
from pathlib import Path

import requests

NEXT_URL = "https://lichess.org/api/puzzle/next"
HEADERS = {
    "User-Agent": "chess-review-studio/puzzle-tab (contact: lichess.org/@/goruopppp)",
    "Accept": "application/json",
}
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "puzzle_cache.json"
CACHE_TTL = 900          # seconds an entry stays fresh on disk
MAX_CACHE_ENTRIES = 48   # trim the file so it cannot grow unbounded

_lock = threading.Lock()


def _cache_load():
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cache_store(cache):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


def _normalize(payload):
    pz = payload.get("puzzle") or {}
    game = payload.get("game") or {}
    solution = pz.get("solution") or []
    fen = pz.get("fen")
    if not fen or not solution:
        raise ValueError("lichess returned an incomplete puzzle payload")
    players = {}
    for p in (game.get("players") or []):
        color = p.get("color")
        if color:
            players[color] = {
                "name": p.get("name"),
                "title": p.get("title"),
                "rating": p.get("rating"),
            }
    theme_list = pz.get("themes") or []
    return {
        "id": pz.get("id"),
        "rating": pz.get("rating"),
        "plays": pz.get("plays"),
        "themes": theme_list,
        "solution": solution,
        "fen": fen,
        "lastMove": pz.get("lastMove"),
        "mateIn": pz.get("mate"),
        "url": f"https://lichess.org/training/{pz.get('id')}",
        "gameUrl": f"https://lichess.org/{game.get('id')}" if game.get("id") else None,
        "players": players,
    }


def get_puzzle(theme=""):
    """Returns one normalized puzzle dict; falls back to stale cache if offline."""
    theme = (theme or "").strip()
    key = theme or "_any"
    stale = None
    with _lock:
        cache = _cache_load()
        entry = cache.get(key)
        if entry:
            age = time.time() - entry.get("ts", 0)
            if age < CACHE_TTL:
                return entry["puzzle"]
            stale = entry.get("puzzle")
    try:
        r = requests.get(NEXT_URL, params=({"angle": theme} if theme else {}),
                         headers=HEADERS, timeout=14)
        r.raise_for_status()
        puzzle = _normalize(r.json())
    except Exception:
        if stale is not None:
            out = dict(stale)
            out["stale"] = True
            return out
        raise
    with _lock:
        cache = _cache_load()
        cache[key] = {"ts": time.time(), "puzzle": puzzle}
        while len(cache) > MAX_CACHE_ENTRIES:
            cache.pop(next(iter(cache)))
        _cache_store(cache)
    return puzzle
