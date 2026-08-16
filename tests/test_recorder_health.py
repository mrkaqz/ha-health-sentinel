"""system_health/info response parsing tests.

Run with: python tests/test_recorder_health.py

The fixture below is the ACTUAL shape returned by a live Home Assistant
instance, captured while diagnosing a bug where the previous implementation
read result["recorder"] directly. The real key is result["data"]["recorder"] —
the command wraps everything as {"type": "initial", "data": {...}}. The old
code returned silently on every call: no exception, stable connection, and
"database size" simply stayed blank forever with nothing in the logs to say
why. This is why the fixture is copied verbatim rather than simplified.
"""

import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "health_sentinel", "rootfs", "opt", "sentinel"))

from collectors.recorder import RecorderWatcher  # noqa: E402

# Captured verbatim from a live instance's system_health/info response.
REAL_FIXTURE = {
    "type": "initial",
    "data": {
        "cloud": {
            "info": {
                "logged_in": False,
                "can_reach_cert_server": {"type": "pending"},
                "can_reach_cloud_auth": {"type": "pending"},
                "can_reach_cloud": {"type": "pending"},
            },
            "manage_url": "/config/cloud",
        },
        "hassio": {
            "info": {
                "host_os": "Home Assistant OS 18.2",
                "supervisor_version": "supervisor-2026.07.5",
                "healthy": True,
            }
        },
        "homeassistant": {
            "info": {"version": "core-2026.8.2", "installation_type": "Home Assistant OS"}
        },
        "recorder": {
            "info": {
                "oldest_recorder_run": {
                    "value": "2025-08-15T17:07:42.152148",
                    "type": "date",
                },
                "current_recorder_run": {
                    "value": "2026-08-16T11:59:16.065861+00:00",
                    "type": "date",
                },
                "estimated_db_size": "82363.42 MiB",
                "database_engine": "mysql",
                "database_version": "11.4.10",
            }
        },
        "sonoff": {"info": {"version": "3.12.2 (871119a)"}},
        "xiaomi_miot": {
            "info": {
                "can_reach_server": {"type": "pending"},
                "can_reach_spec": {"type": "pending"},
                "total_devices": 15,
            }
        },
    },
}


def make_watcher() -> RecorderWatcher:
    class FakeClient:
        base = "http://supervisor"

    return RecorderWatcher(FakeClient(), token="x")  # type: ignore[arg-type]


results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)  # keep test output clean

    # --- the real shape, wrapped in {"type": "initial", "data": {...}} ---
    watcher = make_watcher()
    watcher._handle_system_health(REAL_FIXTURE)
    expected_bytes = 82363.42 * 1024 * 1024
    check(
        "real fixture: db size parsed",
        watcher.db_size_bytes is not None,
    )
    check(
        "real fixture: db size is correct",
        watcher.db_size_bytes is not None
        and abs(watcher.db_size_bytes - expected_bytes) < 1.0,
        f"got={watcher.db_size_bytes} want={expected_bytes}",
    )

    # --- a component list missing recorder entirely must not raise --------
    watcher = make_watcher()
    watcher._handle_system_health({"type": "initial", "data": {"hassio": {"info": {}}}})
    check("missing recorder component does not crash", watcher.db_size_bytes is None)

    # --- recorder present but no size yet (still gathering) ---------------
    watcher = make_watcher()
    watcher._handle_system_health(
        {"type": "initial", "data": {"recorder": {"info": {"database_engine": "sqlite"}}}}
    )
    check("recorder without size does not crash", watcher.db_size_bytes is None)

    # --- garbage size string must not raise --------------------------------
    watcher = make_watcher()
    watcher._handle_system_health(
        {"type": "initial", "data": {"recorder": {"info": {"estimated_db_size": "???"}}}}
    )
    check("unparseable size does not crash", watcher.db_size_bytes is None)

    # --- an unwrapped legacy/alternate shape is still tolerated -------------
    watcher = make_watcher()
    watcher._handle_system_health(
        {"recorder": {"info": {"estimated_db_size": "10.0 GiB"}}}
    )
    check(
        "unwrapped fallback shape still parses",
        watcher.db_size_bytes is not None
        and abs(watcher.db_size_bytes - 10.0 * 1024**3) < 1.0,
    )

    # --- unit variants -------------------------------------------------------
    for raw, expected in [
        ("1.0 GiB", 1024**3),
        ("1.0 GB", 1000**3),
        ("500.0 KiB", 500 * 1024),
        ("2048 MiB", 2048 * 1024**2),
    ]:
        watcher = make_watcher()
        watcher._handle_system_health({"type": "initial", "data": {"recorder": {"info": {"estimated_db_size": raw}}}})
        check(
            f"unit parses: {raw}",
            watcher.db_size_bytes is not None and abs(watcher.db_size_bytes - expected) < 1.0,
            f"got={watcher.db_size_bytes}",
        )

    # --- an entirely empty result must not crash ----------------------------
    watcher = make_watcher()
    watcher._handle_system_health({})
    check("empty result does not crash", watcher.db_size_bytes is None)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
