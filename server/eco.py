"""Offline ECO opening classification table.

Longest-match by plies from the start position. deliberately partial; unmatched
games stay "Unknown". No network, no engine: classification works on move order only.
"""
import chess
import chess.pgn

# ordered roughly by specificity; longest uci match wins at runtime anyway
ECO_TABLE = [
    # King's pawn openings
    ("e2e4 e7e5 g1f3 b8c6 f1b5", "Ruy Lopez", "C60"),
    ("e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 f1a4", "Ruy Lopez, Closed", "C84"),
    ("e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6 e4e5", "Ruy Lopez, Steinitz", "C62"),
    ("e2e4 e7e5 g1f3 b8c6 f1b5 g8f6", "Ruy Lopez, Berlin", "C65"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4", "Italian Game", "C50"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4 f8c5", "Italian Game", "C53"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 d2d3", "Italian Game, Giuoco Piano", "C50"),
    ("e2e4 e7e5 g1f3 g8f6", "Petrov's Defense", "C42"),
    ("e2e4 e7e5 g1f3 d7d6 d2d4 e5d4 f3d4", "Scotch Game", "C45"),
    ("e2e4 e7e5 d2d4 e5d4 d1d4", "Center Game", "C21"),
    ("e2e4 e7e5 g1f3 f7f6 h1g1", "King's Pawn Opening", "C20"),
    ("e2e4 e7e5 d2d4 e5d4 c2c3", "Danish Gambit", "C21"),
    ("e2e4 e7e5 f2f4", "King's Gambit", "C30"),
    ("e2e4 e7e5 f2f4 e5f4", "King's Gambit Accepted", "C33"),
    ("e2e4 e7e5 f2f4 d8e7", "King's Gambit Declined", "C30"),
    ("e2e4 e7e5 f2f4 e5f4 g1f3 g7g5", "King's Gambit Modern", "C34"),
    ("e2e4 e7e5 b1c3", "Vienna Game", "C25"),
    ("e2e4 e7e5 b1c3 g8f6", "Vienna Game", "C26"),
    ("e2e4 e7e5 b1c3 f8c5", "Vienna Game, Max Lange", "C27"),
    ("e2e4 e7e6 d2d4 d7d5 b1c3", "French Defense", "C15"),
    ("e2e4 e7e6 d2d4 d7d5 e4e5", "French Defense, Advance", "C02"),
    ("e2e4 e7e6 d2d4 d7d5 b1d2", "French Defense, Tarrasch", "C03"),
    ("e2e4 e7e6 d2d4 d7d5 b1c3 b8c6", "French Defense, Winawer", "C15"),
    ("e2e4 e7e6 d2d4 d7d5 e4d5 e6d5", "French Defense, Exchange", "C01"),
    ("e2e4 c7c5", "Sicilian Defense", "B20"),
    ("e2e4 c7c5 b1c3 d7d6 g1f3 g8f6 d2d4 c5d4 d1d4 b8c6", "Sicilian Defense, Classical", "B55"),
    ("e2e4 c7c5 b1c3 g8f6 e4e5", "Sicilian Defense, Rossolimo", "B23"),
    ("e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3", "Sicilian Defense", "B52"),
    ("e2e4 c7c5 g1f3 b8c6 d2d4 c5d4 f3d4", "Sicilian Defense, Open", "B50"),
    ("e2e4 c7c5 g1f3 e7e6 d2d4 c5d4 f3d4", "Sicilian Defense, Scheveningen", "B80"),
    ("e2e4 c7c5 g1f3 e7e6 b1c3 d7d6", "Sicilian Defense, Najdorf", "B90"),
    ("e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 d1d4 c8e6", "Sicilian Defense, Sveshnikov", "B33"),
    ("e2e4 c7c5 c2c3 d7d6 d2d4", "Sicilian Defense, Alapin", "B22"),
    ("e2e4 c7c6 d2d4 d7d5 d1d3", "Caro-Kann Defense", "B10"),
    ("e2e4 c7c6 d2d4 d7d5 e4e5", "Caro-Kann Defense, Advance", "B12"),
    ("e2e4 c7c6 d2d4 d7d5 b1d2", "Caro-Kann Defense", "B12"),
    ("e2e4 c7c6 d2d4 d7d5 e4d5 c6d5 b1c3", "Caro-Kann Defense, Exchange", "B13"),
    ("e2e4 c7c6 d2d4 d7d5 e4d5 c6d5", "Caro-Kann Defense, Exchange", "B13"),
    ("e2e4 c7c6 d2d4 d7d5 e4d5 c6d5", "Caro-Kann Defense", "B12"),
    ("e2e4 g8f6", "Alekhine Defense", "B02"),
    ("e2e4 g7g6 d2d4 f8g7 b1c3 d7d6", "Modern Defense", "B06"),
    ("e2e4 g7g6 f1d3 f8g7 g1f3", "Modern Defense", "B06"),
    ("e2e4 d7d6 d2d4 g8f6 b1c3 g7g6", "Pirc Defense", "B07"),
    ("e2e4 d7d6 d2d4 g8f6 c2c3", "Pirc Defense", "B07"),
    ("e2e4 c7c5 g1f3", "Sicilian Defense", "B20"),
    # Queen's pawn openings
    ("d2d4 d7d5 c2c4", "Queen's Gambit", "D06"),
    ("d2d4 d7d5 c2c4 e7e6", "Queen's Gambit Declined", "D30"),
    ("d2d4 d7d5 c2c4 c7c6", "Slav Defense", "D10"),
    ("d2d4 d7d5 c2c4 d5c4", "Queen's Gambit Accepted", "D20"),
    ("d2d4 d7d5 c2c4 g8f6 b1c3", "Queen's Gambit Declined", "D30"),
    ("d2d4 d7d5 e2e3 c8f5", "London System", "D02"),
    ("d2d4 d7d5 g1f3 g8f6 c1g5", "Queen's Gambit", "D06"),
    ("d2d4 g8f6 c2c4 e7e6 b1c3 f8b4", "Nimzo-Indian Defense", "E25"),
    ("d2d4 g8f6 c2c4 e7e6 b1c3 b8c6", "Nimzo-Indian Defense", "E30"),
    ("d2d4 g8f6 g1f3 g7g6 f1g2 f8g7", "King's Indian Defense", "E60"),
    ("d2d4 g8f6 c2c4 g7g6 b1c3 d7d6", "King's Indian Defense", "E70"),
    ("d2d4 g8f6 c2c4 g7g6 g1f3 f8g7 g2g3", "King's Indian Defense", "E60"),
    ("d2d4 g8f6 g1f3 d7d5", "Neo-Indian Defense", "D02"),
    ("d2d4 d7d5 g1f3 c8g4 c1f4", "London System", "D02"),
    ("d2d4 d8d5 e2e3 c8f5", "London System", "D02"),
    ("d2d4 f7f5", "Dutch Defense", "A80"),
    ("d2d4 g8f6 c2c4 c7c5 d4d5", "Benoni Defense", "A60"),
    ("d2d4 d7d5 g1f3", "Queen's Pawn Game", "D00"),
    ("d2d4 g8f6", "Indian Game", "A40"),
    ("d2d4 d7d6 g1f3 g8f6 c1g5 e7e5", "Philidor's Defense", "C41"),
    # Flank openings
    ("c2c4 e7e5", "English Opening", "A20"),
    ("c2c4 g8f6 b1c3", "English Opening", "A15"),
    ("c2c4 c7c6", "English Opening", "A12"),
    ("g1f3 d7d5", "Reti Opening", "A04"),
    ("g1f3", "Reti Opening", "A04"),
    ("f2f4", "Bird's Opening", "A02"),
    ("b2b4", "Polish Opening", "A00"),
    # Benoni / Leningrad
    ("d2d4 g8f6 c2c4 c7c6", "Slav Defense", "D10"),
]

_COMPILED = [(tuple(seq.split()), name, eco) for seq, name, eco in ECO_TABLE]


def classify(moves_uci):
    """Returns (opening_name, eco). Longest matching prefix wins."""
    if not moves_uci:
        return ("Unknown", "")
    uci = tuple(moves_uci)
    best_len = 0
    best_name = "Unknown"
    best_eco = ""
    for seq, name, eco in _COMPILED:
        if len(seq) <= len(uci) and uci[:len(seq)] == seq and len(seq) > best_len:
            best_len = len(seq)
            best_name = name
            best_eco = eco
    return (best_name, best_eco)


def review_opening(uci_moves):
    return classify(uci_moves)
