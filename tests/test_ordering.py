"""Tests for move ordering.

Two layers, and both matter for different reasons.

The unit tests pin down the heuristics themselves -- MVV-LVA's key order, killer shifting,
history ageing. These are cheap and they catch the silent errors: an inverted comparison in
MVV-LVA still produces a working engine that simply searches more nodes, which no
correctness test would notice and which only a benchmark would reveal, long after the change.

The differential tests are the real gate. Move ordering is **provably score-preserving**:
reordering the siblings of a node cannot change its minimax value. So the requirement is
exact score equality against the frozen Phase 0 search at every depth, plus a node count
that is never higher. If a score moves, ordering has been implemented as something other
than ordering.
"""

import chess
import pytest

from bench.positions import BENCH_POSITIONS
from engine.evaluate import PIECE_VALUES
from engine.ordering import (
    CAPTURE_BASE,
    HISTORY_CEILING,
    KILLER_PRIMARY,
    KILLER_SECONDARY,
    MAX_PLY,
    HistoryTable,
    KillerTable,
    captured_value,
    mvv_lva,
    order_moves,
)
from engine.search import SearchStats, search as frozen_search
from engine.searcher import CONFIGS, SearchConfig, Searcher

SAMPLE_FENS = [
    (name, fen)
    for name, fen, _category in BENCH_POSITIONS
    if name in {"startpos", "open-game", "kiwipete", "perft-4", "perft-5", "wac-001",
                "wac-002", "hanging-queen", "perft-3-endgame", "kpk", "rook-endgame"}
]

# Positions whose *unordered* depth-4 search is cheap enough for a test suite. The
# differential tests have to run the frozen baseline too, and the frozen baseline at depth 4
# over the whole set is the 69-second benchmark -- not something to pay on every `pytest`.
# Depth 4 therefore runs here on the cheap positions only; the full-set depth-4 comparison
# is `bench/nodes.py --baseline`, which is where an expensive measurement belongs.
CHEAP_FENS = [
    (name, fen)
    for name, fen in SAMPLE_FENS
    if name in {"startpos", "wac-002", "hanging-queen", "perft-3-endgame", "kpk",
                "rook-endgame"}
]


# -- captured_value ---------------------------------------------------------------------

def test_captured_value_of_a_quiet_move_is_zero():
    board = chess.Board()
    assert captured_value(board, chess.Move.from_uci("e2e4")) == 0


def test_captured_value_of_an_ordinary_capture():
    board = chess.Board("4k3/8/8/8/3q4/8/8/3RK3 w - - 0 1")
    assert captured_value(board, chess.Move.from_uci("d1d4")) == PIECE_VALUES[chess.QUEEN]


def test_captured_value_of_en_passant():
    """The captured pawn is not on the destination square, which is the whole trap here."""
    board = chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1")
    move = chess.Move.from_uci("e5d6")
    assert board.is_en_passant(move)
    assert board.piece_type_at(move.to_square) is None, "destination really is empty"
    assert captured_value(board, move) == PIECE_VALUES[chess.PAWN]


def test_captured_value_of_a_quiet_promotion():
    """A pawn is consumed and a queen appears, so the net gain is queen minus pawn."""
    board = chess.Board("5k2/3P4/8/8/8/8/8/4K3 w - - 0 1")
    move = chess.Move.from_uci("d7d8q")
    expected = PIECE_VALUES[chess.QUEEN] - PIECE_VALUES[chess.PAWN]
    assert captured_value(board, move) == expected


def test_captured_value_of_a_capturing_promotion():
    board = chess.Board("3rk3/4P3/8/8/8/8/8/4K3 w - - 0 1")
    move = chess.Move.from_uci("e7d8q")
    expected = PIECE_VALUES[chess.ROOK] + PIECE_VALUES[chess.QUEEN] - PIECE_VALUES[chess.PAWN]
    assert captured_value(board, move) == expected


# -- MVV-LVA ----------------------------------------------------------------------------

def test_mvv_lva_prefers_the_more_valuable_victim():
    """Victim is the primary key: taking the queen beats taking the pawn either way."""
    board = chess.Board("4k3/8/8/8/3qp3/8/8/3RKR2 w - - 0 1")
    take_queen = chess.Move.from_uci("d1d4")
    take_pawn = chess.Move.from_uci("f1f4")
    queen_score = mvv_lva(board, take_queen, captured_value(board, take_queen))
    pawn_score = mvv_lva(board, take_pawn, captured_value(board, take_pawn))
    assert queen_score > pawn_score


def test_mvv_lva_prefers_the_cheaper_attacker_for_the_same_victim():
    """Pawn takes queen must outrank rook takes queen: if it loses, it loses less."""
    board = chess.Board("4k3/8/8/8/3q4/2P5/8/3RK3 w - - 0 1")
    pawn_takes = chess.Move.from_uci("c3d4")
    rook_takes = chess.Move.from_uci("d1d4")
    pawn_score = mvv_lva(board, pawn_takes, captured_value(board, pawn_takes))
    rook_score = mvv_lva(board, rook_takes, captured_value(board, rook_takes))
    assert pawn_score > rook_score


def test_a_king_capture_is_not_ranked_below_a_pawn_capture():
    """Regression guard for using centipawn values for the attacker term.

    The king is worth 20000 in the evaluation so that material can never outweigh it. If
    that value were reused as the MVV-LVA attacker term it would swamp the victim term and
    order king-takes-queen below pawn-takes-pawn, which is backwards. The attacker term is
    an ordinal rank precisely to stop that.
    """
    board = chess.Board("4k3/8/8/8/8/8/4q3/4K3 w - - 0 1")
    king_takes_queen = chess.Move.from_uci("e1e2")
    assert captured_value(board, king_takes_queen) == PIECE_VALUES[chess.QUEEN]
    score = mvv_lva(board, king_takes_queen, PIECE_VALUES[chess.QUEEN])
    pawn_takes_pawn = CAPTURE_BASE + PIECE_VALUES[chess.PAWN] * 16 - 1
    assert score > pawn_takes_pawn


def test_every_capture_outranks_every_killer():
    """The priority bands must not overlap, or ordering inverts on the best capture."""
    board = chess.Board("3rk3/4P3/8/8/8/8/8/4K3 w - - 0 1")
    best_possible = chess.Move.from_uci("e7d8q")
    worst_capture_score = CAPTURE_BASE + PIECE_VALUES[chess.PAWN] * 16 - 6
    best_capture_score = mvv_lva(board, best_possible, captured_value(board, best_possible))
    assert worst_capture_score > KILLER_PRIMARY
    assert best_capture_score > worst_capture_score


def test_killer_band_outranks_the_history_ceiling():
    assert KILLER_SECONDARY > HISTORY_CEILING
    assert KILLER_PRIMARY > KILLER_SECONDARY


# -- KillerTable ------------------------------------------------------------------------

def test_killer_table_starts_empty():
    killers = KillerTable()
    assert killers.primary(0) is None
    assert killers.secondary(0) is None


def test_killer_table_shifts_the_old_primary_down():
    killers = KillerTable()
    first = chess.Move.from_uci("e2e4")
    second = chess.Move.from_uci("d2d4")
    killers.record(3, first)
    killers.record(3, second)
    assert killers.primary(3) == second
    assert killers.secondary(3) == first


def test_killer_table_ignores_a_repeat_of_the_primary():
    """Otherwise one repeatedly-good move fills both slots and the second carries nothing."""
    killers = KillerTable()
    move = chess.Move.from_uci("e2e4")
    killers.record(3, move)
    killers.record(3, move)
    assert killers.primary(3) == move
    assert killers.secondary(3) is None


def test_killer_table_is_per_ply():
    killers = KillerTable()
    move = chess.Move.from_uci("e2e4")
    killers.record(3, move)
    assert killers.primary(4) is None


def test_killer_table_tolerates_a_ply_past_the_bound():
    """Quiescence can push the ply deep; going out of range must not raise."""
    killers = KillerTable()
    killers.record(MAX_PLY + 10, chess.Move.from_uci("e2e4"))
    assert killers.primary(MAX_PLY + 10) is None


def test_killer_table_clear():
    killers = KillerTable()
    killers.record(3, chess.Move.from_uci("e2e4"))
    killers.clear()
    assert killers.primary(3) is None


# -- HistoryTable -----------------------------------------------------------------------

def test_history_starts_at_zero():
    history = HistoryTable()
    assert history.score(chess.WHITE, chess.Move.from_uci("e2e4")) == 0


def test_history_weights_by_depth_squared():
    """A cutoff near the root prunes far more, so it is stronger evidence."""
    history = HistoryTable()
    move = chess.Move.from_uci("e2e4")
    history.record(chess.WHITE, move, 5)
    assert history.score(chess.WHITE, move) == 25


def test_history_accumulates():
    history = HistoryTable()
    move = chess.Move.from_uci("e2e4")
    history.record(chess.WHITE, move, 2)
    history.record(chess.WHITE, move, 3)
    assert history.score(chess.WHITE, move) == 4 + 9


def test_history_is_per_colour():
    """The same from/to squares mean something different for the other side."""
    history = HistoryTable()
    move = chess.Move.from_uci("e2e4")
    history.record(chess.WHITE, move, 4)
    assert history.score(chess.BLACK, move) == 0


def test_history_distinguishes_moves():
    history = HistoryTable()
    history.record(chess.WHITE, chess.Move.from_uci("e2e4"), 4)
    assert history.score(chess.WHITE, chess.Move.from_uci("d2d4")) == 0


def test_history_ages_at_the_ceiling():
    """Halving keeps scores below the killer band and decays stale evidence.

    Driven with an exact record count rather than a `while` loop on the score. A loop
    conditioned on the score not having dropped is the obvious way to write this and it
    hangs: halving lands the counter back on the loop's own threshold, so the condition
    never becomes false.
    """
    history = HistoryTable()
    move = chess.Move.from_uci("e2e4")
    other = chess.Move.from_uci("d2d4")

    step = 100 * 100  # depth 100 credits depth * depth
    history.record(chess.WHITE, other, 100)

    # Climb to just below the ceiling without crossing it.
    for _ in range((HISTORY_CEILING - step) // step):
        history.record(chess.WHITE, move, 100)

    before = history.score(chess.WHITE, move)
    other_before = history.score(chess.WHITE, other)
    assert before < HISTORY_CEILING, "setup should not have aged the table yet"
    assert other_before == step

    # One more record crosses the ceiling and triggers the halving.
    history.record(chess.WHITE, move, 100)

    assert history.score(chess.WHITE, move) < before, "the credited move should be halved"
    assert history.score(chess.WHITE, move) < HISTORY_CEILING
    assert history.score(chess.WHITE, other) == other_before // 2, "whole table ages"


def test_history_never_reaches_the_killer_band():
    history = HistoryTable()
    move = chess.Move.from_uci("e2e4")
    for _ in range(2000):
        history.record(chess.WHITE, move, 20)
        assert history.score(chess.WHITE, move) < KILLER_SECONDARY


def test_history_clear():
    history = HistoryTable()
    move = chess.Move.from_uci("e2e4")
    history.record(chess.WHITE, move, 4)
    history.clear()
    assert history.score(chess.WHITE, move) == 0


# -- order_moves ------------------------------------------------------------------------

def test_order_moves_puts_captures_first():
    board = chess.Board("4k3/8/8/8/3q4/8/4P3/3RK3 w - - 0 1")
    ordered = order_moves(board, list(board.legal_moves), KillerTable(), HistoryTable(), 0)
    assert ordered[0] == chess.Move.from_uci("d1d4")


def test_order_moves_puts_a_killer_ahead_of_other_quiet_moves():
    board = chess.Board()
    killers = KillerTable()
    killer = chess.Move.from_uci("h2h3")  # a move nothing else would rank highly
    killers.record(0, killer)
    ordered = order_moves(board, list(board.legal_moves), killers, HistoryTable(), 0)
    assert ordered[0] == killer


def test_order_moves_puts_a_capture_ahead_of_a_killer():
    board = chess.Board("4k3/8/8/8/3q4/8/7P/3RK3 w - - 0 1")
    killers = KillerTable()
    killers.record(0, chess.Move.from_uci("h2h3"))
    ordered = order_moves(board, list(board.legal_moves), killers, HistoryTable(), 0)
    assert ordered[0] == chess.Move.from_uci("d1d4")


def test_order_moves_uses_history_for_the_rest():
    board = chess.Board()
    history = HistoryTable()
    favoured = chess.Move.from_uci("b1c3")
    history.record(chess.WHITE, favoured, 6)
    ordered = order_moves(board, list(board.legal_moves), KillerTable(), history, 0)
    assert ordered[0] == favoured


def test_order_moves_is_a_permutation():
    """Ordering must never add, drop or duplicate a move."""
    for _name, fen in SAMPLE_FENS:
        board = chess.Board(fen)
        moves = list(board.legal_moves)
        ordered = order_moves(board, moves, KillerTable(), HistoryTable(), 0)
        assert sorted(ordered, key=str) == sorted(moves, key=str), fen


def test_order_moves_is_deterministic():
    """The benchmark compares node counts across runs, so ordering must be stable."""
    board = chess.Board("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1")
    moves = list(board.legal_moves)
    first = order_moves(board, moves, KillerTable(), HistoryTable(), 0)
    second = order_moves(board, moves, KillerTable(), HistoryTable(), 0)
    assert first == second


def test_order_moves_does_not_mutate_the_input():
    board = chess.Board()
    moves = list(board.legal_moves)
    original = list(moves)
    order_moves(board, moves, KillerTable(), HistoryTable(), 0)
    assert moves == original


# -- differential: ordering must not change the score -----------------------------------

@pytest.mark.parametrize("name,fen", SAMPLE_FENS, ids=[n for n, _ in SAMPLE_FENS])
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_ordering_preserves_the_score(name, fen, depth):
    """The central gate. Reordering siblings cannot change a minimax value."""
    reference_stats = SearchStats()
    reference = frozen_search(chess.Board(fen), depth, reference_stats)

    searcher = Searcher(CONFIGS["ordering"])
    candidate = searcher.search(chess.Board(fen), depth)

    assert candidate.score == reference.score, (
        f"{name} d{depth}: ordering changed the score "
        f"{reference.score} -> {candidate.score}"
    )


@pytest.mark.parametrize("name,fen", CHEAP_FENS, ids=[n for n, _ in CHEAP_FENS])
def test_ordering_preserves_the_score_at_depth_four(name, fen):
    """Depth 4 matters because it is the depth the benchmark reports."""
    reference = frozen_search(chess.Board(fen), 4)
    candidate = Searcher(CONFIGS["ordering"]).search(chess.Board(fen), 4)
    assert candidate.score == reference.score, (
        f"{name} d4: ordering changed the score {reference.score} -> {candidate.score}"
    )


@pytest.mark.parametrize("name,fen", SAMPLE_FENS, ids=[n for n, _ in SAMPLE_FENS])
@pytest.mark.parametrize("depth", [2, 3])
def test_ordering_never_increases_node_count(name, fen, depth):
    """Ordering may tie on trivial positions, but it must never cost more nodes."""
    reference_stats = SearchStats()
    frozen_search(chess.Board(fen), depth, reference_stats)

    searcher = Searcher(CONFIGS["ordering"])
    searcher.search(chess.Board(fen), depth)

    assert searcher.stats.nodes <= reference_stats.nodes, (
        f"{name} d{depth}: ordering searched {searcher.stats.nodes} nodes, "
        f"baseline searched {reference_stats.nodes}"
    )


def test_ordering_is_off_by_default():
    """The default config is the baseline, so a benchmark cannot be accidentally ordered."""
    assert SearchConfig().move_ordering is False


def test_ordering_state_does_not_leak_across_new_game():
    searcher = Searcher(CONFIGS["ordering"])
    searcher.search(chess.Board(), 3)
    searcher.new_game()
    assert searcher.killers.primary(1) is None
    assert searcher.history.score(chess.WHITE, chess.Move.from_uci("e2e4")) == 0


def test_repeated_search_gives_the_same_node_count_after_new_game():
    """Guards the specific predecessor bug: a warmed table inflating the next measurement."""
    searcher = Searcher(CONFIGS["ordering"])
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 4 3")

    searcher.search(board, 3)
    first = searcher.stats.nodes

    searcher.new_game()
    searcher.search(board, 3)
    assert searcher.stats.nodes == first
