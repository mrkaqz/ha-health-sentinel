"""Full diagnostic export test.

Run with: python tests/test_export.py

Drives the real _build_full_export against a real Storage with a faked
Supervisor client, and asserts every section an AI would need is present.
"""

import asyncio
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "health_sentinel", "rootfs", "opt", "sentinel"))

import web  # noqa: E402
from collectors.availability import AvailabilityTracker  # noqa: E402
from storage import Storage  # noqa: E402

NOW = int(time.time())


class FakeClient:
    async def addon_log(self, slug, lines):
        return f"[{slug}] frigate.detector: CUDA out of memory\n" * 3

    async def try_get_text(self, path, lines=None):
        return f"line from {path}\n" * 4


class FakeAddons:
    restart_counts = {"ccab4aaf_frigate": 5}

    def name_for(self, slug):
        return {"ccab4aaf_frigate": "Frigate",
                "homeassistant": "Home Assistant Core"}.get(slug, slug)

    def snapshot(self):
        return [
            {"slug": "ccab4aaf_frigate", "name": "Frigate", "state": "error"},
            {"slug": "core_mariadb", "name": "MariaDB", "state": "started"},
        ]


class FakeNetwork:
    state = {
        "host_internet": True,
        "supervisor_internet": True,
        "interfaces": [{
            "interface": "eno1", "type": "ethernet", "connected": True,
            "primary": True, "address": "192.168.42.5/24",
            "gateway": "192.168.42.1", "nameservers": ["192.168.88.1"],
        }],
    }


class FakeConfig:
    ring_buffer_minutes = 30
    chronic_after_minutes = 60


class FakeSentinel:
    def __init__(self, storage, availability):
        self.storage = storage
        self.client = FakeClient()
        self.addons = FakeAddons()
        self.network = FakeNetwork()
        self.availability = availability
        self.config = FakeConfig()
        self.capabilities = {"proc_psi": True}
        self.live = {
            "host": {"hostname": "cattus-house", "kernel": "6.18.39-haos",
                     "disk_used": 189.3, "disk_total": 457.7,
                     "disk_life_time": 17},
            "os": {"version": "18.2", "boot": "A", "board": "generic-x86-64"},
            "core": {"reachable": True, "latency_ms": 187.4},
            "metrics": {"host.mem.used_pct": 78.4},
        }

    def uptime(self):
        return "3d 4h"


REQUIRED = [
    "HOME ASSISTANT DIAGNOSTIC EXPORT",
    "== SYSTEM",
    "== INCIDENTS",
    "== KERNEL, HARDWARE AND ADD-ON EVENTS",
    "== INTEGRATION HEALTH",
    "== NETWORK",
    "== CONTAINER RESOURCE USE",
    "== HOME ASSISTANT CORE LOG",
    "== SUPERVISOR LOG",
    "== HOST JOURNAL",
    # Only the unhealthy add-on's log should appear.
    "ADD-ON LOG: Frigate",
    # Evidence content.
    "Out of memory",
    "wled",
    "eno1",
]

FORBIDDEN = [
    # MariaDB is healthy; including every add-on log would bury the signal.
    "ADD-ON LOG: MariaDB",
]


async def main() -> int:
    storage = Storage(os.path.join(tempfile.mkdtemp(), "sentinel.db"))
    storage.add_event(
        "oom",
        "kernel: Out of memory: Killed process 4127 (frigate.detector)",
        "critical", "host_journal", ts=NOW - 1800,
    )
    incident = storage.open_incident(
        NOW - 1800, "core_unreachable", "Core stopped responding.")
    storage.close_incident(incident, NOW - 1500)
    storage.write_container_samples(NOW - 60, [{
        "slug": "ccab4aaf_frigate", "cpu": 62.8, "mem_bytes": 3.4 * 1024 ** 3,
        "mem_percent": 29.1, "net_rx": 0, "net_tx": 0,
        "blk_read": 0, "blk_write": 0,
    }])

    availability = AvailabilityTracker()
    availability.set_mapping(
        {"light.wled_a": "wled", "light.wled_b": "wled",
         "camera.ezviz_a": "ezviz"}, "test")
    availability.seed("light.wled_a", "unavailable", NOW - 86400)

    text = await web._build_full_export(FakeSentinel(storage, availability), 20)

    failures = 0
    for needle in REQUIRED:
        ok = needle in text
        if not ok:
            failures += 1
        print(f"{'PASS' if ok else 'FAIL'}  contains: {needle}")
    for needle in FORBIDDEN:
        ok = needle not in text
        if not ok:
            failures += 1
        print(f"{'PASS' if ok else 'FAIL'}  excludes: {needle}")

    total = len(REQUIRED) + len(FORBIDDEN)
    print(f"\n{total - failures}/{total} passed  ({len(text)} chars exported)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
