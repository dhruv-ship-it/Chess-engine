"""Move ordering: MVV-LVA, killer moves and the history heuristic.

Why ordering is the first optimisation and not a micro-optimisation
------------------------------------------------------------------
Alpha-beta's entire saving comes from finding a cutoff *early*. If the best move at a node
is the first one searched, everything under the remaining moves gets pruned; if it is the
last, nothing gets pruned and the search degenerates to full minimax. Identical answer,
wildly different cost. In the best case alpha-beta visits about the square root of the tree,
which is roughly double the depth for the same work -- but only with good ordering. Ordering
changes the effective branching factor, and the branching factor sits in the exponent, so
this is an exponential-scale change dressed up as a sorting problem.

**Ordering cannot change the score.** Reordering the siblings of a node cannot change its
minimax value. So the gate for this module is: identical score at a fixed depth, strictly
fewer nodes. A score change here is a bug, not a trade-off.

The three heuristics, and why each one exists
---------------------------------------------
They are complementary: one is static, one is local, one is global.

* **MVV-LVA** (Most Valuable Victim, Least Valuable Attacker) -- static, for captures. Sort
  captures by what they capture, descending, then by what captures with, ascending. Pawn
  takes queen is the best thing you could possibly try first; queen takes pawn is the worst
  capture to lead with, because it is most likely to simply lose the queen. No search cost
  at all, just two piece lookups.

* **Killer moves** -- ply-local, for quiet moves. A quiet move that caused a beta cutoff at
  the same ply in a *sibling* subtree very often causes one here too, because sibling
  positions differ by one move and are therefore nearly identical. Stored per ply, because
  that similarity is a ply-local property: a killer from a different depth is describing a
  different position.

* **History heuristic** -- global, for the remaining quiet moves. A table of
  (colour, from-square, to-square) counters incremented whenever a quiet move causes a
  cutoff anywhere in the tree. It answers "has this move been good in this game so far",
  which is a weak signal but free and better than arbitrary generation order.

Only *quiet* moves feed the killer and history tables. Captures are already ordered by
MVV-LVA, so spending killer slots on them wastes the slots.
"""

from __future__ import annotations

import chess

from engine.evaluate import PAWN, PIECE_VALUES

# Priority bands. The transposition-table move must outrank every capture, every capture
# must outrank every killer, and every killer must outrank every history-scored quiet move,
# so the bands are spaced far wider than any score that can be produced inside them.
#
# A capture's score is CAPTURE_BASE + at most (900 + 900) * 16, about 29k, so captures
# occupy [1_000_000, 1_029_000] and can never reach the TT band or fall into the killer band.
#
# The TT move goes first because it is the best-informed guess available: it is the move that
# was actually best when this position was searched before, which is strictly better evidence
# than any static heuristic. See engine/transposition.py.
TT_MOVE = 2_000_000
CAPTURE_BASE = 1_000_000
KILLER_PRIMARY = 900_000
KILLER_SECONDARY = 890_000

# History counters are kept strictly below the killer band by halving the whole table when
# any entry reaches this ceiling. See `HistoryTable.record`.
HISTORY_CEILING = 800_000

# The attacker term in MVV-LVA only needs to *order* attackers, not value them, so it uses
# an ordinal rank rather than a centipawn value. Using centipawns would make the king
# (worth 20000 in the evaluation, because it must dominate material) the least attractive
# attacker by a margin large enough to swamp the victim term, which would order
# king-takes-queen below pawn-takes-pawn. The rank is multiplied by 1 and the victim value
# by 16, so the victim is always the primary key: the closest two victims are the knight
# (320) and the bishop (330), giving a gap of 160, against a maximum attacker term of 6.
_ATTACKER_RANK = {
    chess.PAWN: 1,
    chess.KNIGHT: 2,
    chess.BISHOP: 3,
    chess.ROOK: 4,
    chess.QUEEN: 5,
    chess.KING: 6,
}

_VICTIM_WEIGHT = 16

# Killers are indexed by ply, so the table needs a hard upper bound. 128 plies is far beyond
# anything this engine will search, including quiescence extensions.
MAX_PLY = 128

# Two killer slots per ply is the standard choice. One is measurably worse; three and beyond
# add table pressure for very little, because the second-best cutoff move at a ply is already
# a weak signal.
KILLERS_PER_PLY = 2


def captured_value(board: chess.Board, move: chess.Move) -> int:
    """Centipawn value gained by `move`, counting the captured piece and any promotion.

    Returns 0 for a quiet move, which is also how callers detect one.

    En passant is the awkward case and the reason this is a function rather than a lookup:
    the captured pawn is not standing on the destination square, so `piece_type_at` on the
    destination returns `None` even though the move is a capture. `is_en_passant` is only
    consulted when the destination is empty, keeping it off the common path.
    """
    victim = board.piece_type_at(move.to_square)
    if victim is None:
        gain = PIECE_VALUES[PAWN] if board.is_en_passant(move) else 0
    else:
        gain = PIECE_VALUES[victim]

    if move.promotion is not None:
        # The pawn is consumed and a new piece appears, so the net material change is the
        # promoted piece minus the pawn.
        gain += PIECE_VALUES[move.promotion] - PIECE_VALUES[PAWN]

    return gain


def mvv_lva(board: chess.Board, move: chess.Move, gain: int) -> int:
    """MVV-LVA score for a capture or promotion whose `gain` is already computed."""
    attacker = board.piece_type_at(move.from_square)
    rank = _ATTACKER_RANK[attacker] if attacker is not None else 0
    return CAPTURE_BASE + gain * _VICTIM_WEIGHT - rank


class KillerTable:
    """Two quiet moves per ply that previously caused a beta cutoff."""

    __slots__ = ("_slots",)

    def __init__(self) -> None:
        self._slots: list[list[chess.Move | None]] = [
            [None] * KILLERS_PER_PLY for _ in range(MAX_PLY)
        ]

    def clear(self) -> None:
        for slot in self._slots:
            for i in range(KILLERS_PER_PLY):
                slot[i] = None

    def record(self, ply: int, move: chess.Move) -> None:
        """Store `move` as the primary killer for `ply`, shifting the old one down.

        Shifting rather than overwriting keeps the second-best cutoff move around, which
        matters because the cutoff move alternates between two candidates in many positions.
        Re-recording a move already in the primary slot is ignored, otherwise a single
        repeatedly-good move would push itself into both slots and the second slot would
        carry no information.
        """
        if ply >= MAX_PLY:
            return
        slot = self._slots[ply]
        if slot[0] == move:
            return
        slot[1] = slot[0]
        slot[0] = move

    def primary(self, ply: int) -> chess.Move | None:
        return self._slots[ply][0] if ply < MAX_PLY else None

    def secondary(self, ply: int) -> chess.Move | None:
        return self._slots[ply][1] if ply < MAX_PLY else None


class HistoryTable:
    """Per-colour (from-square, to-square) cutoff counters for quiet moves."""

    __slots__ = ("_table",)

    # Indexed as colour * 4096 + from_square * 64 + to_square. A flat list rather than a
    # dict or nested lists because this is read once per quiet move at every node, and a
    # single integer index into a flat list is the cheapest lookup available in CPython.
    _SIZE = 2 * 64 * 64

    def __init__(self) -> None:
        self._table = [0] * self._SIZE

    def clear(self) -> None:
        table = self._table
        for i in range(self._SIZE):
            table[i] = 0

    @staticmethod
    def _index(color: bool, move: chess.Move) -> int:
        # `color` is a bool in python-chess: True is White. Keeping the colours in separate
        # halves matters because a quiet move that is good for White says nothing about the
        # same from/to squares played by Black -- the position is upside down.
        return (4096 if color else 0) + (move.from_square << 6) + move.to_square

    def record(self, color: bool, move: chess.Move, depth: int) -> None:
        """Credit `move` for a cutoff at `depth`.

        The increment is `depth * depth`, not 1. A cutoff near the root prunes a subtree
        that is exponentially larger than one near the leaves, so it is far stronger
        evidence about the move and is weighted accordingly. Squaring is the conventional
        choice; it is a heuristic weight, not a derived quantity.

        When any counter reaches the ceiling the whole table is halved. That does two jobs
        at once: it keeps history scores strictly below the killer band, so the priority
        ordering can never invert, and it ages the table so that evidence from early in a
        long search decays rather than permanently dominating.
        """
        table = self._table
        index = self._index(color, move)
        table[index] += depth * depth
        if table[index] >= HISTORY_CEILING:
            for i in range(self._SIZE):
                table[i] >>= 1

    def score(self, color: bool, move: chess.Move) -> int:
        return self._table[self._index(color, move)]


def order_moves(
    board: chess.Board,
    moves: list[chess.Move],
    killers: KillerTable,
    history: HistoryTable,
    ply: int,
    tt_move: chess.Move | None = None,
) -> list[chess.Move]:
    """Return `moves` sorted best-guess-first.

    `tt_move` is scored first when present. It is matched against the generated move list
    rather than trusted, which is the defence against a hash collision: an entry from a
    different position that happens to index to the same slot can carry a move that is
    illegal here, and such a move simply never matches anything in `moves` and is ignored.
    A transposition table must never be trusted to prove legality.

    A full sort, not a lazy selection sort. Most nodes cut off after one or two moves, so
    sorting all ~35 moves does strictly more work than necessary, and real engines stage
    this: yield the transposition-table move, then generate and pick captures, then quiets,
    stopping as soon as a cutoff happens. That is a worthwhile later optimisation and it is
    measured separately; the full sort is used here because it is obviously correct and this
    step's purpose is to establish the node reduction, not to chase constant factors.

    `sorted` is stable, so moves with equal keys keep `python-chess`'s generation order and
    the result stays deterministic -- which the benchmark depends on.
    """
    color = board.turn
    killer_1 = killers.primary(ply)
    killer_2 = killers.secondary(ply)

    def key(move: chess.Move) -> int:
        if move == tt_move:
            return TT_MOVE
        gain = captured_value(board, move)
        if gain:
            return mvv_lva(board, move, gain)
        if move == killer_1:
            return KILLER_PRIMARY
        if move == killer_2:
            return KILLER_SECONDARY
        return history.score(color, move)

    return sorted(moves, key=key, reverse=True)
