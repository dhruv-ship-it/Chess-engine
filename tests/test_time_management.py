"""Tests for time management.

A timed search is the only part of the engine where being *wrong* is not the main risk --
being *late* is. An engine that overshoots its budget loses on time regardless of how good
its move was. So the tests here are mostly about limits being respected, state being restored
after an abort, and a partial iteration being handled honestly.

The one that matters most is `test_the_board_is_restored_after_an_abort`. The hard limit is
enforced by raising an exception from deep inside the recursion, which skips every pending
`board.pop()` on the way out. Without explicit restoration the board would be left somewhere
in the middle of a variation, and the *next* search would run on the wrong position -- a bug
that would look like a mysterious evaluation error far from its cause.
"""

import time

import chess
import pytest

from bench.positions import BENCH_POSITIONS
from engine.searcher import (
    CLOCK_CHECK_MASK,
    CONFIGS,
    MAX_GROWTH,
    MIN_GROWTH,
    SearchAborted,
    Searcher,
    allocate_time,
)

TIMED_CONFIG = "phase1"

# A dense middlegame, so that a short budget really does cut an iteration short.
BUSY_FEN = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"


# -- time allocation ---------------------------------------------------------------------

def test_allocation_splits_a_clock_sensibly():
    soft, hard = allocate_time(60_000)
    assert 0 < soft < hard
    assert hard <= 60_000 * 0.4, "never commit most of the clock to one move"


def test_allocation_uses_the_increment():
    without = allocate_time(60_000, increment_ms=0)[0]
    with_increment = allocate_time(60_000, increment_ms=1_000)[0]
    assert with_increment > without


def test_allocation_claims_less_than_the_whole_increment():
    """A margin, so that a consistently slight overshoot cannot drain the clock."""
    soft_zero = allocate_time(60_000, increment_ms=0)[0]
    soft_inc = allocate_time(60_000, increment_ms=1_000)[0]
    assert soft_inc - soft_zero < 1_000


def test_allocation_spends_more_when_fewer_moves_remain():
    many = allocate_time(60_000, moves_to_go=40)[0]
    few = allocate_time(60_000, moves_to_go=5)[0]
    assert few > many


def test_allocation_never_exceeds_a_short_clock():
    """With very little time left the formula alone can ask for more than exists."""
    for remaining in (1, 10, 50, 200, 500):
        soft, hard = allocate_time(remaining)
        assert hard <= remaining, f"{remaining}ms clock allocated {hard}ms"
        assert soft <= hard


def test_allocation_of_an_empty_clock_is_zero():
    assert allocate_time(0) == (0, 0)
    assert allocate_time(-100) == (0, 0)


def test_allocation_is_always_at_least_one_millisecond():
    """Zero would make the search abort before completing even depth 1."""
    soft, hard = allocate_time(2)
    assert soft >= 1 and hard >= 1


# -- limits are respected ----------------------------------------------------------------

@pytest.mark.parametrize("hard_ms", [120, 300, 700])
def test_the_hard_limit_is_respected(hard_ms):
    """The clock-check interval bounds the overshoot; the margin allows for scheduling.

    The engine checks the clock every 256 nodes, which at its throughput is on the order of
    10ms. The 150ms allowance here is for this being a shared machine running a test suite,
    not for the engine's own granularity: the benchmark over all 13 positions and four
    budgets records a worst overshoot of about 25ms.
    """
    board = chess.Board(BUSY_FEN)
    searcher = Searcher(CONFIGS[TIMED_CONFIG])
    started = time.perf_counter()
    result = searcher.search_timed(board, hard_ms=hard_ms, soft_ms=hard_ms // 2)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms <= hard_ms + 150, (
        f"budget {hard_ms}ms, took {elapsed_ms:.0f}ms"
    )
    assert result.elapsed_ms == pytest.approx(elapsed_ms, abs=50)


def test_a_longer_budget_reaches_at_least_as_deep():
    """Monotonicity. More time must never produce a shallower search."""
    depths = []
    for budget in (200, 600, 1500):
        board = chess.Board(BUSY_FEN)
        result = Searcher(CONFIGS[TIMED_CONFIG]).search_timed(
            board, hard_ms=budget, soft_ms=int(budget * 0.7)
        )
        depths.append(result.completed_depth)
    assert depths == sorted(depths), f"depth did not increase with budget: {depths}"


def test_a_tiny_budget_still_returns_a_legal_move():
    """The engine must never answer "no move", whatever the clock says.

    Note that `completed_depth` can be **0** here: the clock is checked every 256 nodes, so
    an abort can fire part-way through the depth-1 iteration. A move is still produced,
    because the root publishes its best-so-far after every completed root move.
    """
    for budget in (1, 5, 20):
        board = chess.Board(BUSY_FEN)
        result = Searcher(CONFIGS[TIMED_CONFIG]).search_timed(board, hard_ms=budget)
        assert result.move is not None, f"{budget}ms returned no move"
        assert result.move in board.legal_moves
        assert result.completed_depth >= 0


def test_max_depth_stops_the_search_before_the_clock():
    """A trivial endgame would otherwise iterate until the budget ran out."""
    board = chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 0 1")
    result = Searcher(CONFIGS[TIMED_CONFIG]).search_timed(
        board, hard_ms=5_000, soft_ms=5_000, max_depth=4
    )
    assert result.completed_depth <= 4
    assert result.elapsed_ms < 5_000


# -- state is restored after an abort -----------------------------------------------------

def test_the_board_is_restored_after_an_abort():
    """The central safety test.

    The hard limit unwinds the recursion with an exception, which skips every pending
    `board.pop()`. If the board were not restored explicitly, the next search would run on a
    position several moves deep into a variation.
    """
    aborted_any = False
    for name, fen, _category in BENCH_POSITIONS:
        board = chess.Board(fen)
        searcher = Searcher(CONFIGS[TIMED_CONFIG])
        result = searcher.search_timed(board, hard_ms=150, soft_ms=60)
        aborted_any = aborted_any or result.aborted
        assert board.fen() == fen, f"{name}: board left at {board.fen()}"
        assert board.move_stack == [], f"{name}: {len(board.move_stack)} moves left pushed"
    assert aborted_any, "no iteration was actually aborted, so nothing was exercised"


def test_the_zobrist_key_is_restored_after_an_abort():
    """The key's stack unwinds with the board's, so it is re-seeded from the restored one.

    Several budgets are tried rather than one, because whether a given budget actually
    triggers an abort depends on where the iteration boundaries happen to fall -- a 200ms
    budget on this position stops cleanly via the soft gate instead. The invariant is checked
    for every budget, and at least one abort is required so the test is not vacuous.
    """
    aborted_any = False
    for hard_ms in (40, 120, 300, 800):
        board = chess.Board(BUSY_FEN)
        searcher = Searcher(CONFIGS[TIMED_CONFIG])
        result = searcher.search_timed(board, hard_ms=hard_ms, soft_ms=hard_ms)
        aborted_any = aborted_any or result.aborted
        assert searcher.zobrist.is_consistent(board), f"key wrong after {hard_ms}ms"
    assert aborted_any, "no budget triggered an abort, so nothing was exercised"


def test_a_move_is_returned_even_if_no_root_move_completes():
    """The last-resort fallback, forced rather than hoped for.

    With the deadline already in the past, the very first clock check aborts, so no root move
    can finish and the root publishes nothing. The engine must still answer with a legal
    move: forfeiting is strictly worse than playing the first move that comes to hand.
    """
    board = chess.Board(BUSY_FEN)
    searcher = Searcher(CONFIGS[TIMED_CONFIG])
    original = searcher._search_with_aspiration

    def always_abort(*args, **kwargs):
        raise SearchAborted

    searcher._search_with_aspiration = always_abort
    try:
        result = searcher.search_timed(board, hard_ms=1_000)
    finally:
        searcher._search_with_aspiration = original

    assert result.move is not None, "the engine answered with no move"
    assert result.move in board.legal_moves
    assert result.completed_depth == 0
    assert board.fen() == BUSY_FEN


def test_a_search_after_an_abort_is_unaffected():
    """The practical consequence of restoration: the next search must be correct."""
    searcher = Searcher(CONFIGS[TIMED_CONFIG])
    searcher.search_timed(chess.Board(BUSY_FEN), hard_ms=150, soft_ms=60)

    board = chess.Board("4k3/8/8/8/3q4/8/8/3RK3 w - - 0 1")
    result = searcher.search_timed(board, hard_ms=3_000, soft_ms=1_500)
    assert result.move == chess.Move.from_uci("d1d4"), (
        "after an abort the engine should still find the free queen"
    )


def test_the_deadline_is_cleared_after_a_timed_search():
    """A stale deadline would abort the *next* search instantly."""
    searcher = Searcher(CONFIGS[TIMED_CONFIG])
    searcher.search_timed(chess.Board(BUSY_FEN), hard_ms=150, soft_ms=60)
    assert searcher._deadline is None
    # And a subsequent untimed search must run to completion.
    result = searcher.search(chess.Board(), 3)
    assert result.move is not None


def test_an_untimed_search_never_aborts():
    """No deadline means the clock is never consulted, so `search` cannot be interrupted."""
    searcher = Searcher(CONFIGS[TIMED_CONFIG])
    assert searcher._deadline is None
    result = searcher.search(chess.Board(BUSY_FEN), 4)
    assert result.move is not None


# -- partial iterations -------------------------------------------------------------------

def test_an_aborted_iteration_reports_its_depth_separately():
    """`completed_depth` is the depth the *score* came from; `depth` may be one deeper.

    When an iteration is cut short, its best move is kept -- the previous iteration's best
    move is searched first, so anything that overtook it is better at the deeper depth -- but
    its score is not, because a root that has examined only some of its moves gives a lower
    bound rather than a value. Reporting both makes that explicit instead of implying the
    score came from the deeper search.
    """
    found = False
    for _name, fen, _category in BENCH_POSITIONS:
        board = chess.Board(fen)
        result = Searcher(CONFIGS[TIMED_CONFIG]).search_timed(
            board, hard_ms=400, soft_ms=120
        )
        assert result.completed_depth <= result.depth
        assert result.depth - result.completed_depth <= 1
        if result.depth > result.completed_depth:
            found = True
            assert result.aborted
            assert result.move in board.legal_moves
    assert found, "no partial iteration contributed a move, so nothing was exercised"


def test_search_aborted_is_an_exception():
    assert issubclass(SearchAborted, Exception)


# -- the iteration-start heuristic --------------------------------------------------------

def test_growth_bounds_are_sane():
    assert 1.0 < MIN_GROWTH < MAX_GROWTH
    assert MAX_GROWTH <= 20, "an unclamped growth estimate can abandon the whole budget"


def test_a_trivially_fast_iteration_does_not_stop_the_search():
    """Dividing two microsecond iterations gives a meaningless ratio.

    Without this guard one fast early iteration produced a prediction three times too high
    and the search banked most of its clock unused.
    """
    assert Searcher._next_iteration_fits([0.0001], 0.0001, 1_000) is True
    assert Searcher._next_iteration_fits([0.0001, 0.0002], 0.0003, 1_000) is True


def test_a_clearly_unaffordable_iteration_stops_the_search():
    """Most of the budget spent and the next iteration predicted to be far too large."""
    # 0.9s spent of a 1.0s budget, last iteration 0.6s, so the next is predicted at 0.9s+.
    assert Searcher._next_iteration_fits([0.1, 0.6], 0.9, 1_000) is False


def test_the_budget_is_mostly_used():
    """A time manager that banks its clock is a bug, not caution.

    Checked against the soft budget rather than the hard one, because the search is supposed
    to stop somewhere between them.
    """
    board = chess.Board(BUSY_FEN)
    result = Searcher(CONFIGS[TIMED_CONFIG]).search_timed(
        board, hard_ms=2_000, soft_ms=1_000
    )
    assert result.elapsed_ms > 300, (
        f"only used {result.elapsed_ms:.0f}ms of a 1000ms target"
    )


def test_clock_check_interval_is_a_power_of_two_minus_one():
    """It is used as a bitmask on the node counter, so it has to be all ones."""
    assert CLOCK_CHECK_MASK & (CLOCK_CHECK_MASK + 1) == 0


def test_phase1_is_the_recommended_config():
    """`phase1` is an alias, so it must stay in step with what it aliases."""
    assert CONFIGS["phase1"] is CONFIGS["pvs"]
    assert CONFIGS["phase1"].aspiration_window is False, (
        "aspiration windows measured at zero here and should not be in the default"
    )
