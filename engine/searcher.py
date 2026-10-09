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
* `transposition_table` -- splits into two benefits with *different* guarantees, which is
  why `tt_score_cutoffs` exists as a separate flag.

  The **best-move hint** is pure ordering, so it is score-preserving exactly as
  `move_ordering` is, and is tested with exact score equality against the frozen baseline.

  The **score cutoff** is not score-preserving, and this was measured rather than assumed.
  An entry is reused whenever `entry.depth >= depth`, so a node nominally searched to depth 2
  can be answered by a value that a depth-5 search produced. The `depth` field is a *lower
  bound* on the quality of the information, not a description of it, so a nominal depth-N
  search with a table returns something better than the pure depth-N minimax value. Measured
  example, in `8/8/8/4k3/8/8/4P3/R3K3 w - - 0 1`: an entry stored at depth 1 held the score
  600, which is this position's true value at depth *3*. Nothing is wrong; that is what a
  transposition table is for. It does mean exact score equality is the wrong gate here --
  the gates are the tactical suite, the move-only differential, and unit tests on the flag
  semantics themselves.
* `iterative_deepening` -- score-preserving at the top depth, and the feature that makes the
  transposition table worth most: each iteration seeds the next one's ordering.
* `quiescence` -- **not** score-preserving, by design, and the only Phase 1 feature that
  makes the engine *stronger* at a fixed depth rather than merely cheaper. It resolves
  captures past the horizon, so fixed-depth scores change -- because the old ones were wrong
  -- and node counts at the same nominal depth *rise*. Its gate is the tactical suite and
  horizon-effect positions, never score equality.
* `see_pruning` -- skips captures that static exchange evaluation says lose material. A
  heuristic filter on quiescence, so also not score-preserving.
* `pvs` -- score-preserving **provided the full-window re-search is present**. A missing
  re-search is the classic PVS bug and is exactly what a fixed-depth score change catches.
* `aspiration_window` -- score-preserving provided the search is widened and repeated when
  the true score falls outside the guessed window.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, replace

import chess

from engine.evaluate import evaluate
from engine.ordering import (
    HistoryTable,
    KillerTable,
    captured_value,
    order_captures,
    order_moves,
)
from engine.see import is_losing_capture
from engine.search import (
    INFINITY,
    MATE,
    MATE_THRESHOLD,
    SearchResult,
    SearchStats,
    _is_drawn,
)
from engine.transposition import (
    EXACT,
    LOWERBOUND,
    UPPERBOUND,
    DEFAULT_SIZE_BITS,
    TranspositionTable,
)
from engine.zobrist import ZobristTracker


@dataclass(frozen=True)
class SearchConfig:
    """Which search features are enabled.

    Frozen so a config cannot be mutated underneath a running search, which would make a
    benchmark result unattributable. Use `dataclasses.replace` to derive a variant.
    """

    # Phase 1, step 1: MVV-LVA for captures, killer moves and history for quiet moves.
    move_ordering: bool = False

    # Phase 1, step 2: Zobrist-keyed transposition table storing the best move, with
    # EXACT/LOWERBOUND/UPPERBOUND flags and depth-preferred replacement.
    transposition_table: bool = False

    # Iterative deepening: search depth 1, 2, ... up to the target. Nearly free, because the
    # tree grows geometrically, and it pays for itself by seeding each iteration's move
    # ordering from the previous one via the transposition table.
    iterative_deepening: bool = False

    # Whether a stored score may end a search early, as opposed to only supplying a move
    # for ordering. Separable because the two halves of a transposition table have different
    # guarantees: the move is pure ordering and cannot change a score, while reusing a score
    # can (see the module docstring). Turning this off yields a provably score-preserving
    # table, which is what the exact differential test uses.
    tt_score_cutoffs: bool = True

    # Table size as a power of two. Only read when `transposition_table` is set.
    tt_size_bits: int = DEFAULT_SIZE_BITS

    # Phase 1, step 3: at depth 0, keep searching captures until the position is quiet
    # instead of evaluating a position where a recapture is pending.
    quiescence: bool = False

    # Skip captures that static exchange evaluation says lose material outright. Only read
    # when `quiescence` is set.
    see_pruning: bool = False

    # Phase 1, step 4: search the first move with the full window and every later move with
    # a zero-width window, re-searching any that unexpectedly beats alpha.
    pvs: bool = False

    # Start each iterative-deepening iteration with a narrow window around the previous
    # iteration's score instead of (-inf, +inf). Only read when `iterative_deepening` is set.
    aspiration_window: bool = False

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
    # Phase 1 step 2. Split so the benchmark can attribute each part separately: the
    # best-move hint, the score cutoff, and iterative deepening are three different claims
    # and reporting only a combined figure would hide which one did the work.
    "tt-move-only": SearchConfig(
        move_ordering=True, transposition_table=True, tt_score_cutoffs=False
    ),
    "tt": SearchConfig(move_ordering=True, transposition_table=True),
    "tt-id": SearchConfig(
        move_ordering=True, transposition_table=True, iterative_deepening=True
    ),
    "id-only": SearchConfig(move_ordering=True, iterative_deepening=True),
    # Phase 1 step 3. Split again so SEE's contribution is attributable on its own.
    "quiescence": SearchConfig(
        move_ordering=True,
        transposition_table=True,
        iterative_deepening=True,
        quiescence=True,
    ),
    "quiescence-see": SearchConfig(
        move_ordering=True,
        transposition_table=True,
        iterative_deepening=True,
        quiescence=True,
        see_pruning=True,
    ),
    # Phase 1 step 4. `pvs-pure` exists for the test rather than the benchmark: ordering plus
    # PVS and nothing else is provably score-preserving, so it can be held to exact equality
    # with the frozen baseline, which is the only way to catch a missing re-search cleanly.
    "pvs-pure": SearchConfig(move_ordering=True, pvs=True),
    "pvs": SearchConfig(
        move_ordering=True,
        transposition_table=True,
        iterative_deepening=True,
        quiescence=True,
        see_pruning=True,
        pvs=True,
    ),
    "pvs-asp": SearchConfig(
        move_ordering=True,
        transposition_table=True,
        iterative_deepening=True,
        quiescence=True,
        see_pruning=True,
        pvs=True,
        aspiration_window=True,
    ),
}

# The recommended configuration: everything that measured as a gain, and nothing that did
# not. Aspiration windows are deliberately absent -- they measured at -0.0% median here, so
# including them would be adding a moving part for no benefit.
CONFIGS["phase1"] = CONFIGS["pvs"]

# How often the clock is consulted during a timed search, as a bitmask on the node counter:
# 255 means every 256th node.
#
# This interval is the granularity of the hard limit, and therefore an upper bound on how far
# the search can overshoot it. The first version used every 2048th node, which at this
# engine's 25,000-30,000 nodes per second is roughly 70-80 ms, and it overshot a 500 ms hard
# limit by 50 ms -- in a real game that is how an engine loses on time. At 256 nodes the
# granularity is around 10 ms.
#
# The cost is negligible in return: `time.perf_counter()` takes on the order of 100 ns, so at
# 30,000 nodes per second this is about 120 calls per second, or roughly 0.001% of the budget.
# Checking at *every* node would not be: an attribute load and a comparison per node is
# measurable in CPython when a node costs around 35 microseconds.
CLOCK_CHECK_MASK = 255

# Growth-factor bounds for predicting the next iteration's cost. The factor is measured from
# the iterations actually run, and clamped: a single anomalous iteration -- an aspiration
# miss, a mate suddenly appearing, a transposition-table hit collapsing a subtree -- would
# otherwise produce a prediction of near-zero or near-infinity and either overspend the clock
# or abandon it. The median of the last three ratios is used rather than the last one, for the
# same reason.
MIN_GROWTH = 1.5
MAX_GROWTH = 8.0
DEFAULT_GROWTH = 3.0

# An iteration shorter than this fraction of the hard budget is treated as too small to
# predict from, and the next iteration is started unconditionally.
MIN_ITERATION_FRACTION = 0.02

# While less than this fraction of the hard budget has been spent, start the next iteration
# regardless of what the prediction says. The justification is that an aborted iteration is
# not wasted -- its best move is kept -- so being optimistic costs little, while being
# pessimistic costs a whole ply. Measured deficiency it fixes: `tt-id` stopped at depth 3 on
# `italian` having spent 276 ms of a 1,500 ms budget, because one fast iteration produced a
# growth estimate three times too high.
OPTIMISTIC_FRACTION = 0.5


class SearchAborted(Exception):
    """Raised to unwind the recursion when the hard time limit is reached.

    An exception rather than a flag checked at every return, because the search is recursive
    and a flag would need handling at every one of the four places a score is returned. The
    cost is that the board is left mid-variation when it propagates, so `search_timed`
    restores it explicitly -- see `_restore`.
    """


@dataclass
class TimedResult:
    """The outcome of a search against a clock."""

    move: chess.Move | None
    score: int
    depth: int
    """Deepest depth whose result is being reported."""

    completed_depth: int
    """Deepest depth that finished entirely. Equals `depth` unless a partial iteration
    produced a better move, in which case `depth` is one greater."""

    elapsed_ms: float
    nodes: int
    aborted: bool
    """Whether the final iteration was cut short by the hard limit."""


def allocate_time(
    remaining_ms: int,
    increment_ms: int = 0,
    moves_to_go: int | None = None,
) -> tuple[int, int]:
    """Split a clock into a soft and a hard budget for one move, in milliseconds.

    Returns `(soft_ms, hard_ms)`.

    * **soft** is the target. After each completed iteration the search asks whether another
      one would fit inside it, and stops if not. Stopping cleanly between iterations is far
      better than being cut off inside one, because a completed iteration has a trustworthy
      score and a partial one does not.
    * **hard** is the ceiling that aborts mid-iteration. It is larger than the soft budget so
      that an iteration which turns out to be unexpectedly expensive can still finish rather
      than being wasted entirely.

    `moves_to_go` is the number of moves until the next time control, when the time control
    has one. Without it the game length has to be guessed, and 30 is the usual assumption:
    guessing too low spends the clock early, too high leaves time unused at the end.

    Only 80% of the increment is claimed, leaving a small margin so that a consistently
    slight overshoot cannot drain the clock across a long game.
    """
    if remaining_ms <= 0:
        return 0, 0

    expected_moves = moves_to_go if moves_to_go and moves_to_go > 0 else 30
    soft = remaining_ms / expected_moves + increment_ms * 0.8

    # Never commit more than a fraction of what is left, whatever the arithmetic says. With a
    # very short clock the formula above can exceed the remaining time outright.
    hard = min(soft * 3.0, remaining_ms * 0.4)
    soft = min(soft, hard)
    return max(1, int(soft)), max(1, int(hard))


# Half-width of the first aspiration window, in centipawns. Roughly half a pawn: wide enough
# that most iterations land inside it, narrow enough to prune hard. On a miss the window is
# widened by this factor rather than jumping straight to infinity, so a near miss stays cheap.
ASPIRATION_DELTA = 50
ASPIRATION_WIDEN = 4

# After this many failed attempts the window is abandoned for a full one. A guard, not a
# tuning knob: without it a pathological position could keep failing and keep re-searching.
ASPIRATION_MAX_ATTEMPTS = 4

# Hard ceiling on how deep quiescence may recurse.
#
# Captures are self-limiting -- material runs out -- but *check evasions* are not: a sequence
# of checks with no captures can in principle continue indefinitely, and quiescence does not
# track repetition. The cap is the safety net. It is set far above anything a real position
# reaches, and `Searcher.max_quiescence_ply` reports the deepest actually seen so that the
# claim "it is never hit" is measured rather than assumed.
MAX_QUIESCENCE_PLY = 32


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

        # Transposition state. Allocated only when enabled: the table is a list of a million
        # slots, so building one for a baseline run that will never read it would distort the
        # very timings the baseline exists to provide.
        self.tt = (
            TranspositionTable(self.config.tt_size_bits)
            if self.config.transposition_table
            else None
        )
        self.zobrist: ZobristTracker | None = None

        # Hoisted out of the search so the hot path reads one attribute instead of two.
        self._tt_score_cutoffs = (
            self.config.transposition_table and self.config.tt_score_cutoffs
        )

        # Quiescence counters. Kept on the Searcher rather than on SearchStats because
        # SearchStats lives in the frozen Phase 0 module, which must not change.
        #
        # `quiescence_nodes` is reported deliberately: the predecessor engine counted nodes
        # only at `depth == 0` in its main search, so its quiescence nodes -- the large
        # majority of the work -- were excluded from every figure it published.
        self.quiescence_nodes = 0
        self.max_quiescence_ply = 0
        self.quiescence_cap_hits = 0
        self.see_prunes = 0

        # PVS and aspiration counters. A re-search is PVS paying for a wrong guess, and an
        # aspiration miss is the window having been too narrow. Both are the cost side of
        # those features, so they are reported rather than left invisible.
        self.pvs_researches = 0
        self.aspiration_misses = 0

        # Timing state. `None` means "no clock", which keeps the untimed path -- the one all
        # the committed benchmark baselines were measured on -- free of any clock handling
        # beyond a single `is not None` test per node.
        self._deadline: float | None = None
        self._root_best_so_far: tuple[chess.Move, int, int] | None = None

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
        nodes = self.stats.nodes + 1
        self.stats.nodes = nodes
        if not nodes & CLOCK_CHECK_MASK and self._deadline is not None:
            self._check_clock()

        # The draw check precedes the transposition probe deliberately. The halfmove clock is
        # not part of the Zobrist key (standard, and noted in engine/transposition.py), so
        # two positions identical except for their progress toward the fifty-move rule share
        # a key. Checking the live board first means the position in front of us always wins
        # over anything the table believes.
        if ply > 0 and _is_drawn(board):
            return 0

        tt = self.tt
        tt_move = None
        if tt is not None:
            key = self.zobrist.key
            entry = tt.probe(key)
            if entry is not None:
                # Even an entry too shallow to trust for its score is worth having, because
                # its move is still the best-informed ordering guess available.
                tt_move = entry.move
                if self._tt_score_cutoffs and entry.depth >= depth:
                    score = tt.to_root_relative(entry.score, ply)
                    flag = entry.flag
                    # EXACT is the true value. A bound is only usable in the direction it
                    # bounds: a LOWERBOUND can prove the position is already good enough to
                    # cut off, and an UPPERBOUND can prove it is already bad enough. Using
                    # either in the wrong direction is a silent correctness bug.
                    if (
                        flag == EXACT
                        or (flag == LOWERBOUND and score >= beta)
                        or (flag == UPPERBOUND and score <= alpha)
                    ):
                        tt.cutoffs += 1
                        return score

        if depth <= 0 and self.config.quiescence:
            # Quiescence takes over entirely at the horizon, including terminal detection,
            # so the full move generation below is skipped. That matters: when not in check
            # quiescence only needs captures, and generating every legal move just to throw
            # it away is the single most expensive mistake available here.
            return self._quiescence(board, alpha, beta, ply, 0)

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
            moves = order_moves(board, moves, self.killers, self.history, ply, tt_move)

        # The window as it was on entry. Comparing the final score against *this* alpha, not
        # the alpha raised during the loop, is what distinguishes a fail-low (no move beat the
        # original alpha, so the score is only an upper bound) from an exact value.
        alpha_original = alpha

        use_pvs = self.config.pvs

        best = -INFINITY
        best_move = None
        for index, move in enumerate(moves):
            if tt is not None:
                self.zobrist.push(board, move)
            else:
                board.push(move)

            if index == 0 or not use_pvs:
                score = -self._negamax(board, depth - 1, -beta, -alpha, ply + 1)
            else:
                # Principal variation search. After good ordering the first move is usually
                # the best one, so every later move is first asked a cheaper question: "is
                # this better than alpha, yes or no?" A zero-width window
                # `(alpha, alpha + 1)` answers exactly that and nothing else, and it is much
                # cheaper because almost every node inside it cuts off immediately.
                score = -self._negamax(board, depth - 1, -alpha - 1, -alpha, ply + 1)

                if alpha < score < beta:
                    # The scout was surprised: this move really does beat alpha, so the
                    # zero-width result is only a bound and the true value has to be
                    # obtained with the full window.
                    #
                    # **This re-search is what makes PVS score-preserving.** Dropping it
                    # leaves a search that is faster and quietly wrong -- the classic PVS
                    # bug, and the reason `pvs-pure` is held to exact score equality with
                    # the frozen baseline in the tests.
                    self.pvs_researches += 1
                    score = -self._negamax(board, depth - 1, -beta, -alpha, ply + 1)

            if tt is not None:
                self.zobrist.pop(board)
            else:
                board.pop()

            if score > best:
                best = score
                best_move = move
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

        if tt is not None:
            if best <= alpha_original:
                flag = UPPERBOUND
            elif best >= beta:
                flag = LOWERBOUND
            else:
                flag = EXACT
            tt.store(key, depth, best, flag, best_move, ply)

        return best

    def _check_clock(self) -> None:
        """Abort if the hard limit has passed."""
        if time.perf_counter() >= self._deadline:
            raise SearchAborted

    def _quiescence(
        self,
        board: chess.Board,
        alpha: int,
        beta: int,
        ply: int,
        qply: int,
    ) -> int:
        """Search only forcing moves until the position is quiet, then evaluate.

        **The problem.** A fixed-depth search stops at an arbitrary moment. If the last move
        searched was "queen takes pawn", the static evaluation is applied to a position where
        the opponent is about to recapture the queen, and records it as "up a pawn". That is
        the *horizon effect*, and it makes fixed-depth evaluations wrong in exactly the
        positions where accuracy matters.

        **The fix.** At the horizon, keep searching -- but only captures (and every move when
        in check) -- until nothing forcing is left and the static evaluation means something.
        The recursion terminates because captures are self-limiting: material runs out.

        This is why quiescence is *not* a speed optimisation. It visits strictly more nodes
        at the same nominal depth, and it returns different scores, because the scores it
        replaces were wrong.
        """
        nodes = self.stats.nodes + 1
        self.stats.nodes = nodes
        if not nodes & CLOCK_CHECK_MASK and self._deadline is not None:
            self._check_clock()
        self.quiescence_nodes += 1
        if qply > self.max_quiescence_ply:
            self.max_quiescence_ply = qply

        if _is_drawn(board):
            return 0

        in_check = board.is_check()

        if in_check and qply < MAX_QUIESCENCE_PLY:
            # **No stand-pat when in check.** Standing pat means "I could choose to stop
            # here", and in check that choice does not exist -- the position must be
            # resolved. Worse, the static evaluation has no idea the king is about to be
            # mated, so using it as a floor would report a lost position as fine.
            #
            # For the same reason every legal move is searched, not just captures: the escape
            # may be a king step or an interposition.
            moves = list(board.legal_moves)
            if not moves:
                return -MATE + ply
            if self.config.move_ordering:
                moves = order_moves(board, moves, self.killers, self.history, ply)

            best = -INFINITY
            for move in moves:
                if self.tt is not None:
                    self.zobrist.push(board, move)
                else:
                    board.push(move)
                score = -self._quiescence(board, -beta, -alpha, ply + 1, qply + 1)
                if self.tt is not None:
                    self.zobrist.pop(board)
                else:
                    board.pop()

                if score > best:
                    best = score
                if best > alpha:
                    alpha = best
                if alpha >= beta:
                    break
            return best

        # **Stand-pat.** Capturing is optional, so the side to move can always decline and
        # accept the position as it stands. The static evaluation is therefore a *floor*, not
        # the answer: only captures that might improve on it are worth searching. Omitting
        # this forces the side to move to capture, which loses material and is a classic way
        # to get a quiescence search that makes the engine weaker.
        self.stats.leaves += 1
        best = evaluate(board)
        if best >= beta:
            return best
        if best > alpha:
            alpha = best

        if qply >= MAX_QUIESCENCE_PLY:
            self.quiescence_cap_hits += 1
            return best

        # Note that stalemate is not detected here. Finding it would cost a full legal-move
        # generation at every quiescence node to catch a rare case, so a stalemate reached at
        # the horizon is scored by the static evaluation instead of as a draw. This is the
        # standard trade and it is stated rather than hidden.
        moves = list(board.generate_legal_captures())

        # Quiet promotions are forcing and swing roughly eight pawns, so leaving them out
        # would be a hole in exactly the kind of position quiescence exists for. The
        # bitboard test first makes the extra generation free in the overwhelming majority
        # of positions, where no pawn is one step from promoting.
        promotion_rank = chess.BB_RANK_7 if board.turn == chess.WHITE else chess.BB_RANK_2
        if board.pawns & board.occupied_co[board.turn] & promotion_rank:
            back_ranks = chess.BB_RANK_1 | chess.BB_RANK_8
            for move in board.generate_legal_moves(board.pawns, back_ranks):
                # Capturing promotions are already in `moves`; under-promotions are left out
                # because they almost never matter and each one doubles this branch's cost.
                if move.promotion == chess.QUEEN and not board.is_capture(move):
                    moves.append(move)

        if not moves:
            return best

        moves = order_captures(board, moves)

        for move in moves:
            if self.config.see_pruning and is_losing_capture(board, move):
                # Static exchange evaluation says this exchange loses material outright, so
                # searching it costs a whole subtree to discover what arithmetic already
                # knows. This is a heuristic filter, not a proof, which is why it is a
                # separate flag and separately measured.
                self.see_prunes += 1
                continue

            if self.tt is not None:
                self.zobrist.push(board, move)
            else:
                board.push(move)
            score = -self._quiescence(board, -beta, -alpha, ply + 1, qply + 1)
            if self.tt is not None:
                self.zobrist.pop(board)
            else:
                board.pop()

            if score > best:
                best = score
            if best > alpha:
                alpha = best
            if alpha >= beta:
                break

        return best

    # -- public API --------------------------------------------------------------------

    def search(self, board: chess.Board, depth: int) -> SearchResult:
        """Search `board` and return the best move and its score.

        With `iterative_deepening` off this is a single fixed-depth search. With it on,
        depths 1..depth are searched in turn and the deepest completed result is returned;
        the shallower iterations are nearly free because the tree grows geometrically, and
        they pay for themselves by filling the transposition table with best moves that
        improve the next iteration's ordering.

        Stats accumulate on the instance, so a caller measuring several positions sees the
        total. Call `reset_stats()` between positions when per-position numbers are wanted.
        """
        if self.config.transposition_table:
            # The key is maintained incrementally from here down, so it has to be seeded from
            # the actual root position rather than carried over from a previous search.
            self.zobrist = ZobristTracker(board)
            self.tt.new_search()

        if not self.config.iterative_deepening:
            return self._search_root(board, depth, -INFINITY, INFINITY)

        result = SearchResult(move=None, score=0, depth=0, stats=self.stats)
        previous: int | None = None
        for current in range(1, depth + 1):
            result = self._search_with_aspiration(board, current, previous)
            previous = result.score
            # A mate in our favour cannot be improved on by searching deeper, and continuing
            # would only find the same mate at a worse distance.
            if result.is_mate and result.score > 0:
                break
        return result

    def _search_with_aspiration(
        self,
        board: chess.Board,
        depth: int,
        previous: int | None,
    ) -> SearchResult:
        """One iteration, optionally inside a narrow window around the previous score.

        Iterative deepening hands over the previous depth's score, and the next depth's score
        is usually close to it. Searching `(prev - 50, prev + 50)` instead of `(-inf, +inf)`
        prunes far harder, because a narrow window makes cutoffs easy to reach.

        The bet can lose. If the true score falls outside the window the search *fails* and
        returns only a bound, so the window has to be widened and the iteration repeated --
        and that repeat is what keeps the feature score-preserving. Only the side that failed
        is widened, because a fail-high says nothing about the lower bound.

        Mate scores are excluded: they are enormous, so a window around an ordinary score can
        never contain one, and a window around a mate score is meaningless. Depths 1 and 2
        are excluded because their scores are too unstable to guess from.
        """
        full_window = (
            not self.config.aspiration_window
            or previous is None
            or depth < 3
            or abs(previous) >= MATE_THRESHOLD
        )
        if full_window:
            return self._search_root(board, depth, -INFINITY, INFINITY)

        delta = ASPIRATION_DELTA
        alpha = previous - delta
        beta = previous + delta

        for _attempt in range(ASPIRATION_MAX_ATTEMPTS):
            result = self._search_root(board, depth, alpha, beta)
            if result.score <= alpha:
                self.aspiration_misses += 1
                delta *= ASPIRATION_WIDEN
                alpha = previous - delta
            elif result.score >= beta:
                self.aspiration_misses += 1
                delta *= ASPIRATION_WIDEN
                beta = previous + delta
            else:
                return result

        # Repeated failures mean the guess was simply wrong; stop paying for it.
        return self._search_root(board, depth, -INFINITY, INFINITY)

    def _search_root(
        self,
        board: chess.Board,
        depth: int,
        alpha: int,
        beta: int,
    ) -> SearchResult:
        """One fixed-depth search of the root, inside the given window.

        The root is kept separate from `_negamax` because it must always return a *move*, so
        it never takes a transposition cutoff -- a cutoff returns a score and would leave the
        caller with nothing to play. It still reads the table's move for ordering, which is
        where iterative deepening's gain comes from: the previous iteration's best move is
        tried first.
        """
        moves = list(board.legal_moves)
        if not moves:
            score = -MATE if board.is_check() else 0
            return SearchResult(move=None, score=score, depth=depth, stats=self.stats)

        window_alpha = alpha
        self._root_best_so_far = None
        tt = self.tt
        tt_move = None
        key = 0
        if tt is not None:
            key = self.zobrist.key
            entry = tt.probe(key)
            if entry is not None:
                tt_move = entry.move

        if self.config.move_ordering:
            # The root is ordered too. It is one node out of hundreds of thousands, but it
            # is the node where getting the first move right prunes the most, so it is the
            # single highest-value place to order.
            moves = order_moves(board, moves, self.killers, self.history, 0, tt_move)

        use_pvs = self.config.pvs
        best_move = moves[0]
        best_score = -INFINITY

        for index, move in enumerate(moves):
            if tt is not None:
                self.zobrist.push(board, move)
            else:
                board.push(move)

            if index == 0 or not use_pvs:
                score = -self._negamax(board, depth - 1, -beta, -alpha, 1)
            else:
                score = -self._negamax(board, depth - 1, -alpha - 1, -alpha, 1)
                if alpha < score < beta:
                    self.pvs_researches += 1
                    score = -self._negamax(board, depth - 1, -beta, -alpha, 1)

            if tt is not None:
                self.zobrist.pop(board)
            else:
                board.pop()

            if score > best_score:
                best_score = score
                best_move = move
            if score > alpha:
                alpha = score

            # Published after every completed root move, so that if the hard limit fires
            # mid-iteration the caller can still use what this iteration established. That is
            # sound because the previous iteration's best move is ordered first here (the
            # transposition table supplies it), so any move that has since overtaken it is
            # genuinely better at this deeper depth.
            self._root_best_so_far = (best_move, best_score, index + 1)

        if tt is not None:
            # Only an exact value may be stored as EXACT. Inside an aspiration window the
            # root can fail low -- no move beat the window's alpha -- and the score is then
            # only an upper bound. Storing that as EXACT would poison the table with a value
            # the next iteration trusts completely.
            if best_score <= window_alpha:
                flag = UPPERBOUND
            elif best_score >= beta:
                flag = LOWERBOUND
            else:
                flag = EXACT
            tt.store(key, depth, best_score, flag, best_move, 0)

        self.stats.depth_reached = depth
        return SearchResult(move=best_move, score=best_score, depth=depth, stats=self.stats)

    def search_timed(
        self,
        board: chess.Board,
        hard_ms: int,
        soft_ms: int | None = None,
        max_depth: int = 64,
    ) -> TimedResult:
        """Search to a time budget rather than to a fixed depth.

        This is what a real engine actually does: it is handed a clock, not a depth. Iterative
        deepening is what makes it possible at all, because it leaves a usable best move after
        every completed iteration, so the search can be stopped at any moment and still have
        an answer.

        Two limits, doing different jobs:

        * `soft_ms` gates **starting** another iteration. After each one the search predicts
          what the next would cost, from the growth actually observed between the last two
          iterations, and declines to start one that will not fit. Stopping cleanly between
          iterations is much better than being cut off inside one.
        * `hard_ms` **aborts** mid-iteration. Needed because the prediction can be wrong -- a
          position where the score suddenly swings makes an iteration far more expensive than
          its predecessor suggested.

        An aborted iteration's **score is discarded**, because a partially searched root has
        only examined some of its moves and its score is a lower bound, not a value. Its
        **move** is kept when at least one root move completed, which is sound because the
        previous iteration's best move is searched first, so anything that overtook it is
        better at the deeper depth.
        """
        if soft_ms is None:
            soft_ms = hard_ms

        start = time.perf_counter()
        self._deadline = start + hard_ms / 1000.0
        depth_floor = len(board.move_stack)

        if self.config.transposition_table:
            self.zobrist = ZobristTracker(board)
            self.tt.new_search()

        best = TimedResult(
            move=None, score=0, depth=0, completed_depth=0,
            elapsed_ms=0.0, nodes=0, aborted=False,
        )
        previous_score: int | None = None
        iteration_times: list[float] = []

        for depth in range(1, max_depth + 1):
            iteration_start = time.perf_counter()
            try:
                result = self._search_with_aspiration(board, depth, previous_score)
            except SearchAborted:
                self._restore(board, depth_floor)
                best.aborted = True
                partial = self._root_best_so_far
                if partial is not None and partial[2] >= 1 and depth > best.completed_depth:
                    # Keep the deeper move, but not its score: a root that has examined only
                    # some of its moves gives a lower bound, and reporting it as the position's
                    # value would be reporting a number the search never established.
                    best.move = partial[0]
                    best.depth = depth
                break

            elapsed = time.perf_counter() - iteration_start
            iteration_times.append(elapsed)
            previous_score = result.score
            best.move = result.move
            best.score = result.score
            best.depth = depth
            best.completed_depth = depth

            if result.is_mate and result.score > 0:
                break
            if result.move is None:
                break

            total_elapsed = time.perf_counter() - start
            if total_elapsed * 1000.0 >= soft_ms:
                break
            if not self._next_iteration_fits(iteration_times, total_elapsed, hard_ms):
                break

        self._deadline = None

        if best.move is None:
            # An engine must never answer "no move". This only happens with a budget so small
            # that not even one root move finished before the first clock check, which is
            # 256 nodes away -- so it is a guard against a pathological clock rather than an
            # expected path, but a guard worth having, because the alternative is forfeiting.
            #
            # It also records an honest limitation: the hard limit's granularity is 256
            # nodes, so a budget smaller than that much work cannot be honoured. Measured, a
            # 1 ms budget on a dense middlegame takes about 16 ms. For the hundreds of
            # milliseconds a real time control allows, this does not arise.
            legal = list(board.legal_moves)
            best.move = legal[0] if legal else None

        best.elapsed_ms = (time.perf_counter() - start) * 1000.0
        best.nodes = self.stats.nodes
        return best

    @staticmethod
    def _next_iteration_fits(
        iteration_times: list[float],
        total_elapsed: float,
        hard_ms: int,
    ) -> bool:
        """Is there any prospect of another iteration finishing?

        The prediction is compared against the **hard** budget, not the soft one, and that is
        a deliberate choice worth explaining because the obvious alternative is worse.

        Testing against the soft budget means only starting an iteration that is expected to
        finish inside the target. It sounds right and it wastes a lot of clock: on `kiwipete`
        with a 1,000 ms target, that rule stopped at depth 3 after 678 ms, declining to spend
        the remaining third because depth 4 would not fit.

        Testing against the hard budget starts the iteration anyway. If it finishes, a whole
        extra ply has been gained; if it does not, the abort keeps the best move the partial
        iteration found, which is at least as good as the previous iteration's because the
        previous best move is searched first. The cost is that the reported score then comes
        from a shallower depth than the reported move, which `TimedResult.completed_depth`
        makes explicit rather than hiding.

        The growth factor is **measured** from the last two iterations rather than assumed,
        because it varies a lot with the position -- a quiet endgame with a low branching
        factor grows far more slowly than a dense middlegame. It is clamped so that one
        anomalous iteration (an aspiration miss, a mate suddenly appearing) cannot produce a
        prediction of either zero or infinity.
        """
        last = iteration_times[-1]
        hard_seconds = hard_ms / 1000.0

        # An iteration that consumed a negligible slice of the budget tells you nothing
        # useful about the next one's cost, and cannot plausibly blow the budget on its own,
        # so just continue.
        #
        # This guard is not a detail -- leaving it out produced a measurably wrong benchmark.
        # Dividing two very fast iterations gives a wild ratio, and with the clamp set
        # generously the prediction came out enormous: `tt-id` stopped at depth 3 on
        # `italian` having used 2,962 nodes of a 1,500 ms budget, while the slower
        # `quiescence-see` reached depth 4 simply because its iterations were big enough for
        # the growth estimate to be meaningful. The timed benchmark was therefore partly
        # comparing time managers rather than searches.
        if last < hard_seconds * MIN_ITERATION_FRACTION:
            return True

        # Plenty of budget left: attempt another ply rather than banking the time. An abort
        # keeps the deeper move, so the downside is a score from one ply shallower, while the
        # upside is a whole extra ply of search.
        if total_elapsed < hard_seconds * OPTIMISTIC_FRACTION:
            return True

        ratios = [
            later / earlier
            for earlier, later in zip(iteration_times, iteration_times[1:])
            if earlier > 0
        ]
        growth = statistics.median(ratios[-3:]) if ratios else DEFAULT_GROWTH
        growth = min(max(growth, MIN_GROWTH), MAX_GROWTH)

        return (total_elapsed + last * growth) <= hard_seconds

    def _restore(self, board: chess.Board, depth_floor: int) -> None:
        """Undo the pushes left behind when an abort unwound the recursion.

        `SearchAborted` propagates straight through the move loops, so the pops never run and
        the board is left somewhere deep in a variation. Wrapping every push in `try/finally`
        would fix it at the cost of a context manager on the hottest path in the engine;
        popping back to a recorded depth afterwards costs nothing and is just as correct.

        The Zobrist key is re-seeded rather than unwound, because its stack is in the same
        state as the board's and re-seeding from the restored position is unambiguous.
        """
        while len(board.move_stack) > depth_floor:
            board.pop()
        if self.zobrist is not None:
            self.zobrist.reset(board)

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
        self.quiescence_nodes = 0
        self.max_quiescence_ply = 0
        self.quiescence_cap_hits = 0
        self.see_prunes = 0
        self.pvs_researches = 0
        self.aspiration_misses = 0
        self._deadline = None
        self._root_best_so_far = None
        if self.tt is not None:
            self.tt.clear()
