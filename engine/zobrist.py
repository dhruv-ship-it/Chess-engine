"""Incremental Zobrist hashing.

What Zobrist hashing is for
---------------------------
The same position arrives by different move orders -- a *transposition*. To reuse work
already done on it you must first recognise it, which means hashing a position to a key that
is cheap to compute and collides only negligibly often.

Zobrist hashing assigns one random 64-bit number to every (colour, piece type, square)
combination, plus one for the side to move, one per castling right, and one per en-passant
file. A position's key is the XOR of the numbers for everything true about it.

Why XOR, and why that *is* the technique
----------------------------------------
XOR is its own inverse: ``x ^ r ^ r == x``. So making a move does not recompute anything. You
XOR the moving piece out of its origin square and back in on its destination, XOR out any
captured piece, flip the side-to-move key, and fix up castling and en passant. A handful of
XORs -- **O(1) per move**, independent of how many pieces are on the board -- against O(64)
to rebuild the key from scratch.

This is the distinction the predecessor engine missed. It had a ``compute_hash()`` that walked
all 64 squares and called it *fresh at every node*. That is a hash function, but it is not
Zobrist hashing in the sense that matters: it cost more than the table lookup it enabled
saved. "I implemented Zobrist hashing" and "I recompute the hash from scratch at every node"
cannot both be true of a correct implementation.

Why this class owns push and pop
--------------------------------
A key that can silently drift out of sync with the board is *worse than no key at all*: every
transposition-table probe afterwards reads some other position's score, and the search is
then wrong in a way that still runs and still returns legal moves. That is exactly the
failure mode this whole project exists to avoid.

So the only way to move a piece is also the only way to update the key. `ZobristTracker.push`
wraps `board.push`, and the pairing cannot be forgotten.

Why ``pop`` restores a saved key rather than un-XORing
------------------------------------------------------
Both are O(1) and either is defensible. Un-XORing would repeat the same handful of
operations, since XOR is symmetric. Saving the key on a stack is one list append and one list
pop, it is simpler to read, and it is what real engines effectively do -- they already keep
per-ply undo state for castling rights, the en-passant square and the halfmove clock, and the
key rides along in it. The claim being made here is O(1) per move, not any particular spelling
of the inverse.

The three fiddly cases
----------------------
Each of these is a separate branch below, and each is covered by a test, because they are
where a Zobrist implementation actually breaks:

1. **Castling** moves two pieces, so the rook has to be XORed as well as the king.
2. **En passant** captures a pawn that is *not* standing on the destination square, so
   looking up the victim by destination square finds nothing.
3. **Promotion** removes a pawn and adds a different piece type, so the piece XORed in on the
   destination is not the piece XORed out of the origin.
"""

from __future__ import annotations

import random

import chess

# Fixed seed, deliberately. The key tables must be identical on every run, or two benchmark
# runs would see different collision patterns and node counts would stop being reproducible.
# The predecessor engine re-randomised its key table on every HTTP request, which made its
# transposition table useless and its measurements meaningless.
DEFAULT_SEED = 0x5A17_C0DE

_PIECE_TYPES = 6  # pawn..king, python-chess numbers them 1..6
_SQUARES = 64


class ZobristKeys:
    """The random key tables, plus a from-scratch key computation."""

    __slots__ = ("_pieces", "_side", "_castling", "_ep_file")

    def __init__(self, seed: int = DEFAULT_SEED) -> None:
        rng = random.Random(seed)

        def key() -> int:
            return rng.getrandbits(64)

        # Flat list indexed by _piece_index(): colour * 384 + (piece_type - 1) * 64 + square.
        # Flat rather than nested because this is the single hottest lookup in the hashing
        # path, and one integer index into one list is the cheapest access CPython offers.
        self._pieces = [key() for _ in range(2 * _PIECE_TYPES * _SQUARES)]

        # XORed in when it is Black's turn. Incrementally this is XORed on every move, since
        # every move flips the side to move.
        self._side = key()

        # One key per castling right, in the order (White kingside, White queenside,
        # Black kingside, Black queenside). Four keys rather than one per bitmask value,
        # which is what makes a single revoked right a one-XOR change.
        self._castling = [key() for _ in range(4)]

        # One key per *file*, not per square. The file is all that distinguishes one
        # en-passant possibility from another, because the rank is implied by the side to
        # move.
        self._ep_file = [key() for _ in range(8)]

    @staticmethod
    def _piece_index(color: bool, piece_type: int, square: int) -> int:
        # `color` is a bool in python-chess, True for White.
        return (384 if color else 0) + (piece_type - 1) * 64 + square

    def piece(self, color: bool, piece_type: int, square: int) -> int:
        return self._pieces[self._piece_index(color, piece_type, square)]

    @property
    def side(self) -> int:
        return self._side

    def castling(self, board: chess.Board) -> int:
        """XOR of the keys for the castling rights currently available."""
        rights = board.castling_rights
        key = 0
        if rights & chess.BB_H1:
            key ^= self._castling[0]
        if rights & chess.BB_A1:
            key ^= self._castling[1]
        if rights & chess.BB_H8:
            key ^= self._castling[2]
        if rights & chess.BB_A8:
            key ^= self._castling[3]
        return key

    def en_passant(self, board: chess.Board) -> int:
        """Key for the en-passant file, or 0 if no en-passant capture is actually available.

        The condition is `has_legal_en_passant()`, not merely "a double pawn push just
        happened". That matters for a transposition table: if the key changed every time a
        pawn moved two squares, two positions that are identical in every way that affects
        play -- same pieces, same rights, same legal moves -- would hash differently and the
        transposition between them would be missed. Gating on an actually available capture
        is the standard convention (Polyglot does the same) and costs almost nothing, because
        `has_legal_en_passant` short-circuits on the common case of no en-passant square at
        all.
        """
        if board.has_legal_en_passant():
            return self._ep_file[chess.square_file(board.ep_square)]
        return 0

    def compute(self, board: chess.Board) -> int:
        """Build the key from scratch, O(pieces).

        Used to seed a tracker at the root, and -- more importantly -- as the reference that
        the incremental key is validated against at every node of a perft walk. If a single
        XOR for castling, en passant or promotion is wrong, that comparison is what catches
        it.
        """
        key = 0
        for square, piece in board.piece_map().items():
            key ^= self.piece(piece.color, piece.piece_type, square)
        if board.turn == chess.BLACK:
            key ^= self._side
        key ^= self.castling(board)
        key ^= self.en_passant(board)
        return key


# One shared, lazily built table set. Building it is cheap but not free, and every Searcher
# must use the *same* keys or two searchers could not share a transposition table.
_DEFAULT_KEYS: ZobristKeys | None = None


def default_keys() -> ZobristKeys:
    global _DEFAULT_KEYS
    if _DEFAULT_KEYS is None:
        _DEFAULT_KEYS = ZobristKeys()
    return _DEFAULT_KEYS


class ZobristTracker:
    """Maintains a Zobrist key in lockstep with a board, O(1) per move."""

    __slots__ = ("keys", "key", "_stack")

    def __init__(self, board: chess.Board, keys: ZobristKeys | None = None) -> None:
        self.keys = keys if keys is not None else default_keys()
        self.key = self.keys.compute(board)
        self._stack: list[int] = []

    def reset(self, board: chess.Board) -> None:
        """Re-seed from a board. Called when a search starts from a new root position."""
        self.key = self.keys.compute(board)
        self._stack.clear()

    def push(self, board: chess.Board, move: chess.Move) -> None:
        """Apply `move` to `board` and update the key. `board` must be in the pre-move state."""
        self._stack.append(self.key)
        keys = self.keys
        key = self.key

        color = board.turn
        moved = board.piece_type_at(move.from_square)

        # Castling rights and the en-passant file are XORed out now and the new values XORed
        # back in after the push. Reading them from the board after `python-chess` has applied
        # the move is deliberate: working out which rights a move revokes by hand means
        # reimplementing rules that `python-chess` already gets right (a rook captured on its
        # home square revokes a right, and so does any king move), and that is a bug waiting
        # to happen in return for nothing.
        key ^= keys.castling(board)
        key ^= keys.en_passant(board)

        # Fiddly case 2: en passant. The captured pawn is beside the destination square, not
        # on it, so the victim lookup by destination finds nothing. Checked before the piece
        # is moved, while the board still shows the original occupancy.
        victim = board.piece_type_at(move.to_square)
        if victim is not None:
            key ^= keys.piece(not color, victim, move.to_square)
        elif board.is_en_passant(move):
            captured_square = move.to_square + (-8 if color == chess.WHITE else 8)
            key ^= keys.piece(not color, chess.PAWN, captured_square)

        # Fiddly case 1: castling moves the rook as well as the king. The rook's squares are
        # derived from the king's rank and direction rather than hardcoded, so the same code
        # covers both colours.
        if board.is_castling(move):
            rank = chess.square_rank(move.from_square)
            kingside = chess.square_file(move.to_square) > chess.square_file(move.from_square)
            rook_from = chess.square(7 if kingside else 0, rank)
            rook_to = chess.square(5 if kingside else 3, rank)
            key ^= keys.piece(color, chess.ROOK, rook_from)
            key ^= keys.piece(color, chess.ROOK, rook_to)

        # The mover leaves its origin square.
        key ^= keys.piece(color, moved, move.from_square)

        # Fiddly case 3: promotion. A pawn is removed and a different piece type appears, so
        # what lands on the destination is not what left the origin.
        key ^= keys.piece(color, move.promotion or moved, move.to_square)

        # Every move flips the side to move.
        key ^= keys.side

        board.push(move)

        key ^= keys.castling(board)
        key ^= keys.en_passant(board)
        self.key = key

    def pop(self, board: chess.Board) -> None:
        """Undo the last `push`, on both the board and the key."""
        board.pop()
        self.key = self._stack.pop()

    def is_consistent(self, board: chess.Board) -> bool:
        """True if the incremental key matches a from-scratch recomputation.

        This is the assertion that makes the incremental implementation trustworthy, and the
        only part of the perft harness that tests code written in this project rather than
        `python-chess`.
        """
        return self.key == self.keys.compute(board)
