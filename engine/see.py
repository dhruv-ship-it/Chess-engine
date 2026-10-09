"""Static Exchange Evaluation.

The problem it solves
---------------------
Quiescence search looks at every capture. Most captures are bad, and searching a bad one
costs a whole subtree to learn what a few arithmetic operations could have told you: if a
queen takes a pawn defended by a pawn, the exchange loses about 800 centipawns and there is
no point searching it. Without some filter, quiescence explodes in exactly the dense
middlegames where it matters most.

SEE answers "what is the net material outcome if both sides keep recapturing on this square
with their cheapest available piece?" statically -- no search, no make/unmake, just
arithmetic over the attacker sets.

The swap algorithm
------------------
Build the list of gains as the exchange proceeds, each side always recapturing with its
**least valuable attacker** (recapturing with a cheaper piece is never worse, because the
more valuable piece stays available for later in the sequence):

    gains[0] = value of the original victim
    gains[d] = value of the piece now standing on the square - gains[d-1]

Then the crucial second pass. The list above assumes the exchange runs to completion, but
either side may **stop** at any point -- capturing is a choice, not an obligation. So the
list is folded back from the end with

    gains[d-1] = -max(-gains[d-1], gains[d])

which is exactly a negamax over the two options "stand here" and "capture again", and is why
SEE returns the value of *optimal* play in the exchange rather than of exhausting it.

X-rays, and why `occupied` is threaded through
----------------------------------------------
When a piece captures, it vacates its square, which can expose a rook or bishop behind it on
the same line -- a battery. The attacker set has to be recomputed against a *modified*
occupancy, not the real board, or every discovered attacker is missed and SEE under-counts
the defenders. `python-chess` exposes `attackers_mask(color, square, occupied)`, which takes
exactly that mask, so no board mutation is needed and SEE never has to make or unmake a move.

Known simplifications, stated rather than hidden
------------------------------------------------
* **Promotions during the exchange are ignored.** A pawn recapturing onto the back rank is
  scored as a pawn. Standard in simple SEE implementations; it under-values a handful of
  exchanges.
* **Pins are ignored.** The attacker set is geometric, so a defender that is pinned and
  cannot legally recapture is still counted. This makes SEE *conservative* -- it over-counts
  defenders and so under-estimates some winning captures -- which is the safe direction for
  a pruning filter: it loses a little search quality rather than pruning a good move.
* **The king may not capture into an attacked square.** That rule is enforced here, because
  ignoring it is not conservative: it would let the king "recapture" illegally and report an
  exchange as safe when it is not.
"""

from __future__ import annotations

import chess

from engine.evaluate import PAWN, PIECE_VALUES


def _least_valuable_attacker(
    board: chess.Board,
    attackers: int,
    occupied: int,
) -> tuple[int, int] | None:
    """Return (square, piece type) of the cheapest attacker in the mask, or None."""
    best_square = -1
    best_type = 0
    best_value = 0
    for square in chess.scan_forward(attackers & occupied):
        piece_type = board.piece_type_at(square)
        if piece_type is None:
            continue
        value = PIECE_VALUES[piece_type]
        if best_square < 0 or value < best_value:
            best_square, best_type, best_value = square, piece_type, value
    if best_square < 0:
        return None
    return best_square, best_type


def see(board: chess.Board, move: chess.Move) -> int:
    """Net material, in centipawns, from the exchange sequence `move` starts.

    Positive means the side making `move` comes out ahead. Returns 0 for a quiet move, since
    there is no exchange to evaluate.
    """
    to_square = move.to_square
    mover = board.turn

    if board.is_en_passant(move):
        captured_value = PIECE_VALUES[PAWN]
        captured_square = to_square + (-8 if mover == chess.WHITE else 8)
    else:
        victim = board.piece_type_at(to_square)
        if victim is None:
            return 0
        captured_value = PIECE_VALUES[victim]
        captured_square = to_square

    attacker_type = board.piece_type_at(move.from_square)
    if attacker_type is None:
        return 0

    # Occupancy as it stands *after* the first capture: the mover has left its origin, the
    # victim is gone, and the mover now stands on the destination. Everything after this is
    # computed against this mask rather than the real board, which is what makes discovered
    # attackers behind the vacated square visible.
    occupied = board.occupied
    occupied &= ~chess.BB_SQUARES[move.from_square]
    occupied &= ~chess.BB_SQUARES[captured_square]
    occupied |= chess.BB_SQUARES[to_square]

    gains = [captured_value]
    # Value of whatever is standing on the contested square right now, i.e. what the next
    # capture would win.
    on_square = PIECE_VALUES[attacker_type]
    side = not mover

    while True:
        attackers = board.attackers_mask(side, to_square, occupied) & occupied
        chosen = _least_valuable_attacker(board, attackers, occupied)
        if chosen is None:
            break
        square, piece_type = chosen

        if piece_type == chess.KING:
            # The king cannot capture into check. If the other side still attacks the square
            # once the king has committed to it, this recapture is illegal and the exchange
            # simply stops here.
            remaining = occupied & ~chess.BB_SQUARES[square]
            if board.attackers_mask(not side, to_square, remaining) & remaining:
                break

        gains.append(on_square - gains[-1])
        on_square = PIECE_VALUES[piece_type]
        occupied &= ~chess.BB_SQUARES[square]
        side = not side

    # Fold back: at every step the side to move may decline to recapture, so each entry is
    # the better of standing pat and continuing. This is a negamax over a one-dimensional
    # game, which is why the sign flips at every level.
    for index in range(len(gains) - 1, 0, -1):
        gains[index - 1] = -max(-gains[index - 1], gains[index])

    return gains[0]


def is_losing_capture(board: chess.Board, move: chess.Move) -> bool:
    """True if the exchange loses material outright.

    The filter quiescence uses. An equal exchange (SEE == 0) is kept, because trading evenly
    can still be the right move and the evaluation after it may differ.
    """
    return see(board, move) < 0
