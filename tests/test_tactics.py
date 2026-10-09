"""Tactical regression suite.

This is the end-to-end strength check: each position has one clearly best move that any
correct engine must find at shallow depth. It is the suite the predecessor engine scored
5/12 on after its "optimisations", against 12/12 before them, so it is kept as the gate that
every future search change has to pass.
"""

import chess
import pytest

from engine.search import search

# (fen, best move in UCI, description)
FREE_MATERIAL = [
    ("4k3/8/8/8/3q4/8/8/3RK3 w - - 0 1", "d1d4", "White: Rxd4 wins the queen"),
    ("3rk3/8/8/3Q4/8/8/8/4K3 b - - 0 1", "d8d5", "Black: Rxd5 wins the queen"),
    ("4k3/8/8/8/4r3/8/8/4QK2 w - - 0 1", "e1e4", "White: Qxe4 wins the rook"),
    ("2kr4/8/8/8/8/8/8/2KQ4 b - - 0 1", "d8d1", "Black: Rxd1 wins the queen"),
]

MATES_IN_ONE = [
    ("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "a1a8", "White: back-rank mate"),
    ("r5k1/8/8/8/8/8/5PPP/6K1 b - - 0 1", "a8a1", "Black: back-rank mate"),
    ("6k1/5ppp/8/8/8/8/5PPP/4R1K1 w - - 0 1", "e1e8", "White: back-rank mate with pawns"),
]


@pytest.mark.parametrize("depth", [2, 3, 4])
@pytest.mark.parametrize(
    "fen,expected,description",
    FREE_MATERIAL,
    ids=[d for _, _, d in FREE_MATERIAL],
)
def test_wins_free_material(fen, expected, description, depth):
    result = search(chess.Board(fen), depth)
    assert result.move is not None, description
    assert result.move.uci() == expected, (
        f"{description}: at depth {depth} played {result.move.uci()}, expected {expected}"
    )


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize(
    "fen,expected,description",
    MATES_IN_ONE,
    ids=[d for _, _, d in MATES_IN_ONE],
)
def test_delivers_mate_in_one(fen, expected, description, depth):
    board = chess.Board(fen)
    result = search(board, depth)
    assert result.move is not None, description
    board.push(result.move)
    assert board.is_checkmate(), (
        f"{description}: at depth {depth} played {result.move.uci()}, "
        f"expected {expected}, which is mate"
    )


@pytest.mark.parametrize("depth", [2, 3])
def test_takes_a_free_piece_rather_than_a_quiet_move(depth):
    """A hanging knight with no compensation; the capture must be chosen."""
    board = chess.Board("4k3/8/8/8/5n2/8/8/4KR2 w - - 0 1")
    result = search(board, depth)
    assert result.move is not None
    assert board.is_capture(result.move), (
        f"at depth {depth} played the quiet move {result.move.uci()} "
        f"instead of capturing the free knight"
    )


def test_does_not_throw_away_material():
    """From the starting position the engine must not lose material in one move."""
    from engine.evaluate import evaluate_white

    board = chess.Board()
    before = evaluate_white(board)
    result = search(board, 3)
    board.push(result.move)
    # After our move it is the opponent's turn; their best reply should not win material.
    reply = search(board, 2)
    board.push(reply.move)
    after = evaluate_white(board)
    assert after >= before - 100, (
        f"lost material opening with {result.move.uci()}: {before} -> {after}"
    )
