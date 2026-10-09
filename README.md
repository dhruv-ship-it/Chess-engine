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
| 1 | Move ordering, transposition table, quiescence, PVS, time management | not started |
| 2 | Benchmark suite and self-play Elo measurement with confidence intervals | not started |
| 3 | UCI protocol, Lichess bot | not started |

Current measurements, all reproducible with the commands below:

- **73 tests passing**, covering evaluation symmetry, mate scoring, alpha-beta soundness and
  a tactical regression suite.
- **Tactical suite: 12/12** on free-material positions across depths 2–4.
- **Perft: 15/15** reference positions correct, ~660k nodes/sec aggregate on the make/unmake
  path.

Nothing about strength is claimed yet, because strength has not been measured yet. That is
Phase 2.

## Architecture

```
engine/evaluate.py   material + piece-square tables, SIDE-RELATIVE score
engine/search.py     negamax with alpha-beta, mate-distance scoring, iterative deepening
tests/               evaluation invariants, search invariants, tactical regressions
bench/perft.py       node-count reference suite and make/unmake throughput benchmark
```

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
```

```python
import chess
from engine.search import search

result = search(chess.Board(), depth=4)
print(result.move, result.score, result.stats.nodes)
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

## Background

`ENGINEERING_NOTES.md` holds the audit of the predecessor engine that motivated this rebuild,
including reproduction steps for each defect. `PLAN.md` holds the phase plan and the ground
rules — chiefly that no number is published unless a committed script reproduces it.
