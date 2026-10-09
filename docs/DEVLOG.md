# Development log

Chronological, one entry per shipped step. Each entry answers four questions: what changed,
why, what it measured, and what went wrong. Numbers come from a committed script named in
the entry, and the command that produces them is written out so anyone can re-run it.

Measurements are on Python 3.11.4 with `python-chess` 1.11.2 on Windows 11. Node counts are
deterministic and will reproduce anywhere; timings are machine-dependent and will not.

---

## Phase 0 — a correct baseline

**Shipped** 2026-10-10 · commit `10f82fb`

### What

A deliberately plain engine: material-plus-piece-square evaluation
([`engine/evaluate.py`](../engine/evaluate.py)) and negamax with alpha-beta
([`engine/search.py`](../engine/search.py)). No move ordering, no transposition table, no
quiescence. 73 tests, a perft harness, CI on three Python versions.

### Why start with something deliberately slow

Because the predecessor project proved the opposite order does not work. It had move
ordering, a transposition table and quiescence, and those three additions took its tactical
score from 12/12 down to 5/12 — it would not play checkmate as White. A node-count benchmark
reported the change as a 69% improvement, which it was: it searched far fewer nodes, to
arrive at wrong answers faster.

So Phase 0 exists to be the thing later phases are measured *against*, and its only job is
to be right. [`engine/search.py`](../engine/search.py) is frozen for that reason — Phase 1
goes in a separate module rather than editing it.

### The two conventions that have to be right

Both of these broke the predecessor, and both are easy to get wrong in a way that still
runs.

**1. Negamax requires side-relative scores.** Negamax collapses minimax's two branches into
one by negating the child's score on the way up:

```python
score = -negamax(board, depth - 1, -beta, -alpha, ply + 1)
```

That negation is only meaningful if a leaf score means "good for whoever is about to move".
The predecessor's evaluation returned a White-positive score instead, so for the position
`4k3/8/8/8/8/8/8/3QK3` it returned +890 with White to move *and* +890 with Black to move —
Black, a queen down, scored it as winning. The fix is one line at the leaf, and
[`engine/evaluate.py`](../engine/evaluate.py) ends with it:

```python
score = evaluate_white(board)
return score if board.turn == chess.WHITE else -score
```

The test that catches this class of bug is evaluation symmetry:
`evaluate(board) == evaluate(board.mirror())` must hold for every position, because mirroring
swaps the colours and the side to move together.

**2. A mate score is negative at the mated node.** A node with no legal moves and the king in
check means *the side to move has been mated*, so in a side-relative convention the value is
always `-MATE + ply`. The predecessor returned `+1000000` whenever `board.result() == "1-0"`,
which is correct in the absolute convention its original author used and wrong in the
negamax convention it was converted to. That single mismatch is why it would not mate as
White, and why it *would* mate as Black — the sign happened to work out for one colour.

The `+ ply` term encodes mate distance. Without it every mating line scores identically and
the engine can shuffle forever instead of playing the mate it has found; with it, a mate in 2
outscores a mate in 4 and the search prefers the faster one.

### Table mirroring: `square ^ 56`

The piece-square tables are written from White's point of view with a8 first, while
`python-chess` numbers a1 as 0 and h8 as 63. So White's lookup has to flip the rank:
`square ^ 56`. XORing the top three bits of a 6-bit square index with `111` flips the rank
and leaves the file alone, which is exactly a vertical mirror: a1 (0) maps to index 56, h1
(7) to 63, a8 (56) to 0. Black reads the table directly, which mirrors it.

Both of the obvious alternatives are wrong, and one of them was a real inherited bug:

* `table[-square]` — Python negative indexing, so square 0 maps to index 0 and square 1 maps
  to index 63. That is a 180-degree rotation off by one, not a rank flip. This was the
  predecessor's actual code and the one genuine pre-existing defect in it.
* `table[63 - square]` — mirrors the files as well as the ranks. Harmless for the symmetric
  tables, wrong for any asymmetric one.

### What the tests actually test

Worth being precise about, because it is easy to overclaim here.

`perft` counts the leaf nodes of the move tree and compares against published reference
counts. It is the standard correctness harness for a *move generator* — and this project
does not have one, because move generation and legality come from `python-chess`. So matching
the reference counts validates `python-chess`, not code written here. It is kept for two
honest reasons: it is a clean throughput benchmark for the make/unmake path the search lives
in, and it is the tree-walk harness that will validate incremental Zobrist hashing in
Phase 1, where every node asserts the incrementally updated key equals a from-scratch
recomputation. That assertion does test our code.

The real correctness harness is the invariant and differential set, because it tests code we
wrote: evaluation symmetry, piece-square tables asserted at exactly 64 entries (closing the
data-corruption class that hit the predecessor), mate scores negative at a mated node and
distance-aware, a narrowed alpha-beta window agreeing with a full window at the same depth,
and a tactical suite of free-material and mate-in-N positions.

### Measured

```
python -m pytest -q
python -m bench.perft --depth 3
```

| | result |
|---|---|
| tests | 73 passing |
| tactical suite | 12/12 across depths 2-4 |
| perft reference positions | 15/15 correct |
| perft throughput | ~660k nodes/sec aggregate |

### What went wrong

One real bug, found by the tests rather than by inspection: terminal detection ran *after*
the depth cutoff, so a depth-1 search returned a static evaluation at a checkmate instead of
a mate score and therefore could not see mate in one. Terminal detection now precedes the
cutoff. The cost is generating moves even at leaf nodes, which is one of the reasons real
engines enter quiescence at depth 0 — quiescence needs a move list anyway.

---

## Phase 1, step 0 — the measurement harness

**Shipped** 2026-10-10

### What

Three things, all infrastructure, no search changes:

* [`bench/positions.py`](../bench/positions.py) — a 13-position benchmark set spanning
  openings, middlegames, tactical positions and endgames, with a validator that rejects a
  malformed or already-terminal FEN at import time.
* [`bench/nodes.py`](../bench/nodes.py) — a fixed-depth node-count and timing benchmark with
  JSON output, a committed baseline, and a comparison mode that reports per-position node
  deltas and flags any score or best-move change.
* [`engine/searcher.py`](../engine/searcher.py) — the Phase 1 search, with every feature
  behind a flag on a frozen `SearchConfig`. All flags off, which is the state it ships in
  today, is required by test to be identical to the frozen Phase 0 search.

### Why this comes before any optimisation

Two reasons, and both are lessons from the audit rather than good practice in the abstract.

**A single-position benchmark cannot tell you what you want to know.** The predecessor's
headline figure — a 69.3% node reduction — was measured on one hardcoded FEN. That number
was real and reproducible, and it still did not mean the engine was better, because the same
change had taken the tactical score from 12/12 to 5/12. A mixed position set at least makes
a per-category regression visible: move ordering that helps in capture-dense middlegames and
hurts in quiet endgames shows up as two different columns instead of one average.

**Per-feature attribution needs the features to be switchable.** Phase 2 has to answer "what
is each feature worth?", which means running the same code with one feature off. Real engines
expose exactly these switches over UCI for exactly this reason. Doing it with flags from the
start is much cheaper than retrofitting it later, and it makes the baseline permanently
reachable instead of a thing that existed in an old commit.

### Why the equivalence test is the important one

Wrapping the search in a config object is itself code, and code that quietly changes
behaviour would corrupt every number the project reports — the baseline would stop being the
baseline. So `test_phase0_config_is_identical_to_frozen_search` asserts three things at once
for every sampled position and depth: same score, same best move, and the **same exact node
count**. Node-count equality is the strict one. It fails on any change to visit order or
cutoff logic, including changes that leave the final score intact.

A second test deserves mention because it encodes a specific predecessor failure.
`test_run_position_does_not_share_state_between_calls` asserts that the benchmark constructs
a fresh `Searcher` per position. The predecessor's benchmark ran its unordered baseline and
its ordered run in the same process without clearing the killer-move table, so the baseline
warmed up the thing it was being compared against. Determinism alone would not catch that,
so the test checks the mechanism directly.

### Measured — the Phase 0 baseline

Committed as [`bench/baselines/phase0-d4.json`](../bench/baselines/phase0-d4.json), and
reproduced by:

```
python -m bench.nodes --config phase0 --depth 4
```

| position | category | nodes | leaves | seconds | score | move |
|---|---|---:|---:|---:|---:|---|
| startpos | opening | 3,154 | 2,368 | 0.20 | 0 | g1f3 |
| open-game | opening | 26,325 | 23,840 | 1.98 | -50 | f1b5 |
| italian | opening | 39,673 | 35,772 | 3.28 | -50 | c4b5 |
| sicilian-open | opening | 65,124 | 59,652 | 5.19 | 10 | f1b5 |
| kiwipete | middlegame | 355,718 | 334,573 | 33.54 | 50 | e2a6 |
| perft-4 | middlegame | 76,986 | 74,496 | 6.26 | -615 | g1h1 |
| perft-5 | middlegame | 148,311 | 136,475 | 12.47 | 275 | d7c8q |
| wac-001 | tactical | 63,344 | 55,571 | 5.07 | mate in 3 | g3g6 |
| wac-002 | tactical | 12,451 | 10,686 | 0.71 | -80 | f6e6 |
| hanging-queen | tactical | 4,681 | 4,186 | 0.23 | 470 | d1d4 |
| perft-3-endgame | endgame | 4,190 | 3,420 | 0.22 | -20 | b4f4 |
| kpk | endgame | 565 | 433 | 0.02 | 90 | e1f2 |
| rook-endgame | endgame | 2,178 | 1,614 | 0.11 | 560 | e1f2 |
| **total** | | **802,700** | **743,086** | **69.27** | | 11,588 nps |

Two things in that table are worth noticing before any optimisation lands.

**`kiwipete` alone is 44% of the total.** 355,718 of 802,700 nodes, and 33.5 of 69.3 seconds.
It has 48 legal moves at the root in a dense middlegame, so it is the position most exposed
to the branching factor and the one where move ordering has the most to win. It is also the
reason the aggregate must never be the only number reported: an optimisation could do nothing
anywhere else and still move the total by 40%.

**`wac-001` already finds a mate in 3** (score 999,997, which is `MATE - 3`) with no
quiescence and no ordering at all. That is a useful sanity check on the mate-distance
encoding: a raw centipawn score could not express "mate in 3", and an absolute mate constant
would not survive the negations.

The endgames are cheap — `kpk` is 565 nodes — which is expected from their low branching
factor, and is also a warning for later: a feature measured only on endgames looks free and
useless, and a feature measured only on `kiwipete` looks transformative.

Every Phase 1 step is reported as a delta against this file:

```
python -m bench.nodes --config <name> --depth 4 --baseline bench/baselines/phase0-d4.json
```

### A correction to the plan, made here

`CLAUDE.md` and `PLAN.md` originally carried a single rule for all of Phase 1: *"these are
speed optimisations; if a score changes at a given depth, that is a bug."* That rule is right
for some of the features and wrong for others, and applied uniformly it would have rejected a
correct quiescence implementation. It is now split by feature:

| feature | score at fixed depth | expected node count | gate |
|---|---|---|---|
| move ordering (MVV-LVA, killers, history) | **cannot change** — reordering siblings cannot change a minimax value | down | same score, fewer nodes |
| transposition table | should not change; not guaranteed, because bound-based cutoffs admit search instability | down | same score treated as a strong signal, then proved |
| quiescence | **changes by design** — it resolves captures past the horizon | **up** at the same nominal depth | tactical suite and horizon positions, never score equality |
| PVS, aspiration windows | **cannot change**, provided the re-search on a fail-high is present | down | same score, fewer nodes |
| null-move pruning, LMR | changes by design — heuristic forward pruning | down | strength at fixed time, not score |

The second half of the correction: **Elo has to be measured at fixed time or a fixed node
budget per move, never at fixed depth.** At fixed depth a score-preserving optimisation plays
*identical moves* and is therefore worth exactly 0 Elo by construction. The gain only exists
when the time saved is spent on extra depth. Phase 2 is designed around that.

---

## Phase 1, step 1 — move ordering

**Shipped** 2026-10-10

### What

[`engine/ordering.py`](../engine/ordering.py): MVV-LVA for captures and promotions, two
killer-move slots per ply, and a per-colour history table. Enabled by
`SearchConfig(move_ordering=True)`, registered as the `ordering` config.

### Why ordering is the first optimisation

Alpha-beta's whole saving comes from finding a cutoff *early*. If the best move at a node is
searched first, the subtrees under every remaining move get pruned; if it is searched last,
nothing is pruned and the search degenerates to full minimax. Identical answer, wildly
different cost. With perfect ordering alpha-beta visits roughly the square root of the
minimax tree — about double the depth for the same work.

So ordering is not a constant-factor tweak. It changes the effective branching factor, and
the branching factor sits in the exponent. Everything else in Phase 1 either depends on
ordering already being good (PVS assumes the first move is usually best) or improves it (the
transposition table's main practical value is supplying a known-good first move). It goes
first for both reasons.

### The three heuristics, and why all three

They are complementary — one static, one ply-local, one global — and each covers a case the
others cannot.

**MVV-LVA**, static, for captures: sort by victim value descending, then attacker value
ascending. Pawn takes queen is the best thing to try first; queen takes pawn is the worst
capture to lead with, because it is the one most likely to simply lose the queen.

One detail worth the comment it has in the code: the attacker term is an **ordinal rank**
(pawn 1 to king 6), not a centipawn value. The king is worth 20,000 in the evaluation so
that material can never outweigh it, and reusing that number as the attacker term would
swamp the victim term and rank king-takes-queen *below* pawn-takes-pawn. There is a test
named for exactly that inversion, because it is the kind of bug that leaves a working engine
which is merely slower, and no correctness test would notice.

**Killer moves**, ply-local, for quiet moves: a quiet move that caused a beta cutoff at the
same ply in a *sibling* subtree very often causes one here too, because siblings differ by
one move and are nearly identical positions. Two slots per ply, with the old primary shifted
down rather than overwritten, because in many positions the cutoff move alternates between
two candidates. Stored per ply rather than globally because sibling similarity is a ply-local
property — a killer from a different depth describes a different position.

**History**, global, for everything left over: a per-colour from/to counter incremented by
`depth * depth` on each quiet-move cutoff. The squaring is a heuristic weight, not a derived
quantity — a cutoff near the root prunes an exponentially larger subtree than one near the
leaves, so it is much stronger evidence about the move. The table is split by colour because
the same from and to squares mean something different for the other side; the position is
upside down.

Only quiet moves feed the killer and history tables. Captures are already ordered by
MVV-LVA, so a killer slot spent on one is a wasted slot.

### Priority bands, and the one non-obvious mechanism

Captures occupy a numeric band strictly above killers, which sit strictly above history
scores. The band separation has to be *maintained*, not merely chosen, because history
counters only ever grow: given a long enough search they would climb into the killer band
and invert the ordering. So the whole history table is halved whenever any entry reaches a
ceiling.

That single mechanism does two jobs. It preserves the band ordering, and it **ages** the
table, so evidence from early in a long search decays instead of permanently dominating. It
is the sort of thing that looks like an arbitrary magic number until you ask what happens
after ten million nodes.

### Measured

```
python -m bench.nodes --config ordering --depth 4 --baseline bench/baselines/phase0-d4.json
```

| position | category | phase0 nodes | ordered nodes | change | score |
|---|---|---:|---:|---:|---|
| startpos | opening | 3,154 | 2,165 | -31.4% | same |
| open-game | opening | 26,325 | 5,682 | -78.4% | same |
| italian | opening | 39,673 | 9,848 | -75.2% | same |
| sicilian-open | opening | 65,124 | 9,171 | -85.9% | same |
| kiwipete | middlegame | 355,718 | 6,267 | **-98.2%** | same |
| perft-4 | middlegame | 76,986 | 4,318 | -94.4% | same |
| perft-5 | middlegame | 148,311 | 3,699 | -97.5% | same |
| wac-001 | tactical | 63,344 | 7,538 | -88.1% | same |
| wac-002 | tactical | 12,451 | 2,230 | -82.1% | same |
| hanging-queen | tactical | 4,681 | 252 | -94.6% | same |
| perft-3-endgame | endgame | 4,190 | 1,307 | -68.8% | same |
| kpk | endgame | 565 | 246 | -56.5% | same |
| rook-endgame | endgame | 2,178 | 1,131 | **-48.1%** | same |
| **total** | | **802,700** | **53,854** | **-93.3%** | |
| time | | 69.27s | 4.36s | -93.7% | |

**All 13 scores identical, all 13 best moves identical.** That is the gate, and it held
without exception. Ordering is provably score-preserving — reordering siblings cannot change
a minimax value — so anything else here would have been a bug.

### Which number to quote, and why not the aggregate

The aggregate is **-93.3%**. The honest headline is the **median per-position reduction,
-82.1%**, with a range of -31% to -98%.

The reason is in the baseline table: `kiwipete` alone was 44% of the baseline's total nodes,
and ordering happens to help it most (-98.2%), so it drags the aggregate well above what a
typical position sees. Quoting -93.3% is not false, but it is a number about one position
wearing the clothes of a number about the engine. That is the same failure the predecessor
project made in a smaller way — a 69% figure from one hardcoded FEN — so both numbers are
reported here and the median is the one that gets quoted.

The spread itself is informative rather than noise:

* `kiwipete`, `perft-4` and `perft-5` (-94% to -98%) are dense middlegames full of captures.
  MVV-LVA has the most to work with and the unordered baseline had the most to lose.
* `rook-endgame` and `kpk` (-48%, -57%) have few pieces and almost no captures, so MVV-LVA
  contributes nothing and the whole gain comes from killers and history.
* `startpos` (-31%) is the weakest case: no captures available at all, and a symmetric
  position where few moves are clearly better than the rest, so there is little signal for
  any of the three heuristics to find.

### Sanity-checking the magnitude

A 57-fold reduction on one position deserves suspicion rather than celebration, so it is
worth checking against theory. `kiwipete` has 48 legal moves at the root. Full minimax at
depth 4 is about 48^4, roughly 5.3M nodes; the best case for alpha-beta is approximately its
square root, around 2 * 48^2, roughly 4,600. The unordered baseline searched 355,718 — far
above that floor, exactly as expected with arbitrary move order. The ordered search finds
6,267, which is close to it. So the result is not too good to be true; it is ordering doing
what the square-root bound says it should, on the position most able to benefit.

### What also moved, slightly unexpectedly

Nodes per second went *up* (11,588 to 12,348) despite now sorting every move list. The
reason is visible in the leaf column: leaves were 92.6% of all nodes in the baseline and are
68.6% now. Ordering prunes leaf-heavy subtrees preferentially, so the surviving tree has a
higher proportion of internal nodes, and an internal node does not pay for a static
evaluation. The sort overhead is real, but it is smaller than the evaluation work it
displaces.

That also sets up the next constant-factor win. `order_moves` sorts the whole move list,
while most nodes cut off after one or two moves. Real engines stage this — yield the
transposition-table move, then captures, then quiet moves, stopping as soon as a cutoff
happens. Deliberately not done yet: this step's job was the node reduction, and a staged
generator is easier to justify and to measure once there is a transposition-table move to
stage first.

### The number that actually matters: effective depth

A node-count reduction is an activity metric. The result metric is how deep the engine can
see for a given amount of work, because that is what converts into playing strength. So the
same benchmark was run one ply deeper:

```
python -m bench.nodes --config ordering --depth 5
```

| | nodes | seconds |
|---|---:|---:|
| phase0, depth 4 | 802,700 | 69.27 |
| ordering, depth 5 | 679,433 | 55.02 |
| | **-15.4%** | **-20.6%** |

Move ordering reaches **depth 5 using 15% fewer nodes and 21% less wall-clock time than the
unordered baseline needed for depth 4.** That is a full extra ply of search for less work
than the shallower search cost — which is the square-root bound showing up as something you
can feel, and a far better description of the change than "-93% nodes".

It is also the right framing for Phase 2. At a fixed depth, move ordering plays *identical
moves* and is therefore worth exactly 0 Elo by construction. The strength gain exists only
because the saved time buys the extra ply, which is why Elo has to be measured under a time
budget rather than a depth limit.

### What went wrong

Nothing in the engine. One self-inflicted wound in the test suite, worth recording because
the failure mode was instructive: `test_history_ages_at_the_ceiling` was written as a `while`
loop that recorded cutoffs until the score stopped rising, and it hung forever. Halving a
counter that has just crossed the ceiling lands it back exactly on the loop's own exit
threshold, so the condition never became false. Rewritten to drive an exact number of
records and assert the halving directly. The lesson is small but general: a test that loops
until a condition on the system under test is a test that can hang when the system behaves
correctly.
