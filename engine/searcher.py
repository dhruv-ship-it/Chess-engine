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
* `pvs` -- score-preserving provided the full-window re-search on a fail-high is present.
  A missing re-search is exactly what a score change catches.
"""

from __future__ import annotations

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
}

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

        best = -INFINITY
        best_move = None
        for move in moves:
            if tt is not None:
                self.zobrist.push(board, move)
            else:
                board.push(move)

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
        self.stats.nodes += 1
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
            return self._search_root(board, depth)

        result = SearchResult(move=None, score=0, depth=0, stats=self.stats)
        for current in range(1, depth + 1):
            result = self._search_root(board, current)
            # A mate in our favour cannot be improved on by searching deeper, and continuing
            # would only find the same mate at a worse distance.
            if result.is_mate and result.score > 0:
                break
        return result

    def _search_root(self, board: chess.Board, depth: int) -> SearchResult:
        """One fixed-depth search of the root.

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

        alpha = -INFINITY
        best_move = moves[0]
        best_score = -INFINITY

        for move in moves:
            if tt is not None:
                self.zobrist.push(board, move)
            else:
                board.push(move)

            score = -self._negamax(board, depth - 1, -INFINITY, -alpha, 1)

            if tt is not None:
                self.zobrist.pop(board)
            else:
                board.pop()

            if score > best_score:
                best_score = score
                best_move = move
            if score > alpha:
                alpha = score

        if tt is not None:
            # The root is searched with a full window, so its score is always exact.
            tt.store(key, depth, best_score, EXACT, best_move, 0)

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
        self.quiescence_nodes = 0
        self.max_quiescence_ply = 0
        self.quiescence_cap_hits = 0
        self.see_prunes = 0
        if self.tt is not None:
            self.tt.clear()
