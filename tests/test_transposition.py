"""Tests for the transposition table and its use in the search.

Three layers, deliberately.

**Unit tests on the table** pin the things that fail silently: the flag semantics, the
mate-score conversion, the key check that turns an index collision into a miss rather than a
wrong answer, and the replacement policy.

**Cutoff tests on the search** seed the table with a known entry and then check the search
honours the bound *in the right direction only*. A LOWERBOUND used to lower beta, or an
UPPERBOUND used to raise alpha, produces an engine that runs, returns legal moves, and is
wrong sometimes -- so the direction is asserted explicitly rather than assumed from the fact
that the tests pass.

**Differential tests** split the table's two benefits, because they carry different
guarantees. The best-move hint is pure ordering and so **cannot** change a score; that gets
exact equality against the frozen baseline. Reusing a stored *score* can change a
fixed-depth score, because an entry is reused whenever `entry.depth >= depth` and so a node
nominally searched to depth 2 can be answered by a depth-5 value. That is what a
transposition table is for, so the gate there is the tactical suite plus a bounded deviation,
not exact equality.
"""

import chess
import pytest

from bench.positions import BENCH_POSITIONS
from engine.search import INFINITY, MATE, SearchStats, search as frozen_search
from engine.searcher import CONFIGS, SearchConfig, Searcher
from engine.transposition import (
    DEFAULT_SIZE_BITS,
    EXACT,
    FLAG_NAMES,
    LOWERBOUND,
    UPPERBOUND,
    TranspositionTable,
    TTEntry,
)
from engine.zobrist import ZobristTracker

SAMPLE_FENS = [
    (name, fen)
    for name, fen, _category in BENCH_POSITIONS
    if name in {"startpos", "open-game", "kiwipete", "perft-4", "wac-002",
                "hanging-queen", "perft-3-endgame", "kpk", "rook-endgame"}
]

CHEAP_FENS = [
    (name, fen)
    for name, fen in SAMPLE_FENS
    if name in {"startpos", "wac-002", "hanging-queen", "perft-3-endgame", "kpk",
                "rook-endgame"}
]


# =======================================================================================
# the table in isolation
# =======================================================================================

def test_store_and_probe_round_trip():
    tt = TranspositionTable(size_bits=8)
    move = chess.Move.from_uci("e2e4")
    tt.store(key=123, depth=4, score=50, flag=EXACT, move=move, ply=0)
    entry = tt.probe(123)
    assert entry is not None
    assert (entry.depth, entry.score, entry.flag, entry.move) == (4, 50, EXACT, move)


def test_probe_misses_on_an_empty_table():
    assert TranspositionTable(size_bits=8).probe(123) is None


def test_a_different_key_in_the_same_slot_is_a_miss_not_a_wrong_answer():
    """The key check is the only thing between an index collision and a corrupt score."""
    tt = TranspositionTable(size_bits=4)  # 16 slots
    slot_size = tt.size
    tt.store(key=1, depth=4, score=50, flag=EXACT, move=None, ply=0)
    colliding = 1 + slot_size  # indexes to the same slot
    assert (colliding & (slot_size - 1)) == (1 & (slot_size - 1))
    assert tt.probe(colliding) is None
    assert tt.collisions == 1


def test_size_is_a_power_of_two():
    assert TranspositionTable(size_bits=10).size == 1024


@pytest.mark.parametrize("bits", [0, -1, 27, 100])
def test_absurd_sizes_are_rejected(bits):
    with pytest.raises(ValueError):
        TranspositionTable(size_bits=bits)


def test_table_is_bounded():
    """An unbounded table grows until memory runs out, which the predecessor's did."""
    tt = TranspositionTable(size_bits=4)
    for key in range(1000):
        tt.store(key=key, depth=1, score=0, flag=EXACT, move=None, ply=0)
    assert tt.size == 16
    assert tt.used <= 16


def test_clear_empties_the_table():
    tt = TranspositionTable(size_bits=8)
    tt.store(key=1, depth=1, score=0, flag=EXACT, move=None, ply=0)
    tt.clear()
    assert tt.probe(1) is None
    assert tt.used == 0


# -- replacement policy ------------------------------------------------------------------

def test_a_deeper_entry_is_preferred_over_a_shallower_one():
    tt = TranspositionTable(size_bits=8)
    tt.store(key=1, depth=6, score=10, flag=EXACT, move=None, ply=0)
    tt.store(key=1, depth=2, score=20, flag=EXACT, move=None, ply=0)
    entry = tt.probe(1)
    assert entry.depth == 6 and entry.score == 10
    assert tt.declined == 1


def test_an_equally_deep_entry_replaces():
    """Equal depth means the newer result is at least as good and more current."""
    tt = TranspositionTable(size_bits=8)
    tt.store(key=1, depth=4, score=10, flag=EXACT, move=None, ply=0)
    tt.store(key=1, depth=4, score=20, flag=EXACT, move=None, ply=0)
    assert tt.probe(1).score == 20


def test_a_stale_entry_is_replaced_regardless_of_depth():
    """Ageing. Without it a deep entry from an earlier search holds its slot forever."""
    tt = TranspositionTable(size_bits=8)
    tt.store(key=1, depth=10, score=10, flag=EXACT, move=None, ply=0)
    tt.new_search()
    tt.store(key=1, depth=1, score=20, flag=EXACT, move=None, ply=0)
    entry = tt.probe(1)
    assert entry.depth == 1 and entry.score == 20


def test_new_search_keeps_entries():
    """Entries must survive a new search -- reuse across iterations is the point."""
    tt = TranspositionTable(size_bits=8)
    tt.store(key=1, depth=4, score=10, flag=EXACT, move=None, ply=0)
    tt.new_search()
    assert tt.probe(1) is not None


def test_fill_permille_tracks_occupancy():
    tt = TranspositionTable(size_bits=4)  # 16 slots
    assert tt.fill_permille == 0
    for key in range(8):
        tt.store(key=key, depth=1, score=0, flag=EXACT, move=None, ply=0)
    assert tt.fill_permille == 500


def test_stats_are_reported():
    tt = TranspositionTable(size_bits=8)
    tt.store(key=1, depth=1, score=0, flag=EXACT, move=None, ply=0)
    tt.probe(1)
    tt.probe(2)
    stats = tt.stats()
    assert stats["stores"] == 1 and stats["hits"] == 1 and stats["probes"] == 2
    assert stats["hit_rate_pct"] == 50.0


# -- mate-score conversion ---------------------------------------------------------------

@pytest.mark.parametrize("ply", [0, 1, 5, 20])
def test_mate_score_conversion_round_trips(ply):
    tt = TranspositionTable(size_bits=8)
    for score in (MATE - 3, -(MATE - 3), MATE - 1, -MATE):
        stored = tt.to_node_relative(score, ply)
        assert tt.to_root_relative(stored, ply) == score


@pytest.mark.parametrize("ply", [0, 3, 7])
def test_ordinary_scores_are_not_adjusted(ply):
    """Only mate scores encode a distance, so only mate scores get shifted."""
    tt = TranspositionTable(size_bits=8)
    for score in (-900, -1, 0, 1, 450):
        assert tt.to_node_relative(score, ply) == score
        assert tt.to_root_relative(score, ply) == score


def test_a_winning_mate_stored_deep_reads_back_at_the_right_distance():
    """A mate found 2 plies from a node at ply 5 is at distance 7 from the root.

    Stored raw, the same entry read back at a different ply would claim the wrong distance,
    which is how an engine ends up reporting mate in 7 for a mate in 3.
    """
    tt = TranspositionTable(size_bits=8)
    root_relative = MATE - 7       # mate 7 plies from the root
    tt.store(key=1, depth=4, score=root_relative, flag=EXACT, move=None, ply=5)
    stored = tt.probe(1).score
    assert stored == MATE - 2, "stored form should be relative to the node, i.e. 2 plies away"
    assert tt.to_root_relative(stored, 5) == root_relative
    # Reached at a shallower ply, the same mate is nearer the root.
    assert tt.to_root_relative(stored, 1) == MATE - 3


def test_flag_names_cover_every_flag():
    assert set(FLAG_NAMES) == {EXACT, LOWERBOUND, UPPERBOUND}


def test_entry_is_a_tuple():
    """TTEntry is a NamedTuple so it stays cheap in the hottest path in the search."""
    entry = TTEntry(key=1, depth=2, score=3, flag=EXACT, move=None, generation=0)
    assert isinstance(entry, tuple)


# =======================================================================================
# the search's use of the flags
# =======================================================================================

def _seeded(fen: str, depth: int, score: int, flag: int) -> tuple[Searcher, chess.Board]:
    """A searcher whose table already holds one entry for `fen`."""
    board = chess.Board(fen)
    searcher = Searcher(CONFIGS["tt"])
    searcher.zobrist = ZobristTracker(board)
    searcher.tt.new_search()
    searcher.tt.store(searcher.zobrist.key, depth, score, flag, None, 0)
    return searcher, board


def test_exact_entry_cuts_off_whatever_the_window():
    """An EXACT score is the true value, so it is usable directly."""
    searcher, board = _seeded(chess.STARTING_FEN, 4, 123, EXACT)
    assert searcher._negamax(board, 2, -INFINITY, INFINITY, 1) == 123
    assert searcher.stats.nodes == 1, "it should not have searched anything"


def test_lowerbound_cuts_off_only_when_it_reaches_beta():
    """A fail-high score is a floor: it can prove a position is good enough, nothing else."""
    # Stored 500 with beta 100: the true value is at least 500, so 500 >= beta proves a
    # cutoff.
    searcher, board = _seeded(chess.STARTING_FEN, 4, 500, LOWERBOUND)
    assert searcher._negamax(board, 2, 0, 100, 1) == 500
    assert searcher.stats.nodes == 1

    # Stored 500 with beta 1000: "at least 500" says nothing about whether the true value
    # reaches 1000, so the search must actually run.
    searcher, board = _seeded(chess.STARTING_FEN, 4, 500, LOWERBOUND)
    searcher._negamax(board, 2, 0, 1000, 1)
    assert searcher.stats.nodes > 1, "a bound below beta must not end the search"


def test_upperbound_cuts_off_only_when_it_falls_to_alpha():
    """A fail-low score is a ceiling: it can prove a position is bad enough, nothing else."""
    # Stored -500 with alpha -100: the true value is at most -500, which is already worse
    # than alpha, so there is nothing to find here.
    searcher, board = _seeded(chess.STARTING_FEN, 4, -500, UPPERBOUND)
    assert searcher._negamax(board, 2, -100, 100, 1) == -500
    assert searcher.stats.nodes == 1

    # Stored -500 with alpha -1000: "at most -500" is still better than -1000, so the
    # position might matter and must be searched.
    searcher, board = _seeded(chess.STARTING_FEN, 4, -500, UPPERBOUND)
    searcher._negamax(board, 2, -1000, 100, 1)
    assert searcher.stats.nodes > 1, "a bound above alpha must not end the search"


def test_a_lowerbound_is_never_used_as_an_upperbound():
    """The inverted-flag bug, asserted directly.

    If LOWERBOUND were treated as an upper bound, a stored floor of -500 against alpha -100
    would wrongly cut off. It must not.
    """
    searcher, board = _seeded(chess.STARTING_FEN, 4, -500, LOWERBOUND)
    searcher._negamax(board, 2, -100, 100, 1)
    assert searcher.stats.nodes > 1


def test_an_upperbound_is_never_used_as_a_lowerbound():
    searcher, board = _seeded(chess.STARTING_FEN, 4, 500, UPPERBOUND)
    searcher._negamax(board, 2, 0, 100, 1)
    assert searcher.stats.nodes > 1


def test_an_entry_shallower_than_the_current_depth_does_not_cut_off():
    """Its score describes a weaker search, so it cannot stand in for a deeper one."""
    searcher, board = _seeded(chess.STARTING_FEN, 1, 9999, EXACT)
    result = searcher._negamax(board, 4, -INFINITY, INFINITY, 1)
    assert searcher.stats.nodes > 1
    assert result != 9999


def test_score_cutoffs_can_be_disabled():
    """With cutoffs off, even an EXACT entry at full depth must not end the search."""
    board = chess.Board()
    searcher = Searcher(
        SearchConfig(move_ordering=True, transposition_table=True, tt_score_cutoffs=False)
    )
    searcher.zobrist = ZobristTracker(board)
    searcher.tt.new_search()
    searcher.tt.store(searcher.zobrist.key, 4, 123, EXACT, None, 0)
    assert searcher._negamax(board, 2, -INFINITY, INFINITY, 1) != 123
    assert searcher.stats.nodes > 1


def test_an_illegal_stored_move_is_ignored():
    """A hash collision can hand over a move that is illegal here.

    The move is matched against the generated list rather than trusted, so an alien move
    simply never matches and is dropped. A transposition table must never be trusted to
    prove legality.
    """
    board = chess.Board("4k3/8/8/8/3q4/8/8/3RK3 w - - 0 1")
    searcher = Searcher(CONFIGS["tt-move-only"])
    searcher.zobrist = ZobristTracker(board)
    searcher.tt.new_search()
    alien = chess.Move.from_uci("a7a8")  # no piece on a7 in this position
    assert alien not in board.legal_moves
    searcher.tt.store(searcher.zobrist.key, 3, 0, EXACT, alien, 0)

    result = searcher.search(board, 3)
    assert result.move in board.legal_moves
    assert result.score == frozen_search(chess.Board(board.fen()), 3).score


# =======================================================================================
# differential: the move hint is score-preserving, the score cutoff need not be
# =======================================================================================

@pytest.mark.parametrize("name,fen", SAMPLE_FENS, ids=[n for n, _ in SAMPLE_FENS])
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_tt_move_only_preserves_the_score_exactly(name, fen, depth):
    """Using the table purely for ordering cannot change a minimax value."""
    reference = frozen_search(chess.Board(fen), depth).score
    candidate = Searcher(CONFIGS["tt-move-only"]).search(chess.Board(fen), depth).score
    assert candidate == reference, f"{name} d{depth}: {reference} -> {candidate}"


@pytest.mark.parametrize("name,fen", CHEAP_FENS, ids=[n for n, _ in CHEAP_FENS])
def test_tt_move_only_preserves_the_score_at_depth_four(name, fen):
    reference = frozen_search(chess.Board(fen), 4).score
    candidate = Searcher(CONFIGS["tt-move-only"]).search(chess.Board(fen), 4).score
    assert candidate == reference, f"{name} d4: {reference} -> {candidate}"


@pytest.mark.parametrize("name,fen", SAMPLE_FENS, ids=[n for n, _ in SAMPLE_FENS])
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_iterative_deepening_without_a_table_preserves_the_score(name, fen, depth):
    """Without a table, iterative deepening just repeats the search and returns the last."""
    reference = frozen_search(chess.Board(fen), depth).score
    candidate = Searcher(CONFIGS["id-only"]).search(chess.Board(fen), depth).score
    assert candidate == reference, f"{name} d{depth}: {reference} -> {candidate}"


@pytest.mark.parametrize("name,fen", SAMPLE_FENS, ids=[n for n, _ in SAMPLE_FENS])
@pytest.mark.parametrize("depth", [2, 3])
def test_the_table_never_increases_node_count(name, fen, depth):
    ordering_stats = Searcher(CONFIGS["ordering"])
    ordering_stats.search(chess.Board(fen), depth)
    with_tt = Searcher(CONFIGS["tt"])
    with_tt.search(chess.Board(fen), depth)
    assert with_tt.stats.nodes <= ordering_stats.stats.nodes, (
        f"{name} d{depth}: {ordering_stats.stats.nodes} -> {with_tt.stats.nodes}"
    )


def _assert_deviation_is_bounded(name: str, fen: str, depth: int) -> None:
    """The deviation score reuse may introduce is bounded, and never flips a mate verdict.

    Exact equality is the wrong gate here: an entry is reused whenever
    `entry.depth >= depth`, so a node nominally at depth 2 can be answered by a value a
    depth-5 search produced -- better information, not worse. Measured example, in
    `8/8/8/4k3/8/8/4P3/R3K3 w - - 0 1`: an entry stored at depth 1 held 600, which is that
    position's true value at depth 3. What *would* indicate a bug is a large deviation, or a
    mate appearing or disappearing, so both of those are asserted instead.
    """
    reference = frozen_search(chess.Board(fen), depth)
    candidate = Searcher(CONFIGS["tt"]).search(chess.Board(fen), depth)
    assert candidate.is_mate == reference.is_mate, (
        f"{name} d{depth}: mate verdict changed, {reference.score} -> {candidate.score}"
    )
    if not reference.is_mate:
        assert abs(candidate.score - reference.score) <= 100, (
            f"{name} d{depth}: score moved by more than a pawn, "
            f"{reference.score} -> {candidate.score}"
        )


@pytest.mark.parametrize("name,fen", SAMPLE_FENS, ids=[n for n, _ in SAMPLE_FENS])
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_score_cutoffs_stay_within_a_pawn_of_the_true_value(name, fen, depth):
    _assert_deviation_is_bounded(name, fen, depth)


@pytest.mark.parametrize("name,fen", CHEAP_FENS, ids=[n for n, _ in CHEAP_FENS])
def test_score_cutoffs_stay_within_a_pawn_at_depth_four(name, fen):
    """Depth 4 is the depth the benchmark reports, so it gets its own check.

    Only on the positions whose *unordered* depth-4 search is cheap: the reference is the
    frozen baseline, and running that over the whole set at depth 4 is the 69-second
    benchmark, which does not belong in a test suite.
    """
    _assert_deviation_is_bounded(name, fen, 4)


def test_the_table_is_not_allocated_when_disabled():
    """A million-slot list built for a baseline run would distort its own timings."""
    assert Searcher(CONFIGS["phase0"]).tt is None
    assert Searcher(CONFIGS["ordering"]).tt is None
    assert Searcher(CONFIGS["tt"]).tt is not None


def test_default_config_has_the_table_off():
    assert SearchConfig().transposition_table is False
    assert SearchConfig().iterative_deepening is False
    assert SearchConfig().tt_size_bits == DEFAULT_SIZE_BITS


def test_new_game_clears_the_table():
    searcher = Searcher(CONFIGS["tt"])
    searcher.search(chess.Board(), 3)
    assert searcher.tt.used > 0
    searcher.new_game()
    assert searcher.tt.used == 0


def test_the_key_is_reseeded_for_each_root_position():
    """A key carried over from a previous search would make every probe read garbage."""
    searcher = Searcher(CONFIGS["tt"])
    searcher.search(chess.Board(), 2)
    other = chess.Board("4k3/8/8/8/3q4/8/8/3RK3 w - - 0 1")
    searcher.search(other, 2)
    assert searcher.zobrist.is_consistent(other)


def test_board_is_restored_after_a_table_search():
    """The table path pushes through the tracker, so push/pop balance is worth re-checking."""
    for _name, fen in SAMPLE_FENS:
        board = chess.Board(fen)
        Searcher(CONFIGS["tt"]).search(board, 3)
        assert board.fen() == fen, fen


def test_the_table_actually_gets_hits():
    """Guards against a table that is wired up but never usefully consulted."""
    searcher = Searcher(CONFIGS["tt"])
    searcher.search(chess.Board("8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1"), 4)
    assert searcher.tt.hits > 0
    assert searcher.tt.cutoffs > 0


def test_the_stored_move_is_what_the_search_chose():
    searcher = Searcher(CONFIGS["tt"])
    board = chess.Board("4k3/8/8/8/3q4/8/8/3RK3 w - - 0 1")
    result = searcher.search(board, 3)
    entry = searcher.tt.probe(ZobristTracker(board).key)
    assert entry is not None
    assert entry.move == result.move
