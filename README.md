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
| 1 | Move ordering, transposition table, quiescence, PVS, time management | **in progress** |
| 2 | Benchmark suite and self-play Elo measurement with confidence intervals | not started |
| 3 | UCI protocol, Lichess bot | not started |

Current measurements, all reproducible with the commands below:

- **268 tests passing**, covering evaluation symmetry, mate scoring, alpha-beta soundness,
  per-feature differential tests, and a tactical regression suite that runs against every
  search configuration.
- **Tactical suite: 12/12** on free-material and mate-in-N positions across depths 2–4, on
  every configuration.
- **Perft: 15/15** reference positions correct, ~660k nodes/sec aggregate on the make/unmake
  path.
- **Move ordering: −82.1% median nodes per position** at depth 4 over a 13-position set
  (−93.3% aggregate), with the score and best move unchanged on every position. A full extra
  ply of search costs 21% *less* wall-clock time than the unordered baseline needed for the
  shallower one.

Nothing about **strength** is claimed yet, because strength has not been measured yet. Node
count is an activity metric; Elo is the result metric, and that is Phase 2. At a fixed depth
a score-preserving optimisation plays identical moves and is worth exactly 0 Elo by
construction, so Phase 2 measures under a time budget.

## Architecture

```
engine/evaluate.py   material + piece-square tables, SIDE-RELATIVE score
engine/search.py     FROZEN baseline: negamax + alpha-beta, mate-distance scoring
engine/searcher.py   Phase 1 search; every feature behind a flag on a frozen SearchConfig
engine/ordering.py   MVV-LVA, killer moves, history heuristic
bench/positions.py   the 13-position benchmark set, validated at import
bench/nodes.py       fixed-depth node/time benchmark, JSON baselines, comparison mode
bench/perft.py       node-count reference suite and make/unmake throughput benchmark
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

```bash
pip install -r requirements.txt

python -m pytest                      # full test suite
python -m bench.perft --depth 3       # node-count suite + throughput
python -m bench.perft --fen "<FEN>" --depth 4 --divide

# the search benchmark, and any configuration compared against the Phase 0 baseline
python -m bench.nodes --config phase0 --depth 4
python -m bench.nodes --config ordering --depth 4 --baseline bench/baselines/phase0-d4.json
```

```python
import chess
from engine.searcher import CONFIGS, Searcher

searcher = Searcher(CONFIGS["ordering"])
result = searcher.search(chess.Board(), depth=4)
print(result.move, result.score, searcher.stats.nodes)
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
a transposition table should not but is not guaranteed not to, and **quiescence changes
scores by design** while *raising* node counts at the same nominal depth. Details in
[docs/ENGINEERING.md](docs/ENGINEERING.md).

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
