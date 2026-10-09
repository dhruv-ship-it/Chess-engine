"""Perft: count leaf nodes of the move tree to a fixed depth.

**What this does and does not prove.** Perft is the standard correctness harness for a chess
engine's *move generator*. This engine does not have its own move generator -- legality and
move generation come from `python-chess` -- so matching the published node counts validates
`python-chess`, not code written here. It is kept for two honest reasons:

1. It is a clean, reproducible **throughput benchmark** (nodes/sec) for the make/unmake path
   that the search spends most of its time in.
2. It is the natural tree-walking harness for validating the **incremental Zobrist hashing**
   arriving in Phase 1, where every node asserts that the incrementally updated key equals a
   from-scratch recomputation. That assertion does test code written here.

Do not describe this as validating move generation. See ENGINEERING_NOTES.md.

Reference node counts are the standard ones from
https://www.chessprogramming.org/Perft_Results
"""

import argparse
import time

import chess

# (name, fen, [perft(1), perft(2), ...]) using published reference counts.
REFERENCE_POSITIONS = [
    (
        "startpos",
        chess.STARTING_FEN,
        [20, 400, 8902, 197281, 4865609],
    ),
    (
        "kiwipete",
        "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
        [48, 2039, 97862, 4085603],
    ),
    (
        "position-3",
        "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
        [14, 191, 2812, 43238, 674624],
    ),
    (
        "position-4",
        "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1",
        [6, 264, 9467, 422333],
    ),
    (
        "position-5",
        "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8",
        [44, 1486, 62379, 2103487],
    ),
]


def perft(board: chess.Board, depth: int) -> int:
    """Count leaf nodes at `depth`."""
    if depth == 0:
        return 1
    if depth == 1:
        return board.legal_moves.count()

    nodes = 0
    for move in board.legal_moves:
        board.push(move)
        nodes += perft(board, depth - 1)
        board.pop()
    return nodes


def perft_divide(board: chess.Board, depth: int) -> dict[str, int]:
    """Per-move node counts, for localising a mismatch."""
    result = {}
    for move in board.legal_moves:
        board.push(move)
        result[move.uci()] = perft(board, depth - 1)
        board.pop()
    return result


def run(max_depth: int = 3, verbose: bool = True) -> list[dict]:
    """Run every reference position up to `max_depth`. Returns per-run records."""
    records = []
    for name, fen, expected_counts in REFERENCE_POSITIONS:
        for depth in range(1, min(max_depth, len(expected_counts)) + 1):
            expected = expected_counts[depth - 1]
            board = chess.Board(fen)
            start = time.perf_counter()
            actual = perft(board, depth)
            elapsed = time.perf_counter() - start
            ok = actual == expected
            records.append(
                {
                    "position": name,
                    "depth": depth,
                    "expected": expected,
                    "actual": actual,
                    "ok": ok,
                    "seconds": elapsed,
                    "nodes_per_sec": actual / elapsed if elapsed > 0 else 0.0,
                }
            )
            if verbose:
                status = "ok " if ok else "FAIL"
                print(
                    f"{status} {name:12s} depth {depth}  "
                    f"{actual:>10,} nodes  {elapsed:7.3f}s  "
                    f"{records[-1]['nodes_per_sec']:>12,.0f} nps"
                )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--depth", type=int, default=3, help="maximum depth (default 3)")
    parser.add_argument("--fen", type=str, help="run a single FEN instead of the suite")
    parser.add_argument("--divide", action="store_true", help="print per-move counts")
    args = parser.parse_args()

    if args.fen:
        board = chess.Board(args.fen)
        if args.divide:
            for move, count in sorted(perft_divide(board, args.depth).items()):
                print(f"{move}: {count:,}")
        start = time.perf_counter()
        total = perft(board, args.depth)
        elapsed = time.perf_counter() - start
        print(f"perft({args.depth}) = {total:,} in {elapsed:.3f}s "
              f"({total / elapsed:,.0f} nps)")
        return 0

    records = run(args.depth)
    failures = [r for r in records if not r["ok"]]
    total_nodes = sum(r["actual"] for r in records)
    total_time = sum(r["seconds"] for r in records)
    print(
        f"\n{len(records) - len(failures)}/{len(records)} positions correct; "
        f"{total_nodes:,} nodes in {total_time:.2f}s "
        f"({total_nodes / total_time:,.0f} nps aggregate)"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
