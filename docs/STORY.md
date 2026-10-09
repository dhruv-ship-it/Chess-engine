# How this project went wrong, and what I did about it

This is the narrative version of the project: what I set out to do, the mistake I made, how I
found it, and why I rebuilt rather than patched. If you want the algorithms and the
measurements, [ENGINEERING.md](ENGINEERING.md) has those. If you want the chronological
record of each step, [DEVLOG.md](DEVLOG.md) has that.

The short version: I added three standard optimisations to a working chess engine and made it
measurably *weaker*, while a performance benchmark told me I had improved it by 69%. Finding
that out, and understanding why, turned out to be worth more than the engine.

---

## Where it started

I wanted to understand game-tree search properly, so I cloned an open-source Python
alpha-beta engine and read it until I understood it. Minimax, alpha-beta pruning, a
piece-square-table evaluation. That part worked — I could follow why alpha-beta prunes what it
prunes and why it gets the same answer as full minimax.

Then I did what you do next: I added the standard optimisations every real engine has.
Move ordering with MVV-LVA and killer moves. Zobrist hashing with a transposition table.
Quiescence search. These are the right features; they are in every textbook and every serious
engine.

I wrote a benchmark, ran it, and got a good number: **69% fewer nodes searched, 64% faster**,
at depth 4. I wrote that on my resume.

## The number was true. The engine was broken.

It took me longer than it should have to discover that the engine had stopped playing chess
properly. There were no tests, so nothing told me.

When I finally sat down to audit it, I built the two things I should have built first: a perft
harness, which walks the move tree and compares node counts against published references, and
a tactical suite — a set of positions where exactly one move is obviously correct, like a
hanging queen you can simply take, or a mate in one.

The result:

| engine version | tactical suite |
|---|---|
| before my "optimisations" | **12 / 12** |
| after my "optimisations" | **5 / 12** |

And the headline symptom: on a position where the only mating move is `Ra8#`, the engine
played `Kh1`. At depth 2, at depth 3, and at depth 4. **It refused to deliver checkmate as
White.** Playing Black, from the mirror-image position, it found the mate immediately.

That asymmetry is the whole clue. A bug that affects one colour and not the other is a sign
bug.

## The root cause was one conceptual mismatch

The original engine used **minimax with an explicit maximise/minimise flag**, and *absolute*
scores: positive always meant "good for White". In that framework, returning `+1000000`
because White has won is correct and consistent.

I had rewritten the search as **negamax**, which is the standard simplification — instead of
two branches for maximising and minimising, you write one and negate the child's score on the
way back up. It is cleaner and it is what every modern engine does.

But negamax requires the opposite convention. A score has to be **relative to whoever is about
to move**: positive means "good for me, the player to move". I converted the search and left
the evaluation and the mate constants absolute.

So the engine's evaluation returned `+890` for a position with White to move *and* `+890` for
the same position with Black to move — meaning Black, a queen down, scored it as winning. And
at a node where Black had just been checkmated, the search returned `+1000000` instead of a
large negative number, so the engine saw delivering mate as a catastrophe and avoided it.

One mismatch, two symptoms, and the engine still ran perfectly happily the whole time.

Along the way the audit found more:

- **Five of the six piece-square tables were no longer 64 entries.** They had drifted to 62,
  63, 80, 82 and 104 through duplicated and dropped rows, so every positional lookup read the
  wrong square. Pure data corruption, invisible unless you check.
- **The Zobrist hashing did not do the thing Zobrist hashing exists for.** The entire point is
  that you update the hash *incrementally* — a couple of XOR operations when a piece moves —
  instead of recomputing it. Mine walked all 64 squares and did so *fresh at every node*. It
  was a hash function, but it cost more than the lookup it enabled saved. "I implemented
  Zobrist hashing" and "I recompute the hash from scratch at every node" cannot both be true,
  and anyone who knows this domain would spot that in under a minute.
- **The transposition table did not store a best move**, which is the single most valuable
  thing a transposition table gives you.
- **The same measurement appeared as "~50%", "~58%" and "69.33%"** in three different places,
  because no script owned the number.

## Why I rebuilt instead of fixing

My first instinct was to fix the four bugs. I decided against it, for a reason that took me a
while to articulate:

**Every bug I had just found was the kind a performance benchmark reports as an improvement.**

My 69% node reduction was real. The engine genuinely searched 69% fewer nodes. It searched
fewer nodes *because it was pruning based on wrong scores* — and arriving at wrong answers
faster is exactly what that looks like from the outside. A faster-and-wrong engine and a
faster-and-right engine produce the same shape of benchmark output.

So the problem was not four bugs. The problem was that I had no way to tell the difference
between an optimisation and a regression. Patching the bugs would have left that intact.

What I needed was an order of operations:

1. Build something deliberately plain and prove it correct.
2. Freeze it as a control.
3. Add one feature at a time, and require each one to prove it did not change the answer.

That is the project now.

## How it is built

**Phase 0 — a correct baseline.** Plain negamax with alpha-beta and nothing else. No ordering,
no transposition table, no quiescence. Its only job is to be right. The tests are built around
*invariants* rather than expected outputs, because invariants are what catch a silently wrong
search: evaluation has to be symmetric under mirroring the board (which directly catches the
sign bug that started all this); every piece-square table is asserted to be exactly 64 entries
at import, which closes the data-corruption class permanently; a mated node must score
negative; a narrowed search window must return the same score as a full one.

That suite immediately earned its keep by finding a bug I had not predicted: terminal detection
ran *after* the depth cutoff, so a depth-1 search returned a static evaluation at a checkmate
instead of a mate score, and therefore could not see mate in one.

**Phase 1 — one feature at a time, measured.** Each optimisation goes in behind a flag, so the
baseline stays reachable and each feature can be switched off to measure what it is worth. The
benchmark runs over **13 positions** spanning openings, middlegames, tactical positions and
endgames — never one position, which was the other half of my original mistake.

**Phase 2 — strength, measured properly.** Self-play between versions, with confidence
intervals, because an Elo difference from a hundred games is noise.

## The thing I got wrong twice

When I set up Phase 1 I wrote down a rule for myself: *these are speed optimisations, so if a
score changes at a fixed depth, that is a bug.*

That rule is right for some of the features and wrong for others, and I only noticed when I
thought carefully about what each one actually does:

- **Move ordering** cannot change the score. Trying the same moves in a different order cannot
  change what the best move is worth. So for move ordering, a changed score really is a bug.
- **Quiescence** changes scores *on purpose*. It exists because a fixed-depth search stops at
  an arbitrary moment — if your last move was "queen takes pawn", you evaluate a position
  where the opponent is about to recapture your queen and record it as "up a pawn". Quiescence
  keeps searching captures until the position settles. It changes fixed-depth scores because
  the old scores were wrong, and it *increases* node counts at the same nominal depth.
- **Null-move pruning and late move reductions** are outright gambles: they skip work that
  might have mattered, in exchange for depth.

Applying my own rule uniformly would have made me reject a correct quiescence implementation
as a bug. Knowing which of your optimisations are provably safe and which are heuristic bets
turns out to be the distinction that matters, and it is not the distinction I started with.

## The second correction: my planned measurement would have reported zero

The related mistake was in the plan for Phase 2. I was going to measure the Elo gain from each
feature by playing version against version at a fixed search depth.

That would have returned zero, by construction, and I would have spent a day debugging a
harness that was working correctly.

Move ordering and a transposition table do not make the engine choose *better* moves at a
given depth. They make that depth *cheaper*. At a fixed depth, the optimised engine plays
**identical moves** to the baseline — that is precisely what the tests enforce — so it cannot
win more games. The strength only appears when the time saved is spent going deeper.

So strength has to be measured under a **time budget**, not a depth limit. That single sentence
is the most useful thing I learned on this project, and I would not have learned it by reading
about alpha-beta.

## Where it stands

Move ordering is in and measured: **82% fewer nodes on a median position** at depth 4, with the
score and best move unchanged on all 13 benchmark positions. More usefully, it now reaches a
full extra ply of depth for **21% less wall-clock time** than the unordered version needed for
the shallower search.

The transposition table, quiescence, PVS and time management come next, each measured the same
way. Then Phase 2, where the question stops being "how many nodes" and becomes "how much
stronger, and how confident am I".

## What I would say about this in an interview

I used to think the interesting answer here was "I wrote a chess engine." It isn't — plenty of
people have. The interesting answer is this:

> I added three standard search optimisations and the engine got weaker, while my benchmark
> reported a 69% improvement. I built a tactical regression suite to find out why, and traced
> it to a minimax-to-negamax conversion that kept the old absolute score convention where
> negamax requires side-relative scores — so the engine would not play checkmate as White.
> Then I rebuilt it with the tests in place first, and measured each feature's contribution
> separately.

Shipping an optimisation that silently loses quality, and having no way to detect it, is one
of the most common failures in real engineering. I would rather have a story about catching
one than a story about not having made the mistake.
