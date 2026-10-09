"""Regenerate the cumulative results table from the committed baselines.

Why this script exists
----------------------
The per-step numbers in `docs/DEVLOG.md` and `docs/ENGINEERING.md` have to come from
somewhere, and the obvious "somewhere" is a human reading benchmark output and typing it into
a table. That is how the predecessor project ended up describing one measurement as "~50%",
"~58%" and "69.33%" in three different artifacts -- and it is how four median figures in this
repo's own `ENGINEERING.md` ended up between 0.6 and 4 percentage points wrong on their first
pass, which is what prompted writing this.

So the table is generated. `python -m bench.summary --markdown` emits the exact rows that go
into the docs, computed from the JSON baselines under `bench/baselines/`, which are themselves
the committed output of `bench/nodes.py` and `bench/timed.py`. Nothing is retyped.

Usage
-----
    python -m bench.summary
    python -m bench.summary --markdown
"""

import argparse
import json
import statistics
from pathlib import Path

BASELINES = Path(__file__).parent / "baselines"

# The order the features were built in, which is also the order the docs report them.
PIPELINE = [
    ("ordering", "move ordering (MVV-LVA, killers, history)"),
    ("tt-move-only", "+ transposition table, best move only"),
    ("tt", "+ score cutoffs"),
    ("tt-id", "+ iterative deepening"),
    ("quiescence", "+ quiescence"),
    ("quiescence-see", "+ SEE capture pruning"),
    ("pvs", "+ PVS"),
    ("pvs-asp", "+ aspiration windows"),
]


def load(name: str) -> dict | None:
    path = BASELINES / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def compare(current: dict, baseline: dict) -> dict:
    """Aggregate, median and per-position range of the node change, plus score agreement."""
    changes = []
    scores_same = 0
    for name, base in baseline["positions"].items():
        cur = current["positions"][name]
        changes.append((cur["nodes"] - base["nodes"]) / base["nodes"] * 100)
        scores_same += cur["score"] == base["score"]
    total_base = baseline["totals"]["nodes"]
    total_cur = current["totals"]["nodes"]
    return {
        "nodes": total_cur,
        "aggregate_pct": (total_cur - total_base) / total_base * 100,
        "median_pct": statistics.median(changes),
        "min_pct": min(changes),
        "max_pct": max(changes),
        "seconds": current["totals"]["seconds"],
        "time_pct": (
            (current["totals"]["seconds"] - baseline["totals"]["seconds"])
            / baseline["totals"]["seconds"] * 100
        ),
        "scores_same": scores_same,
        "position_count": len(changes),
    }


def cumulative_rows(depth: int = 4) -> list[tuple[str, str, dict]]:
    """Every configuration compared against the Phase 0 baseline at the same depth."""
    base = load(f"phase0-d{depth}.json")
    if base is None:
        raise SystemExit(f"no phase0 baseline at depth {depth}")
    rows = []
    for config, label in PIPELINE:
        current = load(f"{config}-d{depth}.json")
        if current is None:
            continue
        rows.append((config, label, compare(current, base)))
    return rows


def timed_rows(ms: int) -> dict | None:
    return load(f"timed-{ms}ms.json")


def print_plain(depth: int) -> None:
    base = load(f"phase0-d{depth}.json")
    print(f"Cumulative at depth {depth}, against the Phase 0 plain baseline")
    print(f"{'config':16s} {'nodes':>10s} {'aggregate':>10s} {'median':>9s} "
          f"{'range':>19s} {'time':>8s} {'scores':>9s}")
    print(f"{'phase0':16s} {base['totals']['nodes']:>10,} {'--':>10s} {'--':>9s} "
          f"{'--':>19s} {base['totals']['seconds']:>7.2f}s {'--':>9s}")
    for config, _label, r in cumulative_rows(depth):
        print(f"{config:16s} {r['nodes']:>10,} {r['aggregate_pct']:>9.1f}% "
              f"{r['median_pct']:>8.1f}% {r['min_pct']:>8.1f}% to {r['max_pct']:>5.1f}% "
              f"{r['seconds']:>7.2f}s {r['scores_same']:>4d}/{r['position_count']}")

    for ms in (1000, 3000):
        data = timed_rows(ms)
        if data is None:
            continue
        print(f"\nMean depth reached in {ms}ms (hard limit {data['hard_ms']}ms)")
        for config, info in data["configs"].items():
            print(f"  {config:16s} {info['totals']['mean_depth']:>5.2f}  "
                  f"median {info['totals']['median_depth']:>3}  "
                  f"range {info['totals']['min_depth']}-{info['totals']['max_depth']}  "
                  f"worst overshoot {info['totals']['worst_overshoot_ms']:+.1f}ms")
        first = next(iter(data["configs"]))
        last = list(data["configs"])[-1]
        gain = (data["configs"][last]["totals"]["mean_depth"]
                - data["configs"][first]["totals"]["mean_depth"])
        print(f"  {last} is {gain:+.2f} plies deeper than {first}")


def print_markdown(depth: int) -> None:
    base = load(f"phase0-d{depth}.json")
    print(f"| step | config | nodes at depth {depth} | vs baseline | median per position "
          f"| scores |")
    print("|---|---|---:|---:|---:|---|")
    print(f"| 0. plain alpha-beta | `phase0` | {base['totals']['nodes']:,} | - | - | - |")
    for config, label, r in cumulative_rows(depth):
        scores = (
            "all identical"
            if r["scores_same"] == r["position_count"]
            else f"{r['position_count'] - r['scores_same']}/{r['position_count']} changed"
        )
        print(f"| {label} | `{config}` | {r['nodes']:,} | {r['aggregate_pct']:.1f}% "
              f"| **{r['median_pct']:.1f}%** | {scores} |")

    for ms in (1000, 3000):
        data = timed_rows(ms)
        if data is None:
            continue
        print(f"\n| config | mean depth in {ms}ms | median | worst overshoot |")
        print("|---|---:|---:|---:|")
        for config, info in data["configs"].items():
            t = info["totals"]
            print(f"| `{config}` | {t['mean_depth']} | {t['median_depth']} "
                  f"| {t['worst_overshoot_ms']:+.1f}ms |")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--markdown", action="store_true",
                        help="emit the rows that go into the docs")
    args = parser.parse_args()

    if args.markdown:
        print_markdown(args.depth)
    else:
        print_plain(args.depth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
