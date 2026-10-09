# Engineering notes

A technical description of how this engine is built, what each component is for, and how
every reported number was produced. Written for an engineer evaluating the work. For the
chronological account of how it got here, see [DEVLOG.md](DEVLOG.md); for the decision
history and the audit that motivated the rebuild, see
[../ENGINEERING_NOTES.md](../ENGINEERING_NOTES.md).

---

## 1. Scope, and what is deliberately not claimed

This is a classical alpha-beta chess engine in Python. It is a demonstration of search
engineering, not an attempt at a strong engine, and the difference is worth stating plainly
up front because it determines what the numbers mean.

**What is implemented here:** evaluation, search, move ordering, transposition table,
quiescence, and the measurement harness that attributes a value to each of those.

**What is not, and is not claimed:** board representation, move generation and legality all
come from [`python-chess`](https://python-chess.readthedocs.io/). This project does not
implement bitboards or a move generator, and writing one is explicitly out of scope.

**On strength.** A Python engine is roughly two orders of magnitude slower than a C++ one.
Stockfish searches tens of millions of nodes per second with an NNUE evaluation; this
searches tens of thousands with hand-written piece-square tables. For calibration,
[Sunfish](https://github.com/thomasahle/sunfish) is about 130 lines of Python and plays
around 1900-2000 on Lichess — smaller and stronger than anything here. The reason to build
this is to understand and measure search, and where ground-truth evaluation is ever needed,
that is Stockfish's job.

**On perft.** The repo has a perft harness, and it does *not* validate move generation here,
because `python-chess` owns move generation. It is kept as a throughput benchmark for the
make/unmake path and as the tree-walk harness that validates incremental Zobrist hashing.
Section 9 covers what the real correctness harness is.

---

## 2. Architecture

```
engine/
  evaluate.py      static evaluation: material + piece-square tables, side-relative
  search.py        FROZEN Phase 0 control: plain negamax + alpha-beta, nothing else
  searcher.py      Phase 1 search: features behind flags on a frozen SearchConfig
  ordering.py      MVV-LVA, killer moves, history heuristic
  zobrist.py       incremental Zobrist hashing, O(1) per move
  transposition.py bounded table with EXACT/LOWERBOUND/UPPERBOUND and best-move storage
bench/
  positions.py     the 13-position benchmark set, validated at import
  nodes.py         fixed-depth node/time benchmark, JSON baselines, comparison mode
  perft.py         move-tree node counts vs published references; throughput benchmark
tests/
  conftest.py      the search_fn fixture: every configuration, so tests cover them all
  test_eval.py     evaluation invariants, including symmetry and table lengths
  test_search.py   search invariants: mate sign, mate distance, window agreement
  test_searcher.py differential tests of Phase 1 against the frozen baseline
  test_ordering.py unit and differential tests for move ordering
  test_zobrist.py  tree-walk validation of the incremental key against recomputation
  test_transposition.py flag semantics, mate conversion, replacement, differentials
  test_tactics.py  end-to-end tactical regression, run against every configuration
  test_bench.py    tests for the measurement harness itself
```

### Why there are two search modules

`engine/search.py` is frozen. It is plain negamax with alpha-beta and no other feature, it is
known correct, and it is the control that every Phase 1 measurement is taken against.
Optimising it in place would destroy the baseline, so Phase 1 lives in `engine/searcher.py`.

### Why features sit behind flags

Phase 2 has to answer "what is each feature worth?", in nodes at fixed depth and in Elo at
fixed time. That requires running the *same* code with one feature disabled. Real engines
expose exactly these switches over UCI for exactly this reason. So every feature is a
boolean on a frozen `SearchConfig`, and named configurations live in one registry:

```python
CONFIGS = {
    "phase0":       SearchConfig(),
    "ordering":     SearchConfig(move_ordering=True),
    "tt-move-only": SearchConfig(move_ordering=True, transposition_table=True,
                                 tt_score_cutoffs=False),
    "tt":           SearchConfig(move_ordering=True, transposition_table=True),
    "tt-id":        SearchConfig(move_ordering=True, transposition_table=True,
                                 iterative_deepening=True),
    "id-only":      SearchConfig(move_ordering=True, iterative_deepening=True),
}
```

The configs are deliberately finer-grained than the features. A transposition table contains
**three separable claims** -- the best-move hint, the score cutoff, and the iterative
deepening that exploits the hint -- and they turn out to be worth wildly different amounts
(section 7). Reporting one combined figure would have hidden that, and would have let the
weakest of the three take credit for the strongest.

The benchmark CLI and the test fixture both read that registry, so adding a feature
subscribes it to the benchmark and to the tactical suite without editing either.

The flag plumbing is itself code, and code that quietly changes behaviour would corrupt
every number the project reports. So the all-flags-off config is required by test to match
the frozen search on **three** things simultaneously: the score, the best move, and the
**exact node count**. Node-count equality is the strict one — it fails on any change to
visit order or cutoff logic, including changes that leave the score intact.

---

## 3. Evaluation

Material plus piece-square tables, using Tomasz Michniewski's
[Simplified Evaluation Function](https://www.chessprogramming.org/Simplified_Evaluation_Function)
— standard published tables from the Chess Programming Wiki, not a hand-tuned invention.
Michniewski's stated purpose fits this project exactly: he proposed that engines under test
share one simplistic evaluation so that *only search and efficiency* affect the result, which
is precisely what the Phase 2 harness measures. (The upgrade path, if evaluation ever becomes
the subject, is [PeSTO](https://chessprogramming.org/PeSTO%27s_Evaluation_Function), which is
tapered between midgame and endgame.)

Two implementation details carry real risk and so are worth stating.

### Scores are relative to the side to move

```python
def evaluate(board):
    score = evaluate_white(board)
    return score if board.turn == chess.WHITE else -score
```

Negamax collapses minimax's maximise/minimise pair into one function by negating the child's
score on the way back up. That negation is only meaningful if a leaf score means "good for
whoever is about to move". An absolute, White-positive leaf score makes the whole search
incoherent — and does so *silently*, because the engine still runs and still returns legal
moves. This is the bug that broke the predecessor project: for `4k3/8/8/8/8/8/8/3QK3` its
evaluation returned +890 with White to move *and* +890 with Black to move, so Black, a queen
down, scored the position as winning.

`evaluate_white()` is kept for debugging and for the symmetry test, and the search never
calls it.

### Table mirroring is `square ^ 56`

The tables are written from White's point of view with a8 first, while `python-chess` numbers
a1 as 0 and h8 as 63. White's lookup therefore has to flip the rank. A square index is six
bits, three of rank and three of file; XORing with 56 (`0b111000`) inverts the rank bits and
leaves the file bits alone, which is exactly a vertical mirror — a1 (0) to index 56, h1 (7)
to 63, a8 (56) to 0. Black reads the table directly, which mirrors it.

Both obvious alternatives are wrong, and one of them was a real inherited bug:

* `table[-square]` — Python negative indexing maps square 0 to index 0 and square 1 to index
  63. That is a 180-degree rotation off by one, not a rank flip. This was the predecessor's
  actual code.
* `table[63 - square]` — mirrors files as well as ranks. Harmless for symmetric tables, wrong
  for any asymmetric one.

Each table is asserted to be exactly 64 entries at import time. That is not defensive
decoration: five of the predecessor's six tables had drifted to 62, 63, 80, 82 and 104
entries through duplicated and dropped rows, so every positional lookup read the wrong
square. An import-time assertion makes that class of corruption impossible to reintroduce
silently.

---

## 4. Search

Negamax with alpha-beta, fail-soft.

```python
score = -negamax(board, depth - 1, -beta, -alpha, ply + 1)
```

The window is negated *and* swapped, because what is an upper bound for me is a lower bound
for my opponent.

**Fail-soft** means the function returns the best score it actually saw, which may lie
outside `(alpha, beta)`, rather than clamping to the window. The extra information is strictly
more useful to a transposition table, which wants the tightest bound available.

### Mate scores

At a node with no legal moves and the king in check, the side to move has *been mated*, so in
a side-relative convention the value is always negative:

```python
return -MATE + ply if board.is_check() else 0
```

The predecessor returned `+1000000` whenever `board.result() == "1-0"`. That is correct in the
absolute convention its original author used, and wrong in the negamax convention it was
converted to — which is why it would not deliver mate as White while it *would* as Black. The
sign happened to work out for one colour, which is exactly the kind of half-working bug that
survives casual testing.

The `+ ply` term encodes mate distance. Without it, every mating line scores identically and
the engine can shuffle indefinitely instead of playing a mate it has already found. With it,
a mate in 2 outscores a mate in 4, so the winning side converges and the losing side delays.

### Terminal detection precedes the depth cutoff

Move generation happens before the `depth <= 0` check. A mated position has a definite value
regardless of remaining depth, so evaluating it statically would hide mate from a depth-1
search. This was a real bug caught by the Phase 0 tests rather than by inspection. The cost
is generating moves at leaf nodes, which is one of the reasons real engines enter quiescence
at depth 0 — quiescence needs a move list anyway.

---

## 5. Move ordering

Alpha-beta's entire saving comes from finding a cutoff *early*. If the best move at a node is
searched first, everything under the remaining moves is pruned; if it is searched last,
nothing is pruned and the search degenerates to full minimax. Same answer, wildly different
cost. With perfect ordering alpha-beta visits about the square root of the minimax tree,
which is roughly double the depth for the same work. Ordering changes the effective branching
factor, and the branching factor sits in the exponent — so this is an exponential-scale change
that looks like a sorting problem.

Three complementary heuristics: one static, one ply-local, one global.

| heuristic | scope | applies to | cost |
|---|---|---|---|
| MVV-LVA | static | captures and promotions | two piece lookups |
| killer moves | per ply | quiet moves | two slots per ply |
| history | per colour, whole search | remaining quiet moves | one flat array |

**MVV-LVA** sorts captures by victim value descending, then attacker value ascending. Pawn
takes queen is the best thing to try first; queen takes pawn is the worst capture to lead
with, because it is the one most likely to simply lose the queen.

One detail that matters: the attacker term is an *ordinal rank* (pawn 1 … king 6), not a
centipawn value. The king is worth 20000 in the evaluation so material can never outweigh it,
and reusing that as the attacker term would swamp the victim term and rank king-takes-queen
below pawn-takes-pawn. There is a test for exactly that inversion.

**Killer moves** are quiet moves that caused a beta cutoff at the same ply in a *sibling*
subtree. Siblings differ by one move and are therefore nearly identical, so a cutoff move
transfers well between them. Two slots per ply, with the old primary shifted down rather than
overwritten, because in many positions the cutoff move alternates between two candidates.
Re-recording the current primary is ignored, or one repeatedly-good move would occupy both
slots and the second would carry no information.

**History** is a per-colour `(from, to)` counter table, incremented by `depth * depth` on
every quiet-move cutoff. The squaring is a heuristic weight, not a derived quantity: a cutoff
near the root prunes an exponentially larger subtree than one near the leaves, so it is much
stronger evidence. The table is split by colour because the same from/to squares mean
something different for the other side — the position is upside down.

**Priority bands and ageing.** Captures occupy a numeric band strictly above killers, which
sit strictly above history scores. Keeping history below the killer band is not automatic,
because the counters only grow, so the whole table is halved whenever any entry reaches a
ceiling. That does two jobs with one mechanism: it preserves the band ordering, and it ages
the table so evidence from early in a long search decays instead of permanently dominating.

Only quiet moves feed the killer and history tables. Captures are already ordered by MVV-LVA,
so a killer slot spent on one is wasted.

**Ordering is provably score-preserving.** Reordering the siblings of a node cannot change its
minimax value. The gate is therefore exact score equality against the frozen baseline at every
depth, plus a node count that is never higher. A score change here is a bug, not a trade-off.

### Current implementation choice: full sort

`order_moves` sorts the entire move list. Real engines *stage* this instead — yield the
transposition-table move, then generate and pick captures, then quiet moves, stopping the
moment a cutoff happens — because most nodes cut off after one or two moves and sorting all
~35 is strictly wasted work. That is a worthwhile later optimisation and is tracked as such.
The full sort is used here because it is obviously correct, and this step's purpose is to
establish the node reduction rather than to chase constant factors.

`sorted` is stable, so equal-keyed moves retain `python-chess`'s generation order and node
counts stay deterministic — which the benchmark depends on.

---

## 6. Zobrist hashing and the transposition table

### The key is maintained incrementally

A Zobrist key is the XOR of one random 64-bit number per fact about a position: per
(colour, piece type, square), per castling right, per en-passant file, and one for the side
to move. XOR is its own inverse, `x ^ r ^ r == x`, so making a move does not recompute
anything -- XOR the mover out of its origin and in on its destination, XOR out any captured
piece, flip the side key, fix up castling and en passant. **O(1) per move**, independent of
the number of pieces, against O(64) to rebuild.

That distinction is the whole technique, and it is what the predecessor engine missed: it
walked all 64 squares in a `compute_hash()` that it called *fresh at every node*, which costs
more than the lookup it enables saves.

Three cases account for essentially every Zobrist bug, and each has its own branch and its
own test: **castling** moves two pieces, **en passant** captures a pawn that is not on the
destination square, and **promotion** replaces one piece type with another.

Castling rights and the en-passant file are XORed out before the push and the new values
XORed back in after it, both read from the board. Deriving by hand which rights a move
revokes would mean reimplementing rules `python-chess` already gets right -- including that
a rook *captured* on its home square revokes its owner's right although its owner never
moved -- for no benefit.

The en-passant key is included only when `has_legal_en_passant()`, not merely when a pawn
moved two squares. Otherwise two positions identical in every way that affects play would
hash differently and the transposition between them would be missed. This is the standard
convention and is what Polyglot does.

**Validation.** A wrong XOR does not crash; it silently identifies the wrong position, and
every probe after it reads another position's score. So the key is validated by brute force:
`python -m bench.perft --validate-zobrist` walks the move tree asserting at every node that
the incremental key equals a from-scratch recomputation. **7,037,881 nodes checked at depth
4 across the five reference positions, zero inconsistencies.** This is the only part of the
perft harness that tests code written here, and CI runs it at depth 3 on every push.

### The table, and why there are exactly three flags

An entry is `(key, depth, score, flag, best_move, generation)` in a fixed-size list indexed
by `key & mask`, the size a power of two so the index is a mask rather than a modulo. The
predecessor's table was an unbounded dictionary and stored no best move.

A stored score is only as trustworthy as the window it was searched with, because alpha-beta
does not compute an exact value at every node -- when it cuts off it has only a bound:

| flag | produced by | means | usable to |
|---|---|---|---|
| EXACT | every move examined, score inside `(alpha, beta)` | the true value | return directly |
| LOWERBOUND | a fail-high (beta cutoff) | true value is **at least** this | cut off if score reaches `beta` |
| UPPERBOUND | a fail-low, no move beat alpha | true value is **at most** this | cut off if score is at most `alpha` |

Using a bound in the wrong direction is a classic bug and it is *silent*. The tests assert
the direction explicitly: a LOWERBOUND pointing the wrong way is seeded into the table and
the search is required to ignore it and keep searching.

**Mate scores are converted on store and probe.** A mate score encodes its distance from the
root, so the same position reached at a different ply would read back the wrong distance.
Stored node-relative (add `ply` to a winning score, subtract it from a losing one) and
converted back on probe. Without this, a mate in 3 can be reported as a mate in 7.

**Collisions are misses, not wrong answers.** Two positions can index to the same slot, so a
slot is usable only if its stored key matches. That comparison is the only thing between a
collision and a corrupt score -- and it is why a stored *move* is still matched against the
generated move list rather than trusted. A transposition table must never be trusted to
prove legality.

**Replacement is depth-preferred with ageing:** store if the slot is empty, or its occupant
is from an earlier search, or the new entry is at least as deep. Depth-preferred because a
deeper result is more valuable and more expensive to reproduce; aged because otherwise a deep
entry from three moves ago holds its slot forever.

### The score cutoff is not score-preserving, and that was measured

Enabling the table changed a fixed-depth score: 590 against the baseline's 580 on
`8/8/8/4k3/8/8/4P3/R3K3 w - - 0 1` at depth 5. Investigated rather than waved away, by
instrumenting every cutoff and checking the returned score was a valid bound for a search at
the entry's stored depth. Three of about 2,500 unique cutoffs failed -- and the failing
entries turned out to hold values that are the position's true value at a *greater* depth.
One stored at depth 1 held 600, which is that position's true value at depth 3.

The cause is structural, not a defect: an entry is reused whenever `entry.depth >= depth`, so
**the `depth` field is a lower bound on the quality of the information, not a description of
it.** A nominal depth-N search with a table returns something better than the pure depth-N
minimax value. That is what a table is for.

So the feature is split. `tt_score_cutoffs=False` gives a table used purely for its best
move, which is pure ordering and therefore provably score-preserving -- that variant is
gated by exact equality with the frozen baseline. The full variant is gated by a bounded
deviation (the mate verdict must never flip; a non-mate score must stay within one pawn),
plus unit tests on the flag semantics, plus the tactical suite.

## 7. What each part is actually worth

Three separable claims, measured separately at depth 5 against `ordering`:

| config | nodes | aggregate | median per position | time |
|---|---:|---:|---:|---:|
| `ordering` | 679,433 | — | — | 55.02s |
| `tt-move-only` | 672,453 | -1.0% | **-0.1%** | 59.36s |
| `tt` | 508,297 | -25.2% | **-24.0%** | 43.74s |
| `tt-id` | 386,507 | -43.1% | **-32.3%** | 33.47s |

**The best-move hint alone is worth nothing here, and costs 7.9% more time.** The received
wisdom -- repeated in this project's own earlier notes -- is that supplying a best move for
ordering is a transposition table's largest practical benefit. In a single fixed-depth search
that is not what the measurement says. Only genuinely transposed nodes get a hit, about 2.5%
of probes at depth 5, and the existing MVV-LVA/killer/history ordering is already close to
the alpha-beta square-root bound, so a better first move has almost nothing left to win.
Meanwhile key maintenance and probing cost real time.

The received wisdom is not wrong, it is about a different setting: engines iteratively
deepening to much greater depth, where the previous iteration left a best move at *every*
node rather than only at transposed ones.

**The score cutoffs are the gain, and it grows with depth:** -7.3% median at depth 4, -24.0%
at depth 5.

**Iterative deepening's depth-5 gain is almost entirely one position, and it is a different
mechanism.** Against `tt` the aggregate is -24.0% but the median is -1.1%; excluding
`wac-001` the aggregate falls to -6.1% and the median to -0.2%. `wac-001` is a mate in 3, so
iterative deepening finds it during the depth-3 iteration and never runs the depth-4 or
depth-5 iterations at all: 3,407 nodes against 137,072. That is legitimate -- a proven mate
cannot be improved by searching further -- but it is **early termination on a mate score, not
better ordering**, and quoting -43.1% without saying so would credit one mechanism with
another's win.

On a typical position at depth 5, iterative deepening is roughly neutral in nodes. That is
expected and is not the reason to have it: it exists so a best move is available **at any
moment**, which is what makes playing to a clock possible. Its payoff is measured under a
time budget, in the time-management step.

---

## 8. Measurement methodology

Two rules, both of them reactions to specific failures in the predecessor project rather
than general good practice.

### Rule 1: no number without a committed script

Every figure in this repo is produced by a script in `bench/`, and the command that produces
it is written next to it. The predecessor described a single measurement as "~50%", "~58%" and
"69.33%" in three different artifacts, because no committed script owned the number.

### Rule 2: never benchmark on one position

`bench/positions.py` holds 13 positions spanning four categories, and the set is validated at
import — a malformed or already-terminal FEN is rejected rather than silently contributing
zero nodes to every aggregate.

The categories exist because search optimisations have very different payoffs by position
type. Openings have a high branching factor and few captures, so ordering is carried by
killers and history rather than MVV-LVA. Tactical middlegames are capture-dense, which is
where MVV-LVA and quiescence earn their keep. Endgames have a low branching factor and many
transpositions, which is where the transposition table pays and where null-move pruning
becomes dangerous because of zugzwang.

Aggregates alone would hide all of that. For scale: `kiwipete` is 44% of the baseline's total
node count on its own, so an optimisation that did nothing anywhere else could still move the
total by 40%. Per-position numbers are reported for that reason.

### Rule 3: a fresh searcher per position

`bench/nodes.py` constructs a new `Searcher` for every position. Sharing one would let killer
moves, history and the transposition table carry over, so an earlier position would warm up a
later one and the per-position numbers would stop being independent. The predecessor's
benchmark made exactly this mistake across its two *runs* — the unordered baseline warmed the
killer table that the ordered run then used — so there is a test asserting the mechanism, not
just the determinism.

### Which features may change the score at a fixed depth

Applying one blanket rule to all of them would reject a correct implementation, so the
expectation is set per feature:

| feature | score at fixed depth | nodes | gate |
|---|---|---|---|
| move ordering | **cannot change** | down | same score, fewer nodes |
| transposition table | should not change, but not guaranteed — bound-based cutoffs admit search instability | down | score change is a strong signal, then proved either way |
| quiescence | **changes by design** — it resolves captures past the horizon | **up** at the same nominal depth | tactical suite and horizon positions |
| PVS, aspiration windows | **cannot change**, given the re-search on a fail-high | down | same score, fewer nodes |
| null-move pruning, LMR | changes by design — heuristic forward pruning | down | strength at fixed time |

### Elo must be measured at fixed time, not fixed depth

This is the methodological point the whole of Phase 2 depends on. At a fixed depth, a
score-preserving optimisation plays *identical moves* — so it is worth exactly 0 Elo by
construction. Move ordering, a transposition table and PVS do not make the engine choose
better moves at a given depth; they make a given depth cheaper. The strength gain exists only
when the saved time is spent reaching greater depth, which means strength has to be measured
under a time or node budget per move.

A second consequence: Elo measured over too few games is noise. The 95% interval on a score
rate from 100 games is roughly ±70 Elo, and halving an interval requires four times the games.
So the Phase 2 harness plays from a book of varied openings, each twice with colours reversed,
reports the draw rate alongside the result, and reports a confidence interval rather than a
point estimate.

---

## 9. Correctness strategy

The predecessor's three "optimisation" commits took its tactical score from 12/12 to 5/12,
and a node-count benchmark reported the change as a 69% improvement — which it was. It
searched far fewer nodes to arrive at wrong answers faster. Every bug in it was the kind a
performance benchmark cheerfully reports as a win.

So the test suite is built around invariants and differentials rather than golden outputs,
because those are the tests that catch a *silently* wrong search.

**Invariants** — properties that must hold for any position:

* `evaluate(board) == evaluate(board.mirror())`. Mirroring swaps the colours and the side to
  move together, so a side-relative evaluation is unchanged. This directly catches the sign
  bug that broke the predecessor.
* Every piece-square table is exactly 64 entries, checked at import.
* A mated node scores negative, and the score encodes mate distance.
* A search leaves the board exactly as it found it — cheap to check, and it catches an
  unbalanced push/pop that would otherwise surface much later as an inexplicable evaluation.

**Differentials** — two code paths that must agree:

* A narrowed alpha-beta window must return the same score as a full window when the true
  score lies inside it.
* All-flags-off `Searcher` must match the frozen search on score, move and exact node count.
* Each score-preserving feature must match the frozen baseline's score exactly, and never
  search more nodes.

**End-to-end** — the tactical suite: free-material positions and mate-in-N, at several
depths. It runs against *every* registered configuration through a pytest fixture, so adding
a feature to `CONFIGS` subscribes it to the suite with no test edit. That matters because the
predecessor's regression lived in a code path that was never exercised the same way as the
original.

**Throughput and Zobrist validation** — `bench/perft.py`. The published reference counts
validate `python-chess`; what will test our code is the Phase 1 assertion that the
incrementally maintained Zobrist key equals a from-scratch recomputation at every node of the
tree.

CI runs the suite on Python 3.11, 3.12 and 3.13 plus the perft references on every push.

---

## 10. Results

Measured on Python 3.11.4, `python-chess` 1.11.2, Windows 11. Node counts are deterministic
and reproduce anywhere; timings are machine-dependent.

### Phase 0 baseline

```
python -m pytest -q
python -m bench.perft --depth 3
python -m bench.nodes --config phase0 --depth 4
```

| | result |
|---|---|
| tests | 547 passing |
| tactical suite | 12/12 across depths 2-4, on every configuration |
| perft references | 15/15 correct, ~660k nodes/sec aggregate |
| incremental Zobrist | 7,037,881 nodes checked at depth 4, 0 inconsistent keys |
| depth-4 search, 13 positions | 802,700 nodes / 69.3s / 11,588 nps |

Committed as [`bench/baselines/phase0-d4.json`](../bench/baselines/phase0-d4.json).

### Phase 1

Each step is reported as a delta against that baseline file:

```
python -m bench.nodes --config <name> --depth 4 --baseline bench/baselines/phase0-d4.json
```

| step | config | nodes at depth 4 | vs baseline | median per position | scores |
|---|---|---:|---:|---:|---|
| 1. move ordering | `ordering` | 53,854 | -93.3% | **-82.1%** | all 13 identical |
| 2. + table, move only | `tt-move-only` | 53,862 | -93.3% | -82.1% | all 13 identical |
| 2. + score cutoffs | `tt` | 49,492 | -93.8% | -84.0% | all 13 identical |
| 2. + iterative deepening | `tt-id` | 46,333 | -94.2% | **-86.2%** | all 13 identical |

Depth 4 understates the table badly, because there is very little tree in which to find a
transposition. Section 7 has the depth-5 figures, which are the honest ones, and the
per-part attribution.

The median is the figure to quote, not the aggregate. `kiwipete` is 44% of the baseline's
total node count and is also the position ordering helps most (-98.2%), so the aggregate is
substantially a statement about one position. The per-position range is -31% (`startpos`: no
captures available and little to distinguish the moves) to -98% (`kiwipete`: a capture-dense
middlegame where MVV-LVA has the most to work with).

**Effective depth** is the number that actually matters, because node count is an activity
metric while depth reached for a given cost is the result:

| | nodes | seconds |
|---|---:|---:|
| `phase0`, depth 4 | 802,700 | 69.27 |
| `ordering`, depth 5 | 679,433 | 55.02 |
| `tt`, depth 5 | 508,297 | 43.74 |
| `tt-id`, depth 5 | 386,507 | 33.47 |

A full extra ply of search now costs **52% fewer nodes and 52% less wall-clock time** than
the plain baseline needed for the shallower search. After step 1 that figure was 15% and
21%.

Per-step reasoning, including the per-category breakdown and a check of the result against
the alpha-beta square-root bound, is in [DEVLOG.md](DEVLOG.md).
