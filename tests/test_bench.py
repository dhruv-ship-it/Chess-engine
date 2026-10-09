"""Tests for the measurement harness itself.

The benchmark is the thing that produces every number in the README and on the resume, so
it gets tested like production code. A silently broken benchmark is worse than no benchmark:
it produces numbers that look authoritative.
"""

import json

import chess

from bench import nodes as nodes_bench
from bench.positions import BENCH_POSITIONS, validate


def test_position_set_validates():
    validate()


def test_position_set_is_mixed():
    """Each category must be represented, or an aggregate hides a per-category regression."""
    categories = {category for _name, _fen, category in BENCH_POSITIONS}
    assert categories == {"opening", "middlegame", "tactical", "endgame"}


def test_position_set_is_big_enough():
    """Hard floor against the predecessor's single-position benchmark."""
    assert len(BENCH_POSITIONS) >= 10


def test_every_position_is_searchable():
    for name, fen, _category in BENCH_POSITIONS:
        board = chess.Board(fen)
        assert board.is_valid(), name
        assert not board.is_game_over(), name


def test_run_position_reports_the_expected_fields():
    record = nodes_bench.run_position(chess.STARTING_FEN, 2, "phase0")
    assert record["nodes"] > 0
    assert record["leaves"] > 0
    assert record["seconds"] >= 0
    assert record["move"] is not None
    assert isinstance(record["score"], int)


def test_run_position_is_deterministic_in_node_count():
    """Node counts must be reproducible; only timings may vary between runs."""
    first = nodes_bench.run_position(chess.STARTING_FEN, 3, "phase0")
    second = nodes_bench.run_position(chess.STARTING_FEN, 3, "phase0")
    assert first["nodes"] == second["nodes"]
    assert first["leaves"] == second["leaves"]
    assert first["score"] == second["score"]
    assert first["move"] == second["move"]


def test_run_position_does_not_share_state_between_calls():
    """Two calls must be independent.

    The predecessor's benchmark shared killer moves between its baseline and its optimised
    run, so the baseline warmed up the thing it was being compared against. Determinism
    above would still hold if state leaked, so this asserts the mechanism directly: a fresh
    Searcher per position.
    """
    from engine.searcher import Searcher

    created = []
    original_init = Searcher.__init__

    def tracking_init(self, config=None):
        created.append(self)
        original_init(self, config)

    Searcher.__init__ = tracking_init  # type: ignore[method-assign]
    try:
        nodes_bench.run_position(chess.STARTING_FEN, 1, "phase0")
        nodes_bench.run_position(chess.STARTING_FEN, 1, "phase0")
    finally:
        Searcher.__init__ = original_init  # type: ignore[method-assign]

    assert len(created) == 2
    assert created[0] is not created[1]


def test_run_aggregates_totals():
    result = nodes_bench.run(1, "phase0", verbose=False)
    assert result["depth"] == 1
    assert result["config"] == "phase0"
    assert len(result["positions"]) == len(BENCH_POSITIONS)
    assert result["totals"]["nodes"] == sum(
        p["nodes"] for p in result["positions"].values()
    )


def test_result_is_json_serialisable():
    """Baselines are committed as JSON, so the record has to round-trip."""
    result = nodes_bench.run(1, "phase0", verbose=False)
    assert json.loads(json.dumps(result))["totals"]["nodes"] == result["totals"]["nodes"]


def test_compare_detects_a_score_change():
    """A fabricated baseline with a different score must be reported, not ignored."""
    current = nodes_bench.run(1, "phase0", verbose=False)
    baseline = json.loads(json.dumps(current))
    first = next(iter(baseline["positions"]))
    baseline["positions"][first]["score"] += 1
    assert nodes_bench.compare(current, baseline) == 1


def test_compare_passes_when_identical():
    current = nodes_bench.run(1, "phase0", verbose=False)
    baseline = json.loads(json.dumps(current))
    assert nodes_bench.compare(current, baseline) == 0


def test_compare_refuses_mismatched_depths():
    current = nodes_bench.run(1, "phase0", verbose=False)
    baseline = json.loads(json.dumps(current))
    baseline["depth"] = 2
    assert nodes_bench.compare(current, baseline) == 2
