"""Game review pipeline: engine analysis + chess.com-style move classification,
accuracy, phases, key moments, time report and coach comments."""
import io
import re
from datetime import datetime, timezone

import chess
import chess.pgn

try:
    from server import accuracy as A
    from server import openings
except ImportError:
    import accuracy as A  # type: ignore
    import openings  # type: ignore

MATE_CP = 100000
PIECE_VAL = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
PIECE_NAME = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
              chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}
CLK_RE = re.compile(r"\[%clk\s+(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)\]")


# ---------------------------------------------------------------- helpers
def material(board, color):
    return sum(PIECE_VAL[p.piece_type] for p in board.piece_map().values() if p.color == color)


def npm_total(board):
    return sum(PIECE_VAL[p.piece_type] for p in board.piece_map().values()
               if p.piece_type not in (chess.PAWN, chess.KING))


def clamp_cp(cp):
    return max(-3000, min(3000, cp))


def fmt_eval(cp_white, mate_white=None):
    if mate_white is not None:
        n = max(1, (abs(mate_white) + 1) // 2)
        return f"M{n}" if mate_white > 0 else f"-M{n}"
    v = cp_white / 100.0
    return f"{v:+.2f}" if v != 0 else "0.00"


def uci_pv_to_san(board, pv, limit=10):
    b = board.copy(stack=False)
    out = []
    for u in pv[:limit]:
        try:
            m = chess.Move.from_uci(u)
            if m not in b.legal_moves:
                break
            out.append(b.san(m))
            b.push(m)
        except Exception:
            break
    return out


def parse_clock(comment):
    if not comment:
        return None
    m = CLK_RE.search(comment)
    if not m:
        return None
    h, mn, s = m.group(1), m.group(2), m.group(3)
    return int(h) * 3600 + int(mn) * 60 + float(s)


# ---------------------------------------------------------------- analysis
def analyse_position(engine, board, depth, multipv, movetime=None):
    """Engine analysis of one position. Returns mover-pov dict."""
    if not any(board.legal_moves):
        if board.is_checkmate():
            return {"cp": -(MATE_CP - 1), "mate": -1, "lines": []}
        return {"cp": 0, "mate": None, "lines": []}  # stalemate / draw
    eff_depth = min(depth, 3) if engine.id == "builtin" else depth
    eff_mt = None if engine.id == "builtin" else movetime
    lines = engine.analyse(board.fen(), depth=eff_depth, multipv=multipv, timeout=180, movetime=eff_mt)
    top = lines[0]
    return {"cp": top["cp"], "mate": top.get("mate"), "lines": lines}


def detect_sacrifice(board_before, move, next_pos):
    """Net material (mover minus opponent, in pawns) the mover gives up across the
    opponent's best reply and the mover's expected follow-up.

    Measuring the *balance* (not just the mover's pieces) is essential: an even
    trade like Bxc6 dxc6 must return ~0, otherwise every normal capture looks
    like a 3-point sacrifice."""
    mover = board_before.turn

    def balance(bd):
        return material(bd, mover) - material(bd, not mover)

    b = board_before.copy(stack=False)
    b.push(move)
    pv = next_pos.get("lines") or []
    if pv:
        try:
            first = chess.Move.from_uci(pv[0]["pv"][0])
            if first in b.legal_moves:
                b.push(first)
                if len(pv[0]["pv"]) > 1:
                    second = chess.Move.from_uci(pv[0]["pv"][1])
                    if second in b.legal_moves:
                        b.push(second)
        except Exception:
            pass
    return balance(board_before) - balance(b)


def classify_move(move, board_before, pos, next_pos, legal_count, is_book):
    """Full classification for one played move. pos/next_pos are mover-pov dicts.

    Returns (cls, loss, epl, drop, win_b, win_a, best_uci): centipawn loss,
    expected-points loss, and the EP-based win% delta (0..100 scale)."""
    played_uci = move.uci()
    best_uci = (pos["lines"][0]["pv"][0]
                if pos["lines"] and pos["lines"][0]["pv"] else played_uci)
    played_cp = -next_pos["cp"]
    best_cp = pos["cp"]
    # Reduce shallow-search noise: when the played move was among the pre-move
    # MultiPV lines, blend that in-search eval into the fresh post-move eval
    # (two estimates of the same position -> more stable win% deltas, closer to
    # chess.com's deep-eval consistency).
    for ln in pos.get("lines") or []:
        if ln.get("pv") and ln["pv"][0] == played_uci:
            played_cp = (played_cp + ln["cp"]) // 2
            break
    loss = max(0, best_cp - played_cp)
    if played_cp > best_cp:
        loss = 0
    # Expected Points model: before = best line, after = the played move.
    # All values mover-pov: pos/next_pos cp+mate are the perspectives of whoever
    # is to move in those positions; the played move's own expected score after
    # the opponent's best reply is -next_cp / -next_mate.
    ep_before = A.expected_points(best_cp, pos.get("mate"))
    mate_after = (-next_pos["mate"]) if next_pos.get("mate") is not None else None
    ep_after = A.expected_points(played_cp, mate_after)
    epl = max(0.0, min(1.0, ep_before - ep_after))
    win_b = ep_before * 100.0
    win_a = ep_after * 100.0
    drop = max(0.0, win_b - win_a)
    second_cp = pos["lines"][1]["cp"] if len(pos["lines"]) > 1 else None
    # forced mate AGAINST the mover already on the board (delaying is not perfect)
    hopeless_mate = pos.get("mate") is not None and pos["mate"] < 0

    if legal_count == 1:
        return "forced", loss, epl, drop, win_b, win_a, best_uci
    if is_book:
        return "book", 0.0, 0.0, 0.0, win_b, win_a, best_uci

    played_mate = played_cp > MATE_CP - 200
    best_mate = best_cp > MATE_CP - 200
    mated_now = played_cp < -(MATE_CP - 200)

    # Mate handling: a mate is encoded as MATE_CP - distance, which squeezes the
    # whole "mate in N" spectrum into ~0 centipawns — a mate in 2 looked like a
    # 1cp slip next to a mate in 1 and was graded "best". Compare the actual
    # distances: only a mate AS FAST as the best line's mate (or faster than
    # what the engine had seen) earns "best"; a slower mating move keeps the
    # win but is not the engine's top choice.
    best_mate_in = pos["mate"] if (best_mate and (pos["mate"] or 0) > 0) else None
    played_mate_in = None
    if played_mate:
        # after the move the opponent is to move: their mate score magnitudes
        # count the remaining mating moves; +1 because the played move itself
        # is one of the mating moves. A delivered mate shows up with mate=None
        # on the finished board (distance 0 -> same +1 = mate in 1, the fastest
        # any move can be)
        rem = (-next_pos["mate"] if (next_pos["mate"] or 0) < 0 else 0)
        played_mate_in = rem + 1

    if played_uci == best_uci:
        is_best = True
    elif played_mate:
        is_best = not best_mate or played_mate_in <= best_mate_in
    elif best_mate:
        is_best = False   # had a forced mate and played something else
    else:
        is_best = loss <= 12

    if played_mate and is_best:
        return "best", loss, epl, drop, win_b, win_a, best_uci

    # Miss: had a forced win / huge advantage and threw it away
    if best_mate and not played_mate and win_a < 75:
        return "miss", loss, epl, drop, win_b, win_a, best_uci
    if win_b >= 88 and win_a <= 62 and (win_b - win_a) >= 22:
        return "miss", loss, epl, drop, win_b, win_a, best_uci

    if is_best and not mated_now:
        sac = detect_sacrifice(board_before, move, next_pos)
        second_win = A.win_percent(clamp_cp(second_cp)) if second_cp is not None else None
        if sac >= 2 and win_a >= 38 and win_a >= win_b - 10 and (
                second_win is None or second_win <= win_b - 12 or played_uci == best_uci):
            return "brilliant", loss, epl, drop, win_b, win_a, best_uci
        if loss <= 25 and second_win is not None and second_win <= win_b - 15 and win_a >= win_b - 8:
            return "great", loss, epl, drop, win_b, win_a, best_uci

    # demotion: losing a clearly winning position is at least a mistake
    if win_b >= 85 and win_a <= 55 and epl < A.EPL_MISTAKE:
        return "mistake", loss, epl, drop, win_b, win_a, best_uci

    # ordinary ladder on expected-points loss (chess.com's public thresholds)
    if epl <= A.EPL_EXCELLENT:
        return "best" if is_best else "excellent", loss, epl, drop, win_b, win_a, best_uci
    if epl <= A.EPL_GOOD:
        return "good", loss, epl, drop, win_b, win_a, best_uci
    if epl <= A.EPL_INACCURACY:
        return "inaccuracy", loss, epl, drop, win_b, win_a, best_uci
    if epl <= A.EPL_MISTAKE:
        return "mistake", loss, epl, drop, win_b, win_a, best_uci
    return "blunder", loss, epl, drop, win_b, win_a, best_uci


def coach_comment(cls, board_before, move, pos, next_pos, win_b, win_a, best_san, cp_before_w, cp_after_w, sac=None):
    mover = "White" if board_before.turn == chess.WHITE else "Black"
    eval_txt = f"({fmt_eval(cp_before_w, mate_white_for(pos, board_before))} → {fmt_eval(cp_after_w, mate_white_for(next_pos, board_before.turn == chess.WHITE and True or False))})"
    opp_mate = next_pos["mate"] is not None and next_pos["mate"] > 0  # opponent (to move) has mate
    if cls == "brilliant":
        return f"A brilliant sacrifice — the attack compensates the material. {eval_txt}"
    if cls == "great":
        return f"Practically the only good move here; everything else lets the advantage slip. {eval_txt}"
    if cls == "miss":
        if pos["mate"] is not None and pos["mate"] > 0:
            n = max(1, pos["mate"] // 2)
            return f"Missed a forced mate in {n}! {best_san} was the move. {eval_txt}"
        return f"Missed a winning chance — {best_san} kept the win. {eval_txt}"
    if cls == "blunder":
        if opp_mate:
            n = max(1, next_pos["mate"] // 2)
            return f"Allows mate in {n}. {best_san} was necessary. {eval_txt}"
        lost = detect_sacrifice(board_before, move, next_pos)
        if lost >= 2:
            piece = PIECE_NAME.get(board_before.piece_type_at(move.to_square) and board_before.piece_type_at(move.from_square), "material")
            return f"Loses material — the position collapses from {fmt_eval(cp_before_w)} to {fmt_eval(cp_after_w)}."
        return f"A serious error: the evaluation swings from {fmt_eval(cp_before_w)} to {fmt_eval(cp_after_w)}. Better was {best_san}."
    if cls == "mistake":
        return f"Better was {best_san} — the advantage goes from {fmt_eval(cp_before_w)} to {fmt_eval(cp_after_w)}."
    if cls == "inaccuracy":
        return f"Slightly imprecise. {best_san} was more testing. {eval_txt}"
    if cls == "best":
        return f"The engine's top choice. {eval_txt}"
    if cls == "excellent":
        return f"An excellent move, keeping everything under control. {eval_txt}"
    if cls == "good":
        return f"A perfectly reasonable move. {eval_txt}"
    if cls == "book":
        return "A well-known opening move."
    if cls == "forced":
        return "Forced — there was nothing else."
    return ""


def mate_white_for(pos, white_to_move):
    """White-pov mate plies for a position record (mover-pov stored)."""
    if pos["mate"] is None:
        return None
    return pos["mate"] if white_to_move else -pos["mate"]


# ---------------------------------------------------------------- phases & reports
def compute_phases(boards, book_flags):
    n = len(boards)  # boards = positions 0..N (N moves => N+1 positions)
    book_end = 0
    for i, f in enumerate(book_flags):
        if f:
            book_end = i + 1
    opening_end = min(30, max(20, book_end + 2))
    endgame_start = None
    for i in range(max(10, opening_end), n):
        if npm_total(boards[i]) <= 13:
            endgame_start = i
            break
    if endgame_start is None and n >= 16:
        # long game that never reached a sparse material endgame: treat the final
        # quarter as the endgame phase so all three phases always report
        endgame_start = (n * 3) // 4
    phases = []
    for i in range(n):
        if i < opening_end:
            phases.append("opening")
        elif endgame_start is not None and i >= endgame_start:
            phases.append("endgame")
        else:
            phases.append("middlegame")
    return phases


def build_time_report(moves, initial_clock):
    have = [m for m in moves if m.get("clock") is not None]
    if not have or len(have) < 0.6 * len(moves):
        return None
    thirds = [[], [], []]
    n = len(moves)
    for i, m in enumerate(moves):
        thirds[min(2, i * 3 // n)].append(m)
    buckets = {"fast": [], "normal": [], "slow": []}
    for m in moves:
        if m.get("timeSpent") is None:
            continue
        if m["timeSpent"] < 5:
            buckets["fast"].append(m)
        elif m["timeSpent"] <= 20:
            buckets["normal"].append(m)
        else:
            buckets["slow"].append(m)

    def acc(ms):
        ds = [(m["drop"], m["accuracy"]) for m in ms if m["class"] not in ("book",)]
        return A.game_accuracy(ds) if ds else None

    def avg_time(ms):
        ts = [m["timeSpent"] for m in ms if m.get("timeSpent") is not None]
        return sum(ts) / len(ts) if ts else None

    tt_errors = [m for m in moves if m.get("timeSpent") is not None and initial_clock
                 and m["timeSpent"] < 0.12 * initial_clock and m["class"] in ("blunder", "mistake", "miss")]
    return {
        "phase": {
            name: {"plies": len(ms), "avgTime": avg_time(ms), "accuracy": acc(ms)}
            for name, ms in zip(("early", "middle", "late"), thirds)
        },
        "bySpeed": {
            name: {"moves": len(ms), "accuracy": acc(ms), "avgTime": avg_time(ms),
                   "blunders": sum(1 for m in ms if m["class"] in ("blunder", "miss"))}
            for name, ms in buckets.items()
        },
        "timeTroubleErrors": len(tt_errors),
        "hasClock": True,
    }


def summarize(name, players, key_moments, time_report, moves):
    bullets = []
    for color, label in (("white", "White"), ("black", "Black")):
        p = players[color]
        bullets.append(f"{label} ({p['name']}): {p['accuracy']}% accuracy — played like a {p['estRating']}-rated player.")
    worst = min(("opening", "middlegame", "endgame"),
                key=lambda ph: min(players["white"]["phases"][ph]["accuracy"] or 100,
                                   players["black"]["phases"][ph]["accuracy"] or 100))
    blunders = players["white"]["counts"]["blunder"] + players["black"]["counts"]["blunder"]
    brilliants = players["white"]["counts"]["brilliant"] + players["black"]["counts"]["brilliant"]
    if blunders:
        txt = f"{blunders} blunder(s) decided the game — review the key moments below."
        if time_report and time_report.get("timeTroubleErrors"):
            txt += f" {time_report['timeTroubleErrors']} of them came in time trouble."
        bullets.append(txt)
    if brilliants:
        bullets.append(f"{brilliants} brilliant move(s) were played — click the teal highlights to see them.")
    non_book = [m for m in moves if m["class"] != "book"]
    best_rate = round(100 * sum(1 for m in non_book if m["class"] == "best") / len(non_book), 1) if non_book else 0
    bullets.append(f"The engine's top choice was played {best_rate}% of the time.")
    bullets.append(f"Weakest phase: {worst} — focus your study there.")
    return {"bullets": bullets}


# ---------------------------------------------------------------- main pipeline
def run_review(pgn_text, engine, depth=12, multipv=3, movetime=900, progress=None):
    def cb(msg):
        if progress:
            progress(msg)

    # replay the whole stream so a PGN with several games is detected instead of
    # silently truncated to the first (python-chess errors=[] either way)
    stream = io.StringIO(pgn_text)
    games = []
    while True:
        g = chess.pgn.read_game(stream)
        if g is None:
            break
        games.append(g)
    game = games[0] if games else None
    pgn_warnings = []
    if len(games) > 1:
        first_name = (games[0].headers.get("Event", "game 1") or "game 1")
        pgn_warnings.append(f"Your PGN contains {len(games)} games - only the first "
                           f"(\"{first_name}\") was reviewed.")
    if game is None:
        raise ValueError("Could not parse PGN. Make sure it contains at least the moves.")
    if game.errors:
        pgn_warnings.append(f"{len(game.errors)} move(s) could not be parsed - they were skipped.")
    board = game.board()
    moves, boards, clocks = [], [board.copy(stack=False)], []
    node = game
    while node.variations:
        node = node.variations[0]
        mv = node.move
        moves.append(mv)
        clocks.append(parse_clock(getattr(node, "comment", "")))
        b = boards[-1].copy(stack=False)
        b.push(mv)
        boards.append(b)
    n = len(moves)
    if n == 0:
        raise ValueError("The PGN contains no moves.")

    headers = {k: game.headers.get(k, "") for k in
               ("White", "Black", "Result", "Date", "Event", "Site", "TimeControl", "WhiteElo", "BlackElo")}

    cb("book")
    uci_moves = [m.uci() for m in moves]
    book_flags, opening = openings.walk_game(uci_moves, board, progress=lambda m: cb("book " + m))
    if opening is None:
        opening = {"eco": "", "name": "Unknown"}

    cb("engine")
    pos_records = []
    for i, b in enumerate(boards):
        cb(f"engine {i}/{n}")
        rec = analyse_position(engine, b, depth, multipv, movetime)
        white_to_move = b.turn == chess.WHITE
        cp_white = rec["cp"] if white_to_move else -rec["cp"]
        mate_white = mate_white_for(rec, white_to_move)
        lines_san = []
        for ln in rec["lines"][:3]:
            line_mate = ln.get("mate")
            lines_san.append({
                "cp": ln["cp"], "mate": line_mate,
                # per-line white-perspective evals (the UI reads these directly;
                # mover-pov would render as NaN for black-to-move positions)
                "cpWhite": ln["cp"] if white_to_move else -ln["cp"],
                "mateWhite": (line_mate * (1 if white_to_move else -1)) if line_mate is not None else None,
                "pv": ln["pv"][:10],
                "san": uci_pv_to_san(b, ln["pv"], 10),
            })
        pos_records.append({
            "fen": b.fen(),
            "cpWhite": cp_white,
            "mateWhite": mate_white,
            "lines": lines_san,
        })
        rec["_mover_cp"] = rec["cp"]  # keep mover-pov for classification

    cb("classify")
    moves_out = []
    per_color = {"white": [], "black": []}
    prev_clock = {"white": None, "black": None}
    initial_clock = max([c for c in clocks if c], default=None)
    for i, mv in enumerate(moves):
        b = boards[i]
        legal_count = len(list(b.legal_moves))
        pos = {"cp": pos_records[i]["lines"][0]["cp"] if pos_records[i]["lines"] else
               (-(MATE_CP - 1) if b.is_checkmate() else 0),
               "mate": pos_records[i]["lines"][0].get("mate") if pos_records[i]["lines"] else None,
               "lines": [{"cp": l["cp"], "mate": l["mate"], "pv": l["pv"]} for l in pos_records[i]["lines"]]}
        if not any(b.legal_moves):
            pos = {"cp": -(MATE_CP - 1) if b.is_checkmate() else 0, "mate": None, "lines": []}
        nxt = {"cp": (pos_records[i + 1]["lines"][0]["cp"] if pos_records[i + 1]["lines"] else
                      (-(MATE_CP - 1) if boards[i + 1].is_checkmate() else 0)),
               "mate": pos_records[i + 1]["lines"][0].get("mate") if pos_records[i + 1]["lines"] else None,
               "lines": [{"cp": l["cp"], "mate": l["mate"], "pv": l["pv"]} for l in pos_records[i + 1]["lines"]]}
        if not any(boards[i + 1].legal_moves):
            nxt = {"cp": -(MATE_CP - 1) if boards[i + 1].is_checkmate() else 0, "mate": None, "lines": []}

        cls, loss, epl, drop, win_b, win_a, best_uci = classify_move(mv, b, pos, nxt, legal_count, book_flags[i])
        # per-move accuracy: composite of expected-points loss + centipawn loss
        # (the EP logistic flattens in lost positions, so CPL keeps bad moves
        # there honest); hopeless-mate cap = delaying checkmate is not perfect play
        hopeless_mate = pos.get("mate") is not None and pos.get("mate", 0) < 0
        acc = A.move_accuracy(epl, loss, hopeless_mate=hopeless_mate)
        best_move_obj = chess.Move.from_uci(best_uci) if best_uci in [m.uci() for m in b.legal_moves] else None
        best_san = b.san(best_move_obj) if best_move_obj else "—"
        cp_before_w = pos_records[i]["cpWhite"]
        cp_after_w = pos_records[i + 1]["cpWhite"]
        color = "white" if b.turn == chess.WHITE else "black"
        clock = clocks[i]
        time_spent = None
        if clock is not None and prev_clock[color] is not None:
            time_spent = max(0.0, prev_clock[color] - clock)
        if clock is not None:
            prev_clock[color] = clock
        elif prev_clock[color] is None:
            prev_clock[color] = initial_clock

        comment = coach_comment(cls, b, mv, pos, nxt, win_b, win_a, best_san, cp_before_w, cp_after_w)
        rec = {
            "ply": i + 1,
            "moveNumber": i // 2 + 1,
            "color": color,
            "san": b.san(mv),
            "uci": mv.uci(),
            "class": cls,
            "label": A.CLASS_META[cls]["label"],
            "symbol": A.CLASS_META[cls]["symbol"],
            "cpBefore": cp_before_w,
            "cpAfter": cp_after_w,
            "winBefore": round(win_b, 2),
            "winAfter": round(win_a, 2),
            "accuracy": round(acc, 1),
            "drop": round(drop, 2),
            "loss": round(loss, 0),
            "epl": round(epl, 3),
            "comment": comment,
            "best": {"uci": best_uci, "san": best_san},
            "clock": clock,
            "timeSpent": time_spent,
        }
        moves_out.append(rec)
        # one entry per ply for BOTH colors keeps zip(per_color[color], phases)
        # aligned: entry j is ply j's record (None for the other color / book)
        for c in ("white", "black"):
            per_color[c].append(rec if (c == color and cls != "book") else None)

    # phases & player stats
    phases = compute_phases(boards, book_flags)
    players = {}
    for color in ("white", "black"):
        recs = [r for r in per_color[color] if r]
        counts = {k: 0 for k in A.CLASS_META}
        for r in recs:
            counts[r["class"]] += 1
        drops_accs = [(r["drop"], r["accuracy"]) for r in recs]
        acc = A.game_accuracy(drops_accs)
        cpls = [min(r["loss"], 1000) for r in recs]
        phase_stats = {}
        for ph in A.PHASES:
            pr = [r for r, p in zip(per_color[color], phases) if r and p == ph]
            phase_stats[ph] = {
                "plies": len(pr),
                "accuracy": A.game_accuracy([(r["drop"], r["accuracy"]) for r in pr]) if pr else None,
                "acpl": round(sum(min(r["loss"], 1000) for r in pr) / len(pr), 1) if pr else None,
            }
        players[color] = {
            "name": headers["White"] if color == "white" else headers["Black"],
            "accuracy": acc,
            "acpl": round(sum(cpls) / len(cpls), 1) if cpls else 0,
            "counts": counts,
            "estRating": A.est_rating(acc),
            "phases": phase_stats,
        }

    # key moments
    key_moments = []
    biggest_swing, swing_ply = 0, None
    for i, r in enumerate(moves_out):
        if r["class"] in ("brilliant", "great", "miss", "blunder"):
            key_moments.append({
                "ply": r["ply"], "kind": r["class"], "color": r["color"],
                "title": f"{r['color'].capitalize()}'s {r['label']}",
                "text": f"{r['moveNumber']}.{'..' if r['color'] == 'black' else ''} {r['san']} — {r['label']}",
            })
        swing = abs(r["cpAfter"] - r["cpBefore"])
        if swing > biggest_swing and swing >= 250:
            biggest_swing, swing_ply = swing, r["ply"]
    if swing_ply and not any(k["ply"] == swing_ply for k in key_moments):
        r = moves_out[swing_ply - 1]
        key_moments.append({"ply": swing_ply, "kind": "turning", "color": r["color"],
                            "title": "Turning point", "text": f"{r['moveNumber']}.{'..' if r['color'] == 'black' else ''} {r['san']} — the evaluation swings here"})

    time_report = build_time_report(moves_out, initial_clock)
    summary = summarize("", players, key_moments, time_report, moves_out)

    # annotated PGN
    game.headers["Event"] = game.headers.get("Event", "Game Review")
    game.headers["Annotator"] = f"{getattr(engine, 'name', 'engine')} depth {depth} movetime {movetime}ms"
    node = game
    idx = 0
    while node.variations:
        node = node.variations[0]
        r = moves_out[idx]
        meta = A.CLASS_META[r["class"]]
        if meta["nag"]:
            node.nags.add(int(meta["nag"][1:]))
        cls_txt = f"{meta['label']}{' ' + meta['symbol'] if meta['symbol'] else ''}"
        eval_b = fmt_eval(r["cpBefore"], pos_records[idx]["mateWhite"])
        eval_a = fmt_eval(r["cpAfter"], pos_records[idx + 1]["mateWhite"])
        # keep [%clk]/[%eval] tags and anything the user wrote in the original
        # comment (verified destruction bug: the old code overwrote node.comment)
        orig = (node.comment or "").strip()
        keep = ""
        if orig:
            keep_parts = []
            pclk = CLK_RE.search(orig)
            if pclk:
                keep_parts.append(pclk.group(0))
            plain = CLK_RE.sub("", orig)
            plain = re.sub(r"\[%[^\]]*\]", "", plain).replace("{", "").replace("}", "").strip()
            if plain:
                keep_parts.insert(0, plain)
            keep = " ".join(keep_parts)
        node.comment = f"[{cls_txt}] {r['comment']} (eval {eval_b} → {eval_a})" + (f" | {keep}" if keep else "")
        idx += 1
    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=True)
    annotated_pgn = game.accept(exporter)

    graph = [{"ply": 0, "y": pos_records[0]["cpWhite"], "mate": pos_records[0]["mateWhite"]}]
    for i, r in enumerate(moves_out):
        graph.append({"ply": r["ply"], "y": pos_records[i + 1]["cpWhite"], "mate": pos_records[i + 1]["mateWhite"]})

    return {
        "game": {
            **headers,
            "opening": opening.get("name", "Unknown"),
            "eco": opening.get("eco", ""),
            "plyCount": n,
            "initialClock": initial_clock,
            "finalFen": boards[-1].fen(),
        },
        "players": players,
        "positions": pos_records,
        "moves": moves_out,
        "bookFlags": book_flags,
        "phases": phases,
        "keyMoments": key_moments,
        "timeReport": time_report,
        "summary": summary,
        "graph": graph,
        "annotatedPgn": annotated_pgn,
        "pgnWarnings": pgn_warnings,
        "analysis": {"engine": getattr(engine, "name", "?"), "depth": depth,
                     "multipv": multipv, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    }
