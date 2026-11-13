"""Accuracy + Expected Points model (CAPS2-style, deliberately stricter).

Mathematical model (an approximation of chess.com's CAPS2 principles, NOT the
undisclosed exact formula, recalibrated v82 to punish mistakes harder):

1. Expected Points (EP, 0..1) from the mover-perspective evaluation:
     EP(cp) = 1 / (1 + e^(-0.00368208 * cp))       # the published logistic
     forced mate for the mover -> 1.0, against -> 0.0
2. Expected Points Lost per move:
     EPL = max(0, EP(before, best move) - EP(after, played move))
3. Per-move accuracy is a weighted geometric mean of two penalised curves:
     acc_EPL = 100 * e^(-4.2  * EPL)        # outcome damage (steepened v82)
     acc_CPL = 100 * e^(-CPL / 70)          # centipawn loss (tightened v82; cap 1200)
     acc     = acc_EPL^0.55 * acc_CPL^0.45  # 0.55 outcome / 0.45 material
   The CPL component keeps bad moves in already-lost positions honest (the
   logistic flattens there, so EP-loss alone would let shuffling score ~100).
4. Game accuracy = severity-weighted mean of the per-move accuracies: moves
   that lose more expected points carry proportionally more weight
   (w = 1 + 0.3 * win%-drop, capped at 12), so blunders and mistakes dent the
   score the way a plain average never lets them.
   Classifications use EPL thresholds mirroring chess.com's public table:
     best/excellent <= 0.02, good <= 0.05, inaccuracy <= 0.10,
     mistake <= 0.20, blunder > 0.20
"""
import math

WIN_K = 0.00368208
EP_K = 0.00368208
MATE_CP = 100000

# Expected-points-lost classification thresholds (see module docstring)
EPL_EXCELLENT = 0.02
EPL_GOOD = 0.05
EPL_INACCURACY = 0.10
EPL_MISTAKE = 0.20

# accuracy-from-EPL steepness / CPL softness / blend weights
EPL_STEEPNESS = 4.2
CPL_SCALE = 70.0
W_EPL = 0.55
W_CPL = 0.45
HOPELESS_MATE_CAP = 70.0

# game-aggregation weights: severity multiplier and its cap (see game_accuracy)
AGG_WEIGHT_PER_DROP = 0.3
AGG_WEIGHT_CAP = 12.0


def expected_points(cp: float, mate=None) -> float:
    """Mover-perspective expected game score in [0, 1].

    mate > 0 (mater delivers in N) -> 1.0; mate < 0 (mover is being mated) -> 0.0.
    Otherwise the published logistic. cp is clamped to +-1500 for stability."""
    if mate is not None:
        return 1.0 if mate > 0 else 0.0
    cp = max(-1500.0, min(1500.0, float(cp)))
    return 1.0 / (1.0 + math.exp(-EP_K * cp))


def win_percent(cp: float) -> float:
    """Legacy helper: expected score as a 0..100 percentage."""
    return expected_points(cp) * 100.0


def move_accuracy(epl: float, cpl: float, hopeless_mate: bool = False,
                  mated: bool = False) -> float:
    """Composite per-move accuracy from expected-points loss + centipawn loss."""
    if mated:
        return 0.0
    epl = max(0.0, min(1.0, float(epl)))
    cpl = max(0.0, min(1200.0, float(cpl)))
    acc_epl = 100.0 * math.exp(-EPL_STEEPNESS * epl)
    acc_cpl = 100.0 * math.exp(-cpl / CPL_SCALE)
    acc = (acc_epl ** W_EPL) * (acc_cpl ** W_CPL)
    if hopeless_mate:
        # delaying checkmate in a forced-mate-against position is not perfect play
        acc = min(acc, HOPELESS_MATE_CAP)
    return round(max(0.0, min(100.0, acc)), 1)


def game_accuracy(drops_and_accs) -> float:
    """Game accuracy = severity-weighted mean of the per-move accuracies.

    Each move is weighted by how much expected score it threw away:
    w = 1 + 0.3 * (win%-drop), capped at 12. Blunders and mistakes therefore
    drag the game score down proportionally to their severity instead of
    dissolving into a plain average, while a game with no real errors reduces
    to the plain arithmetic mean. The honesty of the scale comes from the
    per-move curves; the weighting just makes big errors bite."""
    pairs = [(d, a) for d, a in drops_and_accs if isinstance(a, (int, float))]
    if not pairs:
        return 100.0
    total = 0.0
    wsum = 0.0
    for drop, a in pairs:
        d = float(drop) if isinstance(drop, (int, float)) else 0.0
        w = min(AGG_WEIGHT_CAP, 1.0 + AGG_WEIGHT_PER_DROP * max(0.0, d))
        total += w * a
        wsum += w
    return round(total / wsum, 1)


def est_rating(accuracy: float) -> int:
    """Rough 'you played like a ...' mapping from game accuracy.

    v82: keys shifted down to stay honest after the stricter scale (a game
    that used to read ~87 now reads ~80, etc.)."""
    table = [
        (98.0, 2900), (97.0, 2700), (95.5, 2500), (94.0, 2350), (92.5, 2250),
        (89.5, 2100), (86.5, 2000), (84.0, 1900), (80.5, 1800), (76.5, 1700),
        (73.5, 1600), (70.0, 1500), (66.5, 1400), (62.0, 1300), (56.0, 1200),
        (50.0, 1100), (40.0, 950), (0.0, 800),
    ]
    prev_acc, prev_elo = table[0]
    if accuracy >= prev_acc:
        return prev_elo
    for acc, elo in table[1:]:
        if accuracy >= acc:
            f = (accuracy - acc) / (prev_acc - acc)
            # interpolate between this key's elo (f=0) and the key above (f=1);
            # the previous code extrapolated from the wrong end and produced a
            # non-monotonic staircase
            return int(round(elo + f * (prev_elo - elo)))
        prev_acc, prev_elo = acc, elo
    return 800


CLASS_META = {
    "brilliant":   {"label": "Brilliant",   "symbol": "!!", "nag": "$3", "severity": 0},
    "great":       {"label": "Great Move",  "symbol": "!",  "nag": "$1", "severity": 1},
    "best":        {"label": "Best",        "symbol": "★",  "nag": None, "severity": 2},
    "excellent":   {"label": "Excellent",   "symbol": "!",  "nag": None, "severity": 3},
    "good":        {"label": "Good",        "symbol": None, "nag": None, "severity": 4},
    "book":        {"label": "Book",        "symbol": None, "nag": None, "severity": 4},
    "inaccuracy":  {"label": "Inaccuracy",  "symbol": "?!", "nag": "$6", "severity": 5},
    "mistake":     {"label": "Mistake",     "symbol": "?",  "nag": "$2", "severity": 6},
    "miss":        {"label": "Miss",        "symbol": "✕",  "nag": None, "severity": 7},
    "blunder":     {"label": "Blunder",     "symbol": "??", "nag": "$4", "severity": 8},
    "forced":      {"label": "Forced",      "symbol": None, "nag": None, "severity": 4},
}

PHASES = ["opening", "middlegame", "endgame"]
