"""Fixed-depth node-count and timing benchmark over the whole position set.

This is the script behind every node-count figure the project reports. Run it, diff it
against a committed baseline, and the number is reproducible by anyone who clones the repo.
That is a hard rule here: the predecessor project described one measurement as "~50%",
"~58%" and "69.33%" in three different artifacts, because no committed script owned the
number.

Usage
-----
    python -m bench.nodes --config phase0 --depth 4
    python -m bench.nodes --config phase0 --depth 4 --save bench/baselines/phase0-d4.json
    python -m bench.nodes --config ordering --depth 4 --baseline bench/baselines/phase0-d4.json

What is measured, per position:

* `nodes`  -- every node the search visited. This is the headline number, because it is
              what move ordering and the transposition table are meant to reduce.
* `leaves` -- nodes where the static evaluation was called. Reported separately because a
              feature can trade one for the other: quiescence *increases* leaves at a fixed
              nominal depth while making the scores it returns correct.
* `seconds` and `nodes_per_sec` -- wall clock. Noisy on a laptop, so node counts are the
              primary comparison and time is secondary.
* `score` and `move` -- so a comparison run can prove the search still returns the same
              answer. For a score-preserving feature a mismatch here is a bug; see the
              classification in `engine/searcher.py`.

Timings are machine-dependent and will not reproduce exactly. Node counts are
deterministic and will.
"""

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

import chess

from bench.positions import BENCH_POSITIONS
from engine.searcher import CONFIGS, Searcher


def run_position(fen: str, depth: int, config_name: str) -> dict:
    """Search one position at a fixed depth with a fresh searcher.

    A fresh `Searcher` per position is deliberate. Sharing one would let killer moves, the
    history table and the transposition table carry over, so an earlier position would warm
    up a later one and the per-position numbers would stop being independent. The
    predecessor engine's benchmark made exactly that mistake and its headline figure was
    inflated by it.
    """
    searcher = Searcher(CONFIGS[config_name])
    board = chess.Board(fen)

    start = time.perf_counter()
    result = searcher.search(board, depth)
    elapsed = time.perf_counter() - start

    record = {
        "nodes": searcher.stats.nodes,
        "leaves": searcher.stats.leaves,
        "seconds": elapsed,
        "nodes_per_sec": searcher.stats.nodes / elapsed if elapsed > 0 else 0.0,
        "score": result.score,
        "move": result.move.uci() if result.move else None,
    }
    if searcher.tt is not None:
        # Hit rate and cutoff count are how you tell a table that is wired up from one that
        # is actually doing work. A table with near-zero hits is a bug, not a null result.
        record["tt"] = searcher.tt.stats()
    return record


def run(depth: int, config_name: str, verbose: bool = True) -> dict:
    """Run the whole position set. Returns a record suitable for JSON serialisation."""
    positions = {}
    if verbose:
        print(f"config={config_name}  depth={depth}  {len(BENCH_POSITIONS)} positions\n")
        print(f"{'position':18s} {'category':11s} {'nodes':>10s} {'leaves':>10s} "
              f"{'sec':>7s} {'nps':>9s} {'score':>7s}  move")

    for name, fen, category in BENCH_POSITIONS:
        record = run_position(fen, depth, config_name)
        record["category"] = category
        record["fen"] = fen
        positions[name] = record
        if verbose:
            print(f"{name:18s} {category:11s} {record['nodes']:>10,} "
                  f"{record['leaves']:>10,} {record['seconds']:>7.2f} "
                  f"{record['nodes_per_sec']:>9,.0f} {record['score']:>7d}  "
                  f"{record['move']}")

    total_nodes = sum(p["nodes"] for p in positions.values())
    total_leaves = sum(p["leaves"] for p in positions.values())
    total_seconds = sum(p["seconds"] for p in positions.values())
    tt_records = [p["tt"] for p in positions.values() if "tt" in p]

    result = {
        "config": config_name,
        "depth": depth,
        "positions": positions,
        "totals": {
            "nodes": total_nodes,
            "leaves": total_leaves,
            "seconds": total_seconds,
            "nodes_per_sec": total_nodes / total_seconds if total_seconds else 0.0,
        },
        "environment": {
            "python": platform.python_version(),
            "python_chess": chess.__version__,
            "platform": platform.platform(),
        },
    }

    if tt_records:
        probes = sum(r["probes"] for r in tt_records)
        hits = sum(r["hits"] for r in tt_records)
        result["totals"]["tt"] = {
            "probes": probes,
            "hits": hits,
            "hit_rate_pct": round(hits / probes * 100, 1) if probes else 0.0,
            "cutoffs": sum(r["cutoffs"] for r in tt_records),
            "collisions": sum(r["collisions"] for r in tt_records),
            "stores": sum(r["stores"] for r in tt_records),
            "declined": sum(r["declined"] for r in tt_records),
            "max_fill_permille": max(r["fill_permille"] for r in tt_records),
        }

    if verbose:
        print(f"\nTOTAL  {total_nodes:,} nodes  {total_leaves:,} leaves  "
              f"{total_seconds:.2f}s  "
              f"{result['totals']['nodes_per_sec']:,.0f} nps")
        tt_totals = result["totals"].get("tt")
        if tt_totals:
            print(f"TT     {tt_totals['probes']:,} probes  "
                  f"{tt_totals['hits']:,} hits ({tt_totals['hit_rate_pct']}%)  "
                  f"{tt_totals['cutoffs']:,} cutoffs  "
                  f"{tt_totals['collisions']:,} key collisions  "
                  f"max fill {tt_totals['max_fill_permille']}/1000")

    return result


def compare(current: dict, baseline: dict) -> int:
    """Print a per-position comparison against a baseline. Returns an exit code.

    Exit code 1 means a position's score or best move changed. Whether that is a bug
    depends on the feature -- see the score-preservation classification in
    `engine/searcher.py` -- so this reports rather than decides, but it reports loudly.
    """
    if current["depth"] != baseline["depth"]:
        print(f"refusing to compare: depth {current['depth']} vs baseline "
              f"{baseline['depth']}", file=sys.stderr)
        return 2

    print(f"\n{'':18s} {'nodes':>12s} {'baseline':>12s} {'change':>9s}   score")
    print(f"vs baseline config={baseline['config']} depth={baseline['depth']}")
    print("-" * 70)

    changed = []
    changes = []
    for name, base in baseline["positions"].items():
        cur = current["positions"].get(name)
        if cur is None:
            print(f"{name:18s} {'MISSING from current run':>35s}")
            changed.append(name)
            continue

        delta = cur["nodes"] - base["nodes"]
        pct = (delta / base["nodes"] * 100) if base["nodes"] else 0.0
        changes.append(pct)
        score_note = "same"
        if cur["score"] != base["score"]:
            score_note = f"CHANGED {base['score']} -> {cur['score']}"
            changed.append(name)
        elif cur["move"] != base["move"]:
            score_note = f"move {base['move']} -> {cur['move']} (same score)"

        print(f"{name:18s} {cur['nodes']:>12,} {base['nodes']:>12,} "
              f"{pct:>8.1f}%   {score_note}")

    base_total = baseline["totals"]["nodes"]
    cur_total = current["totals"]["nodes"]
    pct = (cur_total - base_total) / base_total * 100 if base_total else 0.0
    print("-" * 70)
    print(f"{'TOTAL':18s} {cur_total:>12,} {base_total:>12,} {pct:>8.1f}%")

    # The median matters more than the aggregate and is printed here rather than worked out
    # by hand, so that the figure quoted in the README and the devlog comes out of a
    # committed script. The aggregate is dominated by whichever position happens to be
    # largest -- `kiwipete` alone is 44% of the Phase 0 baseline's node count -- so an
    # optimisation that helps only that position can move the total by 40% while doing
    # nothing for a typical position.
    if changes:
        median = statistics.median(changes)
        print(f"{'median':18s} {'':>12s} {'':>12s} {median:>8.1f}%   "
              f"(range {min(changes):.1f}% to {max(changes):.1f}%)")

    base_time = baseline["totals"]["seconds"]
    cur_time = current["totals"]["seconds"]
    time_pct = (cur_time - base_time) / base_time * 100 if base_time else 0.0
    print(f"{'time':18s} {cur_time:>11.2f}s {base_time:>11.2f}s {time_pct:>8.1f}%"
          f"   (timings are machine-dependent)")

    if changed:
        print(f"\nscore changed on {len(changed)} position(s): {', '.join(changed)}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--depth", type=int, default=4, help="fixed search depth")
    parser.add_argument("--config", default="phase0", choices=sorted(CONFIGS),
                        help="named search configuration from engine.searcher.CONFIGS")
    parser.add_argument("--save", type=Path, help="write results as JSON to this path")
    parser.add_argument("--baseline", type=Path, help="compare against this JSON baseline")
    parser.add_argument("--quiet", action="store_true", help="only print the comparison")
    args = parser.parse_args()

    result = run(args.depth, args.config, verbose=not args.quiet)

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"\nsaved to {args.save}")

    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        return compare(result, baseline)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
