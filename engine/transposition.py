"""Transposition table: a bounded cache of search results, keyed by Zobrist hash.

What it is for, in order of actual value
----------------------------------------
1. **It supplies a best move for ordering.** This is the largest practical benefit and the
   one most often left out. A position searched before has a known-good first move, and
   trying that move first is worth more than any static heuristic -- see `engine/ordering.py`
   for why ordering dominates everything. The predecessor engine's table stored only
   `{depth, score, flag}` and therefore could not help ordering at all, which removed most of
   the reason to have a table.
2. **It avoids re-searching transpositions.** The same position reached by a different move
   order can be answered from the table instead of searched again.

Why there must be three flags
-----------------------------
A score stored in the table is only as trustworthy as the window it was searched with.
Alpha-beta does not compute an exact value at every node -- when it cuts off, it has only
established a bound.

* **EXACT** -- every move was examined and the score fell strictly inside `(alpha, beta)`.
  This is the true value and is usable directly.
* **LOWERBOUND** -- a fail-high, i.e. a beta cutoff: some move scored at least `beta`, so the
  search stopped. The true value is *at least* this score and may be higher. Usable only to
  prove the position is good enough: cut off if the stored score is at least the current
  `beta`.
* **UPPERBOUND** -- a fail-low: no move beat `alpha`, so the true value is *at most* this
  score. Usable only to prove the position is bad enough: cut off if the stored score is at
  most the current `alpha`.

Confusing these is a classic bug and it is **silent** -- the engine runs, returns legal
moves, and is simply wrong sometimes. The test for it is therefore differential: scores with
the table enabled must match scores with it disabled.

Mate scores need adjusting on the way in and out
------------------------------------------------
In this engine a mate score encodes its distance *from the root*: a mated node at ply `p`
returns `-MATE + p`, so a mate found at ply 3 propagates to the root as `MATE - 3`. That
number therefore depends on how far the node was from the root along the path taken.

The same position can be reached at a different ply, at which point a raw stored score would
claim the wrong mate distance. So mate scores are converted to **node-relative** form on
store (add `ply` to a winning score, subtract it from a losing one) and back to root-relative
on probe. Without this, mate distances drift and the search can report mate in 7 for a mate
in 3, or fail to convert a won position because it keeps seeing a different distance.

Why bounded, and the replacement policy
---------------------------------------
An unbounded dictionary grows until memory runs out; the predecessor's did exactly that. This
is a fixed-size list indexed by `key & mask`, with the size a power of two so the index is a
mask rather than a modulo.

Two entries can land in the same slot, so a slot is only usable if its stored key matches --
that check is what makes a collision a miss rather than a wrong answer. The policy for which
of two competing entries to keep is **depth-preferred with ageing**: store if the slot is
empty, or the occupant is from an earlier search (stale), or the new entry was searched at
least as deep. Depth-preferred because a deeper result is more valuable and more expensive to
reproduce; ageing because without it a deep entry from three moves ago would hold a slot
forever against useful current results.

A note on what is *not* in the key
----------------------------------
The halfmove clock is not part of the Zobrist key, which is standard. The consequence is that
two positions identical except for their progress toward the fifty-move rule share a key. All
engines accept this; it is noted rather than fixed, and the draw check runs before the probe
so the live position's own clock is always respected.
"""

from __future__ import annotations

from typing import NamedTuple

import chess

from engine.search import MATE_THRESHOLD

# Flag values. Plain module-level integers rather than an enum: this is compared once per
# probe in the hottest path in the search, and an IntEnum member lookup is measurably slower
# than an int for no readability gain at the call site.
EXACT = 0
LOWERBOUND = 1
UPPERBOUND = 2

FLAG_NAMES = {EXACT: "EXACT", LOWERBOUND: "LOWERBOUND", UPPERBOUND: "UPPERBOUND"}

DEFAULT_SIZE_BITS = 20  # 1,048,576 slots


class TTEntry(NamedTuple):
    """One stored search result.

    A NamedTuple because it is a tuple at runtime -- cheap to allocate and unpack -- while
    still being readable at the point of use.
    """

    key: int
    depth: int
    score: int
    flag: int
    move: chess.Move | None
    generation: int


class TranspositionTable:
    """Fixed-size, direct-indexed transposition table."""

    __slots__ = (
        "_table",
        "_mask",
        "generation",
        "probes",
        "hits",
        "collisions",
        "cutoffs",
        "stores",
        "replacements",
        "declined",
        "used",
    )

    def __init__(self, size_bits: int = DEFAULT_SIZE_BITS) -> None:
        if not 1 <= size_bits <= 26:
            raise ValueError(f"size_bits must be between 1 and 26, got {size_bits}")
        size = 1 << size_bits
        self._table: list[TTEntry | None] = [None] * size
        self._mask = size - 1
        self.generation = 0
        self.clear_stats()
        self.used = 0

    # -- lifecycle ---------------------------------------------------------------------

    def clear(self) -> None:
        """Drop every entry. Used between games, not between search iterations."""
        self._table = [None] * (self._mask + 1)
        self.generation = 0
        self.used = 0
        self.clear_stats()

    def clear_stats(self) -> None:
        self.probes = 0
        self.hits = 0
        self.collisions = 0
        self.cutoffs = 0
        self.stores = 0
        self.replacements = 0
        self.declined = 0

    def new_search(self) -> None:
        """Mark the start of a search, so existing entries become eligible for replacement.

        Entries are kept rather than cleared -- that is the point of the table across
        iterative deepening -- but they are now a generation old, which the replacement
        policy uses to prefer current results over stale deep ones.
        """
        self.generation += 1

    @property
    def size(self) -> int:
        return self._mask + 1

    @property
    def fill_permille(self) -> int:
        """Occupancy in parts per thousand, the unit UCI's `hashfull` uses."""
        return (self.used * 1000) // self.size

    # -- mate-score conversion ---------------------------------------------------------

    @staticmethod
    def to_node_relative(score: int, ply: int) -> int:
        """Convert a root-relative mate score into one relative to this node."""
        if score >= MATE_THRESHOLD:
            return score + ply
        if score <= -MATE_THRESHOLD:
            return score - ply
        return score

    @staticmethod
    def to_root_relative(score: int, ply: int) -> int:
        """Inverse of `to_node_relative`, applied when a stored score is read back."""
        if score >= MATE_THRESHOLD:
            return score - ply
        if score <= -MATE_THRESHOLD:
            return score + ply
        return score

    # -- probe and store ---------------------------------------------------------------

    def probe(self, key: int) -> TTEntry | None:
        """Return the entry for `key`, or None.

        A slot whose stored key differs holds another position that happens to index here, so
        it is a miss. That key comparison is the only thing standing between a hash collision
        and a wrong answer, which is why a stored move is still re-validated against the
        generated move list before being used.
        """
        self.probes += 1
        entry = self._table[key & self._mask]
        if entry is None:
            return None
        if entry.key != key:
            self.collisions += 1
            return None
        self.hits += 1
        return entry

    def store(
        self,
        key: int,
        depth: int,
        score: int,
        flag: int,
        move: chess.Move | None,
        ply: int,
    ) -> None:
        """Store a result, applying the replacement policy and the mate-score conversion."""
        slot = key & self._mask
        existing = self._table[slot]

        if existing is not None:
            stale = existing.generation != self.generation
            if not stale and depth < existing.depth:
                self.declined += 1
                return
            self.replacements += 1
        else:
            self.used += 1

        self._table[slot] = TTEntry(
            key=key,
            depth=depth,
            score=self.to_node_relative(score, ply),
            flag=flag,
            move=move,
            generation=self.generation,
        )
        self.stores += 1

    def stats(self) -> dict[str, int | float]:
        """Counters, for the benchmark and the devlog."""
        hit_rate = (self.hits / self.probes * 100) if self.probes else 0.0
        return {
            "size": self.size,
            "probes": self.probes,
            "hits": self.hits,
            "hit_rate_pct": round(hit_rate, 1),
            "collisions": self.collisions,
            "cutoffs": self.cutoffs,
            "stores": self.stores,
            "replacements": self.replacements,
            "declined": self.declined,
            "fill_permille": self.fill_permille,
        }
