"""Accuracy + Expected Points model (CAPS2-style approximation).

Mathematical model (an approximation of chess.com's CAPS2 principles, NOT the
undisclosed exact formula):

1. Expected Points (EP, 0..1) from the mover-perspective evaluation:
     EP(cp) = 1 / (1 + e^(-0.00368208 * cp))       # the published logistic
     forced mate for the mover -> 1.0, against -> 0.0
2. Expected Points Lost per move:
     EPL = max(0, EP(before, best move) - EP(after, played move))
3. Per-move accuracy is a weighted geometric mean of two penalised curves:
     acc_EPL = 100 * e^(-2.8  * EPL)        # outcome damage
     acc_CPL = 100 * e^(-CPL / 110)         # centipawn loss (CPL capped 1200)
     acc     = acc_EPL^0.55 * acc_CPL^0.45  # 0.55 outcome / 0.45 material
   The CPL component keeps bad moves in already-lost positions honest (the
   logistic flattens there, so EP-loss alone would let shuffling score ~100).
4. Game accuracy = plain arithmetic mean of the per-move accuracies.
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
EPL_STEEPNESS = 2.8
CPL_SCALE = 110.0
W_EPL = 0.55
W_CPL = 0.45
HOPELESS_MATE_CAP = 70.0


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
    """Game accuracy = plain arithmetic mean of per-move accuracies.

    (Matches chess.com's aggregation: a blunder lowers the average like any
    other move - it does not dominate it, and no artificial floor/ceiling is
    applied. The honesty comes from the per-move curve above.)"""
    accs = [a for _drop, a in drops_and_accs]
    if not accs:
        return 100.0
    return round(sum(accs) / len(accs), 1)


def est_rating(accuracy: float) -> int:
    """Rough 'you played like a ...' mapping from game accuracy."""
    table = [
        (99.0, 2900), (98.0, 2700), (97.0, 2500), (96.0, 2350), (95.0, 2250),
        (93.0, 2100), (91.0, 2000), (89.0, 1900), (86.0, 1800), (83.0, 1700),
        (80.0, 1600), (77.0, 1500), (74.0, 1400), (70.0, 1300), (65.0, 1200),
        (60.0, 1100), (50.0, 950), (0.0, 800),
    ]
    prev_acc, prev_elo = table[0]
    if accuracy >= prev_acc:
        return prev_elo
    for acc, elo in table[1:]:
        if accuracy >= acc:
            f = (accuracy - acc) / (prev_acc - acc)
            return int(prev_elo + f * (elo - prev_elo))
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
