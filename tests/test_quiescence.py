"""Tests for quiescence search.

Quiescence is the one Phase 1 feature that is *not* a speed optimisation, and the tests have
to be written accordingly. It visits strictly **more** nodes at the same nominal depth and it
returns **different** scores, because the scores it replaces were wrong. Asserting score
equality here -- the gate used for move ordering and the transposition table's move hint --
would reject a correct implementation.

So the gates are behavioural:

* the horizon effect is actually fixed, demonstrated on a position where the engine without
  quiescence plays a capture that loses a queen;
* stand-pat exists, so the side to move is never forced into a bad capture;
* stand-pat is *not* used when in check, where declining to move is not an option;
* the recursion terminates, and the safety cap is measured rather than assumed;
* the tactical suite still passes -- which the `conftest` fixture handles automatically for
  every registered configuration.
"""

import chess
import pytest

from bench.positions import BENCH_POSITIONS
from engine.evaluate import evaluate
from engine.search import INFINITY, MATE
from engine.searcher import MAX_QUIESCENCE_PLY, CONFIGS, SearchConfig, Searcher

QUIESCENCE_CONFIGS = ["quiescence", "quiescence-see"]

# White can play Qxd5, but the pawn on c6 recaptures. At depth 1 the recapture is one ply
# past the horizon, so a search without quiescence sees "won a pawn" and plays it.
HORIZON_FEN = "4k3/8/2p5/3p4/8/8/8/3QK3 w - - 0 1"


def test_the_horizon_effect_is_real_without_quiescence():
    """Establishes the bug before asserting the fix, so the next test cannot pass vacuously."""
    board = chess.Board(HORIZON_FEN)
    result = Searcher(CONFIGS["tt-id"]).search(board, 1)
    assert result.move == chess.Move.from_uci("d1d5"), (
        "without quiescence a depth-1 search should grab the pawn, not seeing the recapture"
    )


@pytest.mark.parametrize("config", QUIESCENCE_CONFIGS)
def test_quiescence_fixes_the_horizon_effect(config):
    """With the capture resolved, Qxd5 is revealed as losing the queen for a pawn."""
    board = chess.Board(HORIZON_FEN)
    result = Searcher(CONFIGS[config]).search(board, 1)
    assert result.move != chess.Move.from_uci("d1d5"), (
        f"{config} still played Qxd5, so captures are not being resolved"
    )


@pytest.mark.parametrize("config", QUIESCENCE_CONFIGS)
def test_stand_pat_means_a_bad_capture_is_never_forced(config):
    """Capturing is optional, so the static evaluation is a floor on the quiescence score.

    Every capture available here loses material, so the right answer is to decline them all
    and accept the position as it stands. Without stand-pat, quiescence would be obliged to
    capture and would return a much worse score -- a classic way to make an engine weaker by
    adding quiescence.
    """
    board = chess.Board(HORIZON_FEN)
    searcher = Searcher(CONFIGS[config])
    searcher.zobrist = _tracker_for(searcher, board)
    score = searcher._quiescence(board, -INFINITY, INFINITY, 0, 0)
    assert score == evaluate(board), (
        "with only losing captures available, quiescence should return the static evaluation"
    )


def _tracker_for(searcher, board):
    from engine.zobrist import ZobristTracker

    if searcher.tt is None:
        return None
    searcher.tt.new_search()
    return ZobristTracker(board)


@pytest.mark.parametrize("config", QUIESCENCE_CONFIGS)
def test_a_winning_capture_improves_on_stand_pat(config):
    """The other half of stand-pat: a free piece must still be taken."""
    board = chess.Board("4k3/8/8/3p4/8/8/8/3QK3 w - - 0 1")
    searcher = Searcher(CONFIGS[config])
    searcher.zobrist = _tracker_for(searcher, board)
    score = searcher._quiescence(board, -INFINITY, INFINITY, 0, 0)
    assert score > evaluate(board), "the undefended pawn should have been taken"


@pytest.mark.parametrize("config", QUIESCENCE_CONFIGS)
def test_no_stand_pat_when_checkmated(config):
    """In check there is no option to stop, and the evaluation cannot see mate.

    Black has just been mated. The static evaluation of this position is merely "down a
    rook"; quiescence must report a mate score instead.
    """
    board = chess.Board("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1")
    board.push_uci("a1a8")
    assert board.is_checkmate()

    searcher = Searcher(CONFIGS[config])
    searcher.zobrist = _tracker_for(searcher, board)
    score = searcher._quiescence(board, -INFINITY, INFINITY, 3, 0)
    assert score == -MATE + 3, f"expected a mate score at ply 3, got {score}"


@pytest.mark.parametrize("config", QUIESCENCE_CONFIGS)
def test_check_evasions_search_non_captures_too(config):
    """An escape may be a quiet move, so restricting to captures would miss it entirely.

    White is checked along the first rank by the rook on a1 and cannot capture it. The only
    legal move is the quiet king step Kg2. A quiescence that generated only captures would
    find no moves at all here and conclude White is mated.
    """
    board = chess.Board("6k1/8/8/8/8/6P1/5P1P/r5K1 w - - 0 1")
    assert board.is_check()
    assert not board.is_checkmate()
    assert not any(board.generate_legal_captures()), "no capture should be available"
    assert [m.uci() for m in board.legal_moves] == ["g1g2"], "the only escape is quiet"

    searcher = Searcher(CONFIGS[config])
    searcher.zobrist = _tracker_for(searcher, board)
    score = searcher._quiescence(board, -INFINITY, INFINITY, 1, 0)
    assert score > -MATE + 100, f"reported a near-mate score ({score}) in an escapable check"


@pytest.mark.parametrize("config", QUIESCENCE_CONFIGS)
def test_quiet_queen_promotions_are_searched(config):
    """A quiet promotion swings about eight pawns, so omitting it leaves a hole.

    `generate_legal_captures()` does not include non-capturing promotions, so they are added
    explicitly. White's pawn on d7 promotes for free here.
    """
    board = chess.Board("5k2/3P4/8/8/8/8/8/4K3 w - - 0 1")
    searcher = Searcher(CONFIGS[config])
    searcher.zobrist = _tracker_for(searcher, board)
    score = searcher._quiescence(board, -INFINITY, INFINITY, 0, 0)
    assert score > evaluate(board) + 500, (
        "promoting to a queen should be found, which is worth far more than a pawn"
    )


# -- the expected direction: more nodes, different scores --------------------------------

NODE_SAMPLE = [
    (name, fen)
    for name, fen, _category in BENCH_POSITIONS
    if name in {"startpos", "kiwipete", "wac-002", "perft-3-endgame", "rook-endgame"}
]


@pytest.mark.parametrize("name,fen", NODE_SAMPLE, ids=[n for n, _ in NODE_SAMPLE])
def test_quiescence_increases_node_count_at_the_same_depth(name, fen):
    """Asserted in the direction a speed optimisation would fail.

    This is the test that encodes the classification: quiescence is extra work that buys
    accuracy, not a saving. A version that reduced nodes here would be searching less than it
    should.
    """
    without = Searcher(CONFIGS["tt-id"])
    without.search(chess.Board(fen), 3)
    with_quiescence = Searcher(CONFIGS["quiescence"])
    with_quiescence.search(chess.Board(fen), 3)
    assert with_quiescence.stats.nodes > without.stats.nodes, (
        f"{name}: quiescence should visit more nodes, "
        f"{without.stats.nodes} -> {with_quiescence.stats.nodes}"
    )
    assert with_quiescence.quiescence_nodes > 0


@pytest.mark.parametrize("name,fen", NODE_SAMPLE, ids=[n for n, _ in NODE_SAMPLE])
def test_see_pruning_reduces_quiescence_nodes(name, fen):
    """SEE is a filter on quiescence, so it must cut the work quiescence added."""
    plain = Searcher(CONFIGS["quiescence"])
    plain.search(chess.Board(fen), 3)
    pruned = Searcher(CONFIGS["quiescence-see"])
    pruned.search(chess.Board(fen), 3)
    assert pruned.quiescence_nodes <= plain.quiescence_nodes, (
        f"{name}: {plain.quiescence_nodes} -> {pruned.quiescence_nodes}"
    )


def test_quiescence_nodes_are_counted():
    """The predecessor counted nodes only at `depth == 0` in its main search, so its
    quiescence nodes -- most of the work -- were missing from every published figure."""
    searcher = Searcher(CONFIGS["quiescence"])
    searcher.search(chess.Board("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"), 3)
    assert searcher.quiescence_nodes > 0
    assert searcher.stats.nodes > searcher.quiescence_nodes, (
        "total nodes must include both main-search and quiescence nodes"
    )


# -- termination and the safety cap ------------------------------------------------------

def test_quiescence_terminates_on_a_capture_dense_position():
    """Captures are self-limiting because material runs out; this checks it in practice."""
    board = chess.Board("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1")
    searcher = Searcher(CONFIGS["quiescence-see"])
    result = searcher.search(board, 3)
    assert result.move in board.legal_moves


def test_the_recursion_cap_is_not_reached_on_the_benchmark_set():
    """The cap is a safety net for check-evasion chains, which are not self-limiting.

    Measured rather than asserted by hope: if the cap were being hit regularly, quiescence
    would be returning a static evaluation while in check, which is exactly the thing it
    exists to avoid.
    """
    deepest = 0
    hits = 0
    for _name, fen, _category in BENCH_POSITIONS:
        searcher = Searcher(CONFIGS["quiescence-see"])
        searcher.search(chess.Board(fen), 3)
        deepest = max(deepest, searcher.max_quiescence_ply)
        hits += searcher.quiescence_cap_hits
    assert hits == 0, f"the quiescence cap was hit {hits} times"
    assert deepest < MAX_QUIESCENCE_PLY, (
        f"deepest quiescence ply {deepest} is at the cap of {MAX_QUIESCENCE_PLY}"
    )


def test_board_is_restored_after_a_quiescence_search():
    for _name, fen in NODE_SAMPLE:
        board = chess.Board(fen)
        Searcher(CONFIGS["quiescence-see"]).search(board, 3)
        assert board.fen() == fen, fen


def test_the_zobrist_key_survives_quiescence():
    """Quiescence pushes through the tracker too, so its push/pop balance matters."""
    board = chess.Board("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1")
    searcher = Searcher(CONFIGS["quiescence-see"])
    searcher.search(board, 3)
    assert searcher.zobrist.is_consistent(board)


# -- configuration -----------------------------------------------------------------------

def test_quiescence_is_off_by_default():
    assert SearchConfig().quiescence is False
    assert SearchConfig().see_pruning is False


def test_configs_without_quiescence_do_no_quiescence_work():
    searcher = Searcher(CONFIGS["tt-id"])
    searcher.search(chess.Board(), 3)
    assert searcher.quiescence_nodes == 0


def test_see_pruning_only_applies_with_quiescence():
    """SEE filters quiescence captures, so it is meaningless on its own."""
    searcher = Searcher(SearchConfig(move_ordering=True, see_pruning=True))
    searcher.search(chess.Board(), 3)
    assert searcher.see_prunes == 0


def test_new_game_clears_quiescence_counters():
    searcher = Searcher(CONFIGS["quiescence-see"])
    searcher.search(chess.Board(), 3)
    assert searcher.quiescence_nodes > 0
    searcher.new_game()
    assert searcher.quiescence_nodes == 0
    assert searcher.max_quiescence_ply == 0
    assert searcher.see_prunes == 0
