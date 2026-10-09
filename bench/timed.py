"""Depth reached within a fixed time budget, per configuration.

Why this benchmark exists alongside `bench/nodes.py`
---------------------------------------------------
`bench/nodes.py` fixes the depth and measures the cost. That is the right way to verify that
a score-preserving optimisation is actually cheaper, and the wrong way to describe what the
engine is *for*. A real engine is handed a clock, not a depth, and the question it has to
answer is "how deep can you see in the time available".

It is also the measurement that makes the Phase 1 features mean anything. At a fixed depth, a
score-preserving optimisation plays **identical moves** to the baseline -- that is precisely
what the differential tests enforce -- so it is worth exactly zero at fixed depth. The gain
only exists because the time saved buys extra depth, and only a timed benchmark can show it.

Every configuration is driven by the same iterative-deepening loop in
`Searcher.search_timed`, including `phase0`, whose own config has iterative deepening off.
That is deliberate: it isolates the search features from the way the clock is handled, so the
comparison is between searches rather than between time managers.

Usage
-----
    python -m bench.timed --ms 1000
    python -m bench.timed --ms 1000 --configs phase0 ordering phase1
    python -m bench.timed --ms 1000 --save bench/baselines/timed-1000ms.json

Timings are machine-dependent, so unlike node counts these figures will not reproduce
exactly. The *ordering* of the configurations will.
"""

import argparse
import json
import platform
import statistics
import time
from pathlib import Path

import chess

from bench.positions import BENCH_POSITIONS
from engine.searcher import CONFIGS, Searcher

DEFAULT_CONFIGS = ["phase0", "ordering", "tt-id", "quiescence-see", "phase1"]


def run_position(fen: str, config_name: str, soft_ms: int, hard_ms: int) -> dict:
    """One position, one configuration, one budget. A fresh Searcher, as always."""
    searcher = Searcher(CONFIGS[config_name])
    board = chess.Board(fen)
    result = searcher.search_timed(board, hard_ms=hard_ms, soft_ms=soft_ms)
    if board.fen() != fen:
        raise AssertionError(f"{config_name} did not restore the board for {fen}")
    return {
        "depth": result.completed_depth,
        "reported_depth": result.depth,
        "score": result.score,
        "move": result.move.uci() if result.move else None,
        "elapsed_ms": result.elapsed_ms,
        "nodes": result.nodes,
        "aborted": result.aborted,
        "overshoot_ms": result.elapsed_ms - hard_ms,
    }


def run(
    soft_ms: int,
    config_names: list[str],
    hard_factor: float = 1.5,
    verbose: bool = True,
) -> dict:
    hard_ms = int(soft_ms * hard_factor)
    results: dict[str, dict] = {}

    for config_name in config_names:
        positions = {}
        if verbose:
            print(f"\nconfig={config_name}  soft={soft_ms}ms  hard={hard_ms}ms")
            print(f"{'position':18s} {'depth':>6s} {'nodes':>10s} {'ms':>8s} "
                  f"{'score':>8s}  move")
        for name, fen, _category in BENCH_POSITIONS:
            record = run_position(fen, config_name, soft_ms, hard_ms)
            positions[name] = record
            if verbose:
                flag = "+1*" if record["reported_depth"] > record["depth"] else "   "
                print(f"{name:18s} {record['depth']:>4d}{flag} {record['nodes']:>10,} "
                      f"{record['elapsed_ms']:>8.1f} {record['score']:>8d}  "
                      f"{record['move']}")

        depths = [p["depth"] for p in positions.values()]
        results[config_name] = {
            "positions": positions,
            "totals": {
                "mean_depth": round(statistics.mean(depths), 2),
                "median_depth": statistics.median(depths),
                "min_depth": min(depths),
                "max_depth": max(depths),
                "nodes": sum(p["nodes"] for p in positions.values()),
                "worst_overshoot_ms": round(
                    max(p["overshoot_ms"] for p in positions.values()), 1
                ),
                "aborted_count": sum(1 for p in positions.values() if p["aborted"]),
            },
        }
        if verbose:
            t = results[config_name]["totals"]
            print(f"{'MEAN DEPTH':18s} {t['mean_depth']:>6.2f}   "
                  f"median {t['median_depth']}, range {t['min_depth']}-{t['max_depth']}   "
                  f"worst overshoot {t['worst_overshoot_ms']:+.1f}ms   "
                  f"{t['aborted_count']}/{len(positions)} aborted")

    return {
        "soft_ms": soft_ms,
        "hard_ms": hard_ms,
        "configs": results,
        "environment": {
            "python": platform.python_version(),
            "python_chess": chess.__version__,
            "platform": platform.platform(),
        },
    }


def summarise(result: dict) -> None:
    print(f"\n{'=' * 72}")
    print(f"Depth reached in {result['soft_ms']}ms (hard limit {result['hard_ms']}ms), "
          f"{len(BENCH_POSITIONS)} positions")
    print(f"{'config':18s} {'mean':>6s} {'median':>7s} {'min':>5s} {'max':>5s} "
          f"{'nodes':>11s} {'overshoot':>10s}")
    print("-" * 72)
    for name, data in result["configs"].items():
        t = data["totals"]
        print(f"{name:18s} {t['mean_depth']:>6.2f} {t['median_depth']:>7} "
              f"{t['min_depth']:>5} {t['max_depth']:>5} {t['nodes']:>11,} "
              f"{t['worst_overshoot_ms']:>+9.1f}ms")

    names = list(result["configs"])
    if len(names) >= 2:
        first = result["configs"][names[0]]["totals"]["mean_depth"]
        last = result["configs"][names[-1]]["totals"]["mean_depth"]
        print("-" * 72)
        print(f"{names[-1]} sees {last - first:+.2f} plies deeper than {names[0]} "
              f"in the same time budget")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ms", type=int, default=1000, help="soft budget per move")
    parser.add_argument("--hard-factor", type=float, default=1.5,
                        help="hard limit as a multiple of the soft budget")
    parser.add_argument("--configs", nargs="+", default=DEFAULT_CONFIGS,
                        choices=sorted(CONFIGS), metavar="CONFIG")
    parser.add_argument("--save", type=Path, help="write results as JSON to this path")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    started = time.perf_counter()
    result = run(args.ms, args.configs, args.hard_factor, verbose=not args.quiet)
    summarise(result)
    print(f"\ntotal wall clock for this benchmark: "
          f"{time.perf_counter() - started:.1f}s")

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"saved to {args.save}")

    worst = max(
        data["totals"]["worst_overshoot_ms"] for data in result["configs"].values()
    )
    if worst > 100:
        print(f"\nWARNING: a hard limit was overshot by {worst:.1f}ms, which is more than "
              f"the clock-check granularity should allow")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
