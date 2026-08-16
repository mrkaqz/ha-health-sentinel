"""system_health/info response parsing AND routing tests.

Run with: python tests/test_recorder_health.py

Two bugs, found in sequence against a live instance, both hiding behind the
same symptom: "database size" was always blank, connection stayed healthy,
nothing in the logs.

Bug 1 (fixed first): _handle_system_health read result["recorder"] directly.
The real key is result["data"]["recorder"].

Bug 2 (found once bug 1's own defensive logging reported "shape=[]" — an
empty result — instead of silence): system_health/info is not a simple
request/response command, it is a SUBSCRIPTION. Home Assistant's own handler
confirms with `connection.send_result(msg["id"])` — no payload — and the real
data streams in afterward as a separate "event" message reusing the same id,
exactly like subscribe_events -> state_changed. The old _handle() routed every
incoming "event" straight to the state_changed handler regardless of which
subscription it belonged to, so system_health's actual data was silently
discarded no matter how correctly bug 1's fix parsed the (empty) result.

The REAL_FIXTURE below is captured from a live instance's system_health/info
response and is reused for both the parsing tests (unit-level, direct calls to
_handle_system_health) and the routing tests (integration-level, the real
message sequence through _handle()).
"""

import asyncio
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


class FakeWebSocket:
    """Records outgoing sends; that's all _handle needs from a real ws."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


async def _drive_health_subscription(watcher: RecorderWatcher, ws: FakeWebSocket) -> None:
    """The real message sequence Core sends for one system_health/info call.

    Mirrors exactly what a live instance sends: the confirming "result" is
    empty, then an "event" carrying the actual data, then a "finish" event.
    Going through watcher._handle() (not calling _handle_system_health
    directly) is the point — it is the routing between these two message
    kinds that was broken, not the parsing of either one alone.
    """
    request_id = watcher._msg_id + 1
    await watcher._send(ws, {"type": "system_health/info"}, "health")

    # 1. The confirming result: success, no payload.
    await watcher._handle(ws, {"id": request_id, "type": "result", "success": True})

    # 2. The real data, as an "event" reusing the request's id.
    await watcher._handle(
        ws,
        {
            "id": request_id,
            "type": "event",
            "event": {"type": "initial", "data": REAL_FIXTURE["data"]},
        },
    )

    # 3. Recorder health has nothing async, so "finish" follows immediately.
    await watcher._handle(
        ws, {"id": request_id, "type": "event", "event": {"type": "finish"}}
    )


results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


async def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)  # keep test output clean

    # ============================================================
    # ROUTING tests — the real message sequence, through _handle().
    # This is the layer that was actually broken: bug 1's parsing was
    # correct from the start, it just never received real data because
    # every "event" message was routed to the state_changed handler
    # regardless of which subscription produced it.
    # ============================================================

    watcher = make_watcher()
    ws = FakeWebSocket()
    await _drive_health_subscription(watcher, ws)
    expected_bytes = 82363.42 * 1024 * 1024
    check(
        "routing: db size arrives via the real result->event->finish sequence",
        watcher.db_size_bytes is not None
        and abs(watcher.db_size_bytes - expected_bytes) < 1.0,
        f"got={watcher.db_size_bytes}",
    )
    check(
        "routing: unsubscribe sent after finish",
        any(m.get("type") == "unsubscribe_events" for m in ws.sent),
        f"sent={ws.sent}",
    )
    check(
        "routing: subscription entry cleaned up after finish",
        len(watcher._subscriptions) == 0,
        f"remaining={watcher._subscriptions}",
    )

    # An "event" whose id was never confirmed as a health subscription (e.g.
    # left over after a reconnect) must be ignored, not crash and not be
    # mistaken for a state_changed event.
    watcher = make_watcher()
    ws = FakeWebSocket()
    await watcher._handle(
        ws,
        {"id": 999, "type": "event", "event": {"type": "initial", "data": REAL_FIXTURE["data"]}},
    )
    check(
        "routing: an event for an unknown subscription id is ignored",
        watcher.db_size_bytes is None,
    )

    # state_changed routing must still work, gated by its own subscription id,
    # and must not be confused with a health event using a different id.
    watcher = make_watcher()
    ws = FakeWebSocket()
    await watcher._send(ws, {"type": "subscribe_events"}, "subscribe")
    await watcher._handle(ws, {"id": 1, "type": "result", "success": True})
    await watcher._handle(
        ws,
        {
            "id": 1,
            "type": "event",
            "event": {
                "event_type": "state_changed",
                "data": {
                    "entity_id": "sensor.x",
                    "old_state": {"state": "1"},
                    "new_state": {"state": "2"},
                },
            },
        },
    )
    check(
        "routing: state_changed events still counted via their own subscription",
        watcher._counts.get("sensor.x") == 1,
    )

    # ============================================================
    # PARSING tests — _handle_system_health called directly, isolating the
    # shape-handling logic from the subscription routing above.
    # ============================================================

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
    sys.exit(asyncio.run(main()))
