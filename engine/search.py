"""Negamax search with alpha-beta pruning.

This is the Phase 0 baseline: correct, deliberately plain, and with no move ordering,
transposition table or quiescence search. Those arrive in Phase 1 and are measured against
this baseline, which is the whole reason the baseline exists and is kept correct.

Two invariants the predecessor engine violated, both enforced here:

1. `evaluate()` returns a score relative to the side to move, so negating on the way up is
   sound.
2. At a mated node the side to move is the one that has been mated, so the score is always
   `-MATE + ply` -- never an absolute, result-derived constant. The `+ ply` term makes a
   faster mate score higher than a slower one, so the search prefers mate in 2 to mate in 4
   and, when losing, delays mate as long as possible.
"""

from dataclasses import dataclass, field

import chess

from engine.evaluate import evaluate

# Comfortably larger than any evaluation, and far enough below the integer bounds that
# `-MATE + ply` and the alpha/beta window arithmetic cannot overflow into nonsense.
MATE = 1_000_000

# A mate score is any score within MATE_THRESHOLD of MATE; used to recognise and report
# mate distances rather than raw centipawns.
MATE_THRESHOLD = MATE - 1000

INFINITY = MATE * 10


@dataclass
class SearchStats:
    """Node counters. `nodes` counts every visited node, `leaves` only evaluated leaves."""

    nodes: int = 0
    leaves: int = 0
    depth_reached: int = 0

    def reset(self) -> None:
        self.nodes = 0
        self.leaves = 0
        self.depth_reached = 0


@dataclass
class SearchResult:
    move: chess.Move | None
    score: int
    depth: int
    stats: SearchStats = field(default_factory=SearchStats)

    @property
    def is_mate(self) -> bool:
        return abs(self.score) >= MATE_THRESHOLD

    @property
    def mate_in(self) -> int | None:
        """Mate distance in plies, positive if the mover is delivering it."""
        if not self.is_mate:
            return None
        plies = MATE - abs(self.score)
        return plies if self.score > 0 else -plies


def _is_drawn(board: chess.Board) -> bool:
    """Draws that are not stalemate.

    Stalemate is handled by the no-legal-moves branch in the search itself. Repetition is
    intentionally excluded for now: detecting it correctly inside a search requires tracking
    the path from the root, which arrives with the transposition table in Phase 1.
    """
    return board.is_insufficient_material() or board.is_fifty_moves()


def negamax(
    board: chess.Board,
    depth: int,
    alpha: int,
    beta: int,
    ply: int = 0,
    stats: SearchStats | None = None,
) -> int:
    """Return the score of `board` relative to the side to move.

    Fail-soft: the returned score may lie outside the (alpha, beta) window, which keeps the
    value usable by callers that want a bound rather than just a cutoff flag.
    """
    if stats is not None:
        stats.nodes += 1

    if ply > 0 and _is_drawn(board):
        return 0

    # Terminal detection must come *before* the depth cutoff. A mated position has a
    # definite value no matter how much depth is left, so returning a static evaluation
    # there would hide mates from a depth-1 search. The cost is generating moves even at
    # leaves; this is one of the reasons real engines enter quiescence at depth 0, which
    # needs a move list anyway, and Phase 1 restructures this accordingly.
    moves = list(board.legal_moves)

    if not moves:
        # No legal moves: mated if in check, stalemate otherwise. The side to move is the
        # one that is mated, so this is always negative from here.
        return -MATE + ply if board.is_check() else 0

    if depth <= 0:
        if stats is not None:
            stats.leaves += 1
        return evaluate(board)

    best = -INFINITY
    for move in moves:
        board.push(move)
        score = -negamax(board, depth - 1, -beta, -alpha, ply + 1, stats)
        board.pop()

        if score > best:
            best = score
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break

    return best


def search(
    board: chess.Board,
    depth: int,
    stats: SearchStats | None = None,
) -> SearchResult:
    """Search the root to a fixed depth and return the best move and its score."""
    if stats is None:
        stats = SearchStats()

    moves = list(board.legal_moves)
    if not moves:
        score = -MATE if board.is_check() else 0
        return SearchResult(move=None, score=score, depth=depth, stats=stats)

    alpha = -INFINITY
    best_move = moves[0]
    best_score = -INFINITY

    for move in moves:
        board.push(move)
        score = -negamax(board, depth - 1, -INFINITY, -alpha, 1, stats)
        board.pop()

        if score > best_score:
            best_score = score
            best_move = move
        if score > alpha:
            alpha = score

    stats.depth_reached = depth
    return SearchResult(move=best_move, score=best_score, depth=depth, stats=stats)


def search_iterative(
    board: chess.Board,
    max_depth: int,
    stats: SearchStats | None = None,
) -> SearchResult:
    """Iterative deepening from depth 1 to `max_depth`.

    Without a transposition table this buys no speed, so for now it exists to give a stable
    best-move-so-far at every depth. It becomes load-bearing in Phase 1, when the previous
    iteration's best move seeds move ordering.
    """
    if stats is None:
        stats = SearchStats()

    result = SearchResult(move=None, score=0, depth=0, stats=stats)
    for depth in range(1, max_depth + 1):
        result = search(board, depth, stats)
        # An exact mate score means the outcome cannot change at greater depth.
        if result.is_mate and result.score > 0:
            break
    return result


def best_move(board: chess.Board, depth: int) -> chess.Move | None:
    """Convenience wrapper returning just the move."""
    return search(board, depth).move
