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

---

## Phase 1, step 2 — transposition table with incremental Zobrist

**Shipped** 2026-10-10

### What

[`engine/zobrist.py`](../engine/zobrist.py): Zobrist key tables and a tracker that maintains
the key incrementally, O(1) per move, in lockstep with the board.

[`engine/transposition.py`](../engine/transposition.py): a fixed-size table storing
`(key, depth, score, flag, best_move, generation)`, with EXACT / LOWERBOUND / UPPERBOUND
semantics, mate-score conversion, and depth-preferred replacement with ageing.

Four new configs, because this step contains three separable claims and lumping them
together would hide which one did the work: `tt-move-only` (table used only for its best
move), `tt` (plus score cutoffs), `tt-id` (plus iterative deepening), and `id-only`
(iterative deepening without a table, as a control).

### Why XOR is the whole technique

A Zobrist key is the XOR of one random 64-bit number per fact about the position: per
(colour, piece type, square), per castling right, per en-passant file, plus one for the side
to move. XOR is its own inverse — `x ^ r ^ r == x` — so making a move does not recompute
anything. XOR the mover out of its origin, back in on its destination, XOR out any captured
piece, flip the side-to-move key, fix up castling and en passant. **O(1) per move,
independent of how many pieces are on the board**, against O(64) to rebuild.

This is precisely the distinction the predecessor engine missed. It had a `compute_hash()`
that walked all 64 squares and called it *fresh at every node*. That is a hash function, but
not Zobrist hashing in the sense that matters: it cost more than the lookup it enabled
saved. "I implemented Zobrist hashing" and "I recompute the hash from scratch at every node"
cannot both be true of a correct implementation.

### Proving the hashing is right — the one honest use of perft

A wrong XOR does not crash. It produces a key that identifies the wrong position, so every
subsequent probe reads another position's score and the search is quietly wrong. So the
hashing is validated by brute force: `bench/perft.py --validate-zobrist` walks the move tree
and asserts at **every node** that the incrementally maintained key equals a from-scratch
recomputation.

```
python -m bench.perft --validate-zobrist --depth 4
```

| position | nodes checked | inconsistent |
|---|---:|---:|
| startpos | 206,604 | 0 |
| kiwipete | 4,185,553 | 0 |
| position-3 | 46,256 | 0 |
| position-4 | 432,071 | 0 |
| position-5 | 2,167,397 | 0 |
| **total** | **7,037,881** | **0** |

This is the only part of the perft harness that tests code written in this project. Matching
published perft counts validates `python-chess`, which owns move generation here; asserting
key consistency at every one of those nodes validates our hashing. CI runs the same check at
depth 3 on every push.

Three cases account for essentially every Zobrist bug, and a tree walk finds all of them
without anyone having to think of them individually:

1. **Castling moves two pieces.** Hashing only the king leaves the rook counted on its old
   square. The rook's squares are derived from the king's rank and direction rather than
   hardcoded, so one branch covers both colours.
2. **En passant captures a pawn that is not on the destination square.** Looking the victim
   up by destination square finds nothing, so an en-passant capture silently leaves the
   captured pawn in the key forever. `is_en_passant` is only consulted when the destination
   is empty, keeping it off the common path.
3. **Promotion replaces one piece type with another.** What lands on the destination is not
   what left the origin.

One deliberate choice worth defending: castling rights and the en-passant file are XORed
*out* before the push and the new values XORed *in* after it, reading both from the board.
Working out by hand which rights a move revokes means reimplementing rules `python-chess`
already gets right — a king move revokes both rights, and *a rook captured on its home
square revokes its owner's right even though its owner did not move* — in exchange for
nothing.

### Why the en-passant key is gated on `has_legal_en_passant()`

The key includes the en-passant file only when an en-passant capture is actually available,
not merely when a pawn moved two squares. Otherwise two positions that are identical in every
way that affects play — same pieces, same rights, same legal moves — would hash differently
and the transposition between them would be missed. This is the standard convention; Polyglot
does the same. It is nearly free because `has_legal_en_passant()` short-circuits on the
common case of no en-passant square at all.

### Why there must be exactly three flags

A stored score is only as trustworthy as the window it was searched with, and alpha-beta does
not compute an exact value at every node — when it cuts off, it has only established a bound.

* **EXACT** — every move was examined and the score fell strictly inside `(alpha, beta)`.
  The true value. Usable directly.
* **LOWERBOUND** — a fail-high, i.e. a beta cutoff. The true value is *at least* this score
  and may be higher. Usable only to prove the position is already good enough: cut off if the
  stored score reaches the current `beta`.
* **UPPERBOUND** — a fail-low, no move beat `alpha`. The true value is *at most* this score.
  Usable only to prove it is already bad enough: cut off if the stored score is at most the
  current `alpha`.

Using a bound in the wrong direction is a classic bug and it is **silent** — the engine runs,
returns legal moves, and is wrong sometimes. So the direction is asserted explicitly rather
than inferred from the suite passing: `test_a_lowerbound_is_never_used_as_an_upperbound` and
its mirror seed the table with a bound pointing the wrong way and require the search to
*ignore* it and keep searching.

### Mate scores have to be converted on the way in and out

A mate score in this engine encodes its distance *from the root*: a mated node at ply `p`
returns `-MATE + p`, so a mate found at ply 3 arrives at the root as `MATE - 3`. That number
depends on the path length taken to the node.

The same position can be reached at a different ply, at which point a raw stored score claims
the wrong mate distance. So mate scores are stored **node-relative** — add `ply` to a winning
score, subtract it from a losing one — and converted back on probe. Without this, a mate in 3
can be reported as a mate in 7, and a won position can fail to convert because the search
keeps seeing a different distance for the same mate.

### Replacement: depth-preferred, with ageing

The table is a fixed-size list indexed by `key & mask`, the size a power of two so the index
is a mask rather than a modulo. The predecessor's table was an unbounded dictionary, which
grows until memory runs out.

Two positions can land in the same slot, so a slot is usable only if its stored key *matches*.
That comparison is the only thing standing between a hash collision and a wrong answer — and
it is why a stored move is still matched against the generated move list rather than trusted.
A transposition table must never be trusted to prove legality. There is a test that seeds the
table with a move that is illegal in the position and requires the search to ignore it.

The policy is: store if the slot is empty, **or** the occupant is from an earlier search
(stale), **or** the new entry was searched at least as deep. Depth-preferred because a deeper
result is both more valuable and more expensive to reproduce; aged because without ageing a
deep entry from three moves ago holds its slot forever against useful current results.

### The score cutoff is not score-preserving, and this was measured rather than assumed

Running the table against the frozen baseline produced a score difference: on
`8/8/8/4k3/8/8/4P3/R3K3 w - - 0 1` at depth 5, the table returned 590 where the plain search
returns 580. By this project's own rules that is a bug signal, so it was investigated rather
than waved away.

The investigation: instrument every transposition cutoff, and check the returned score is a
valid bound for a search at the depth the entry was stored with — EXACT must equal the true
value, LOWERBOUND must not exceed it, UPPERBOUND must not be below it. Out of about 2,500
unique cutoffs, 3 failed. All three were in the same sparse endgame, all at a stored depth of
1, all off by a single piece-square step.

The explanation, confirmed by checking the values against the true value at *every* depth:
one of those entries was stored at depth 1 with the score 600, and 600 is that position's
true value at depth **3**. An entry is reused whenever `entry.depth >= depth`, so a node
nominally searched to depth 2 can be answered by a value a depth-5 search produced. **The
`depth` field is a lower bound on the quality of the information, not a description of it.**
A nominal depth-N search with a table therefore returns something *better* than the pure
depth-N minimax value, which is the entire point of having a table.

(The first attempt at this instrumentation reported 51 failures, and was wrong: it attributed
a descendant's table entry to the parent node. Fixed by requiring that a cutoff at *this*
node means `_negamax` returned having visited exactly one node.)

So exact score equality is the wrong gate here. The gates are instead:

* `tt-move-only` — the table used purely for its best move. That is pure ordering, so it
  **cannot** change a score, and it is tested with exact equality against the frozen baseline
  at depths 1-4. Having this flag exists for the test as much as for the measurement.
* `id-only` — iterative deepening with no table just repeats searches and returns the last,
  so it too is tested with exact equality.
* `tt` — the deviation is bounded: the mate verdict must never flip, and a non-mate score
  must stay within one pawn of the true value.
* unit tests on the flag semantics, the mate conversion and the replacement policy.
* the tactical suite, which now runs against all seven configurations automatically.

### Measured

```
python -m bench.nodes --config tt-id --depth 5 --baseline bench/baselines/ordering-d5.json
```

Cumulative, at depth 4, against the Phase 0 plain baseline:

| config | nodes | aggregate | median per position | scores vs baseline |
|---|---:|---:|---:|---|
| `phase0` | 802,700 | — | — | — |
| `ordering` | 53,854 | -93.3% | -82.1% | 13/13 identical |
| `tt-move-only` | 53,862 | -93.3% | -82.1% | 13/13 identical |
| `tt` | 49,492 | -93.8% | -84.0% | 13/13 identical |
| `tt-id` | 46,333 | -94.2% | -86.2% | 13/13 identical |

Depth 4 badly understates the table, because at depth 4 there is very little tree to find
transpositions in. Depth 5, measured against `ordering` at the same depth, is the honest
picture:

| config | nodes | aggregate | median per position | time |
|---|---:|---:|---:|---:|
| `ordering` | 679,433 | — | — | 55.02s |
| `tt-move-only` | 672,453 | -1.0% | **-0.1%** | 59.36s |
| `tt` | 508,297 | -25.2% | **-24.0%** | 43.74s |
| `tt-id` | 386,507 | -43.1% | **-32.3%** | 33.47s |

Scores at depth 5: `tt-move-only` 13/13 identical to `ordering`, as required. `tt` and
`tt-id` 12/13, differing only on `rook-endgame` — the deviation analysed above.

### Three findings worth stating plainly, one of which contradicts the usual advice

**1. The best-move hint alone is worth nothing here: -0.1% median, and 7.9% *more* wall-clock
time.** The received wisdom — which this project's own notes repeated — is that supplying a
best move for ordering is the largest practical benefit of a transposition table. In a single
fixed-depth search that is simply not what the measurement says. The reason is that in one
search, the nodes that get a table hit are only the genuinely transposed ones, about 2.5% of
probes at depth 5, and the existing MVV-LVA/killer/history ordering is already close to the
alpha-beta square-root bound, so there is almost nothing left for a better first move to win.
Meanwhile the key maintenance and the probe cost real time.

The received wisdom is not wrong; it is about a different setting. It describes engines doing
iterative deepening to much greater depth, where the previous iteration has left a best move
at **every** node rather than only at transposed ones. Which is exactly what the next row
shows.

**2. The score cutoffs are where the gain is: -24.0% median nodes, -20.5% time.** And the gain
grows with depth — -7.3% median at depth 4, -24.0% at depth 5 — because a deeper tree contains
more transpositions and the stored entries are deep enough to be reusable.

**3. Iterative deepening's measured gain at depth 5 is almost entirely one position.** The
aggregate says -43.1% against `ordering`, and -24.0% against `tt`. But the median against `tt`
is only **-1.1%**, and with `wac-001` excluded the aggregate against `tt` falls to -6.1% and
the median to -0.2%.

`wac-001` is a mate in 3. Iterative deepening finds it during the depth-3 iteration and stops,
so it never runs the depth-4 and depth-5 iterations at all: 3,407 nodes against 137,072, a
97.5% reduction. That is a real and legitimate feature — a proven mate cannot be improved by
searching further — but it is **early termination on a mate score, not better ordering**, and
reporting -43.1% without saying so would be attributing one mechanism's win to another.

On a typical position, at depth 5, iterative deepening is roughly neutral in node count while
costing nothing. That is the expected result, and it is not the reason to have it: iterative
deepening exists so that a best move is available **at any moment**, which is what makes
playing to a clock possible at all. Its payoff is measured in the time-management step, under
a time budget, not here under a depth limit.

### The headline: effective depth

Node counts are an activity metric. The result metric is how deep the engine sees for a given
cost:

| | nodes | seconds |
|---|---:|---:|
| `phase0`, depth 4 | 802,700 | 69.27 |
| `ordering`, depth 5 | 679,433 | 55.02 |
| `tt`, depth 5 | 508,297 | 43.74 |
| `tt-id`, depth 5 | 386,507 | 33.47 |

**A full extra ply of search now costs 52% fewer nodes and 52% less wall-clock time than the
plain baseline needed for the shallower search.** After step 1 that figure was 15% and 21%.

### What went wrong

Two things, both mine, both in the instrumentation rather than the engine.

**The bound-validation harness was wrong before it was right.** Its first version reported 51
invalid bounds and sent me hunting a flag inversion that did not exist. It detected "a cutoff
happened in this subtree" and then compared the *parent's* search value against a
*descendant's* table entry. The fix is to require that a cutoff at this node means `_negamax`
returned having visited exactly one node. Lesson: an instrument that can attribute an
observation to the wrong object is worse than no instrument, because it manufactures bugs.

**The first attempt to monkey-patch `TranspositionTable.probe` failed** with
`attribute 'probe' is read-only`, because the class uses `__slots__`. Subclassing works and
is cleaner anyway. Minor, but it is the kind of thing that costs ten minutes if you have not
seen it before.

---

## Phase 1, step 3 — quiescence search with SEE

**Shipped** 2026-10-10

### What

Quiescence search in [`engine/searcher.py`](../engine/searcher.py) — stand-pat, capture
generation, quiet queen promotions, full check evasions, and a recursion cap — plus static
exchange evaluation in [`engine/see.py`](../engine/see.py) as a pruning filter. Configs
`quiescence` and `quiescence-see`, the second adding SEE so its contribution is separable.

### This is the first feature that is not a speed optimisation

Every Phase 1 step so far made a given depth cheaper while leaving the answer alone. This one
is the opposite: it **changes the answer, visits more nodes, and makes the engine stronger**.
Reporting it as a node-count change would be reporting the cost and calling it the result.

**The problem.** A fixed-depth search stops at an arbitrary moment. If the last move searched
was "queen takes pawn", the static evaluation is applied to a position where the opponent is
about to recapture the queen, and records it as "up a pawn". That is the *horizon effect*.

**The demonstration**, which is a test rather than a claim. On
`4k3/8/2p5/3p4/8/8/8/3QK3 w - - 0 1` at depth 1, where Qxd5 is met by cxd5 exactly one ply
past the horizon:

| config | best move | score |
|---|---|---|
| `tt-id` | **d1d5** (Qxd5) | 815 — "I won a pawn" |
| `quiescence-see` | e1f2 | 715 — "that loses the queen" |

The suite asserts *both* halves: that the engine without quiescence really does grab the pawn,
and that the engine with it does not. Asserting only the second would pass vacuously if the
position stopped being a horizon trap.

### Stand-pat is the part that is easy to get wrong

Inside quiescence, capturing is **optional** — the side to move can always decline and accept
the position as it stands. So the static evaluation is a *floor*, not the answer:

```python
best = evaluate(board)
if best >= beta:
    return best          # already too good; the opponent avoids this line
if best > alpha:
    alpha = best
```

Omit it and the side to move is *forced* to capture, which loses material on purpose. That is
one of the standard ways to add quiescence and make an engine weaker, so there is a test for
it: in a position where every available capture loses material, quiescence must return exactly
`evaluate(board)`.

### No stand-pat when in check, and all moves rather than captures

Two separate reasons, both worth stating because they are easy to conflate.

Standing pat means "I could choose to stop here", and **in check that choice does not exist**
— the position has to be resolved. Worse, the static evaluation has no idea the king is about
to be mated, so using it as a floor would report a lost position as fine.

And the escape may not be a capture. On `6k1/8/8/8/8/6P1/5P1P/r5K1 w - - 0 1` White is checked
along the first rank, cannot capture the rook, and has exactly one legal move: the quiet king
step Kg2. A quiescence that generated only captures would find no moves at all and conclude
White is mated. So when in check, every legal move is searched.

### The recursion cap, and why it is measured

Captures are self-limiting — material runs out — so a capture-only quiescence terminates.
**Check evasions are not.** A sequence of checks with no captures can in principle continue
indefinitely, and quiescence does not track repetition, so there is a hard cap of 32 plies.

A cap that silently fires would be a quality problem: the engine would return a static
evaluation while in check, which is precisely the thing quiescence exists to prevent. So the
searcher records the deepest quiescence ply it actually reaches and how often the cap fires,
and there is a test asserting zero cap hits across the benchmark set. Measured: the deepest
quiescence ply reached anywhere is **25 of 32**, on `kiwipete`, and the cap never fires. With
SEE enabled that falls to 18.

### Two deliberate omissions, stated rather than hidden

**Stalemate is not detected in quiescence.** Finding it would cost a full legal-move
generation at every quiescence node to catch a rare case, so a stalemate reached at the
horizon is scored by the static evaluation instead of as a draw. Standard trade.

**Under-promotions are not searched.** Quiet *queen* promotions are, because a quiet promotion
swings about eight pawns and leaving it out would be a hole in exactly the positions
quiescence exists for — and `generate_legal_captures()` does not include non-capturing
promotions. The extra generation is gated behind a bitboard test for a pawn on the seventh,
so it is free in the overwhelming majority of positions.

### SEE: the swap algorithm, and the two things it must get right

Quiescence looks at every capture, and most captures are bad. Searching a losing one costs a
whole subtree to discover what arithmetic already knows. SEE answers, statically, "what is the
net material outcome if both sides keep recapturing on this square with their cheapest
piece?"

The swap algorithm builds the gain list as the exchange proceeds and then folds it back:

```
gains[d] = value of the piece now on the square - gains[d-1]
...
gains[d-1] = -max(-gains[d-1], gains[d])
```

That second pass is the part worth understanding. The first list assumes the exchange runs to
completion, but **either side may stop at any point** — capturing is a choice. The fold is a
negamax over "stand here" versus "capture again", which is why SEE returns the value of
optimal play in the exchange rather than of exhausting it.

Two details it has to get right:

**X-rays.** When a piece captures it vacates its square, which can expose a rook or bishop
behind it — a battery. So the attacker set is recomputed against a *modified* occupancy mask,
not the real board. `python-chess` exposes `attackers_mask(color, square, occupied)`, which
takes exactly that mask, so SEE never makes or unmakes a move — it is genuinely static. There
is a test for a rook capturing with a queen behind it on the same file.

**The king cannot capture into check.** This one is enforced rather than simplified away,
because ignoring it is *not* conservative: it would let the king "recapture" illegally and
report a losing exchange as safe. The test has a control — remove the pawn covering the
square and the king's recapture becomes legal, and SEE must then report -400 instead of +100 —
so the test cannot pass by the king simply never being considered.

Two simplifications are conservative and therefore acceptable: **pins are ignored** (a pinned
defender is still counted, so SEE over-counts defenders and under-estimates some winning
captures, losing a little search rather than pruning a good move), and **promotions during the
exchange are ignored**.

### Measured

```
python -m bench.nodes --config quiescence-see --depth 4 --baseline bench/baselines/tt-id-d4.json
```

Depth 4, against `tt-id` at the same depth:

| config | nodes | aggregate | median | time | scores changed | best move changed |
|---|---:|---:|---:|---:|---:|---:|
| `tt-id` | 46,333 | — | — | 3.97s | — | — |
| `quiescence` | 208,119 | +349.2% | +168.1% | 8.85s | 8/13 | 4/13 |
| `quiescence-see` | 118,509 | +155.8% | +81.4% | 5.18s | 8/13 | 4/13 |

Node counts going **up** is the correct direction here, and the test suite asserts it in that
direction: `test_quiescence_increases_node_count_at_the_same_depth` would fail if quiescence
ever searched *less*, because that would mean it was not resolving the captures it exists to
resolve.

The useful number in that table is the last column. Quiescence changed the score on **8 of 13**
positions and the chosen move on **4 of 13** — `open-game`, `italian`, `perft-4` and
`wac-002`. Those four are positions where the engine was previously making a horizon error and
now is not. That is not a strength measurement, but it is an honest count of decisions changed.

### SEE's own contribution, and a result worth not over-claiming

`quiescence-see` against `quiescence`:

| | value |
|---|---|
| nodes | **-43.1%** aggregate, -16.1% median |
| time | **-41.4%** |
| best move | **13/13 identical** |
| score | **13/13 identical** |

So on this position set SEE removed 43% of the work and changed nothing at all. That is a very
clean result and it should not be over-read: **SEE is a heuristic filter, not a
score-preserving transformation.** It prunes captures it believes lose material, and because
it ignores pins it is wrong in a conservative direction some of the time. 13/13 here means
that on these thirteen positions every capture it pruned was genuinely irrelevant; it does not
mean that is guaranteed. It is reported as a measurement on a named set, not as a property.

### The headline: a more accurate search, far cheaper than the plain baseline

Quiescence is the first feature whose benefit is accuracy, so the right comparison is against
the original plain search at the same nominal depth — and then at greater depth.

| | nodes | seconds | vs `phase0` depth 4 |
|---|---:|---:|---:|
| `phase0`, depth 4 | 802,700 | 69.27 | — |
| `quiescence-see`, depth 4 | 118,509 | 5.18 | **-92.5% time** |
| `quiescence-see`, depth 5 | 670,564 | 21.43 | **-69.1% time** |
| `quiescence-see`, depth 6 | 1,991,205 | 81.20 | +17.2% time |

The first row of that comparison is the one to quote: **a depth-4 search that also resolves
every capture past the horizon now costs 7.5% of what the plain depth-4 search cost** — more
accurate and 13 times faster. And within roughly the same wall clock as the original depth-4
search, the engine now reaches **depth 6 with quiescence**, two plies deeper with the horizon
effect fixed.

### An incidental observation: nodes per second went up a lot

11,588 nps for `phase0`, around 11,500 for `tt-id`, and 23,000-31,000 for `quiescence-see`.
Quiescence nodes are individually much cheaper than main-search nodes: they call
`generate_legal_captures()` instead of generating every legal move, they sort by MVV-LVA alone
rather than consulting killers and history, and many of them return immediately on stand-pat.
So adding quiescence roughly doubles throughput while more than doubling the node count —
which is a good reminder that nodes per second is a property of the *mix* of node types, not a
measure of engine quality. An engine can raise its nps by doing cheaper and less useful work.

### What went wrong

Three things, all in the tests, and one of them was a genuine defect in work already
committed.

**An illegal test position had been committed in step 1.** A sweep of every FEN in the
repository through `python-chess`'s `status()` found that
`4k3/3P4/8/8/8/8/8/4K3 w - - 0 1` — used by three tests for promotion hashing and promotion
ordering — is not a legal position: the black king on e8 stands in check from the pawn on d7
while it is White's move. `python-chess` happily generated `d7xe8`, a move that captures the
king. The tests passed, because nothing they asserted depended on legality, but a promotion
test whose position allows capturing the king is not testing promotion. Replaced with
`5k2/3P4/8/8/8/8/8/4K3 w - - 0 1`. The sweep is worth running again whenever FENs are added
by hand.

**Two test positions in this step were wrong in the same family.** The first attempt at the
king-cannot-recapture test put the king where it could not make the *initial* capture at all,
so the test failed before reaching the thing it meant to check. The first attempt at the
check-evasion test used a rook on a8 and a king on g1 — which is not a check, since a8
attacks neither the first rank nor the g-file. Both were caught by assertions placed *before*
the interesting one (`assert move in board.legal_moves`, `assert board.is_check()`), which is
the argument for writing those guard assertions even though they look redundant.

---

## Phase 1, step 4 — principal variation search and aspiration windows

**Shipped** 2026-10-10

### What

PVS in both the main search and the root, plus aspiration windows around the previous
iteration's score. Three configs: `pvs-pure` (ordering plus PVS and nothing else, which
exists for the test rather than the benchmark), `pvs`, and `pvs-asp`.

### PVS: ask a cheaper question first

After good ordering, the first move at a node is usually the best one. PVS exploits that by
searching the first move with the full window and every later move with a **zero-width
window** `(alpha, alpha + 1)`:

```python
score = -self._negamax(board, depth - 1, -alpha - 1, -alpha, ply + 1)
if alpha < score < beta:
    self.pvs_researches += 1
    score = -self._negamax(board, depth - 1, -beta, -alpha, ply + 1)
```

A zero-width window can only answer "is this better than alpha, yes or no?" — and that is
much cheaper, because almost every node inside it finds a cutoff immediately. When the scout
is *surprised* and the move does beat alpha, its answer is only a bound, so the move has to be
re-searched with the full window to get a usable value.

**That re-search is the entire correctness argument.** Drop it and you have a search that is
faster and quietly wrong — the classic PVS bug. Which shapes the test: score equality alone
would also be satisfied by a PVS whose re-search never *fires*, so the suite asserts score
equality **and** `pvs_researches > 0`, which proves the surprise path is actually exercised.

Verified: `pvs-pure` returns the frozen baseline's exact score on all 13 positions at depths
1-3, on the cheap positions at depth 4, and specifically at depths 4, 5 and 6 on the one
endgame where the transposition table's score reuse deviates.

### PVS made the engine slower until it had a transposition table

This is the result worth keeping. Measured at depth 4 over the 13-position set:

| configuration | nodes | PVS's effect |
|---|---:|---:|
| ordering only | 53,854 | — |
| ordering + PVS | 59,023 | **+9.6%** |
| ordering + table | 49,492 | — |
| ordering + table + PVS | 45,512 | **-8.0%** |
| ordering + table + iterative deepening | 46,333 | — |
| ordering + table + iterative deepening + PVS | 43,505 | **-6.1%** |

Reproduce with `python -m bench.nodes --config pvs-pure --depth 4 --baseline bench/baselines/ordering-d4.json`.

So PVS is a **9.6% regression on its own** and an 8.0% win once a table exists. The reason is
exactly the mechanism above: when the scout is surprised, the re-search walks the same subtree
a second time. Without a table that work is simply repeated. With one, the scout has already
filled the table with bounds and best moves for that subtree, so the re-search mostly reads
back what the scout established, and the second pass is nearly free.

Which is a general point, not a chess one: PVS trades a cheap test plus an occasional repeat
against one expensive search, and the trade is only profitable when repeating is cheap. The
order the features were built in mattered — had PVS been attempted before the transposition
table, the measurement would have said "PVS does not work" and the conclusion would have been
wrong.

### Aspiration windows measured as worthless here, and that is the honest answer

Iterative deepening hands over the previous depth's score, and the next depth's score is
usually close to it. So search `(prev - 50, prev + 50)` instead of `(-inf, +inf)`: a narrow
window prunes much harder because cutoffs become easy to reach. If the true score falls
outside, the search *fails*, returns only a bound, and the window has to be widened and the
iteration repeated.

Measured, `pvs-asp` against `pvs`:

| depth | aggregate nodes | median per position | time |
|---|---:|---:|---:|
| 4 | +1.1% | -0.0% | +1.3% |
| 5 | +0.2% | -0.0% | +1.8% |
| 6 | -0.1% | -0.0% | -0.1% |

Zero, within noise, at every depth measured. **So it stays off in the recommended
configuration** and `pvs` rather than `pvs-asp` is the one to use.

Why it does nothing here, which is more interesting than the number:

* An aspiration window only opens from **depth 3 onwards** (depths 1 and 2 have scores far too
  unstable to guess from), so a depth-6 search gets four aspirated iterations and a depth-4
  search gets two. There is very little to win.
* The window has to be **re-searched on a miss**, and a miss costs more than the narrow window
  saved. On `wac-001` — a mate in 3 — aspiration drove the node count from 10,731 up to
  12,870 at depth 4, because the score swings violently between iterations and the window
  misses every time.
* Mate scores are excluded entirely, since a ±50 window around an ordinary score can never
  contain one. That removes the positions where iterative deepening was doing best.

Aspiration windows are standard in real engines and they do work there — at depth 20 with
dozens of iterations and scores that move by a few centipawns between them. The feature is
sound; the *setting* is wrong. Keeping it implemented, tested and off is the right outcome,
and being able to say which of your features did nothing and why is worth more than quietly
leaving it enabled.

### Testing a feature whose correctness lives in a re-search

Two tests are worth describing because they generalise.

`test_a_badly_wrong_window_still_produces_the_right_answer` feeds the aspiration logic a
`previous` score that is 600 or 5,000 centipawns away from the truth, which *guarantees* the
first search fails, and then requires the returned score to equal a full-window search's
score and `aspiration_misses > 0`. If widening were missing, or only widened the side that
did not fail, the result would be a bound rather than the true value. The general trick:
to test an error-recovery path, force the error rather than hoping the corpus contains one.

`test_a_root_fail_low_is_not_stored_as_exact` covers a bug this step introduced and the
implementation then had to fix. The root used to store its result as EXACT unconditionally,
which was correct while the root was always searched with a full window. Inside an aspiration
window it is not: the root can fail low, meaning no move beat the window's alpha, and the
score is then only an upper bound. Storing that as EXACT would poison the table with a value
the *next iteration* trusts completely. The root now classifies its flag the same way an
interior node does, and the test pins a window far above the true score to force the fail-low
and assert the stored flag is UPPERBOUND — with a control asserting a full-window root is
still stored as EXACT.

### Measured

`pvs` against `quiescence-see` at the same depth:

| depth | nodes | aggregate | median | time | scores |
|---|---:|---:|---:|---:|---|
| 4 | 111,401 | -6.0% | -1.6% | -8.2% | 13/13 identical |
| 5 | 621,103 | -7.4% | -3.7% | -9.6% | 13/13 identical |
| 6 | 1,879,060 | -5.6% | -2.4% | -7.8% | 12/13 identical |

The single difference at depth 6 is `rook-endgame`, which is the same position where the
transposition table's score reuse already deviated in step 2. Checked rather than assumed:
PVS with no table returns the frozen baseline's exact score on that position at depths 4, 5
and 6, so the deviation is the table's and not PVS's.

Cumulative, against the Phase 0 plain baseline at depth 4 (802,700 nodes, 69.27s):

| | nodes | seconds | time vs `phase0` depth 4 |
|---|---:|---:|---:|
| `pvs`, depth 4 | 111,401 | 4.76 | **-93.1%** |
| `pvs`, depth 5 | 621,103 | 19.37 | -72.0% |
| `pvs`, depth 6 | 1,879,060 | 74.84 | +8.0% |

### What went wrong

**The root's EXACT flag was wrong as soon as aspiration existed**, as described above. It was
not wrong before — the root really was always searched with a full window — which is a good
example of a correct assumption quietly becoming false when a feature lands two steps later.
Nothing flagged it; it was noticed while reasoning about what a failed root search returns,
and only then written as a test.

**The edit script failed halfway through.** The patch applying this step matched an anchor in
`__init__` that had already shifted, so the first three edits applied and the rest did not,
leaving the module in a half-patched state. It reported the failure clearly and the remaining
edits were applied separately, but the lesson stands: a multi-step edit script that is not
idempotent needs to say exactly which step failed, or recovery means guessing what landed.

---

## Phase 1, step 5 — time management

**Shipped** 2026-10-10 · completes Phase 1

### What

`Searcher.search_timed()`: iterative deepening against a millisecond budget, with a soft
limit that gates starting another iteration and a hard limit that aborts mid-iteration.
`allocate_time()` turns a clock into those two budgets. [`bench/timed.py`](../bench/timed.py)
measures depth reached per configuration at a fixed budget, which is the benchmark that makes
every previous step mean something.

Also: `CONFIGS["phase1"]`, an alias for `pvs` — everything that measured as a gain and
nothing that did not. Aspiration windows are deliberately absent.

### Why this is the step that makes the rest matter

At a **fixed depth**, a score-preserving optimisation plays *identical moves* to the
baseline — which is exactly what the differential tests enforce. So move ordering, the
transposition table and PVS are each worth exactly **zero** at fixed depth, by construction.
Their value exists only because the time they save buys extra depth, and nothing measured so
far could show that.

This is also why Phase 2's Elo harness has to play at a fixed time or node budget rather than
a fixed depth. Measuring Elo at fixed depth would correctly report +0 and look like a broken
harness.

### Two limits, doing different jobs

**Soft** is the target. After each completed iteration the search asks whether another would
fit, and stops if not. Stopping cleanly between iterations is much better than being cut off
inside one, because a completed iteration has a trustworthy *score* and a partial one does
not.

**Hard** is the ceiling that aborts mid-iteration. It exists because the prediction can be
wrong — a position where the score suddenly swings makes an iteration far more expensive than
its predecessor suggested.

`allocate_time(remaining_ms, increment_ms, moves_to_go)` splits a clock:
`remaining / expected_moves + 0.8 * increment` for the soft budget, with the hard budget at
three times that and both capped at 40% of what remains. Only 80% of the increment is
claimed, so that a consistently slight overshoot cannot drain the clock across a long game.
`moves_to_go` is used when the time control supplies it, and 30 is assumed otherwise:
guessing too low spends the clock early, too high leaves time unused at the end.

### Aborting a recursive search, and the bug that is waiting there

The hard limit is enforced by raising `SearchAborted` from deep inside the recursion. An
exception rather than a flag checked at every return, because the search returns a score from
four different places and a flag would have to be handled at each.

**The exception skips every pending `board.pop()` on the way out.** So after an abort the
board is sitting somewhere in the middle of a variation, several moves deep — and the *next*
search would run on the wrong position. That is a bug that would surface as an inexplicable
evaluation a long way from its cause.

Two options. Wrap every push in `try/finally`, which puts a context manager on the hottest
path in the engine. Or record the move-stack depth before the search and pop back to it
afterwards, which costs nothing and is just as correct. The second, with the Zobrist key
re-seeded from the restored position rather than unwound, since its stack is in the same
state as the board's.

`test_the_board_is_restored_after_an_abort` runs every benchmark position at a budget small
enough to force aborts and asserts the board is byte-identical afterwards, plus that at least
one abort actually happened so the test cannot pass vacuously.

### A partial iteration is not wasted, but its score is

When an iteration is cut short its **move** is kept and its **score** is discarded.

The move is sound because the previous iteration's best move is ordered first (the
transposition table supplies it), so any move that has since overtaken it is genuinely better
at the deeper depth. The score is not sound, because a root that has examined only some of its
moves has produced a lower bound, not a value.

So `TimedResult` reports `depth` and `completed_depth` separately, and they differ by one
when a partial iteration contributed the move. Collapsing them into a single number would
imply the score came from the deeper search.

### Three measured deficiencies, each found by looking at the numbers

None of these were predicted. All three came from reading the benchmark output and asking why
it looked wrong.

**1. The clock was checked too rarely to respect the hard limit.** The first version checked
every 2048th node, which at this engine's 25,000-30,000 nodes per second is 70-80ms. It
overshot a 500ms hard limit by 50ms. In a real game that is how an engine loses on time.
Tightened to every 256th node, about 10ms. The cost is negligible — `time.perf_counter()` is
on the order of 100ns, so this is roughly 120 calls per second — while checking at *every*
node would be measurable, since a node here costs about 35 microseconds. Worst overshoot now
measured across the whole set at four budgets: **+26.5ms**.

**2. The growth prediction banked unused clock.** The rule "start the next iteration only if
it is predicted to fit" is the textbook one and it was quietly wasting a third of the budget.
Dividing two very fast iterations gives a wild ratio, and with a generous clamp the prediction
came out enormous: `tt-id` stopped at depth 3 on `italian` having spent **276ms of a 1,500ms
budget**, while the *slower* `quiescence-see` reached depth 4 purely because its iterations
were large enough for the growth estimate to be meaningful.

That made the whole timed benchmark partly a comparison of time managers rather than of
searches, which would have produced a wrong conclusion about quiescence. Three fixes: the
growth factor is the **median of the last three ratios** rather than the last one; an
iteration taking under 2% of the budget is treated as too small to predict from; and while
less than **half** the hard budget has been spent, the next iteration is started regardless of
the prediction. The last one is justified by the step above — an aborted iteration keeps its
move, so being optimistic costs a score from one ply shallower while being pessimistic costs a
whole ply.

**3. The engine could in principle answer with no move at all.** With a budget smaller than
256 nodes of work, the first clock check can fire before any root move has completed, leaving
nothing published. A fallback now plays the first legal move. It never triggered in testing —
a 1ms budget still returns a move, because the first root move's subtree happened to be under
256 nodes — so the test *forces* it by making every iteration abort immediately. Returning no
move is a forfeit; playing the first move that comes to hand is strictly better.

That third point also records an honest limitation: the hard limit's granularity is 256 nodes,
so a budget smaller than that much work cannot be honoured. A 1ms budget on a dense middlegame
actually takes about 16ms. For the hundreds of milliseconds a real time control allows, this
does not arise.

### Measured — the headline for all of Phase 1

```
python -m bench.timed --ms 1000
python -m bench.timed --ms 3000
```

Mean depth reached across the 13 positions, same budget for every configuration, all driven
by the same iterative-deepening loop so that the comparison is between *searches* and not
between time managers:

| config | depth in 1s | depth in 3s |
|---|---:|---:|
| `phase0` (plain alpha-beta) | 3.54 | 4.15 |
| `ordering` | 4.85 | 5.23 |
| `tt-id` | 5.46 | 6.31 |
| `quiescence-see` | 5.54 | 7.00 |
| `phase1` | **5.62** | **7.08** |
| | **+2.08 plies** | **+2.93 plies** |

Median depth goes from 3 to 5 at one second and from 4 to 6 at three seconds. The gap widens
with the budget, which is what an exponential tree predicts: the deeper you are, the more a
constant-factor saving is worth in plies.

Per-step attribution at one second: ordering **+1.31**, transposition table and iterative
deepening **+0.62**, quiescence **+0.08**, PVS **+0.08**.

Quiescence contributing almost nothing *in depth* is the correct result and worth saying out
loud: quiescence does not buy depth, it buys accuracy at a given depth. It is roughly
depth-neutral here because its nodes are individually much cheaper — 31,782 nodes per second
against 15,244 for `tt-id` — so the extra nodes it visits very nearly pay for themselves. Its
actual contribution is the 8 of 13 positions whose score it corrected and the 4 of 13 whose
move it changed, which is a strength claim that only Phase 2 can put a number on.

### Where Phase 1 ended up overall

| | nodes | seconds |
|---|---:|---:|
| `phase0`, depth 4, fixed depth | 802,700 | 69.27 |
| `phase1`, depth 4, fixed depth | 111,401 | 4.76 |

Same depth, **6.9% of the time**, and with the horizon effect fixed on top. Under a
one-second clock the engine sees two plies deeper than the plain baseline, and under three
seconds, nearly three plies.

### What went wrong

The three deficiencies above are the story of this step, and the pattern in all three is the
same: the feature worked on the first attempt, and the *measurement* is what showed it was
working badly. The 2048-node check passed every test. The growth prediction passed every test.
Neither would have been found by anything except looking at the output and asking why
`tt-id` had used 276ms of 1,500.

One smaller thing: two tests in this file were written on assumptions that turned out to be
false — that depth 1 always completes (it does not; the abort can fire inside it, so
`completed_depth` can be 0), and that a 200ms budget on `kiwipete` would force an abort (it
stops cleanly via the soft gate instead). Both were rewritten to assert the invariant across
several budgets and to require that an abort happened *somewhere*, rather than assuming one
particular budget would produce one.
