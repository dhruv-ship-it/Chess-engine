"""Shared test fixtures.

The important thing here is `SEARCH_IMPLEMENTATIONS`. The tactical suite is the gate every
search change has to pass, so it must run against *every* search configuration, not just the
one that happened to exist when it was written. Listing the frozen Phase 0 function plus
every registered config means each new Phase 1 feature is automatically covered by the
tactical suite the moment it is added to `CONFIGS`, rather than when someone remembers to
extend the test.
"""

from collections.abc import Callable

import chess
import pytest

from engine.search import SearchResult, search as frozen_search
from engine.searcher import CONFIGS, Searcher

SearchFn = Callable[[chess.Board, int], SearchResult]


def _config_search(config_name: str) -> SearchFn:
    def run(board: chess.Board, depth: int) -> SearchResult:
        # A fresh Searcher per call, so killer moves and history cannot leak between test
        # cases and make a failure depend on test ordering.
        return Searcher(CONFIGS[config_name]).search(board, depth)

    run.__name__ = f"search_{config_name}"
    return run


# (label, callable). The label becomes the pytest id, so a failure names the configuration.
SEARCH_IMPLEMENTATIONS: list[tuple[str, SearchFn]] = [
    ("frozen", frozen_search),
    *((name, _config_search(name)) for name in sorted(CONFIGS)),
]


@pytest.fixture(params=SEARCH_IMPLEMENTATIONS, ids=[label for label, _ in SEARCH_IMPLEMENTATIONS])
def search_fn(request) -> SearchFn:
    """Every registered search configuration, one at a time."""
    return request.param[1]
