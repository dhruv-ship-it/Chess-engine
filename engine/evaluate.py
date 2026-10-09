"""Static evaluation.

Material plus piece-square tables, using Tomasz Michniewski's Simplified Evaluation
Function (https://www.chessprogramming.org/Simplified_Evaluation_Function).

The one rule that matters here: `evaluate()` returns a score **relative to the side to
move**, because the search is negamax and negates on the way up. Returning a White-positive
score from a negamax leaf is the bug that broke the predecessor of this engine -- see
ENGINEERING_NOTES.md section 2.1. `evaluate_white()` is available for debugging and is
deliberately not used by the search.
"""

import chess

PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING = range(1, 7)

# Michniewski's piece values, in centipawns.
PIECE_VALUES = {
    PAWN: 100,
    KNIGHT: 320,
    BISHOP: 330,
    ROOK: 500,
    QUEEN: 900,
    KING: 20000,
}

# Piece-square tables, written from White's point of view with a8 first.
# Index 0 is a8, index 7 is h8, index 56 is a1, index 63 is h1.
# Every table is asserted to be exactly 64 entries at import time, because silent row
# duplication in these literals is precisely how the predecessor engine was corrupted.

_PAWN = [
      0,   0,   0,   0,   0,   0,   0,   0,
     50,  50,  50,  50,  50,  50,  50,  50,
     10,  10,  20,  30,  30,  20,  10,  10,
      5,   5,  10,  25,  25,  10,   5,   5,
      0,   0,   0,  20,  20,   0,   0,   0,
      5,  -5, -10,   0,   0, -10,  -5,   5,
      5,  10,  10, -20, -20,  10,  10,   5,
      0,   0,   0,   0,   0,   0,   0,   0,
]

_KNIGHT = [
    -50, -40, -30, -30, -30, -30, -40, -50,
    -40, -20,   0,   0,   0,   0, -20, -40,
    -30,   0,  10,  15,  15,  10,   0, -30,
    -30,   5,  15,  20,  20,  15,   5, -30,
    -30,   0,  15,  20,  20,  15,   0, -30,
    -30,   5,  10,  15,  15,  10,   5, -30,
    -40, -20,   0,   5,   5,   0, -20, -40,
    -50, -40, -30, -30, -30, -30, -40, -50,
]

_BISHOP = [
    -20, -10, -10, -10, -10, -10, -10, -20,
    -10,   0,   0,   0,   0,   0,   0, -10,
    -10,   0,   5,  10,  10,   5,   0, -10,
    -10,   5,   5,  10,  10,   5,   5, -10,
    -10,   0,  10,  10,  10,  10,   0, -10,
    -10,  10,  10,  10,  10,  10,  10, -10,
    -10,   5,   0,   0,   0,   0,   5, -10,
    -20, -10, -10, -10, -10, -10, -10, -20,
]

_ROOK = [
      0,   0,   0,   0,   0,   0,   0,   0,
      5,  10,  10,  10,  10,  10,  10,   5,
     -5,   0,   0,   0,   0,   0,   0,  -5,
     -5,   0,   0,   0,   0,   0,   0,  -5,
     -5,   0,   0,   0,   0,   0,   0,  -5,
     -5,   0,   0,   0,   0,   0,   0,  -5,
     -5,   0,   0,   0,   0,   0,   0,  -5,
      0,   0,   0,   5,   5,   0,   0,   0,
]

_QUEEN = [
    -20, -10, -10,  -5,  -5, -10, -10, -20,
    -10,   0,   0,   0,   0,   0,   0, -10,
    -10,   0,   5,   5,   5,   5,   0, -10,
     -5,   0,   5,   5,   5,   5,   0,  -5,
      0,   0,   5,   5,   5,   5,   0,  -5,
    -10,   5,   5,   5,   5,   5,   0, -10,
    -10,   0,   5,   0,   0,   0,   0, -10,
    -20, -10, -10,  -5,  -5, -10, -10, -20,
]

_KING_MIDDLEGAME = [
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -20, -30, -30, -40, -40, -30, -30, -20,
    -10, -20, -20, -20, -20, -20, -20, -10,
     20,  20,   0,   0,   0,   0,  20,  20,
     20,  30,  10,   0,   0,  10,  30,  20,
]

_KING_ENDGAME = [
    -50, -40, -30, -20, -20, -30, -40, -50,
    -30, -20, -10,   0,   0, -10, -20, -30,
    -30, -10,  20,  30,  30,  20, -10, -30,
    -30, -10,  30,  40,  40,  30, -10, -30,
    -30, -10,  30,  40,  40,  30, -10, -30,
    -30, -10,  20,  30,  30,  20, -10, -30,
    -30, -30,   0,   0,   0,   0, -30, -30,
    -50, -30, -30, -30, -30, -30, -30, -50,
]

PIECE_SQUARE_TABLES = {
    PAWN: _PAWN,
    KNIGHT: _KNIGHT,
    BISHOP: _BISHOP,
    ROOK: _ROOK,
    QUEEN: _QUEEN,
    KING: _KING_MIDDLEGAME,
}

for _piece_type, _table in PIECE_SQUARE_TABLES.items():
    if len(_table) != 64:
        raise AssertionError(
            f"piece-square table for {chess.piece_name(_piece_type)} has {len(_table)} "
            f"entries, expected 64"
        )
if len(_KING_ENDGAME) != 64:
    raise AssertionError(f"endgame king table has {len(_KING_ENDGAME)} entries, expected 64")

# Non-pawn, non-king material below which the endgame king table is used. Two rooks and
# two minors per side is roughly the usual cutoff.
ENDGAME_MATERIAL_THRESHOLD = 1300


def _table_index(square: int, color: bool) -> int:
    """Map a python-chess square onto an index into an a8-first table.

    python-chess numbers a1 as 0 and h8 as 63, while the tables above are written with a8
    first, so White's lookup flips the rank with `square ^ 56`. Black reads the table
    directly, which mirrors it.

    Note `square ^ 56` rather than `-square` or `63 - square`: negative indexing was the
    inherited mirroring bug in the predecessor engine, and `63 - square` additionally
    mirrors files, which is wrong for asymmetric tables.
    """
    return square ^ 56 if color == chess.WHITE else square


def non_pawn_material(board: chess.Board) -> int:
    """Total non-pawn, non-king material on the board, for both sides."""
    total = 0
    for piece_type in (KNIGHT, BISHOP, ROOK, QUEEN):
        count = len(board.pieces(piece_type, chess.WHITE)) + len(
            board.pieces(piece_type, chess.BLACK)
        )
        total += count * PIECE_VALUES[piece_type]
    return total


def is_endgame(board: chess.Board) -> bool:
    """Phase detection. Deliberately colour-symmetric so evaluation stays symmetric."""
    return non_pawn_material(board) <= ENDGAME_MATERIAL_THRESHOLD


def evaluate_white(board: chess.Board) -> int:
    """Evaluate from White's point of view: positive means White is better.

    For debugging and for symmetry tests. The search must not call this directly.
    """
    king_table = _KING_ENDGAME if is_endgame(board) else _KING_MIDDLEGAME
    score = 0
    for piece_type in (PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING):
        table = king_table if piece_type == KING else PIECE_SQUARE_TABLES[piece_type]
        value = PIECE_VALUES[piece_type]
        for square in board.pieces(piece_type, chess.WHITE):
            score += value + table[_table_index(square, chess.WHITE)]
        for square in board.pieces(piece_type, chess.BLACK):
            score -= value + table[_table_index(square, chess.BLACK)]
    return score


def evaluate(board: chess.Board) -> int:
    """Evaluate relative to the side to move: positive means the mover is better.

    This is the function the negamax search calls at its leaves.
    """
    score = evaluate_white(board)
    return score if board.turn == chess.WHITE else -score
