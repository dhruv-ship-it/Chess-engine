"""The benchmark position set.

Every number this project reports comes from running over *this list*, never a single FEN.
That rule exists because the predecessor engine quoted a 69% node reduction measured on one
hardcoded position, and a single position cannot distinguish "this optimisation works" from
"this optimisation happens to suit this position".

The set is deliberately mixed, because search optimisations have very different payoffs by
position type:

* opening and early middlegame -- high branching factor, few captures, so move ordering is
  carried mostly by killers and history rather than MVV-LVA;
* tactical middlegame -- capture-dense, which is where MVV-LVA and quiescence earn their
  keep and where a wrong sign convention shows up as a blunder;
* endgames -- low branching factor and many transpositions, which is where the
  transposition table pays and where null-move pruning becomes dangerous (zugzwang).

Positions marked "perft" are the standard reference positions also used by `bench/perft.py`;
reusing them keeps the throughput and search benchmarks comparable.
"""

import chess

# (name, fen, category)
BENCH_POSITIONS: list[tuple[str, str, str]] = [
    (
        "startpos",
        chess.STARTING_FEN,
        "opening",
    ),
    (
        "open-game",
        "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 4 3",
        "opening",
    ),
    (
        "italian",
        "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 6 5",
        "opening",
    ),
    (
        "sicilian-open",
        "rnbqkb1r/pp2pppp/3p1n2/8/3NP3/8/PPP2PPP/RNBQKB1R w KQkq - 0 5",
        "opening",
    ),
    (
        "kiwipete",
        "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
        "middlegame",
    ),
    (
        "perft-4",
        "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1",
        "middlegame",
    ),
    (
        "perft-5",
        "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8",
        "middlegame",
    ),
    (
        "wac-001",
        "2rr3k/pp3pp1/1nnqbN1p/3pN3/2pP4/2P3Q1/PPB4P/R4RK1 w - - 0 1",
        "tactical",
    ),
    (
        "wac-002",
        "8/7p/5k2/5p2/p1p2P2/Pr1pPK2/1P1R3P/8 b - - 0 1",
        "tactical",
    ),
    (
        "hanging-queen",
        "4k3/8/8/8/3q4/8/8/3RK3 w - - 0 1",
        "tactical",
    ),
    (
        "perft-3-endgame",
        "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
        "endgame",
    ),
    (
        "kpk",
        "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1",
        "endgame",
    ),
    (
        "rook-endgame",
        "8/8/8/4k3/8/8/4P3/R3K3 w - - 0 1",
        "endgame",
    ),
]


def validate() -> None:
    """Fail loudly if a FEN is malformed, terminal, or has no legal moves.

    A terminal position would silently contribute zero search nodes and quietly skew every
    aggregate in the benchmark, so it is rejected rather than tolerated.
    """
    seen: set[str] = set()
    for name, fen, category in BENCH_POSITIONS:
        if name in seen:
            raise AssertionError(f"duplicate benchmark position name: {name}")
        seen.add(name)
        board = chess.Board(fen)
        if not board.is_valid():
            raise AssertionError(f"{name}: invalid position {fen}")
        if board.is_game_over():
            raise AssertionError(f"{name}: position is already over, cannot be searched")
        if category not in {"opening", "middlegame", "tactical", "endgame"}:
            raise AssertionError(f"{name}: unknown category {category!r}")


validate()
