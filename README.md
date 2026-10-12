# Chess-engine

A chess engine in Python, built on `python-chess` for board representation and move
generation, with its own evaluation and negamax alpha-beta search.

The point of the project is not to produce a strong engine — [Sunfish][sunfish] reaches
~2000 Lichess strength in 131 lines, and Stockfish is in another universe entirely. The
point is a search implementation that is **demonstrably correct** and whose every
optimisation is **measured** rather than assumed.

[sunfish]: https://github.com/thomasahle/sunfish

## Status

| phase | scope | state |
|---|---|---|
| 0 | Correct baseline: evaluation, negamax alpha-beta, test suite, CI | **done** |
| 1 | Move ordering, transposition table, quiescence, PVS, time management | **done** |
| 2 | Benchmark suite and self-play Elo measurement with confidence intervals | not started |
| 3 | UCI protocol, Lichess bot | not started |

Current measurements, all reproducible with the commands below:

- **862 tests passing**, covering evaluation symmetry, mate scoring, alpha-beta soundness,
  per-feature differential tests, and a tactical regression suite that runs against every
  search configuration.
- **Tactical suite: 312/312.** Nine positions — free material, mate-in-one, and a
  don't-throw-away-material check from the opening — across depths 1–4 make 24 cases, and
  each case runs against all 13 registered search implementations: the frozen Phase 0 search
  plus every entry in `CONFIGS`. Adding a configuration subscribes it to the suite with no
  test edit, which is the point.
- **Perft: 15/15** reference checks correct — five reference positions at depths 1–3,
  185,934 nodes, matching the published counts exactly. Throughput on the make/unmake path
  was ~660k nodes/sec aggregate on the machine it was recorded on; that figure is
  hardware- and load-dependent and will vary on yours, while the node counts will not.
- **Move ordering: −82.1% median nodes per position** at depth 4 over a 13-position set
  (−93.3% aggregate), with the score and best move unchanged on every position.
- **Transposition table: a further −24.0% median** at depth 5, rising to −32.3% with
  iterative deepening. Incremental Zobrist hashing validated against from-scratch
  recomputation at **7,037,881 nodes**, zero inconsistencies.
- **Quiescence with SEE pruning** fixes the horizon effect: it changed the chosen move on
  4 of 13 benchmark positions, and SEE removes 43% of its node cost with identical scores and
  best moves on all 13. The deepest quiescence recursion observed is 25 plies against a cap
  of 32, measured rather than assumed.
- **PVS was a 9.6% regression on its own and an 8.0% win once the transposition table
  existed** — it trades one expensive search for a cheap test plus an occasional repeat, and
  that only pays when repeating is cheap. Measured both ways rather than assumed.
- **Aspiration windows measured at zero** (−0.0% median at every depth tested), so they are
  implemented, tested, and switched off. Which features did nothing is part of the result.
- **A depth-4 search that also resolves every capture past the horizon now costs 6.9% of what
  the plain depth-4 search cost** — more accurate and 15× faster.
- **Under a clock, which is the measurement that matters: +2.08 plies of mean search depth at
  a 1-second budget and +2.93 plies at 3 seconds**, against the plain alpha-beta baseline
  over the same 13 positions. Per-step: move ordering +1.31, transposition table with
  iterative deepening +0.62, quiescence +0.08, PVS +0.08.
- **Worst hard-time-limit overshoot: +26.5 ms**, with the clock checked every 256 nodes.

Nothing about **strength** is claimed yet, because strength has not been measured yet. Node
count and depth are activity metrics; Elo is the result metric, and that is Phase 2. At a
fixed depth a score-preserving optimisation plays identical moves and is worth exactly 0 Elo
by construction, which is why the depth-per-budget figures above are measured under a clock
and why Phase 2's self-play harness will be too.

## Architecture

```
engine/evaluate.py   material + piece-square tables, SIDE-RELATIVE score
engine/search.py     FROZEN baseline: negamax + alpha-beta, mate-distance scoring
engine/searcher.py   Phase 1 search; every feature behind a flag on a frozen SearchConfig
engine/ordering.py   MVV-LVA, killer moves, history heuristic
engine/zobrist.py    incremental Zobrist hashing, O(1) per move, tree-walk validated
engine/transposition.py  bounded table, EXACT/LOWERBOUND/UPPERBOUND, stores the best move
engine/see.py        static exchange evaluation (swap algorithm) for capture pruning
                     PVS, aspiration windows and time management are in engine/searcher.py
bench/timed.py       depth reached within a fixed time budget, per configuration
bench/positions.py   the 13-position benchmark set, validated at import
bench/nodes.py       fixed-depth node/time benchmark, JSON baselines, comparison mode
bench/perft.py       node-count reference suite and make/unmake throughput benchmark
bench/summary.py     regenerates the results table from the committed baselines
tests/               invariants, differentials, tactical regressions, harness tests
docs/ENGINEERING.md  technical deep-dive: algorithms, methodology, results
docs/DEVLOG.md       one entry per shipped step: what, why, measured, what went wrong
```

`engine/search.py` is deliberately frozen. It is the control that every Phase 1 measurement
is taken against, so Phase 1 lives in `engine/searcher.py` with each feature behind a flag.
The all-flags-off configuration is required *by test* to match the frozen search on score,
best move and exact node count — otherwise the baseline would quietly stop being a baseline.

### The two invariants that hold this together

This engine is a rebuild of a predecessor that was broken by exactly two mistakes. Both are
now enforced by tests rather than by care.

**1. Evaluation is relative to the side to move.** `evaluate()` returns a positive score when
the player *about to move* is better. Negamax negates on the way up, so a White-positive
score at a leaf makes the search believe one side's advantage belongs to the other. The
regression test is `evaluate(board) == evaluate(board.mirror())`, since mirroring swaps
colours, flips the board and swaps the side to move.

**2. Mate scores are relative and distance-aware.** A node with no legal moves and a king in
check means *the side to move has been mated*, so the score is always `-MATE + ply` — never a
constant derived from the game result. The `+ ply` term makes faster mates score higher. The
predecessor returned `+1000000` whenever White won, regardless of whose node it was, and so
refused to play checkmate as White.

Terminal detection also runs *before* the depth cutoff, so a depth-1 search still sees mate
in one. This was caught by a failing test rather than by reasoning.

## Running it

**Requires Python 3.11 or newer** (CI runs 3.11, 3.12 and 3.13). The only runtime dependency
is `python-chess`; `pytest` is needed for the test suite.

```bash
git clone https://github.com/dhruv-ship-it/Chess-engine
cd Chess-engine

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

Everything below is run from the repository root, since the benchmarks are module entry
points (`python -m bench.x`) rather than scripts.

```bash
python -m pytest                      # full test suite, ~3 minutes (862 tests)
python -m pytest tests/test_tactics.py -q   # just the tactical gate, ~10 seconds
python -m bench.perft --depth 3       # node-count suite + throughput
python -m bench.perft --fen "<FEN>" --depth 4 --divide

# validate the incremental Zobrist key against a from-scratch recomputation at every node
python -m bench.perft --validate-zobrist --depth 3

# the search benchmark, and any configuration compared against a committed baseline
python -m bench.nodes --config phase0 --depth 4
python -m bench.nodes --config tt-id --depth 5 --baseline bench/baselines/ordering-d5.json

# depth reached in a fixed time budget -- the measurement that makes the rest mean anything
python -m bench.timed --ms 1000

# regenerate the cumulative results table from the committed baselines, rather than
# retyping numbers into the docs by hand
python -m bench.summary --depth 4 --markdown
```

There is **no UCI interface or GUI yet** — that is Phase 3 — so the engine cannot be
plugged into Arena or Lichess at the moment. The way to get a move out of it today is the
Python API below.

Configurations are `phase0`, `ordering`, `tt-move-only`, `tt`, `tt-id`, `id-only`,
`quiescence`, `quiescence-see`, `pvs-pure`, `pvs` and `pvs-asp`. `phase1` is an alias for
the recommended one. They
are finer-grained than the features on purpose: a transposition table bundles three separable
claims -- the best-move hint, the score cutoff, and the iterative deepening that exploits the
hint -- and they are worth very different amounts, so each is measured on its own.

```python
import chess
from engine.searcher import CONFIGS, Searcher

searcher = Searcher(CONFIGS["phase1"])

# fixed depth
result = searcher.search(chess.Board(), depth=5)
print(result.move, result.score, searcher.stats.nodes)

# or against a clock, which is what an engine actually does
timed = searcher.search_timed(chess.Board(), hard_ms=1500, soft_ms=1000)
print(timed.move, timed.score, timed.completed_depth, timed.elapsed_ms)
```

## Evaluation

Material and piece-square tables from Tomasz Michniewski's
[Simplified Evaluation Function][simplified], with a tapered king table switching between
middlegame and endgame placement.

[simplified]: https://www.chessprogramming.org/Simplified_Evaluation_Function

Michniewski designed these so that every engine under test shares one simplistic evaluation,
leaving *only search and efficiency* to determine the result. That is a deliberate fit for
this project: Phase 2 measures what each search feature is worth, so a fixed, standard,
unremarkable evaluation is a feature rather than a shortcoming. Upgrading to a tapered
[PeSTO][pesto] evaluation is a later option.

[pesto]: https://chessprogramming.org/PeSTO's_Evaluation_Function

## Scope and honesty notes

- **Move generation is `python-chess`, not this project.** Perft therefore validates
  `python-chess` rather than code written here. It is kept as a throughput benchmark and as
  the tree-walk harness that will validate incremental Zobrist hashing in Phase 1. It is not
  described as validating move generation.
- **No strength claim exists yet.** Elo requires self-play measurement with confidence
  intervals, which is Phase 2.
- Repetition detection inside the search is deliberately absent until Phase 1, when
  root-path tracking arrives with the transposition table.

## Measurement rules

Three of them, and each is a reaction to a specific failure in the predecessor project
rather than general good practice.

1. **No number without a committed script.** Every figure here comes from something in
   `bench/`, and the command is printed next to the number. The predecessor described one
   measurement as "~50%", "~58%" and "69.33%" in three different places, because no script
   owned it.
2. **Never benchmark on one position.** `bench/positions.py` holds 13 positions across
   openings, middlegames, tactical positions and endgames, validated at import.
   Per-position numbers are reported alongside the aggregate, because one position accounts
   for 44% of the aggregate's node count and would otherwise speak for the whole engine.
3. **A fresh searcher per position.** Sharing one would let killer moves, history and the
   transposition table warm up later positions. The predecessor's benchmark did exactly that
   between its baseline and its optimised run, so there is a test asserting the mechanism
   rather than just the determinism.

And one classification, because applying a single rule to every feature would reject a
correct implementation: move ordering and PVS **cannot** change the score at a fixed depth,
a transposition table's *move hint* cannot but its *score cutoff* can, and **quiescence
changes scores by design** while *raising* node counts at the same nominal depth.

The transposition table's deviation was measured rather than assumed, and then traced: an
entry is reused whenever `entry.depth >= depth`, so a node nominally searched to depth 2 can
be answered by a value a depth-5 search produced. The `depth` field is a lower bound on the
quality of the information, not a description of it. Details in
[docs/ENGINEERING.md](docs/ENGINEERING.md) section 6.

## Documentation

- [docs/ENGINEERING.md](docs/ENGINEERING.md) — technical deep-dive: architecture, each
  algorithm and why it is there, measurement methodology, results.
- [docs/DEVLOG.md](docs/DEVLOG.md) — one entry per shipped step: what changed, why, what it
  measured, what went wrong.
- [docs/STORY.md](docs/STORY.md) — the narrative version: how the project got here, and the
  reasoning behind each decision.
- `ENGINEERING_NOTES.md` — the audit of the predecessor engine that motivated this rebuild,
  with reproduction steps for each defect.
- `PLAN.md` — the phase plan and the ground rules.
