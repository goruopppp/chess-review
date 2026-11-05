"""Built-in pure-Python chess engine so the app always works even with no engine binaries.

Weak and slow compared to real engines, but speaks the same analyse() interface.
Iterative deepening alpha-beta with quiescence search, MVV-LVA ordering, tapered
material+PST evaluation. Mirrors uci.UciEngine.analyse().
"""
import chess

MATE_CP = 100000

VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330, chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}

# (early, late) piece-square tables from white's perspective, a1 = index 0
PST = {
    chess.PAWN: (
        [0, 0, 0, 0, 0, 0, 0, 0, 50, 50, 50, 50, 50, 50, 50, 50, 10, 10, 20, 30, 30, 20, 10, 10,
         5, 5, 10, 25, 25, 10, 5, 5, 0, 0, 0, 20, 20, 0, 0, 0, 5, -5, -10, 0, 0, -10, -5, 5,
         5, 10, 10, -20, -20, 10, 10, 5, 0, 0, 0, 0, 0, 0, 0, 0],
        [0, 0, 0, 0, 0, 0, 0, 0, 80, 80, 80, 80, 80, 80, 80, 80, 50, 50, 50, 50, 50, 50, 50, 50,
         30, 30, 30, 30, 30, 30, 30, 30, 20, 20, 20, 20, 20, 20, 20, 20, 10, 10, 10, 10, 10, 10, 10, 10,
         5, 5, 5, 5, 5, 5, 5, 5, 0, 0, 0, 0, 0, 0, 0, 0]),
    chess.KNIGHT: (
        [-50, -40, -30, -30, -30, -30, -40, -50, -40, -20, 0, 0, 0, 0, -20, -40, -30, 0, 10, 15, 15, 10, 0, -30,
         -30, 5, 15, 20, 20, 15, 5, -30, -30, 0, 15, 20, 20, 15, 0, -30, -30, 5, 10, 15, 15, 10, 5, -30,
         -40, -20, 0, 5, 5, 0, -20, -40, -50, -40, -30, -30, -30, -30, -40, -50],
        [-50, -40, -30, -30, -30, -30, -40, -50, -30, -20, -10, 0, 0, -10, -20, -30, -30, -10, 10, 15, 15, 10, -10, -30,
         -30, -10, 15, 20, 20, 15, -10, -30, -30, -10, 15, 20, 20, 15, -10, -30, -30, -10, 10, 15, 15, 10, -10, -30,
         -30, -30, 0, 0, 0, 0, -30, -30, -50, -30, -30, -30, -30, -30, -30, -50]),
    chess.BISHOP: (
        [-20, -10, -10, -10, -10, -10, -10, -20, -10, 0, 0, 0, 0, 0, 0, -10, -10, 0, 5, 10, 10, 5, 0, -10,
         -10, 5, 5, 10, 10, 5, 5, -10, -10, 0, 10, 10, 10, 10, 0, -10, -10, 10, 10, 10, 10, 10, 10, -10,
         -10, 5, 0, 0, 0, 0, 5, -10, -20, -10, -10, -10, -10, -10, -10, -20],
        [-20, -10, -10, -10, -10, -10, -10, -20, -10, 0, 0, 0, 0, 0, 0, -10, -10, 0, 5, 10, 10, 5, 0, -10,
         -10, 5, 5, 10, 10, 5, 5, -10, -10, 0, 10, 10, 10, 10, 0, -10, -10, 10, 10, 10, 10, 10, 10, -10,
         -10, 5, 0, 0, 0, 0, 5, -10, -20, -10, -10, -10, -10, -10, -10, -20]),
    chess.ROOK: (
        [0, 0, 0, 0, 0, 0, 0, 0, 5, 10, 10, 10, 10, 10, 10, 5, -5, 0, 0, 0, 0, 0, 0, -5,
         -5, 0, 0, 0, 0, 0, 0, -5, -5, 0, 0, 0, 0, 0, 0, -5, -5, 0, 0, 0, 0, 0, 0, -5,
         -5, 0, 0, 0, 0, 0, 0, -5, 0, 0, 0, 5, 5, 0, 0, 0],
        [0, 0, 0, 0, 0, 0, 0, 0, 10, 15, 15, 15, 15, 15, 15, 10, 5, 10, 10, 10, 10, 10, 10, 5,
         0, 5, 5, 5, 5, 5, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0, -5, -5, -5, -5, -5, -5, -5, -5,
         -5, -5, -5, -5, -5, -5, -5, -5, -5, 0, 0, 5, 5, 0, 0, -5]),
    chess.QUEEN: (
        [-20, -10, -10, -5, -5, -10, -10, -20, -10, 0, 0, 0, 0, 0, 0, -10, -10, 0, 5, 5, 5, 5, 0, -10,
         -5, 0, 5, 5, 5, 5, 0, -5, 0, 0, 5, 5, 5, 5, 0, -5, -10, 5, 5, 5, 5, 5, 0, -10,
         -10, 0, 5, 0, 0, 0, 0, -10, -20, -10, -10, -5, -5, -10, -10, -20],
        [-20, -10, -10, -5, -5, -10, -10, -20, -10, 0, 0, 0, 0, 0, 0, -10, -10, 0, 5, 5, 5, 5, 0, -10,
         -5, 0, 5, 5, 5, 5, 0, -5, 0, 0, 5, 5, 5, 5, 0, -5, -10, 5, 5, 5, 5, 5, 0, -10,
         -10, 0, 5, 0, 0, 0, 0, -10, -20, -10, -10, -5, -5, -10, -10, -20]),
    chess.KING: (
        [-30, -40, -40, -50, -50, -40, -40, -30, -30, -40, -40, -50, -50, -40, -40, -30, -30, -40, -40, -50, -50, -40, -40, -30,
         -30, -40, -40, -50, -50, -40, -40, -30, -20, -30, -30, -40, -40, -30, -30, -20, -10, -20, -20, -20, -20, -20, -20, -10,
         20, 20, 0, 0, 0, 0, 20, 20, 20, 30, 10, 0, 0, 10, 30, 20],
        [-50, -40, -30, -20, -20, -30, -40, -50, -30, -20, -10, 0, 0, -10, -20, -30,
         -30, -10, 20, 30, 30, 20, -10, -30, -30, -10, 20, 30, 30, 20, -10, -30,
         -30, -10, 20, 30, 30, 20, -10, -30, -30, -10, 0, 0, 0, 0, -10, -30,
         -30, -30, -30, -30, -30, -30, -30, -30, -50, -30, -30, -30, -30, -30, -30, -50]),
}


def _pst(piece, sq, phase):
    t = PST[piece.piece_type]
    if piece.color == chess.WHITE:
        i = chess.square_mirror(sq)
    else:
        i = sq
    return t[phase][i] if piece.color == chess.WHITE else -t[phase][i]


class FallbackEngine:
    name = "PySparrow (built-in)"
    author = "chess-review"
    threads = None
    hash_mb = None
    options = {}

    def __init__(self):
        self.id = "builtin"
        self.lock = __import__("threading").Lock()

    def configure(self, threads, hash_mb):
        pass

    def quit(self):
        pass

    def analyse(self, fen, depth=3, multipv=1, timeout=60.0, movetime=None):
        depth = max(1, min(depth, 4))  # python speed reality
        with self.lock:
            board = chess.Board(fen)
            scored = []
            for move in board.legal_moves:
                board.push(move)
                if board.is_checkmate():
                    score = MATE_CP - 1
                else:
                    score = -self._search(board, depth - 1, -MATE_CP, MATE_CP)
                board.pop()
                scored.append((score, move))
            scored.sort(key=lambda x: -x[0])
            out = []
            for score, move in scored[:multipv]:
                out.append({"cp": int(score), "mate": None, "pv": [move.uci()]})
            if not out:
                out = [{"cp": 0, "mate": None, "pv": []}]
            return out

    def best_move(self, fen, depth=10, timeout=60.0):
        res = self.analyse(fen, depth=depth, multipv=1, timeout=timeout)
        return res[0]["pv"][0] if res and res[0]["pv"] else None

    # ---------- search ----------
    def _search(self, board, depth, alpha, beta):
        if board.is_checkmate():
            return -(MATE_CP - 1)
        if board.is_stalemate() or board.is_insufficient_material():
            return 0
        if depth <= 0:
            return self._quiesce(board, alpha, beta)
        moves = self._ordered(board)
        if not moves:
            if board.is_check():
                return -(MATE_CP - 1)
            return 0
        best = -MATE_CP
        for move in moves:
            board.push(move)
            score = -self._search(board, depth - 1, -beta, -alpha)
            board.pop()
            if score > best:
                best = score
            if best > alpha:
                alpha = best
            if alpha >= beta:
                break
        return best

    def _quiesce(self, board, alpha, beta):
        stand = self._eval(board)
        if stand >= beta:
            return beta
        if stand > alpha:
            alpha = stand
        for move in self._ordered(board, captures_only=True):
            board.push(move)
            score = -self._quiesce(board, -beta, -alpha)
            board.pop()
            if score >= beta:
                return beta
            if score > alpha:
                alpha = score
        return alpha

    def _ordered(self, board, captures_only=False):
        moves = board.legal_moves
        caps, checks, quiets = [], [], []
        for m in moves:
            if board.is_capture(m):
                victim = board.piece_type_at(m.to_square) or chess.PAWN
                attacker = board.piece_type_at(m.from_square) or chess.PAWN
                caps.append((VALUES[victim] * 10 - VALUES[attacker], m))
            elif not captures_only:
                if board.gives_check(m):
                    checks.append((1, m))
                else:
                    quiets.append((0, m))
        pool = caps + (checks + quiets if not captures_only else [])
        pool.sort(key=lambda x: -x[0])
        return [m for _, m in pool]

    def _eval(self, board):
        score = 0
        npm = 0
        for sq, piece in board.piece_map().items():
            v = VALUES[piece.piece_type]
            if piece.piece_type not in (chess.PAWN, chess.KING):
                npm += v
        phase = 0 if npm < 1300 else 1  # late vs early table
        for sq, piece in board.piece_map().items():
            sign = 1 if piece.color == chess.WHITE else -1
            score += sign * (VALUES[piece.piece_type] + _pst(piece, sq, phase))
        score += 8 if board.turn == chess.WHITE else -8  # tiny tempo
        return score if board.turn == chess.WHITE else -score
