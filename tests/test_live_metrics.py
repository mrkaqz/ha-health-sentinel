"""Live-metrics merge tests.

Run with: python tests/test_live_metrics.py

Reproduces the bug reported from live use: "Disk Free" and "Unavailable
entities" showed "—" almost all of the time. Root cause: the fast loop
(~every 15s) wrote `live["metrics"] = metrics`, a full replace, while the slow
loop (every slow_interval, default 300s) wrote the disk/entity-census/
network/integration keys via a merge. The very next fast-loop tick — at most
15s later — discarded them, leaving them missing for the remaining ~285s of
every 300s cycle. A user looking at the dashboard would see the value blank
about 95% of the time.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "health_sentinel", "rootfs", "opt", "sentinel"))

from main import merge_live_metrics  # noqa: E402

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def main() -> int:
    live: dict = {}

    # Slow loop writes its keys once.
    merge_live_metrics(live, {"host.disk.free_pct": 58.6, "core.entities.unavailable": 1004.0})
    check(
        "slow-loop keys present immediately after write",
        live["metrics"].get("host.disk.free_pct") == 58.6,
    )

    # Fast loop then ticks 20 times (simulating 20 * 15s = 5 minutes) with its
    # own, disjoint set of keys.
    for i in range(20):
        merge_live_metrics(
            live,
            {"host.mem.used_pct": 70.0 + i, "core.latency_ms": 100.0 + i},
        )

    check(
        "disk metric survives 20 subsequent fast-loop ticks",
        live["metrics"].get("host.disk.free_pct") == 58.6,
        f"got={live['metrics'].get('host.disk.free_pct')}",
    )
    check(
        "unavailable-entities metric survives 20 fast-loop ticks",
        live["metrics"].get("core.entities.unavailable") == 1004.0,
    )
    check(
        "fast-loop metric reflects the latest tick, not a stale one",
        live["metrics"].get("host.mem.used_pct") == 89.0,
        f"got={live['metrics'].get('host.mem.used_pct')}",
    )
    check(
        "fast-loop metric key count did not accumulate duplicates",
        len(live["metrics"]) == 4,
        f"keys={sorted(live['metrics'].keys())}",
    )

    # A later slow-loop cycle updates its own key; earlier fast-loop values
    # must survive that too — merging must be symmetric.
    merge_live_metrics(live, {"host.disk.free_pct": 55.1})
    check(
        "fast-loop metric survives a subsequent slow-loop write",
        live["metrics"].get("host.mem.used_pct") == 89.0,
    )
    check(
        "slow-loop metric updates in place",
        live["metrics"].get("host.disk.free_pct") == 55.1,
    )

    # First call on a fresh dict must not raise (no live["metrics"] yet).
    fresh: dict = {}
    merge_live_metrics(fresh, {"x": 1.0})
    check("first call on an empty dict does not raise", fresh["metrics"]["x"] == 1.0)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
