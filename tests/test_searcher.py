"""Differential tests for the Phase 1 `Searcher` against the frozen Phase 0 search.

These are the tests that make the whole measurement story trustworthy, so they matter more
than they look.

The central one is `test_phase0_config_is_identical_to_frozen_search`. Phase 1 wraps the
search in a config object so that features can be switched off for per-feature measurement.
That wrapping is itself code, and code that quietly changes behaviour would corrupt every
number the project reports -- the baseline would no longer be the baseline. So the all-flags-
off config is required to match `engine/search.py` on *three* things at once: the score, the
best move, and the exact node count. Node-count equality is the strict one; it fails on any
change to visit order or cutoff logic, including ones that leave the score intact.
"""

import chess
import pytest

from bench.positions import BENCH_POSITIONS
from engine.search import INFINITY, SearchStats, negamax, search
from engine.searcher import CONFIGS, SearchConfig, Searcher

# A representative subset, kept small enough to run at depth 3 in a few seconds. The full
# set at depth 4 is the benchmark's job, not the test suite's.
SAMPLE_FENS = [
    (name, fen)
    for name, fen, _category in BENCH_POSITIONS
    if name in {"startpos", "open-game", "kiwipete", "perft-4", "wac-002",
                "hanging-queen", "perft-3-endgame", "kpk"}
]


@pytest.mark.parametrize("name,fen", SAMPLE_FENS, ids=[n for n, _ in SAMPLE_FENS])
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_phase0_config_is_identical_to_frozen_search(name, fen, depth):
    """All features off must reproduce engine/search.py exactly: score, move and nodes."""
    reference_stats = SearchStats()
    reference = search(chess.Board(fen), depth, reference_stats)

    searcher = Searcher(SearchConfig())
    candidate = searcher.search(chess.Board(fen), depth)

    assert candidate.score == reference.score, f"{name} d{depth}: score differs"
    assert candidate.move == reference.move, f"{name} d{depth}: best move differs"
    assert searcher.stats.nodes == reference_stats.nodes, (
        f"{name} d{depth}: visited {searcher.stats.nodes} nodes, "
        f"baseline visited {reference_stats.nodes}"
    )
    assert searcher.stats.leaves == reference_stats.leaves, (
        f"{name} d{depth}: evaluated {searcher.stats.leaves} leaves, "
        f"baseline evaluated {reference_stats.leaves}"
    )


def test_phase0_is_a_registered_config():
    """The benchmark CLI selects configs by name, so the baseline name must exist."""
    assert "phase0" in CONFIGS
    assert CONFIGS["phase0"] == SearchConfig()


def test_config_is_immutable():
    """A config must not be mutable mid-search, or a benchmark result is unattributable."""
    config = SearchConfig()
    with pytest.raises(Exception):
        config.move_ordering = True  # type: ignore[misc]


def test_with_returns_a_copy():
    base = SearchConfig()
    derived = base.with_(move_ordering=True)
    assert derived.move_ordering is True
    assert base.move_ordering is False


def test_stats_accumulate_and_reset():
    searcher = Searcher()
    searcher.search(chess.Board(), 2)
    first = searcher.stats.nodes
    assert first > 0

    searcher.search(chess.Board(), 2)
    assert searcher.stats.nodes == 2 * first, "stats should accumulate across searches"

    searcher.reset_stats()
    assert searcher.stats.nodes == 0


def test_new_game_clears_state():
    searcher = Searcher()
    searcher.search(chess.Board(), 2)
    searcher.new_game()
    assert searcher.stats.nodes == 0


def test_searcher_handles_a_terminal_root():
    """Mated at the root: no move, and a losing mate score."""
    board = chess.Board("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1")
    board.push_uci("a1a8")
    result = Searcher().search(board, 3)
    assert result.move is None
    assert result.score < 0


@pytest.mark.parametrize("name,fen", SAMPLE_FENS, ids=[n for n, _ in SAMPLE_FENS])
def test_searcher_returns_a_legal_move(name, fen):
    board = chess.Board(fen)
    result = Searcher().search(board, 2)
    assert result.move in board.legal_moves, name


def test_narrow_window_agrees_with_full_window():
    """The same differential invariant the Phase 0 suite enforces, through the Searcher."""
    for _name, fen in SAMPLE_FENS:
        searcher = Searcher()
        board = chess.Board(fen)
        full = searcher._negamax(board, 3, -INFINITY, INFINITY, 0)
        wide = searcher._negamax(board, 3, full - 500, full + 500, 0)
        assert full == wide, f"window changed the score for {fen}"


def test_board_is_restored_after_search():
    """A search must leave the board exactly as it found it.

    Cheap to test and catches an unbalanced push/pop, which would otherwise show up much
    later as an inexplicable evaluation.
    """
    for _name, fen in SAMPLE_FENS:
        board = chess.Board(fen)
        before = board.fen()
        Searcher().search(board, 3)
        assert board.fen() == before, fen
