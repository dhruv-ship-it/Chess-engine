"""The Phase 1 search, built one feature at a time behind config flags.

Why this module exists separately from `engine/search.py`:

`engine/search.py` is the **frozen Phase 0 control**. It is plain negamax with alpha-beta
and nothing else, it is known correct, and Phase 2 measures everything against it. Editing
it in place would destroy the baseline, so Phase 1 lives here instead.

Why feature flags rather than one optimised search:

Phase 2 has to answer "what is each feature actually worth?" -- in nodes at fixed depth, and
in Elo at fixed time. That requires running the *same* code with one feature turned off,
which is how real engines do it too (Stockfish exposes its search options over UCI for
exactly this reason). So every feature is a boolean on `SearchConfig`, and
`SearchConfig()` with everything off is required by test to be bit-for-bit identical to the
Phase 0 search: same score, same node count, same move. That equivalence test is what stops
the flag plumbing from silently becoming its own source of bugs.

**Which features may change the score at a fixed depth.** This distinction matters enough to
state in the code, because applying the wrong expectation to a feature would make a correct
implementation look like a regression:

* `move_ordering` -- score-preserving. Reordering siblings cannot change a minimax value, so
  a score change here is a bug. Expect the same score and fewer nodes.
* `transposition_table` -- score-preserving in practice but not in theory. Entries are only
  reused at `entry.depth >= depth`, yet bound-based cutoffs combined with changed move order
  can cause search instability. A score change is a strong bug signal, not a proof.
* `quiescence` -- **not** score-preserving, by design. It resolves captures past the horizon,
  so fixed-depth scores change and node counts at the same nominal depth *rise*. Its gate is
  the tactical suite, not score equality.
* `pvs` -- score-preserving provided the full-window re-search on a fail-high is present.
  A missing re-search is exactly what a score change catches.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import chess

from engine.evaluate import evaluate
from engine.ordering import HistoryTable, KillerTable, captured_value, order_moves
from engine.search import (
    INFINITY,
    MATE,
    SearchResult,
    SearchStats,
    _is_drawn,
)


@dataclass(frozen=True)
class SearchConfig:
    """Which search features are enabled.

    Frozen so a config cannot be mutated underneath a running search, which would make a
    benchmark result unattributable. Use `dataclasses.replace` to derive a variant.
    """

    # Phase 1, step 1: MVV-LVA for captures, killer moves and history for quiet moves.
    move_ordering: bool = False

    def with_(self, **changes) -> SearchConfig:
        """Return a copy with the given flags changed. Convenience for benchmarks."""
        return replace(self, **changes)


# Named configurations the benchmark CLI can select by name. Each Phase 1 step adds one
# entry here, so the measurement command line never has to change.
CONFIGS: dict[str, SearchConfig] = {
    # Everything off. Required by test to match engine/search.py exactly.
    "phase0": SearchConfig(),
    # Phase 1 step 1.
    "ordering": SearchConfig(move_ordering=True),
}


class Searcher:
    """A search instance.

    Instance-based rather than a module function because the Phase 1 features need state
    that must live across the whole search but *not* leak between searches: killer moves,
    the history table, and later the transposition table. The predecessor engine got this
    wrong in the other direction -- it built a fresh `Engine` per HTTP request, throwing the
    transposition table away on every move, and separately failed to reset killer moves
    between two benchmark runs, so the baseline run warmed up the optimised one.
    """

    def __init__(self, config: SearchConfig | None = None) -> None:
        self.config = config if config is not None else SearchConfig()
        self.stats = SearchStats()

        # Ordering state. Allocated unconditionally -- they are small and allocating them
        # lazily would mean a branch on every node to check whether they exist.
        self.killers = KillerTable()
        self.history = HistoryTable()

    # -- internals ---------------------------------------------------------------------

    def _negamax(
        self,
        board: chess.Board,
        depth: int,
        alpha: int,
        beta: int,
        ply: int,
    ) -> int:
        """Return the score of `board` relative to the side to move.

        Fail-soft: the returned score may lie outside the (alpha, beta) window, which keeps
        the value usable as a bound rather than only as a cutoff flag. The transposition
        table in a later step depends on that.
        """
        self.stats.nodes += 1

        if ply > 0 and _is_drawn(board):
            return 0

        # Terminal detection must precede the depth cutoff. A mated position has a definite
        # value regardless of remaining depth, so returning a static evaluation here would
        # hide mate from a depth-1 search -- a real bug the Phase 0 tests caught.
        moves = list(board.legal_moves)

        if not moves:
            # No legal moves means the side to move is mated (if in check) or stalemated.
            # The side to move is the mated one, so the score is always negative here.
            # `+ ply` makes a nearer mate score higher, so shorter mates are preferred.
            return -MATE + ply if board.is_check() else 0

        if depth <= 0:
            self.stats.leaves += 1
            return evaluate(board)

        if self.config.move_ordering:
            moves = order_moves(board, moves, self.killers, self.history, ply)

        best = -INFINITY
        for move in moves:
            board.push(move)
            score = -self._negamax(board, depth - 1, -beta, -alpha, ply + 1)
            board.pop()

            if score > best:
                best = score
            if best > alpha:
                alpha = best
            if alpha >= beta:
                # Beta cutoff. Credit the move that caused it, so that the next sibling
                # subtree tries it early -- this is the feedback loop that makes killers and
                # history work at all. Only quiet moves are credited: captures are already
                # ordered first by MVV-LVA, so a killer slot spent on one is wasted.
                if self.config.move_ordering and not captured_value(board, move):
                    self.killers.record(ply, move)
                    self.history.record(board.turn, move, depth)
                break

        return best

    # -- public API --------------------------------------------------------------------

    def search(self, board: chess.Board, depth: int) -> SearchResult:
        """Search the root to a fixed depth and return the best move and its score.

        Stats accumulate on the instance, so a caller measuring several positions sees the
        total. Call `reset_stats()` between positions when per-position numbers are wanted.
        """
        moves = list(board.legal_moves)
        if not moves:
            score = -MATE if board.is_check() else 0
            return SearchResult(move=None, score=score, depth=depth, stats=self.stats)

        if self.config.move_ordering:
            # The root is ordered too. It is one node out of hundreds of thousands, but it
            # is the node where getting the first move right prunes the most, so it is the
            # single highest-value place to order.
            moves = order_moves(board, moves, self.killers, self.history, 0)

        alpha = -INFINITY
        best_move = moves[0]
        best_score = -INFINITY

        for move in moves:
            board.push(move)
            score = -self._negamax(board, depth - 1, -INFINITY, -alpha, 1)
            board.pop()

            if score > best_score:
                best_score = score
                best_move = move
            if score > alpha:
                alpha = score

        self.stats.depth_reached = depth
        return SearchResult(move=best_move, score=best_score, depth=depth, stats=self.stats)

    def reset_stats(self) -> None:
        self.stats.reset()

    def new_game(self) -> None:
        """Clear everything carried between searches.

        Callers that benchmark or play games must use this, because shared ordering state
        makes results unattributable: the predecessor engine's benchmark ran its unordered
        baseline and its ordered run in one process without clearing killer moves, so the
        baseline silently warmed up the run it was being compared against.
        """
        self.reset_stats()
        self.killers.clear()
        self.history.clear()
