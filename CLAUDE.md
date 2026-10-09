# Context for Claude Code sessions

This file is loaded automatically. Read it before doing anything in this repo.

## What this is

A rebuild of a chess engine. The predecessor lives at `D:\projects\ChessAI` (archived, not to
be worked on). That repo was forked from an open-source alpha-beta engine, then had three
agent-generated commits added that **broke it** — tactical score went from 12/12 to 5/12 and
it would not play checkmate as White.

`ENGINEERING_NOTES.md` is the full audit with reproduction steps. `PLAN.md` is the decided
plan. Read both before proposing changes.

## Decisions already made — do not relitigate

- **Engine only.** No LLM, agentic or RAG layer on this project. That was explored in depth
  and rejected: the chess + LLM space is saturated (7+ comparable repos, 7+ papers, see
  `ENGINEERING_NOTES.md` section 13), no novelty is available, and the agentic surface in
  chess is shallow. The user explicitly rejected it as forced. An AI project will be built
  separately in a domain that fits.
- **This engine is not an evaluation oracle.** If ground-truth evaluation is ever needed,
  that is Stockfish's job. See the correction in `ENGINEERING_NOTES.md` section 12.
- **Move generation stays with `python-chess`.** Writing one is out of scope.
- **Do not describe perft as validating move generation** — `python-chess` owns that. Perft is
  a throughput benchmark and the harness for Zobrist validation.
- **Never claim the engine was "inherited broken."** It was working; the agent commits broke
  it. See the narrative section in `PLAN.md`.

## Ground rules

1. **Correctness before performance.** `python -m pytest` must pass before any search change
   lands. Every bug in the predecessor was the kind a node-count benchmark cheerfully reports
   as an improvement.
2. **No number ships without a committed script that reproduces it.** This includes README
   figures and anything destined for the user's resume. The predecessor had the same
   measurement described as ~50%, ~58% and 69.33% in three different places.
3. **Never benchmark on a single position.** Use a position set.
4. **Know which features may change the score at fixed depth, and which may not.**
   Corrected 2026-10-10; an earlier version of this rule said *all* Phase 1 features are
   score-preserving, which is false for quiescence and would make a correct implementation
   look like a bug.
   - **Score-preserving — a score change is a bug:** move ordering (MVV-LVA, killers,
     history), PVS and aspiration windows. Gate:
     `test_alpha_beta_agrees_with_full_window_at_same_depth`, plus per-feature differential
     tests asserting the same score and strictly fewer nodes.
   - **Score-preserving in practice, not in theory:** the transposition table. Entries are
     reused only when `entry.depth >= depth`, but bound-based cutoffs can cause search
     instability. Treat a score change as a strong bug signal, then prove it either way.
   - **Not score-preserving, by design:** quiescence (it resolves captures past the horizon,
     so fixed-depth scores change and nominal-depth node counts *rise*), null-move pruning
     and late move reductions. Their gates are the tactical suite and horizon-effect
     positions, never score equality.
5. Keep the Phase 0 plain search reachable, because Phase 2 measures Phase 1 against it.
   `engine/search.py` is frozen as that control; Phase 1 lives in `engine/searcher.py`.
6. **Elo is measured at fixed time or fixed node budget per move, never at fixed depth.** At
   fixed depth a score-preserving optimisation plays identical moves and is worth 0 Elo by
   construction. The gain only exists when saved time buys extra depth.

## Working agreements — apply without being asked

- **Commit as the user's GitHub id.** Local config is already
  `user.name = dhruv-ship-it`, `user.email = irldhruv196@gmail.com`. Do not change it.
- **Commit incrementally, one commit per completed step.** Run the tests and record the
  measurement first; put the number in the commit message. Do not push unless asked.
- **Document in the same commit as the code**, at four levels:
  `docs/DEVLOG.md` (what/why/measured, per step), `docs/ENGINEERING.md` (technical
  deep-dive for a hiring engineer), `docs/STORY.md` (portfolio narrative for a
  non-specialist), and code comments carrying the *why*.
- **`private/` is gitignored and must never be committed.** It holds interview prep, study
  lists, follow-up questions, resume bullet drafts and job-market notes. Rule of thumb:
  how the engine works goes in `docs/`; how to pitch it to employers goes in `private/`.
- **The end deliverable is 1-2 short resume bullets** with quantifiers that each come from a
  committed script. Drafts and their provenance live in `private/RESUME_BULLETS.md`.

## Current state

**Phase 0 and Phase 1 are both complete.** 859 tests passing, tactical suite 12/12 on every
search configuration, perft 15/15, incremental Zobrist validated over 7,037,881 nodes with 0
inconsistencies.

Phase 1 results: at fixed depth 4 over the 13-position benchmark set, 802,700 nodes / 69.27s
became 111,401 / 4.76s, with the horizon effect fixed on top. Under a clock, +2.08 plies of
mean depth at a 1-second budget and +2.93 at 3 seconds.

`engine.searcher.CONFIGS` holds every intermediate configuration (`phase0`, `ordering`,
`tt-move-only`, `tt`, `tt-id`, `id-only`, `quiescence`, `quiescence-see`, `pvs-pure`, `pvs`,
`pvs-asp`, and `phase1` as an alias for the recommended one), which is what makes per-feature
attribution possible. Aspiration windows are implemented, tested, and **off**, because they
measured at -0.0% median here.

Phase 2 is next — see the "Immediate next steps" section of `PLAN.md`, which records the
three findings from Phase 1 that change how it has to be built. Per-step reasoning and every
number is in `docs/DEVLOG.md`.

## About the user

Fourth-year student, targeting SDE/SWE roles. They will be studying this code to explain it
in interviews, so **prefer clear, conventional implementations over clever ones**, and comment
the *why* — particularly for anything non-obvious like score conventions, table mirroring
(`square ^ 56`) or transposition-table flag semantics. The study list is in `PLAN.md`.

They asked for blunt assessments over encouragement. If something will not hold up in an
interview, say so.
