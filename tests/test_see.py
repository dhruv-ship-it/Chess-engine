"""Tests for static exchange evaluation.

SEE is unusually testable, because every case has a material outcome you can compute by hand
in a few seconds. It is also unusually worth testing, because its failure mode is invisible:
a wrong SEE prunes good captures and keeps bad ones, which makes the engine weaker while
every correctness test still passes.

Each case below states the exchange in words, so a failure says what is actually wrong
rather than just printing two numbers.
"""

import chess
import pytest

from engine.evaluate import PIECE_VALUES
from engine.see import is_losing_capture, see

# (fen, uci, expected centipawns, description)
EXCHANGES = [
    ("4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1", "e4d5", 100,
     "pawn takes an undefended pawn: wins a pawn"),
    ("4k3/8/2p5/3p4/4P3/8/8/4K3 w - - 0 1", "e4d5", 0,
     "pawn takes pawn, pawn recaptures: an even trade"),
    ("4k3/8/8/3p4/8/8/8/3QK3 w - - 0 1", "d1d5", 100,
     "queen takes an undefended pawn: wins a pawn"),
    ("4k3/8/2p5/3p4/8/8/8/3QK3 w - - 0 1", "d1d5", -800,
     "queen takes a pawn defended by a pawn: loses the queen for a pawn"),
    ("4k3/8/8/3r4/8/8/8/3RK3 w - - 0 1", "d1d5", 500,
     "rook takes an undefended rook"),
    ("4k3/3r4/8/3r4/8/8/8/3RK3 w - - 0 1", "d1d5", 0,
     "rook takes rook, rook recaptures: even"),
    ("4k3/3r4/8/3p4/8/8/3R4/3QK3 w - - 0 1", "d2d5", 100,
     "rook takes pawn; the rook behind it is an X-ray defender once d2 is vacated"),
    ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6", 100,
     "en passant: the captured pawn is not on the destination square"),
]


@pytest.mark.parametrize(
    "fen,uci,expected,description", EXCHANGES, ids=[c[3][:40] for c in EXCHANGES]
)
def test_exchange_value(fen, uci, expected, description):
    board = chess.Board(fen)
    assert board.status() == chess.STATUS_VALID, f"test FEN is not a legal position: {fen}"
    move = chess.Move.from_uci(uci)
    assert move in board.legal_moves, f"{uci} is not legal in {fen}"
    assert see(board, move) == expected, description


def test_a_quiet_move_has_no_exchange():
    board = chess.Board()
    assert see(board, chess.Move.from_uci("e2e4")) == 0


def test_the_least_valuable_attacker_recaptures_first():
    """Recapturing with the cheaper piece is never worse, so SEE must assume it.

    Black has both a pawn and a queen able to take on d5. Recapturing with the queen would
    be a blunder, so SEE must use the pawn and report an even trade rather than a win.
    """
    board = chess.Board("3qk3/8/2p5/3p4/4P3/8/8/4K3 w - - 0 1")
    assert see(board, chess.Move.from_uci("e4d5")) == 0


def test_a_king_cannot_recapture_into_check():
    """Ignoring this rule is not conservative, so it is enforced rather than simplified away.

    White plays Rxd5. Black's only piece attacking d5 is the king on e6 -- but d5 is also
    covered by the white pawn on e4, so Kxd5 is illegal and the exchange simply stops. A SEE
    that let the king recapture anyway would score this at -400 and prune a capture that
    wins a clean pawn.
    """
    board = chess.Board("8/8/4k3/3p4/4P3/8/8/3R2K1 w - - 0 1")
    move = chess.Move.from_uci("d1d5")
    assert move in board.legal_moves
    attackers = board.attackers_mask(chess.BLACK, chess.D5)
    assert attackers == chess.BB_SQUARES[chess.E6], "the king must be the only defender"
    assert see(board, move) == PIECE_VALUES[chess.PAWN]


def test_the_king_rule_is_not_vacuous():
    """The control for the test above: remove the pawn covering d5 and Kxd5 becomes legal.

    Without this, the previous test would still pass if the king were simply never
    considered as an attacker at all.
    """
    board = chess.Board("8/8/4k3/3p4/8/8/8/3R2K1 w - - 0 1")
    move = chess.Move.from_uci("d1d5")
    expected = PIECE_VALUES[chess.PAWN] - PIECE_VALUES[chess.ROOK]
    assert see(board, move) == expected, "the king should recapture and win the rook"


def test_a_capture_by_a_piece_worth_more_than_the_victim_can_still_be_good():
    """Only if the victim is undefended. Value alone does not decide it."""
    board = chess.Board("4k3/8/8/3p4/8/8/8/3RK3 w - - 0 1")
    assert see(board, chess.Move.from_uci("d1d5")) > 0


def test_is_losing_capture_flags_only_losing_ones():
    losing = chess.Board("4k3/8/2p5/3p4/8/8/8/3QK3 w - - 0 1")
    assert is_losing_capture(losing, chess.Move.from_uci("d1d5"))

    winning = chess.Board("4k3/8/8/3p4/8/8/8/3QK3 w - - 0 1")
    assert not is_losing_capture(winning, chess.Move.from_uci("d1d5"))


def test_an_even_trade_is_not_pruned():
    """SEE == 0 is kept: trading evenly can be right, and the resulting position differs."""
    board = chess.Board("4k3/8/2p5/3p4/4P3/8/8/4K3 w - - 0 1")
    move = chess.Move.from_uci("e4d5")
    assert see(board, move) == 0
    assert not is_losing_capture(board, move)


def test_see_does_not_mutate_the_board():
    """It is *static* exchange evaluation: no make, no unmake, no board changes at all."""
    fen = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"
    board = chess.Board(fen)
    for move in board.generate_legal_captures():
        see(board, move)
        assert board.fen() == fen


def test_see_handles_every_capture_in_a_dense_position():
    """Smoke test for crashes and absurd magnitudes across a real middlegame."""
    board = chess.Board("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1")
    captures = list(board.generate_legal_captures())
    assert captures, "the test position should have captures"
    for move in captures:
        value = see(board, move)
        assert -2000 < value < 2000, f"{move.uci()} gave an implausible SEE of {value}"
