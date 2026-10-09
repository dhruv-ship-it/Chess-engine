"""Tests for principal variation search and aspiration windows.

Both features are **score-preserving, but only because of a re-search**. PVS asks a cheap
zero-width question first and has to redo the search properly when the answer surprises it;
an aspiration window guesses a narrow window and has to widen and repeat when the true score
falls outside. Drop either re-search and you get a search that is faster and quietly wrong.

That shapes the tests. Score equality alone is not enough -- a PVS whose re-search never
*triggers* would also pass it. So the suite asserts score equality **and** that the re-search
path is actually exercised, and separately feeds the aspiration logic a deliberately wrong
previous score to force the widening path and check the answer survives it.
"""

import chess
import pytest

from bench.positions import BENCH_POSITIONS
from engine.search import INFINITY, search as frozen_search
from engine.searcher import (
    ASPIRATION_DELTA,
    CONFIGS,
    SearchConfig,
    Searcher,
)
from engine.transposition import EXACT, UPPERBOUND
from engine.zobrist import ZobristTracker

SAMPLE_FENS = [(name, fen) for name, fen, _c in BENCH_POSITIONS]

CHEAP_FENS = [
    (name, fen)
    for name, fen in SAMPLE_FENS
    if name in {"startpos", "wac-002", "hanging-queen", "perft-3-endgame", "kpk",
                "rook-endgame"}
]

# Depth 3 is used for the per-position differentials. Depth 4 equality is covered by
# CHEAP_FENS above and by the committed benchmark baselines; paying for it on all thirteen
# positions in every test would roughly double the suite's runtime for no new coverage.
DIFFERENTIAL_DEPTH = 3

# PVS with nothing else. Isolated on purpose: with a transposition table in play, a score
# difference could come from the table's score reuse (which is allowed to deviate), so only
# the table-free configuration can be held to exact equality.
PVS_ONLY = SearchConfig(move_ordering=True, pvs=True)


# -- score preservation ------------------------------------------------------------------

@pytest.mark.parametrize("name,fen", SAMPLE_FENS, ids=[n for n, _ in SAMPLE_FENS])
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_pvs_preserves_the_score_exactly(name, fen, depth):
    """The central gate: the zero-width scout plus its re-search must be equivalent."""
    reference = frozen_search(chess.Board(fen), depth).score
    candidate = Searcher(PVS_ONLY).search(chess.Board(fen), depth).score
    assert candidate == reference, f"{name} d{depth}: {reference} -> {candidate}"


@pytest.mark.parametrize("name,fen", CHEAP_FENS, ids=[n for n, _ in CHEAP_FENS])
def test_pvs_preserves_the_score_at_depth_four(name, fen):
    reference = frozen_search(chess.Board(fen), 4).score
    candidate = Searcher(PVS_ONLY).search(chess.Board(fen), 4).score
    assert candidate == reference, f"{name} d4: {reference} -> {candidate}"


def test_the_research_path_is_actually_exercised():
    """Without this, a PVS whose re-search never fires would pass every equality test.

    A scout search returning a value strictly between alpha and beta is the surprise case
    that forces the full-window re-search. If that never happened, the equality tests above
    would be proving nothing about the re-search at all.
    """
    searcher = Searcher(PVS_ONLY)
    searcher.search(
        chess.Board("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
        4,
    )
    assert searcher.pvs_researches > 0, "the zero-width scout was never surprised"


def test_configs_without_pvs_never_research():
    searcher = Searcher(CONFIGS["quiescence-see"])
    searcher.search(chess.Board(), 4)
    assert searcher.pvs_researches == 0


@pytest.mark.parametrize("name,fen", SAMPLE_FENS, ids=[n for n, _ in SAMPLE_FENS])
def test_pvs_does_not_change_the_score_of_the_full_engine(name, fen):
    """PVS added to the full configuration must not move the score at a fixed depth."""
    depth = DIFFERENTIAL_DEPTH
    without = Searcher(CONFIGS["quiescence-see"]).search(chess.Board(fen), depth).score
    with_pvs = Searcher(CONFIGS["pvs"]).search(chess.Board(fen), depth).score
    assert with_pvs == without, f"{name}: {without} -> {with_pvs}"


# -- node counts, in the direction that PVS is useful ------------------------------------

@pytest.mark.parametrize("name,fen", SAMPLE_FENS, ids=[n for n, _ in SAMPLE_FENS])
def test_pvs_reduces_nodes_when_a_transposition_table_is_present(name, fen):
    """Aggregate-level claim, asserted per position only in the direction it holds.

    PVS needs a table to be worthwhile: the re-search repeats work the scout already did, and
    the table is what makes repeating it nearly free. Measured, at depth 4 over this set:
    PVS costs +9.6% nodes with no table and saves 8.0% with one. The per-position assertion
    is therefore made only for the configuration that has a table, and with a small tolerance
    because an individual position can still go the other way.
    """
    without = Searcher(CONFIGS["quiescence-see"])
    without.search(chess.Board(fen), DIFFERENTIAL_DEPTH)
    with_pvs = Searcher(CONFIGS["pvs"])
    with_pvs.search(chess.Board(fen), DIFFERENTIAL_DEPTH)
    assert with_pvs.stats.nodes <= without.stats.nodes * 1.15, (
        f"{name}: PVS inflated the node count, "
        f"{without.stats.nodes} -> {with_pvs.stats.nodes}"
    )


def test_pvs_reduces_total_nodes_over_the_whole_set():
    """The claim that matters is the aggregate, so it is asserted as an aggregate.

    Measured: -3.8% at depth 3 and -6.0% at depth 4. Asserted at depth 3, because the
    direction is what the test is for and depth 4 costs three times as much to prove it.
    """
    without = 0
    with_pvs = 0
    for _name, fen, _c in BENCH_POSITIONS:
        a = Searcher(CONFIGS["quiescence-see"])
        a.search(chess.Board(fen), DIFFERENTIAL_DEPTH)
        without += a.stats.nodes
        b = Searcher(CONFIGS["pvs"])
        b.search(chess.Board(fen), DIFFERENTIAL_DEPTH)
        with_pvs += b.stats.nodes
    assert with_pvs < without, f"{without} -> {with_pvs}"


# -- aspiration windows -------------------------------------------------------------------

@pytest.mark.parametrize("name,fen", CHEAP_FENS, ids=[n for n, _ in CHEAP_FENS])
def test_aspiration_preserves_the_score(name, fen):
    """Kept at depth 4, not 3: an aspiration window only opens from depth 3 onwards, so a
    depth-3 search exercises it for a single iteration and barely tests it at all."""
    without = Searcher(CONFIGS["pvs"]).search(chess.Board(fen), 4).score
    with_asp = Searcher(CONFIGS["pvs-asp"]).search(chess.Board(fen), 4).score
    assert with_asp == without, f"{name}: {without} -> {with_asp}"


@pytest.mark.parametrize("offset", [-5000, -600, 600, 5000])
def test_a_badly_wrong_window_still_produces_the_right_answer(offset):
    """Forces the widening path, which is the whole correctness argument for aspiration.

    `previous` is the guess the window is centred on. Feeding it a value that is wildly wrong
    guarantees the first search fails, so the widen-and-repeat logic has to run. If widening
    were missing or only widened the wrong side, the returned score would be a bound rather
    than the true value and would not match a full-window search.
    """
    fen = "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 6 5"
    board = chess.Board(fen)

    truthful = Searcher(CONFIGS["pvs-asp"])
    expected = truthful.search(chess.Board(fen), 4).score

    searcher = Searcher(CONFIGS["pvs-asp"])
    searcher.zobrist = ZobristTracker(board)
    searcher.tt.new_search()
    result = searcher._search_with_aspiration(board, 4, expected + offset)

    assert result.score == expected, (
        f"a window centred {offset} away returned {result.score}, expected {expected}"
    )
    assert searcher.aspiration_misses > 0, "the window should have failed and been widened"


def test_aspiration_is_skipped_for_mate_scores():
    """A window of +-50 around an ordinary score can never contain a mate score.

    Centring one on a mate score is equally meaningless, so mate scores fall back to a full
    window rather than guaranteeing a failed search plus a re-search.
    """
    fen = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"
    board = chess.Board(fen)
    searcher = Searcher(CONFIGS["pvs-asp"])
    searcher.zobrist = ZobristTracker(board)
    searcher.tt.new_search()
    from engine.search import MATE

    result = searcher._search_with_aspiration(board, 4, MATE - 1)
    assert searcher.aspiration_misses == 0, "a mate score should not open an aspiration window"
    assert result.is_mate


def test_aspiration_is_skipped_at_shallow_depths():
    """Depth 1 and 2 scores are too unstable to be worth guessing from."""
    board = chess.Board()
    searcher = Searcher(CONFIGS["pvs-asp"])
    searcher.zobrist = ZobristTracker(board)
    searcher.tt.new_search()
    searcher._search_with_aspiration(board, 2, 100_000)
    assert searcher.aspiration_misses == 0


def test_a_root_fail_low_is_not_stored_as_exact():
    """Storing a bound as EXACT would poison the table for the next iteration.

    Inside an aspiration window the root can fail low: no move beat the window's alpha, so
    the score is only an upper bound. The next iteration would read an EXACT entry and trust
    it completely.
    """
    fen = "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 6 5"
    board = chess.Board(fen)
    searcher = Searcher(CONFIGS["pvs"])
    searcher.zobrist = ZobristTracker(board)
    searcher.tt.new_search()
    key = searcher.zobrist.key

    # A window pinned far above the true score guarantees a fail-low at the root.
    result = searcher._search_root(board, 3, 20_000, 20_000 + 1)
    assert result.score <= 20_000, "this window should have failed low"

    entry = searcher.tt.probe(key)
    assert entry is not None
    assert entry.flag == UPPERBOUND, (
        f"a failed-low root was stored as {entry.flag}, which must not be EXACT"
    )


def test_a_full_window_root_is_stored_as_exact():
    """The control for the test above."""
    board = chess.Board()
    searcher = Searcher(CONFIGS["pvs"])
    searcher.zobrist = ZobristTracker(board)
    searcher.tt.new_search()
    key = searcher.zobrist.key
    searcher._search_root(board, 3, -INFINITY, INFINITY)
    assert searcher.tt.probe(key).flag == EXACT


# -- configuration ------------------------------------------------------------------------

def test_pvs_and_aspiration_are_off_by_default():
    assert SearchConfig().pvs is False
    assert SearchConfig().aspiration_window is False


def test_aspiration_delta_is_sane():
    """Roughly half a pawn: wide enough to usually contain the next score, narrow enough to
    prune. A window far wider than a pawn would not prune; far narrower would always fail."""
    assert 20 <= ASPIRATION_DELTA <= 100


def test_new_game_clears_the_pvs_counters():
    searcher = Searcher(CONFIGS["pvs-asp"])
    searcher.search(chess.Board(), 4)
    searcher.new_game()
    assert searcher.pvs_researches == 0
    assert searcher.aspiration_misses == 0


def test_board_is_restored_after_a_pvs_search():
    for _name, fen in CHEAP_FENS:
        board = chess.Board(fen)
        Searcher(CONFIGS["pvs-asp"]).search(board, 4)
        assert board.fen() == fen, fen
