"""Opening/book detection via the free Lichess opening-explorer API (masters database).

No local database download. Walks the game from the start position; a played move is a
"Book" move while it stays among the master database's common continuations. The opening
name/ECO comes from the explorer's response for the deepest reached position.

If the API is unreachable we fall back to a small embedded book of mainlines so the
review still has book moves + opening names offline. Responses are cached on disk.
"""
import json
import re
import threading
import time
from pathlib import Path

import requests

try:
    from server.accuracy import PHASES  # noqa: F401  (import keeps module coherent)
except ImportError:
    from accuracy import PHASES  # type: ignore

EXPLORER = "https://explorer.lichess.ovh/masters"
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "explorer_cache.json"
MIN_GAMES = 300          # position needs this many master games to be considered book
MIN_MOVE_SHARE = 0.05    # played move must be >=5% of the database replies
MAX_BOOK_PLIES = 28

# Lichess now requires an API token for the explorer. Users paste a free personal
# token (lichess.org/account/oauth/token); it is stored locally in data/.
TOKEN_PATH = Path(__file__).resolve().parent.parent / "data" / "lichess_token.txt"
_token = None
try:
    if TOKEN_PATH.exists():
        _token = TOKEN_PATH.read_text(encoding="utf-8").strip() or None
except Exception:
    _token = None


def set_token(tok):
    """Store/clear the Lichess API token (persisted to data/lichess_token.txt)."""
    global _token
    _token = (tok or "").strip() or None
    try:
        if _token:
            TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_PATH.write_text(_token, encoding="utf-8")
        elif TOKEN_PATH.exists():
            TOKEN_PATH.unlink()
    except Exception:
        pass


def has_token():
    return bool(_token)

_cache_lock = threading.Lock()
_disk_cache = {}
_flush_timer = None
try:
    if CACHE_PATH.exists():
        _disk_cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
except Exception:
    _disk_cache = {}


def _cache_get(fen):
    with _cache_lock:
        return _disk_cache.get(fen)


def _flush_cache():
    try:
        with _cache_lock:
            data = dict(_disk_cache)
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _cache_put(fen, payload):
    global _flush_timer
    with _cache_lock:
        _disk_cache[fen] = payload
    if _flush_timer is None:
        _flush_timer = threading.Timer(5.0, _flush_cache)
        _flush_timer.daemon = True
        _flush_timer.start()


def query_position(fen: str, timeout: float = 10.0):
    """One explorer query. Returns {'total', 'moves' {uci:share}, 'moveList', 'opening'} or None."""
    cached = _cache_get(fen)
    if cached is not None:
        return cached
_last_status = None


def last_status():
    return _last_status


def query_position(fen: str, timeout: float = 10.0):
    """One explorer query. Returns {'total', 'moves' {uci:share}, 'moveList', 'opening'} or None."""
    global _last_status
    cached = _cache_get(fen)
    if cached is not None:
        return cached
    try:
        headers = {"User-Agent": "chess-review-local/1.0"}
        if _token:
            headers["Authorization"] = f"Bearer {_token}"
        r = requests.get(EXPLORER, params={"fen": fen}, timeout=timeout, headers=headers)
        _last_status = r.status_code
        r.raise_for_status()
        data = r.json()
        total = data.get("white", 0) + data.get("draws", 0) + data.get("black", 0)
        moves = {}
        move_list = []
        for m in data.get("moves", []):
            games = m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)
            moves[m["uci"]] = games / total if total else 0.0
            move_list.append({
                "uci": m["uci"],
                "san": m.get("san") or m["uci"],
                "games": games,
                "share": games / total if total else 0.0,
                "white": m.get("white", 0),
                "draws": m.get("draws", 0),
                "black": m.get("black", 0),
            })
        payload = {"total": total, "moves": moves, "moveList": move_list, "opening": data.get("opening")}
    except Exception:
        payload = None
        if _last_status is None:
            _last_status = 0
    if payload:
        _cache_put(fen, payload)
    return payload


# --- offline fallback: common mainlines (uci sequences) with names at the final prefix ---
_OFFLINE_BOOK = [
    ("e2e4 e7e5 g1f3 b8c6 f1b5", "Ruy Lopez"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4", "Italian Game"),
    ("e2e4 e7e5 g1f3 b8c6 d2d4", "Scotch Game"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4 f8c5", "Giuoco Piano"),
    ("e2e4 e7e5 f2f4", "King's Gambit"),
    ("e2e4 e7e5 g1f3 g8f6", "Petrov's Defence"),
    ("e2e4 c7c5", "Sicilian Defence"),
    ("e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3", "Sicilian Najdorf"),
    ("e2e4 c7c5 g1f3 b8c6", "Sicilian Old Sicilian"),
    ("e2e4 c7c5 g1f3 e7e6", "Sicilian French Variation"),
    ("e2e4 c7c5 g1f3 d7d6", "Sicilian Najdorf Setup"),
    ("e2e4 c7c5 b1c3", "Sicilian Closed"),
    ("e2e4 e7e6", "French Defence"),
    ("e2e4 e7e6 d2d4 d7d5", "French, Main Line"),
    ("e2e4 c7c6", "Caro-Kann Defence"),
    ("e2e4 c7c6 d2d4 d7d5", "Caro-Kann, Main Line"),
    ("e2e4 d7d5", "Scandinavian Defence"),
    ("e2e4 g8f6", "Alekhine Defence"),
    ("e2e4 d7d6", "Pirc Defence"),
    ("e2e4 b7b6", "Owen Defence"),
    ("d2d4 d7d5 c2c4 e7e6", "Queen's Gambit Declined"),
    ("d2d4 d7d5 c2c4 c7c6", "Slav Defence"),
    ("d2d4 d7d5 c2c4 d5c4", "Queen's Gambit Accepted"),
    ("d2d4 g8f6 c2c4 e7e6", "Indian Game"),
    ("d2d4 g8f6 c2c4 g7g6", "King's Indian Defence"),
    ("d2d4 g8f6 c2c4 e7e6 b1c3 f8b4", "Nimzo-Indian Defence"),
    ("d2d4 g8f6 c2c4 g7g6 b1c3 d7d5", "Grünfeld Defence"),
    ("d2d4 g8f6 c2c4 c7c5", "Benoni Defence"),
    ("d2d4 g8f6 c2c4 e7e5", "Budapest Gambit"),
    ("d2d4 g8f6 b1c3 d7d5", "Richter-Veresov Attack"),
    ("d2d4 d7d5 g1f3 g8f6 c1f4", "London System"),
    ("d2d4 d7d5 g1f3 g8f6 c1g5", "Torre Attack"),
    ("d2d4 g8f6 g1f3 e7e6 c1g5", "Torre Attack vs Indian"),
    ("d2d4 f7f5", "Dutch Defence"),
    ("d2d4 e7e5", "Englund Gambit"),
    ("c2c4", "English Opening"),
    ("c2c4 e7e5", "English, Reversed Sicilian"),
    ("c2c4 g8f6", "English, Symmetrical"),
    ("c2c4 c7c5", "English, Symmetrical c5"),
    ("g1f3 d7d5 d2d4 g8f6 c1f4", "London System"),
    ("g1f3 g8f6 c2c4 e7e6 b1c3 f8b4", "Nimzo-English"),
    ("g1f3 d7d5 g2g3 g8f6 f1g2 e7e6", "King's Indian Attack"),
    ("b1c3 e7e5", "Vienna Game"),
    ("b1c3 g8f6", "Nimzowitsch Defence"),
    ("f2f4", "Bird's Opening"),
    ("e2e4 e7e5 b1c3 g8f6", "Vienna Game"),
    ("e2e4 g7g6 d2d4 f8g7 b1c3 d8h4", "Modern Defence"),
    ("d2d4 g7g6", "Modern Defence"),
    ("e2e4 b8c6", "Nimzowitsch Defence"),
    ("e2e4 d7d5 e4d5 c7c6", "Scandinavian, Main Line"),
    ("e2e4 e7e5 f1c4 g8f6", "Italian, Two Knights"),
    ("e2e4 c7c5 g1f3 e7e6 d2d4 c5d4 f3d4 b8c6", "Sicilian, Taimanov"),
    ("e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3 a7a6", "Sicilian, Najdorf"),
    ("d2d4 d7d5 g1f3 g8f6 c1f4 c7c5 e2e3 b8c6", "London, Main Line"),
]


def _offline_lookup(uci_history, fen):
    """Returns (is_book_position, book_moves, name) using the embedded mainlines."""
    prefix = " ".join(uci_history)
    book_moves = set()
    name = None
    best_len = -1
    for line, lname in _OFFLINE_BOOK:
        toks = line.split()
        if len(toks) > len(uci_history) and " ".join(toks[:len(uci_history)]) == prefix:
            book_moves.add(toks[len(uci_history)])
            if len(toks) > best_len:
                best_len = len(toks)
                name = lname
    return book_moves, name


def walk_game(moves_uci, board, progress=None):
    """Walk the game's moves from the start; classify each ply as book or not.

    Returns (book_flags: list[bool] per ply, opening: {'eco','name'} | None)."""
    book_flags = [False] * len(moves_uci)
    opening = None
    offline = False
    history = []
    b = board.copy(stack=False)

    for i, uci in enumerate(moves_uci):
        if i >= MAX_BOOK_PLIES:
            break
        if not offline:
            info = query_position(b.fen())
            if info is None:
                offline = True  # API unreachable -> switch to embedded book for rest
            else:
                if info["opening"]:
                    opening = info["opening"]
                if info["total"] >= MIN_GAMES and info["moves"].get(uci, 0.0) >= MIN_MOVE_SHARE:
                    book_flags[i] = True
                    history.append(uci)
                    b.push(_parse_uci(b, uci))
                    if progress:
                        progress(f"book {i+1}/{len(moves_uci)}")
                    continue
                break  # out of book: stop walking
        if offline:
            bm, name = _offline_lookup(history, b.fen())
            if uci in bm:
                book_flags[i] = True
                if name:
                    opening = {"eco": "", "name": name}
                history.append(uci)
                b.push(_parse_uci(b, uci))
                continue
            break
    return book_flags, opening


def _parse_uci(board, uci):
    import chess
    return chess.Move.from_uci(uci)
