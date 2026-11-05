"""Automatic opening repertoire builder from the Lichess masters database.

Builds a tree of opening lines from a root FEN: at each position where the
chosen side is to move, ONE move is selected according to the requested style
(mode); at each opponent position the top `breadth` replies become branches.
Every root-to-leaf path is one repertoire line, exported as a PGN tree.
"""
import math
import time

import chess
import chess.pgn

try:
    from server import openings
except ImportError:
    import openings  # type: ignore

MATE_CP = 100000
MODES = ("mainline", "strongest", "attacking", "solid", "creative")
CAND_MIN_GAMES = 8
LINE_CAP = 400


class RepertoireDeadline(Exception):
    """Raised when the build's wall-clock budget runs out; partial results are kept."""


class RepertoireAuthError(Exception):
    """Raised when the explorer rejects us (missing/invalid Lichess token)."""


class RepertoireBuilder:
    def __init__(self, fen, side, mode, your_moves, breadth, use_engine,
                 engine=None, budget=260, progress=None, deadline_s=240):
        self.root_fen = fen
        self.side = side
        self.mode = mode if mode in MODES else "mainline"
        self.your_moves = max(2, min(12, int(your_moves or 5)))
        self.breadth = max(1, min(4, int(breadth or 2)))
        self.use_engine = bool(use_engine)
        self.engine = engine
        self.budget = budget
        self.progress = progress or (lambda s: None)
        self.deadline = time.time() + max(30, min(600, deadline_s))
        self.queries = 0
        self.evals = 0
        self.fails = 0
        self.lines = []
        your_nodes = sum((self.breadth ** i) for i in range(self.your_moves))
        self.est = max(6, min(self.budget, int(your_nodes * 2.1)))

    # ---------- stat helpers ----------
    def own_pct(self, m):
        g = m.get("games") or 0
        if not g:
            return 50.0
        wins = m.get("white", 0) if self.side == "white" else m.get("black", 0)
        return wins / g * 100

    def draw_pct(self, m):
        g = m.get("games") or 0
        return m.get("draws", 0) / g * 100 if g else 50.0

    def q(self, fen):
        if time.time() > self.deadline:
            raise RepertoireDeadline()
        self.queries += 1
        self.progress(f"position {min(self.queries, self.est)}/{self.est}")
        p = openings.query_position(fen)
        if p is None:
            self.fails += 1
            if self.fails >= 3 and not self.lines:
                raise RepertoireAuthError(
                    "The Lichess explorer is unreachable or needs a free API token - "
                    "open settings and paste a personal token from lichess.org/account/oauth/token")
        else:
            self.fails = 0
        return p

    def pick(self, cands, b):
        """Select your move at a decision node according to the mode."""
        if self.mode == "mainline":
            return max(cands, key=lambda m: m.get("games", 0))
        if self.mode == "attacking":
            return max(cands, key=lambda m: self.own_pct(m) - 0.5 * self.draw_pct(m))
        if self.mode == "solid":
            return max(cands, key=lambda m: self.draw_pct(m) + 0.5 * self.own_pct(m))
        if self.mode == "creative":
            by_rarity = sorted(cands, key=lambda m: m.get("games", 0))
            rare = by_rarity[:max(1, len(by_rarity) // 2)]
            rare = [m for m in rare if m.get("games", 0) >= 12] or by_rarity
            return max(rare, key=lambda m: self.own_pct(m))
        # strongest: engine decides among the 3 most-played candidates
        if self.use_engine and self.engine is not None:
            best, best_score = None, None
            for m in sorted(cands, key=lambda x: -x.get("games", 0))[:3]:
                mv = chess.Move.from_uci(m["uci"])
                if mv not in b.legal_moves:
                    continue
                b2 = b.copy(stack=False)
                b2.push(mv)
                self.evals += 1
                try:
                    r = self.engine.analyse(b2.fen(), depth=14, multipv=1,
                                            movetime=260, timeout=60)
                    cp = -(r[0]["cp"])  # opponent-pov -> your pov
                    score = cp + self.own_pct(m) / 100.0  # tiny win% tiebreak
                    if best_score is None or score > best_score:
                        best, best_score = m, score
                except Exception:
                    continue
            if best is not None:
                return best
        # strongest without engine: win% blended with popularity (trust)
        return max(cands, key=lambda m: (self.own_pct(m) + 10 * math.log10(m.get("games", 0) + 1)))

    # ---------- tree walk ----------
    def close_line(self, path, b):
        if len(self.lines) >= LINE_CAP:
            self.queries = max(self.queries, self.budget)  # stop expanding
            return
        last_own = next((m["own"] for m in reversed(path) if m.get("own") is not None), None)
        last_games = next((m["games"] for m in reversed(path) if m.get("own") is not None), 0)
        eval_cp = next((m.get("evalCp") for m in reversed(path) if m.get("evalCp") is not None), None)
        self.lines.append({"path": list(path), "own": last_own, "games": last_games,
                           "evalCp": eval_cp, "fen": b.fen()})

    def expand_your(self, b, plies_left, path):
        if plies_left <= 0 or self.queries >= self.budget or b.is_game_over():
            self.close_line(path, b)
            return
        try:
            p = self.q(b.fen())
        except RepertoireDeadline:
            self.close_line(path, b)   # time up: keep the path built so far
            return
        if not p:
            self.close_line(path, b)
            return
        cands = [m for m in p.get("moveList", []) if m.get("games", 0) >= CAND_MIN_GAMES][:6]
        if not cands:
            self.close_line(path, b)
            return
        for m in cands:
            m["own"] = round(self.own_pct(m), 1)
            m["draw"] = round(self.draw_pct(m), 1)
        pick = self.pick(cands, b)
        if pick is None:
            self.close_line(path, b)
            return
        mv = chess.Move.from_uci(pick["uci"])
        if mv not in b.legal_moves:
            self.close_line(path, b)
            return
        entry = {"uci": pick["uci"], "san": b.san(mv), "games": pick.get("games", 0),
                 "own": pick["own"], "draw": pick["draw"]}
        b.push(mv)
        self.expand_opp(b, plies_left - 1, path + [entry])

    def expand_opp(self, b, plies_left, path):
        if b.is_game_over():
            self.close_line(path, b)
            return
        if plies_left <= 0 or self.queries >= self.budget:
            self.close_line(path, b)
            return
        try:
            p = self.q(b.fen())
        except RepertoireDeadline:
            self.close_line(path, b)   # time up: keep the path built so far
            return
        if not p:
            self.close_line(path, b)
            return
        replies = [m for m in p.get("moveList", []) if m.get("games", 0) >= CAND_MIN_GAMES][:self.breadth]
        if not replies:
            self.close_line(path, b)
            return
        for r in replies:
            if self.queries >= self.budget or len(self.lines) >= LINE_CAP:
                break
            mv = chess.Move.from_uci(r["uci"])
            if mv not in b.legal_moves:
                continue
            san = b.san(mv)
            b2 = b.copy(stack=False)
            b2.push(mv)
            entry = {"uci": r["uci"], "san": san, "games": r.get("games", 0),
                     "own": None, "draw": None}
            self.expand_your(b2, plies_left, path + [entry])

    # ---------- outputs ----------
    def path_text(self, path):
        b = chess.Board(self.root_fen)
        out = []
        for m in path:
            num = f"{b.fullmove_number}." if b.turn == chess.WHITE else f"{b.fullmove_number}..."
            out.append(num + " " + m["san"])
            b.push(chess.Move.from_uci(m["uci"]))
        return " ".join(out)

    def lines_to_pgn(self):
        game = chess.pgn.Game()
        game.headers["Event"] = f"Repertoire - {self.mode} ({self.side})"
        game.headers["White"] = "Repertoire builder" if self.side == "white" else "?"
        game.headers["Black"] = "Repertoire builder" if self.side == "black" else "?"
        if self.root_fen != chess.STARTING_FEN:
            game.setup(chess.Board(self.root_fen))
            game.headers["SetUp"] = "1"
            game.headers["FEN"] = self.root_fen
        for line in self.lines:
            node = game
            b = chess.Board(self.root_fen)
            for m in line["path"]:
                mv = chess.Move.from_uci(m["uci"])
                if mv not in b.legal_moves:
                    break
                nxt = next((v for v in node.variations if v.move == mv), None)
                if nxt is None:
                    nxt = node.add_variation(mv)
                    if m.get("own") is not None:
                        nxt.comment = f"{m['own']}% in {m['games']} games"
                node = nxt
                b.push(mv)
        exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
        return str(game.accept(exporter))

    def build(self):
        b = chess.Board(self.root_fen)
        try:
            if b.turn == (chess.WHITE if self.side == "white" else chess.BLACK):
                self.expand_your(b, self.your_moves, [])
            else:
                self.expand_opp(b, self.your_moves, [])
        except RepertoireDeadline:
            self.progress("time limit - keeping partial repertoire")
        except RepertoireAuthError:
            raise
        out_lines = []
        for line in self.lines:
            out_lines.append({
                "san": self.path_text(line["path"]),
                "moves": [{"uci": m["uci"], "san": m["san"]} for m in line["path"]],
                "own": line["own"],
                "games": line["games"],
                "evalCp": line["evalCp"],
            })
        return {
            "side": self.side,
            "mode": self.mode,
            "rootFen": self.root_fen,
            "lines": out_lines,
            "pgn": self.lines_to_pgn(),
            "stats": {"queries": self.queries, "evals": self.evals, "lines": len(out_lines)},
        }


def build_repertoire(fen, side, mode, your_moves, breadth, use_engine, engine=None, progress=None):
    b = RepertoireBuilder(fen, side, mode, your_moves, breadth, use_engine, engine=engine, progress=progress)
    return b.build()
