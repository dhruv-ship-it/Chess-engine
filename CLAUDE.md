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
4. **Phase 1 features must not change the score at fixed depth.** Move ordering, the
   transposition table and quiescence are speed optimisations. If a score changes at a given
   depth, that is a bug, and `test_alpha_beta_agrees_with_full_window_at_same_depth` plus the
   tactical suite are the gates.
5. Keep the Phase 0 plain search reachable, because Phase 2 measures Phase 1 against it.

## Current state

Phase 0 is complete: 73 tests passing, tactical suite 12/12, perft 15/15 at ~660k nps.
Phase 1 is next — see `PLAN.md`.

## About the user

Fourth-year student, targeting SDE/SWE roles. They will be studying this code to explain it
in interviews, so **prefer clear, conventional implementations over clever ones**, and comment
the *why* — particularly for anything non-obvious like score conventions, table mirroring
(`square ^ 56`) or transposition-table flag semantics. The study list is in `PLAN.md`.

They asked for blunt assessments over encouragement. If something will not hold up in an
interview, say so.
