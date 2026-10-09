"""Search invariants: mate scoring, terminal nodes, and alpha-beta soundness."""

import chess
import pytest

from engine.search import (
    INFINITY,
    MATE,
    SearchStats,
    negamax,
    search,
    search_iterative,
)

# Both of these are mate in one; `Ra8#` and `...Ra1#` respectively. The predecessor engine
# found the Black one and refused the White one, which is the signature of an absolute
# (result-derived) mate score inside a negamax search.
MATE_IN_ONE_WHITE = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"
MATE_IN_ONE_BLACK = "r5k1/8/8/8/8/8/5PPP/6K1 b - - 0 1"


def test_mated_node_scores_negative_for_white():
    """White is mated and White is to move: the score must be a losing mate score."""
    board = chess.Board(MATE_IN_ONE_BLACK)
    board.push_uci("a8a1")
    assert board.is_checkmate() and board.turn == chess.WHITE
    assert negamax(board, 3, -INFINITY, INFINITY) == -MATE


def test_mated_node_scores_negative_for_black():
    """Black is mated and Black is to move. The old engine returned +MATE here."""
    board = chess.Board(MATE_IN_ONE_WHITE)
    board.push_uci("a1a8")
    assert board.is_checkmate() and board.turn == chess.BLACK
    assert negamax(board, 3, -INFINITY, INFINITY) == -MATE


def test_stalemate_scores_zero():
    board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    assert board.is_stalemate()
    assert negamax(board, 3, -INFINITY, INFINITY) == 0


@pytest.mark.parametrize(
    "fen,expected",
    [(MATE_IN_ONE_WHITE, "a1a8"), (MATE_IN_ONE_BLACK, "a8a1")],
)
@pytest.mark.parametrize("depth", [1, 2, 3, 4])
def test_finds_mate_in_one_for_both_colours(fen, expected, depth):
    result = search(chess.Board(fen), depth)
    assert result.move is not None
    assert result.move.uci() == expected
    assert result.is_mate and result.score > 0


def test_mate_score_encodes_distance():
    """A mate found at ply 1 must score higher than the same mate found deeper."""
    result = search(chess.Board(MATE_IN_ONE_WHITE), 3)
    assert result.mate_in == 1, f"expected mate in 1 ply, got {result.mate_in}"


def test_prefers_the_faster_mate():
    """With both a mate in one and a slower forced mate available, take the fast one."""
    board = chess.Board(MATE_IN_ONE_WHITE)
    result = search(board, 3)
    board.push(result.move)
    assert board.is_checkmate(), "should have played the immediate mate"


def test_avoids_being_mated_when_an_escape_exists():
    """Black is threatened with Ra8#; ...h6 makes luft and escapes it."""
    board = chess.Board("6k1/5ppp/8/8/8/8/8/R5K1 b - - 0 1")
    result = search(board, 3)
    board.push(result.move)
    followup = search(board, 2)
    assert not (followup.is_mate and followup.score > 0), (
        f"played {result.move}, which allows a forced mate"
    )


def test_alpha_beta_agrees_with_full_window_at_same_depth():
    """A narrowed window must not change the score when the true score lies inside it.

    This is the differential test that keeps future pruning work honest: Phase 1 adds move
    ordering, a transposition table and quiescence, none of which may change the score
    returned at a fixed depth.
    """
    for fen in (
        chess.STARTING_FEN,
        "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 4 3",
        "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
    ):
        board = chess.Board(fen)
        full = negamax(board, 3, -INFINITY, INFINITY)
        wide = negamax(board, 3, full - 500, full + 500)
        assert full == wide, f"window changed the score for {fen}"


def test_search_returns_a_legal_move():
    for fen in (
        chess.STARTING_FEN,
        "r2qkbr1/ppp1pppp/2n1b2n/8/8/5P2/PPPP2PP/RNB1KBNR b KQq - 0 6",
        "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
    ):
        board = chess.Board(fen)
        result = search(board, 3)
        assert result.move in board.legal_moves


def test_no_legal_moves_at_root_returns_none():
    board = chess.Board(MATE_IN_ONE_BLACK)
    board.push_uci("a8a1")
    result = search(board, 3)
    assert result.move is None
    assert result.score == -MATE


def test_iterative_deepening_matches_fixed_depth():
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 4 3")
    assert search_iterative(board, 3).score == search(board, 3).score


def test_stats_are_counted():
    stats = SearchStats()
    search(chess.Board(), 3, stats)
    assert stats.nodes > 0
    assert stats.leaves > 0
    assert stats.depth_reached == 3
