"""Tests for incremental Zobrist hashing.

The headline test is `test_incremental_key_matches_recomputation_over_the_tree`: walk the
whole legal move tree and assert at **every node** that the incrementally maintained key
equals a from-scratch recomputation. A Zobrist implementation fails in a handful of specific
places -- castling moves two pieces, en passant captures a pawn that is not on the
destination square, promotion replaces one piece type with another -- and a tree walk reaches
all of them without anyone having to think of them individually.

This is also the one part of the perft harness that tests code written in this project.
Matching published perft node counts validates `python-chess`, which owns move generation
here; asserting the key is consistent at every one of those nodes validates our hashing.
"""

import chess
import pytest

from engine.zobrist import ZobristKeys, ZobristTracker, default_keys


def walk(board: chess.Board, tracker: ZobristTracker, depth: int) -> tuple[int, int]:
    """Walk the move tree, returning (nodes visited, inconsistent keys found)."""
    nodes, bad = 1, 0
    if not tracker.is_consistent(board):
        return nodes, 1
    if depth == 0:
        return nodes, 0
    for move in board.legal_moves:
        tracker.push(board, move)
        sub_nodes, sub_bad = walk(board, tracker, depth - 1)
        tracker.pop(board)
        nodes += sub_nodes
        bad += sub_bad
    return nodes, bad


# (name, fen, depth) -- chosen for coverage of the hard cases, not for size.
TREE_CASES = [
    ("startpos", chess.STARTING_FEN, 3),
    # Kiwipete: all four castling rights available for both sides.
    ("castling", "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", 2),
    # Promotions, including capturing promotions, plus a rook on its home square to capture.
    ("promotions", "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1", 3),
    ("promotions-2", "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8", 3),
    # An en-passant capture is immediately available.
    ("en-passant", "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", 4),
    # Pawns that can create en-passant situations for both colours.
    ("pawn-endgame", "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1", 4),
]


@pytest.mark.parametrize("name,fen,depth", TREE_CASES, ids=[c[0] for c in TREE_CASES])
def test_incremental_key_matches_recomputation_over_the_tree(name, fen, depth):
    board = chess.Board(fen)
    tracker = ZobristTracker(board)
    nodes, bad = walk(board, tracker, depth)
    assert bad == 0, f"{name}: {bad} of {nodes} nodes had an inconsistent key"
    assert nodes > 1, "the walk should have visited more than the root"


@pytest.mark.parametrize("name,fen,depth", TREE_CASES, ids=[c[0] for c in TREE_CASES])
def test_board_and_key_are_restored_after_the_walk(name, fen, depth):
    """push/pop must be exactly balanced on both the board and the key."""
    board = chess.Board(fen)
    tracker = ZobristTracker(board)
    key_before = tracker.key
    walk(board, tracker, depth)
    assert board.fen() == fen
    assert tracker.key == key_before


# -- the three fiddly cases, called out individually ------------------------------------

def test_castling_updates_the_rook_as_well_as_the_king():
    """Castling moves two pieces. Hashing only the king leaves the rook double-counted."""
    board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    tracker = ZobristTracker(board)
    for uci in ("e1g1", "e1c1", "e8g8", "e8c8"):
        board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        if uci.startswith("e8"):
            board.turn = chess.BLACK
        tracker = ZobristTracker(board)
        move = chess.Move.from_uci(uci)
        assert board.is_castling(move), uci
        tracker.push(board, move)
        assert tracker.is_consistent(board), f"{uci}: key wrong after castling"


def test_en_passant_removes_a_pawn_that_is_not_on_the_destination_square():
    board = chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1")
    tracker = ZobristTracker(board)
    move = chess.Move.from_uci("e5d6")
    assert board.is_en_passant(move)
    tracker.push(board, move)
    assert tracker.is_consistent(board)
    # The captured pawn really is gone, i.e. this is not a vacuously passing test.
    assert board.piece_at(chess.D5) is None


def test_en_passant_for_black_too():
    """The captured pawn is on the other side of the destination when Black captures."""
    board = chess.Board("4k3/8/8/8/3pP3/8/8/4K3 b - e3 0 1")
    tracker = ZobristTracker(board)
    move = chess.Move.from_uci("d4e3")
    assert board.is_en_passant(move)
    tracker.push(board, move)
    assert tracker.is_consistent(board)


def test_promotion_replaces_the_piece_type():
    board = chess.Board("4k3/3P4/8/8/8/8/8/4K3 w - - 0 1")
    for promo in ("q", "r", "b", "n"):
        board = chess.Board("4k3/3P4/8/8/8/8/8/4K3 w - - 0 1")
        tracker = ZobristTracker(board)
        tracker.push(board, chess.Move.from_uci(f"d7d8{promo}"))
        assert tracker.is_consistent(board), f"promotion to {promo} hashed wrongly"


def test_capturing_promotion():
    board = chess.Board("3rk3/4P3/8/8/8/8/8/4K3 w - - 0 1")
    tracker = ZobristTracker(board)
    tracker.push(board, chess.Move.from_uci("e7d8q"))
    assert tracker.is_consistent(board)


def test_losing_castling_rights_changes_the_key():
    """Moving a rook off its home square revokes a right, which is part of the key."""
    board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    tracker = ZobristTracker(board)
    before = tracker.key
    tracker.push(board, chess.Move.from_uci("h1g1"))
    assert tracker.is_consistent(board)
    assert tracker.key != before


def test_capturing_a_rook_on_its_home_square_revokes_the_opponents_right():
    """The revocation is not caused by the opponent moving, which is the subtle case."""
    board = chess.Board("r3k2r/8/8/8/8/8/8/R3K1BR w KQkq - 0 1")
    keys = default_keys()
    before_rights = keys.castling(board)
    tracker = ZobristTracker(board)
    tracker.push(board, chess.Move.from_uci("a1a8"))
    assert tracker.is_consistent(board)
    assert keys.castling(board) != before_rights


# -- the key as an identity function ----------------------------------------------------

def test_transpositions_hash_to_the_same_key():
    """The entire point: the same position by a different move order is the same key."""
    first = chess.Board()
    for uci in ("e2e4", "e7e5", "g1f3", "b8c6"):
        first.push_uci(uci)

    second = chess.Board()
    for uci in ("g1f3", "b8c6", "e2e4", "e7e5"):
        second.push_uci(uci)

    keys = default_keys()
    assert first.board_fen() == second.board_fen()
    assert keys.compute(first) == keys.compute(second)


def test_side_to_move_changes_the_key():
    white = chess.Board("4k3/8/8/8/8/8/8/4K2R w - - 0 1")
    black = chess.Board("4k3/8/8/8/8/8/8/4K2R b - - 0 1")
    keys = default_keys()
    assert keys.compute(white) != keys.compute(black)


def test_castling_rights_change_the_key():
    with_rights = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    without = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w - - 0 1")
    keys = default_keys()
    assert keys.compute(with_rights) != keys.compute(without)


def test_a_legal_en_passant_changes_the_key():
    available = chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1")
    unavailable = chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - - 0 1")
    keys = default_keys()
    assert available.has_legal_en_passant()
    assert keys.compute(available) != keys.compute(unavailable)


def test_an_unavailable_en_passant_does_not_change_the_key():
    """Deliberate convention, and the reason `has_legal_en_passant` is used.

    If merely pushing a pawn two squares changed the key, two positions identical in every
    way that affects play would hash differently and the transposition between them would be
    missed. Gating on an actually available capture is also what Polyglot does.
    """
    board = chess.Board("4k3/8/8/8/8/8/P7/4K3 w - - 0 1")
    keys = default_keys()
    board.push_uci("a2a4")  # sets an ep square, but no black pawn can capture
    assert board.ep_square is not None
    assert not board.has_legal_en_passant()
    assert keys.en_passant(board) == 0


# -- tables -----------------------------------------------------------------------------

def test_keys_are_deterministic_for_a_seed():
    """Non-deterministic keys would make node counts irreproducible between benchmark runs.

    The predecessor engine re-randomised its key table on every HTTP request, which made its
    transposition table useless and its measurements meaningless.
    """
    a = ZobristKeys(seed=1234)
    b = ZobristKeys(seed=1234)
    board = chess.Board()
    assert a.compute(board) == b.compute(board)


def test_different_seeds_give_different_keys():
    a = ZobristKeys(seed=1)
    b = ZobristKeys(seed=2)
    assert a.compute(chess.Board()) != b.compute(chess.Board())


def test_default_keys_are_shared():
    """Two searchers must agree on keys, or they could not share a transposition table."""
    assert default_keys() is default_keys()


def test_keys_are_64_bit():
    keys = ZobristKeys()
    assert 0 <= keys.side < (1 << 64)
    assert 0 <= keys.piece(chess.WHITE, chess.PAWN, chess.A2) < (1 << 64)


def test_piece_keys_are_distinct_across_colour_type_and_square():
    keys = ZobristKeys()
    seen = set()
    for color in (chess.WHITE, chess.BLACK):
        for piece_type in range(1, 7):
            for square in chess.SQUARES:
                seen.add(keys.piece(color, piece_type, square))
    assert len(seen) == 2 * 6 * 64, "a duplicate key would merge two distinct positions"


def test_reset_reseeds_from_a_board():
    board = chess.Board()
    tracker = ZobristTracker(board)
    tracker.push(board, chess.Move.from_uci("e2e4"))
    other = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    tracker.reset(other)
    assert tracker.is_consistent(other)


def test_empty_key_for_an_empty_board():
    """Nothing true about the position means nothing XORed in."""
    board = chess.Board(None)  # completely empty
    board.turn = chess.WHITE
    board.castling_rights = chess.BB_EMPTY
    assert default_keys().compute(board) == 0
