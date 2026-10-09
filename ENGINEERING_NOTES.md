# Engineering Notes — ChessAI

> Working notes for this repo. Read this first in a new session.
> Status as of 2026-10-09. Audit performed on commit `4f146de`.

## 1. What this repo actually is

- `chess_engine.py` (525 lines) — one `Engine` class: eval, minimax, negamax + alpha-beta,
  move ordering, Zobrist/TT, quiescence. This is the whole engine.
- `flask_app.py` / `flask_appnew.py` (31/28 lines) — near-duplicate Flask wrappers. One
  `Engine` is constructed **per HTTP request**, so the transposition table and the Zobrist
  key table are thrown away and re-randomised on every move.
- `static/scripts.js` (523), `static/style.css` + `style2.css` (1366), `templates/index.html`
  (165) — UI. Board rendering is chessboard.js; frontend legality is chess.js.
- Board representation, move generation and legality all come from `python-chess`. The
  project does **not** implement bitboards, move generation, or make/unmake.
- Provenance: forked from an open-source alpha-beta base (see `ATTRIBUTION.md`). The
  minimax/alpha-beta skeleton and the eval were inherited. The three features the resume
  bullets describe (MVV-LVA + killer moves, Zobrist + TT, quiescence) were added in commits
  `50533e5`, `5d0b77b`, `4f146de`.

## 2. Audit result: the added features made the engine weaker

Measured and reproducible. See section 4 for how.

**Free-material tactics suite** — 4 positions, each with exactly one obviously winning
capture, tested at depths 2/3/4 (12 trials):

| engine path | score |
|---|---|
| `calculate_minimax` (inherited, pre-"optimisation") | **12 / 12** |
| `calculate_ab` (negamax + TT + quiescence, current) | **5 / 12** |

**Mate in 1** — `6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1`, where only `Ra8#` mates:
`calculate_ab` plays `Kh1`/`Kh2` at depths 2, 3 and 4. It refuses to deliver mate as White.
`calculate_minimax` finds `Ra8#`. The same engine playing Black *does* find mate.

### 2.1 Root cause — four correctness bugs

1. **The negamax sign convention is violated.** `position_eval()` returns a White-positive
   score. `negamax` and `quiescence` negate on the way up (`score = -new_score`), which
   requires the leaf value to be **relative to the side to move**. It never is.
   Verified: for `4k3/8/8/8/8/8/8/3QK3`, `position_eval()` returns `+890` with White to move
   *and* `+890` with Black to move. Black is a queen down and scores it as winning.
   Fix: return `eval if board.turn == WHITE else -eval` at every negamax leaf.

2. **The checkmate score is absolute, not side-relative.** `chess_engine.py:298-302` returns
   `+1000000` when `board.result() == "1-0"`. In negamax, a node with no legal moves and the
   king in check means *the side to move has been mated*, so the value is always `-MATE`.
   Verified: at a node where Black is mated and Black is to move, `negamax` returns
   `+1000000`. This is why White never mates.
   Fix: always `-MATE + ply`, which also adds mate-distance so shorter mates are preferred.

3. **Five of six piece-square tables are not 64 entries.** Rows are missing or duplicated:

   | piece | length | expected |
   |---|---|---|
   | pawn | 62 | 64 |
   | knight | 63 | 64 |
   | bishop | 82 | 64 |
   | rook | 80 | 64 |
   | queen | 104 | 64 |
   | king | 64 | 64 |

   Positional lookups therefore read the wrong squares, and for the queen and bishop the
   index often lands past the intended row entirely. Separately, the `# bishop` / `# knight`
   comments in `piece_values` are swapped relative to `python-chess`, where 2 = KNIGHT and
   3 = BISHOP. The tables themselves happen to be in `python-chess` order, so only the
   comments and the 310/300 values are transposed — cosmetic next to the length problem.

4. **Table mirroring for White is wrong.** `position_eval` uses
   `self.square_table[i][-square]`. Negative indexing maps square 0 to index 0 and square 1
   to index 63 — a 180-degree rotation off by one, not a rank flip. The tables are written
   a8-first, so the correct lookup is `table[square ^ 56]` for White and `table[square]` for
   Black.

## 3. Audit result: the performance work is real, but the implementation defeats itself

**The node-reduction claim reproduces.** `python chess_engine.py`, on the hardcoded FEN, at
depth 4:

| | leaf nodes | time |
|---|---|---|
| no ordering | 24,692 | 34.88 s |
| MVV-LVA + killer moves | 7,574 | 12.56 s |
| | **-69.3%** | **-64.0%** |

Move ordering genuinely works; that part is not fabricated. Caveats before quoting it: it is
**one position**; `killer_moves` is not reset between the two runs, so the baseline run warms
it for the ordered run; and the counter only increments at `depth == 0` in `negamax`, so
quiescence nodes are excluded from "nodes explored".

**But 12.6 s for depth 4 is two to three orders of magnitude off.** `cProfile` on that search
(42 s under the profiler, 77 M function calls):

- `quiescence` accounts for 40.6 s of 42 s.
- `chess_engine.py:200` — `[m for m in self.board.legal_moves if self.board.is_capture(m)]`
  — costs 23 s on its own. It generates *every* legal move and filters. Called 133 k times,
  driving **3.44 M** `generate_legal_moves` calls. `python-chess` ships
  `board.generate_legal_captures()`; switching is a one-line change.
- `position_eval` costs 12.1 s over 182 k calls, via 2.3 M `board.pieces()` calls.

**The Zobrist hashing does not do the thing Zobrist hashing exists for.** `compute_hash()`
walks all 64 squares and re-queries castling and en-passant, and it is called *fresh at every
node* (`chess_engine.py:274`). The entire point of a Zobrist key is **incremental** update:
XOR the moving piece out of its origin square and into its destination on make/unmake, O(1)
per move. `self.current_hash` is set in `__init__` and then never read or updated again —
dead code. As written the hash is no cheaper than `board.fen()`, and `python-chess` already
provides `chess.polyglot.zobrist_hash(board)`. **An interviewer who knows chess programming
finds this in under a minute**, because "I implemented Zobrist hashing" and "I recompute the
hash from scratch at every node" cannot both be true of a correct implementation.

**The transposition table does not store a best move.** The largest practical benefit of a TT
in an alpha-beta searcher is retrieving the previous best move and searching it first. This TT
stores only `{depth, score, flag}`, so it cannot help ordering at all. It is also unbounded
(no replacement policy, no ageing), and TT hits `return [], score`, which destroys the PV.

**`ATTRIBUTION.md` presents a bug signature as a result.** It cites "Non-zero leaf metrics
confirmed (276 leaves at depth 4)". That 276 comes from the iterative-deepening run in
`__main__`, which executes *after* the two benchmark searches **without clearing the TT**, so
it is answering almost entirely from stale, wrong-sign TT entries. A depth-4 search reporting
276 leaves is evidence of a broken TT, not of an optimisation.

**The numbers are inconsistent across artifacts.** Commit `50533e5` says "~58%",
`ATTRIBUTION.md` says 69.33%, the resume bullet said "~50%". Measured today: 69.3%. Three
different figures for one measurement is itself an interview liability.

## 4. Reproducing the audit

`python-chess 1.11.2` and Python 3.11.4, both already installed.

- Project's own benchmark: `python chess_engine.py` (~50 s).
- Table lengths: build `Engine(chess.STARTING_FEN)` and print
  `{pt: len(t) for pt, t in e.square_table.items()}`.
- Sign bug: compare `Engine(fen).position_eval()` for one FEN with ` w ` and with ` b `.
- Mate bug: `Engine("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1").calculate_ab(3)` should return
  `a1a8` and does not.
- Mated-node score: push `a1a8`, then `Engine(board.fen()).negamax(1, -inf, inf)` returns
  `+1000000`; the correct value is negative.

`calculate_ab` and `iterative_deepening` print to stdout — redirect it when scripting.

## 5. Other repo-hygiene issues

- Committed editor swap files: `.flask_app.py.swp`, `templates/.index.html.swp`,
  `static/libs/chessboard/js/.3.swp`, plus stray files `static/libs/chessboard/js/1` and `3`.
- `templates/index.html_orig` left in the tree.
- `readme.txt` rather than `README.md`; it documents only `pip install` — no usage, no
  results, no screenshot.
- `flask_appnew.py` hardcodes `host="172.21.0.4"`.
- Unused imports in `chess_engine.py`: `signal`, `cProfile`. Unused `MAX_DEPTH = 60`.
- Zero tests. `board_test.py` is a 12-line scratch file that prints a board.
- No `requirements.txt`, no CI.
- `ATTRIBUTION.md` is written in "we" and reads as machine-generated. It also asserts
  "textbook-correct flag classification", which section 2 disproves.

## 6. Verdict on resume use, as of today

**Do not ship the current bullets.** They are accurate *descriptions of code that exists*,
but the code is wrong, and the engine is measurably weaker than the version it was forked
from. The failure mode is bad: the bullets name exactly the three things that are broken, so
they invite precisely the questions that expose them. Anyone who clones the repo finds an
engine that will not deliver checkmate. That is worse than omitting the project.

The project *type* is good. Classical search is a respected SDE signal — search, pruning,
data structures, measurable performance — and it is not saturated the way a CRUD app is. It
is a weak signal for "AI Engineer" in the current meaning of that title unless a learned
component is added. The fix is not to abandon it. It is to make it true, then add one thing
that is genuinely hard.

## 7. Roadmap

Ordered. Each phase is independently shippable and each produces a number.

**Phase 0 — make it honest (prerequisite for everything).**
- `perft` suite against published node counts for standard positions (Kiwipete and friends).
  This is the canonical correctness harness for a chess engine, the main credibility artifact,
  and it also yields a nodes/sec figure.
- Fix the four bugs in section 2.1. Re-run the tactics suite; target 12/12.
- Tactical regression suite (mate-in-N, free material, a WAC/ECM subset) wired into `pytest`.
- Rewrite `ATTRIBUTION.md` in first person and drop the false claims.

**Phase 1 — make it fast.**
- `generate_legal_captures()` in quiescence; check evasions in quiescence; SEE for capture
  pruning.
- Truly incremental Zobrist on make/unmake, or drop the hand-rolled version in favour of
  `chess.polyglot.zobrist_hash` and describe it accurately. Store the best move in the TT and
  use it as the first ordering key. Bound the TT with a replacement policy.
- PVS / null-window, aspiration windows, null-move pruning, late move reductions, history
  heuristic.
- Time management: search to a time budget rather than a fixed depth.
- Benchmark over a *position set*, not one FEN. Report nodes/sec and depth reached in 1 s.

**Phase 2 — measure strength properly.**
- Self-play harness: version A against version B, N games, Elo delta **with error bars**.
  This is the step that converts the project from "I implemented features from Wikipedia"
  into "I measured what each feature was worth," and it is the most interview-durable part of
  the whole plan.

**Phase 3 — make it real software.**
- UCI protocol. Then it runs in any real chess GUI (Cute Chess, Arena) and can be deployed to
  Lichess via `lichess-bot` to play rated games. A public bot with a real rating is an
  externally verifiable claim, which almost no student project has.

**Phase 4 — the learned component (the differentiator).** Pick one:
- *Texel tuning*: fit the hand-crafted eval weights by logistic regression on game outcomes.
  Cheap, roughly a day, legitimately "learned parameters", and measurable in Elo.
- *NNUE-style eval*: a small net (768 to N to 1) trained on positions labelled with engine
  evaluations, quantised, with an incrementally updated accumulator. This is the actual modern
  technique, it is real ML, and it bridges the SDE and AI-Engineer framings. Biggest payoff,
  biggest cost.
- *Policy network for move ordering*: train a net to predict the move a strong player would
  make and order by its output, then measure node reduction against the MVV-LVA baseline.
  This is the strictly better version of the existing resume bullet.

**Explicitly rejected: RAG, or bolting an LLM onto the engine.** There is nothing to retrieve,
LLMs play weak chess, and it reads as resume-driven architecture to anyone who knows the
domain. The legitimate AI angle here is a learned evaluation or a learned policy.

## 8. Ground rules for future sessions

- Every claim that could become a resume line needs a committed, runnable script that
  reproduces the number, and the number in the README must match that script's output.
- Never quote a benchmark from a single position.
- Correctness before performance. `perft` and the tactical suite must pass before any pruning
  or ordering change is accepted, because every bug in section 2.1 is the kind that a
  node-count benchmark will cheerfully report as an *improvement*.

## 9. Market research, 2026-10-09 (for choosing the AI layer)

Sources are listed in section 10.

**What the 2026 AI-engineer hiring market rewards.** Consistent across sources:
- *Evals are the differentiator.* "Eval is the new system design." A production eval suite is
  cited as among the highest-callback portfolio projects, and the common failure is being
  unable to defend your own eval.
- Agentic tool-use orchestration is the fastest-growing LLM hiring category, but agent
  projects are only credible with **trajectory evaluation**: retry budgets, structured tool
  errors, a real verifier, and pass-rate reliability numbers.
- Production RAG ranks highest of all *if* it has hybrid retrieval (dense + BM25), a
  reranker, and grounding metrics (faithfulness, recall@20) plus a documented failure
  taxonomy. Chess is a poor RAG domain — there is no document corpus to ground against.
- Non-negotiable packaging: README as product spec with an architecture diagram, a live
  deployed URL (reviewers do not clone), named eval metrics with targets, and cost/latency
  figures ($/1k tokens, p50/p95).
- Explicitly warned against: many toy notebooks, and copied eval frameworks you cannot
  defend.
- For SDE specifically: numbers in every bullet; "designed, built, deployed" over
  "contributed to"; evidence of real use; a tutorial clone is not persuasive unless
  substantially extended.

**Saturation check on the obvious chess + AI ideas.**
- *Voice move input* ("move e4 to e5") is heavily saturated and shallow. Existing work
  includes a published Chrome extension (Speak to Lichess), MLH Fellowship's ACE, several
  GitHub repos, Hugging Face Spaces, and `wchess`, the official whisper.cpp browser demo.
  Chess notation is a constrained grammar, so the "NLP" reduces to a regex over
  speech-to-text output. **Do not make this the headline feature.**
- *LLM chess coach* is saturated — at least two separate repos are literally named
  `LLM-ChessCoach`, plus `chess_openings_teacher` and others.
- *LLM-vs-engine play via tool calls* already exists: `LYZhelloworld/chess-llm`,
  `carlini/chess-llm`, `AidanCooper/llm-chess`, `maxim-saplin/llm_chess`, and
  `byjustinjones/agentchess` (round-robin tournaments against a calibrated engine ladder,
  with MCP support).

**The one angle where owning an engine is a real architectural advantage.** Chess-as-LLM-eval
is an active research area, not a toy: ChessQA (arXiv 2510.23948), Chess-World-Model
(arXiv 2605.30100), PGN2FEN-style state tracking (arXiv 2508.19851), SPIN-Bench
(arXiv 2503.12349). The reason this matters here: the scarcest resource in LLM evaluation is
**ground truth**, and chess supplies an oracle. A correct engine can verify legality, score
every position in centipawns, and name the best move at a fixed depth — so it can grade an
LLM's play objectively, which an LLM-judge cannot. That is a defensible reason for this repo
to exist inside an AI project rather than being decoration.

Caveat to stay honest about: the *idea* is not novel (see the repos above). Differentiation
has to come from rigor — own engine as oracle, a real failure taxonomy, statistics with
error bars, and a deployed dashboard — not from claiming to be first.

**Deprioritised:** NNUE / learned eval / policy-net move ordering are genuine ML and good
SDE signals, but they produce none of the keywords or skills that "AI Engineer" currently
means, and the user has asked specifically for agentic/GenAI. Keep as optional later work.

## 10. Sources

- https://github.com/landedjobs/projects-to-land-an-ai-job
- https://github.com/landedjobs/ai-engineer-portfolio-projects
- https://dev.to/klement_gunndu/5-ai-portfolio-projects-that-actually-get-you-hired-in-2026-5bpl
- https://www.dataquest.io/blog/ai-projects/
- https://mirrorcv.com/resume-guide/software-engineer-faang
- https://profileelevate.com/resources/guides/faang-resume-playbook/
- https://arxiv.org/pdf/2510.23948 (ChessQA)
- https://arxiv.org/abs/2605.30100 (Chess-World-Model)
- https://arxiv.org/pdf/2508.19851 (state tracking with chess)
- https://arxiv.org/pdf/2503.12349 (SPIN-Bench)
- https://github.com/maxim-saplin/llm_chess
- https://github.com/byjustinjones/agentchess
- https://github.com/LYZhelloworld/chess-llm
- https://github.com/AidanCooper/llm-chess
- https://chrome.google.com/webstore/detail/speak-to-lichess/ldiiocggpmgljihlkgpngjlhnggpapig
- https://github.com/MLH-Fellowship/ACE
- https://whisper.ggerganov.com/wchess

> **Direction not yet chosen.** Section 7 phases 0-1 (correctness + speed) are agreed and
> unblocked. The AI layer on top is under discussion; do not start it without the user's
> decision.

## 11. Repo archaeology, 2026-10-09 — the breakage is traced and reversible

**Verified by checking out the first commit (`38a8d68`, 2025-06-13) and running it.**

The repo has 18 commits. `chess_engine.py` was 348 lines at the first commit and is 525 now.
The user's own commits are UI work (bootstrap, timer, board skins, captured-piece display,
algorithm selector). The engine changes all live in the last three commits (`50533e5`,
`5d0b77b`, `4f146de`) and were agent-generated.

**The original engine was correct. The agent edits broke it.** Same test suites, same
positions:

| | tactics suite | mate in 1 (White) | mate in 1 (Black) | PST lengths |
|---|---|---|---|---|
| original `alpha_beta` (`38a8d68`) | **12 / 12** | finds `Ra8#` at d2, d3 | finds `Ra1#` | **6/6 correct at 64** |
| current `calculate_ab` (`4f146de`) | **5 / 12** | misses at d2, d3, d4 | finds it | **5/6 corrupted** |

Two specific regressions, both introduced by the agent commits:

1. **The piece-square tables were corrupted.** At `38a8d68` all six tables are exactly
   8 rows of 8. The agent edits duplicated and dropped rows, producing the 62/63/82/80/104
   lengths in section 2.1. This is pure data corruption and is fixed by restoring the
   original literals from git.
2. **A minimax-to-negamax conversion was done without converting the score convention.**
   The original `alpha_beta` used an explicit `maximiser` flag with **absolute**
   (White-positive) scores — internally consistent, and `+1000000` for a `1-0` result is
   *correct* in that framework. The agent rewrote the search as negamax, which requires
   **side-relative** scores, but left `position_eval()` and the mate constants absolute.
   That single conceptual mismatch is the root of both sign bugs.

The pre-existing `[-square]` mirroring bug (section 2.1 item 4) *is* inherited from the
original, not agent-introduced.

**Conclusion: this is one data corruption plus one conceptual error, not pervasive rot.**
`git` holds a known-good baseline, and the file is small. There is no reason to expect a
long tail of hidden bugs once the score convention is made consistent and a perft plus
tactical suite is in place. The correct move is to restore the original tables from
`38a8d68` and re-implement the three features (ordering, TT, quiescence) against tests,
rather than to patch the current code or to abandon the engine.

## 12. Engine landscape — why write one at all

- **Stockfish** is the reference: C++, NNUE evaluation, far beyond anything written here. It
  is the correct choice whenever *strength* or *ground-truth evaluation* is required, and it
  drives via UCI through `chess.engine` in `python-chess`.
- **Sunfish** (`thomasahle/sunfish`) is ~111-131 lines of Python and plays roughly
  1900-2000 on Lichess. Worth internalising: it is **smaller than this repo's 525-line
  engine and vastly stronger**.
- A fixed version of this engine will land far below both.

So the honest reason to own an engine is **not** to obtain a strong engine — that is a losing
race. It is to demonstrate understanding of search: alpha-beta, move ordering, transposition
tables, quiescence, profiling, and correctness testing. That is a legitimate SDE signal on
its own, and it should be claimed as exactly that and nothing more.

**Correction to section 9.** Section 9 proposed using this engine as the ground-truth oracle
for an LLM evaluation harness. That is methodologically weak and should not be done: a
sub-1500 Python engine is not a credible authority on best-move or centipawn ground truth,
and "why not Stockfish?" has no good answer. The defensible architecture is:
- **Stockfish** = the oracle (centipawn scores, best move at fixed depth).
- **`python-chess`** = legality and state verification.
- **This engine** = one *graded rung on the opponent ladder*, with its own measured Elo
  reported alongside the LLMs and Stockfish skill levels.

That keeps the engine honest and useful without overclaiming, and "I used Stockfish where
strength mattered and my own engine where understanding mattered" is a strong interview
answer because it demonstrates judgement.

## 13. Saturation reality check for chess + LLM work

Beyond the repos in section 9, the academic space is crowded: ChessQA (2510.23948),
Chess-World-Model (2605.30100), ChessArena (2509.24239), LLM Chess (2512.01992), strategic
reasoning post-training (2507.00726), tool-augmented commentary hallucination evaluation
(2608.04240), SPIN-Bench (2503.12349). Prompt-representation ablations (FEN vs PGN vs ASCII,
with and without a legal-move list) are **already published** — notably the finding that
representation choice matters a lot for some models and barely at all for others.

**Therefore: novelty is not available in this domain.** Any chess + LLM project here is a
*competence demonstration*, not a research contribution, and must be pitched that way. The
corollary is that the agentic engineering in chess is also fairly shallow — it is
single-domain with a handful of trivial tools, so it exercises orchestration far less than a
messy multi-tool or document-retrieval domain would. Weigh that against the cohesion and
speed benefits before committing.
