"""Chess.com public API client.

Only public data is used: a player's monthly archives, which contain the PGN of
every finished game. No password, OAuth token or cookie is involved.

The archive URLs come from chess.com's API response, not from the caller, but a
compromised or spoofed response must not turn this server into a request proxy,
so every URL is pinned to chess.com's own HTTPS archive host before it is
followed.
"""
import datetime as _dt
import re
from urllib.parse import urlsplit

import requests


API_ROOT = "https://api.chess.com/pub"
ARCHIVE_HOST = "api.chess.com"
HEADERS = {"User-Agent": "chess-review-local-app/1.0 (local game review tool)"}

# Chess.com archive URLs look like
#   https://api.chess.com/pub/player/<name>/games/2026/11
# Pinning the whole shape means a spoofed API response cannot point us at any
# other path (or another host) even though the URL is followed server-side.
_ARCHIVE_RE = re.compile(r"^/pub/player/[A-Za-z0-9_-]+/games/\d{4}/\d{1,2}$")


class ChessComError(Exception):
    """A fetch failure with a message that is safe to show to the user."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


def is_archive_url(url):
    """Only chess.com's own HTTPS monthly archive endpoints may be followed."""
    try:
        u = urlsplit(url or "")
        return (u.scheme == "https" and u.hostname == ARCHIVE_HOST
                and bool(_ARCHIVE_RE.match(u.path)))
    except Exception:
        return False


def fetch_games(username, max_games=8, time_class="", months=0):
    """Return recent public games, newest first.

    `months` > 0 limits how far back the archives are scanned. The manual fetch
    endpoint keeps the historical behaviour (all months); auto-sync uses a small
    window because it only cares about games played since the last visit.
    """
    username = (username or "").strip().lstrip("@").lower()
    if not username:
        raise ChessComError("Missing username.", 400)
    max_games = max(1, min(20, int(max_games or 8)))
    time_class = (time_class or "").strip().lower()

    try:
        arch = requests.get(f"{API_ROOT}/player/{username}/games/archives",
                            headers=HEADERS, timeout=30)
        arch.raise_for_status()
        archives = list(arch.json().get("archives", []))
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        msg = ("User not found on chess.com." if code == 404
               else f"chess.com API error (HTTP {code})")
        raise ChessComError(msg, 502)
    except Exception as e:
        raise ChessComError(f"chess.com fetch failed: {e}", 502)

    if months:
        archives = archives[-max(1, int(months)):]

    games = []
    try:
        for url in reversed(archives):
            if len(games) >= max_games:
                break
            if not is_archive_url(url):
                continue
            month = requests.get(url, headers=HEADERS, timeout=30)
            month.raise_for_status()
            for g in reversed(month.json().get("games", [])):
                if len(games) >= max_games:
                    break
                pgn = g.get("pgn")
                if not pgn:
                    continue
                if time_class and g.get("time_class") != time_class:
                    continue
                w, b = g.get("white") or {}, g.get("black") or {}
                wres, bres = w.get("result"), b.get("result")
                result = "1-0" if wres == "win" else ("0-1" if bres == "win" else "1/2-1/2")
                end = g.get("end_time")
                date = (_dt.datetime.fromtimestamp(end).strftime("%Y-%m-%d")
                        if end else "")
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
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        raise ChessComError(f"chess.com API error (HTTP {code})", 502)
    except Exception as e:
        raise ChessComError(f"chess.com fetch failed: {e}", 502)

    if not games:
        raise ChessComError("No games found for that username / speed filter.", 404)
    return games
