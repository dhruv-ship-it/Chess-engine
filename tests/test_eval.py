"""Evaluation invariants.

These target the exact defect classes that broke the predecessor engine: corrupted
piece-square tables, and a White-positive evaluation used inside a negamax search.
"""

import chess
import pytest

from engine.evaluate import (
    PIECE_SQUARE_TABLES,
    _KING_ENDGAME,
    evaluate,
    evaluate_white,
    is_endgame,
)

# Positions spanning opening, middlegame, endgame and lopsided material.
POSITIONS = [
    chess.STARTING_FEN,
    "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 4 3",
    "r2qkbr1/ppp1pppp/2n1b2n/8/8/5P2/PPPP2PP/RNB1KBNR b KQq - 0 6",
    "8/5k2/8/8/8/8/5K2/8 w - - 0 1",
    "4k3/8/8/8/8/8/8/3QK3 w - - 0 1",
    "3qk3/8/8/8/8/8/8/4K3 w - - 0 1",
    "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
    "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
]


@pytest.mark.parametrize("piece_type,table", sorted(PIECE_SQUARE_TABLES.items()))
def test_piece_square_tables_are_64_entries(piece_type, table):
    """Row duplication in these literals is how the predecessor engine was corrupted."""
    assert len(table) == 64, (
        f"{chess.piece_name(piece_type)} table has {len(table)} entries"
    )


def test_endgame_king_table_is_64_entries():
    assert len(_KING_ENDGAME) == 64


@pytest.mark.parametrize("fen", POSITIONS)
def test_evaluation_is_colour_symmetric(fen):
    """`board.mirror()` swaps colours, flips the board and swaps the side to move.

    A correctly side-relative evaluation must therefore return the *same* score for a
    position and its mirror. This is the single test that would have caught the sign bug:
    the old engine returned +890 both for "White is a queen up, White to move" and for
    "White is a queen up, Black to move".
    """
    board = chess.Board(fen)
    assert evaluate(board) == evaluate(board.mirror())


@pytest.mark.parametrize("fen", POSITIONS)
def test_white_relative_evaluation_negates_under_mirror(fen):
    board = chess.Board(fen)
    assert evaluate_white(board) == -evaluate_white(board.mirror())


def test_starting_position_is_balanced():
    assert evaluate_white(chess.Board()) == 0
    assert evaluate(chess.Board()) == 0


def test_evaluation_is_relative_to_side_to_move():
    """Same material, opposite side to move: the scores must be opposites."""
    white_to_move = chess.Board("4k3/8/8/8/8/8/8/3QK3 w - - 0 1")
    black_to_move = chess.Board("4k3/8/8/8/8/8/8/3QK3 b - - 0 1")

    assert evaluate(white_to_move) > 0, "the side a queen up should score positively"
    assert evaluate(black_to_move) < 0, "the side a queen down should score negatively"
    assert evaluate(white_to_move) == -evaluate(black_to_move)


def test_material_advantage_dominates_placement():
    """A queen is worth more than any plausible sum of piece-square bonuses."""
    up_a_queen = chess.Board("4k3/8/8/8/8/8/8/3QK3 w - - 0 1")
    assert evaluate_white(up_a_queen) > 500


def test_phase_detection_is_symmetric():
    for fen in POSITIONS:
        board = chess.Board(fen)
        assert is_endgame(board) == is_endgame(board.mirror())


def test_phase_detection_classifies_extremes():
    assert not is_endgame(chess.Board())
    assert is_endgame(chess.Board("8/5k2/8/8/8/8/5K2/8 w - - 0 1"))


def test_white_pawn_on_seventh_beats_pawn_on_second():
    """Sanity-checks table orientation: advanced White pawns must score higher.

    Catches a mirroring error, which the predecessor engine had via negative indexing.
    """
    advanced = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
    home = chess.Board("4k3/8/8/8/8/8/P7/4K3 w - - 0 1")
    assert evaluate_white(advanced) > evaluate_white(home)


def test_black_pawn_on_second_beats_pawn_on_seventh():
    """The same check for Black, which reads the tables unmirrored."""
    advanced = chess.Board("4k3/8/8/8/8/8/p7/4K3 b - - 0 1")
    home = chess.Board("4k3/p7/8/8/8/8/8/4K3 b - - 0 1")
    assert evaluate_white(advanced) < evaluate_white(home)
