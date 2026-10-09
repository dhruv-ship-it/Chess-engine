# Plan — Chess Engine Rebuild

**Decided 2026-10-09.** Supersedes the roadmap in `ENGINEERING_NOTES.md` section 7.
Read `ENGINEERING_NOTES.md` first for the audit evidence behind every decision here.

## Decision

**Option 1: engine only.** No LLM/agentic layer on this project.

Rejected, deliberately: an agentic/eval layer on top of chess. The domain is saturated
(7+ comparable repos, 7+ papers; the research is in `private/CAREER_NOTES.md`,
which is not committed), no novelty is
available, and the agentic surface is shallow (one domain, a handful of trivial tools, one
loop). It would read as forced. An AI project will be built **separately**, in a domain that
actually fits — most likely a production RAG pipeline with an eval harness and documented
failure analysis, which was the highest-signal option in the research.

## Scope: what happens to the existing code

| component | fate | reason |
|---|---|---|
| UI work (timer, board skins, captured pieces, bootstrap) | **dropped** | Not going on the resume; 1366 lines of CSS and vendored chessboard.js are dead weight |
| Agent commits `50533e5`, `5d0b77b`, `4f146de` (MVV-LVA, Zobrist/TT, quiescence) | **implementations discarded, features rebuilt from scratch against tests** | The features are essential; the implementations are broken (5/12 tactics, won't mate as White) |
| Original base at `38a8d68` (piece-square tables, eval, working search) | **restored, then fixed** | Verified correct: 12/12 tactics, finds mate at every depth, all 6 tables correct |

We do **not** revert-and-stop. Plain alpha-beta with no ordering or TT is a textbook
exercise. Move ordering, transposition tables and quiescence are the substance of the
project and get rebuilt properly.

### What is actually carried over — not a clone

Clarified 2026-10-10. The new repo is **not** a fork or clone of this one. Concretely:

- **Piece values and piece-square tables: carried over as reference data.** These are not the
  original repo author's invention — they are Tomasz Michniewski's
  [Simplified Evaluation Function](https://www.chessprogramming.org/Simplified_Evaluation_Function),
  standard published tables from the Chess Programming Wiki. Using them is like using any
  standard constant table. (Michniewski's original purpose is a good fit here: he proposed
  that all engines under test share one simplistic evaluation so that *only search and
  efficiency* affect the result — which is exactly what the Phase 2 Elo harness measures.)
  Upgrade path later: [PeSTO's evaluation function](https://chessprogramming.org/PeSTO's_Evaluation_Function),
  which is tapered midgame/endgame.
- **The original `alpha_beta` function: not carried over.** It is written as minimax with a
  `maximiser` flag, has confused PV handling and a dubious `prev_moves` ordering hack, and
  carries the original author's own comment: *"What are these used for again? I need to
  simplify the logic here."* Clean negamax is roughly 100 lines and is written fresh.
- **Board representation, move generation and legality: `python-chess`**, as always. The
  project never implemented these.

So the amount of inherited *search code* is approximately zero. What carries over is standard
public evaluation data. This matters for how the work is described: it is an engine built on
`python-chess` using the standard Simplified Evaluation Function tables, not a modification
of someone else's engine.

### Consequence: this is a rebuild, not a repair

The four bugs in `ENGINEERING_NOTES.md` section 2.1 do not get "fixed" one by one, because
the code containing three of them is being discarded. The audit's value is **diagnostic** —
it tells us precisely what to get right the first time:

- Side-relative scores at every negamax leaf (the bug that broke the engine).
- `-MATE + ply` at mated nodes, never absolute.
- `table[square ^ 56]` for White — note this mirroring bug *was* inherited from the original
  and is the one genuine pre-existing defect.
- Tables validated as exactly 64 entries by a test, so corruption cannot recur silently.

**New repository.** Not a salvage of this one. This repo keeps committed `.swp` files, two
near-duplicate Flask apps, `index.html_orig`, stray files named `1` and `3`, no tests, no CI
and no `requirements.txt`, and its history would read "introduced bugs, then fixed my own
bugs." The engine is ported across; this repo is archived private.
**Repo name still to be chosen by the user — do not create it unprompted.**

## Are the features worth building? Yes — verified

Checked 2026-10-10, because the features being rebuilt are the same ones the agent commits
attempted. **The feature choices were correct; only the implementations were wrong.** These
are the standard, textbook components of a modern engine — iterative-deepening negamax with
alpha-beta, quiescence, a transposition table, null-move pruning and late-move reductions —
and their gains are documented:

| feature | reported gain |
|---|---|
| quiescence search | one engine went 1698 → 1937 Elo |
| transposition table (on top of the above) | 1937 → 1967 Elo |
| null-move pruning + a simple TT | ≈ +350 Elo (MinimalChess) |
| probing/storing the TT inside quiescence | +22.9 Elo |

Treat these as order-of-magnitude expectations, not targets — they are other people's engines
on other hardware. Phase 2 exists precisely so our own numbers are measured rather than
borrowed.

Comparable finished work, useful as a reference for scope and as proof this is a known-good
destination: [`MajdHail/chess-engine`](https://github.com/MajdHail/chess-engine) (Python:
alpha-beta, iterative deepening, TT, quiescence, null-move, LMR, PeSTO eval, UCI, difficulty
levels) and [`antoine-pz/pychess-engine-tipe`](https://github.com/antoine-pz/pychess-engine-tipe),
which evaluates its search optimisations via Bayeselo — i.e. the Phase 2 approach is
established practice, not something invented here.

## Phases

Each phase is independently shippable and each produces a measured number.

### Phase 0 — correctness (1 session)
- Restore the six piece-square tables from `38a8d68`.
- Fix the negamax score convention: leaf eval must be side-relative
  (`eval if turn == WHITE else -eval`).
- Fix mate scoring: always `-MATE + ply` at a mated node, never absolute. Adds mate-distance
  so shorter mates are preferred.
- Fix White's table mirroring: `table[square ^ 56]`, not `table[-square]`.
- **`perft` suite** against published node counts for standard positions (Kiwipete et al.).
  **Correction, 2026-10-10:** an earlier version of this plan called perft "the canonical
  correctness harness" for this engine. That is wrong and must not be repeated. Perft
  validates a *move generator*, and this project's move generator is `python-chess` — so
  matching the reference counts validates `python-chess`, not code written here. Perft is
  kept for two honest reasons: it is a clean throughput benchmark for the make/unmake path
  the search spends its time in, and it is the tree-walk harness that will validate
  incremental Zobrist hashing in Phase 1 (assert the incrementally updated key equals a
  from-scratch recomputation at every node) — and *that* assertion does test our code.
- **The real correctness harness is the invariant and differential test set**, because it
  tests code we wrote:
  - evaluation symmetry, `evaluate(board) == evaluate(board.mirror())`, which directly
    catches the sign bug that broke the predecessor;
  - piece-square tables asserted at exactly 64 entries, closing the corruption class;
  - mate score negative at a mated node, and distance-aware;
  - narrowed vs full window agreeing on score at fixed depth;
  - tactical regressions (free material, mate-in-N).
- Tactical regression suite (mate-in-N, free material, WAC subset) under `pytest`.
- CI running the suite on every push.
- Exit criterion: perft reference counts matched, tactics 12/12, invariants green.

**Phase 0 outcome (complete, 2026-10-10):** 73 tests passing; tactical suite 12/12 across
depths 2-4; perft 15/15 reference positions correct at ~660k nps aggregate. One real bug was
found *by* the tests: terminal (mate/stalemate) detection ran after the depth cutoff, so a
depth-1 search could not see mate in one. Terminal detection now precedes the cutoff.

### Phase 1 — search, rebuilt (1-2 sessions)
- MVV-LVA, killer moves, history heuristic.
- Transposition table that **stores the best move** (the main reason to have one), bounded
  with a replacement policy, correct EXACT/LOWERBOUND/UPPERBOUND flags.
- **Truly incremental Zobrist** — XOR on make/unmake, O(1) per move. This was claimed in the
  old code but never done; the hash was recomputed from scratch at every node.
- Quiescence using real capture generation (`generate_legal_captures`), plus SEE and check
  evasions.
- PVS / null-window, aspiration windows, null-move pruning, late move reductions.
- Iterative deepening done correctly.
- Fix the capture-generation hot path (23 of 42 profiled seconds).
- **Time management**: search to a millisecond budget, not a fixed depth.
- Exit criterion: depth 6-7 within a sane time budget, up from depth 4 in 12.5s.

### Phase 2 — measurement (1 session)
- Benchmark over a **position set**, never one FEN. Report nodes/sec and depth-in-1s.
- **Elo harness**: version A vs version B, N games, Elo delta **with confidence intervals**.
- Per-feature attribution: what each search feature is actually worth in Elo.
- Exit criterion: a results table where every number has a committed script that reproduces
  it.

### Phase 3 — ship (1 session)
- UCI protocol, so it runs in Cute Chess / Arena.
- Lichess bot via `lichess-bot` — a public, externally verifiable rating.
- README as product spec: architecture diagram, results table, reproduction commands.

Total: roughly 4-5 working sessions, with the user's study running in parallel.

## Resume, interview framing and the study list

Moved out of this file on 2026-10-10 into `private/CAREER_NOTES.md`,
`private/RESUME_BULLETS.md`, `private/INTERVIEW_QA.md` and `private/STUDY_PREREQS.md`, none
of which are committed. How the work is presented to employers does not belong in a public
repository that those employers will read; how the engine works does, and that is what
`docs/` is for.

The engineering constraints that came out of those sections *do* stay here, because they
constrain the code: every number needs a committed script that reproduces it, the README must
agree with the script, and no benchmark is ever quoted from a single position.

## Ground rules

- Correctness before performance. Perft and the tactical suite must pass before any pruning
  or ordering change lands — every bug found in the audit is the kind a node-count benchmark
  will happily report as an *improvement*.
- Never quote a benchmark from a single position.
- Every resume-eligible claim needs a committed, runnable script, and the README must agree
  with it.

## Status

- [x] Audit complete (`ENGINEERING_NOTES.md`)
- [x] Market research complete (`ENGINEERING_NOTES.md` sections 9-13)
- [x] Direction decided: Option 1, engine only
- [x] New repo created: `D:\projects\Chess-engine` (local; GitHub remote pending)
- [x] **Phase 0 — correct baseline.** 73 tests green, tactics 12/12, perft 15/15 at ~660k nps
- [x] **Phase 1 — ordering, transposition table, quiescence, PVS, time management.**
      859 tests green. Depth 4 at fixed depth: 802,700 nodes / 69.27s -> 111,401 / 4.76s.
      Under a clock: +2.08 plies at 1s, +2.93 plies at 3s. Incremental Zobrist validated
      over 7,037,881 nodes with 0 inconsistencies. Per-step numbers in `docs/DEVLOG.md`.
- [ ] Phase 2 — benchmark position set, self-play Elo with confidence intervals
- [ ] Phase 3 — UCI, Lichess bot
- [ ] Separate AI project (RAG + evals), scoped later

### Immediate next steps

Phase 1 is done. Phase 2 is next, and three things learned while building Phase 1 change how
it has to be built:

1. **Elo must be measured at fixed time or a fixed node budget per move, never fixed depth.**
   At fixed depth a score-preserving optimisation plays identical moves -- the differential
   tests enforce exactly that -- so it is worth 0 Elo by construction. `search_timed` and
   `bench/timed.py` already exist for this.
2. **The control is `engine/search.py` driven through `Searcher.search_timed`**, so the
   comparison is between searches rather than between time managers. `bench/timed.py` does
   this already; the discovery that it matters is written up in the step-5 devlog entry.
3. **Per-feature attribution is already wired.** `engine.searcher.CONFIGS` holds each
   intermediate configuration, so an Elo-per-feature table needs the harness, not new engine
   code.

Remaining Phase 2 work: an opening book of varied positions, each played twice with colours
reversed; a self-play harness with Elo plus confidence intervals (and SPRT if time allows);
and a results table where every number names the script that produced it.
