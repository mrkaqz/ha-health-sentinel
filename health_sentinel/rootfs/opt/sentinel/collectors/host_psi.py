"""Host resource metrics read straight out of procfs and sysfs.

None of this needs elevated privileges. `loadavg`, `meminfo`, `uptime` and
`pressure/*` are not namespaced per container, so an ordinary unprivileged
add-on reads the *host's* real numbers, and none of these paths are on Docker's
masked-paths list.

PSI (Pressure Stall Information) is the headline act. Unlike "CPU is at 90%",
PSI measures time actually lost to waiting for a resource, which is the
difference between a machine that is busy and a machine that is dying. Rising
`memory.full` is the clearest early warning of an incoming OOM kill.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import Any

from config import (
    PROC_LOADAVG,
    PROC_MEMINFO,
    PROC_PRESSURE,
    PROC_UPTIME,
    SYS_THERMAL,
)

_LOGGER = logging.getLogger(__name__)

_PSI_RESOURCES = ("cpu", "memory", "io")
_PSI_WINDOWS = ("avg10", "avg60", "avg300")


def psi_available() -> bool:
    """PSI needs CONFIG_PSI=y. HAOS ships it, but degrade rather than crash."""
    return os.path.isdir(PROC_PRESSURE) and os.path.exists(
        os.path.join(PROC_PRESSURE, "cpu")
    )


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def read_psi() -> dict[str, float]:
    """Parse /proc/pressure/{cpu,memory,io}.

    Each file looks like:
        some avg10=0.00 avg60=0.00 avg300=0.00 total=0
        full avg10=0.00 avg60=0.00 avg300=0.00 total=0

    "some" means at least one task stalled; "full" means everything stalled.
    """
    metrics: dict[str, float] = {}
    for resource in _PSI_RESOURCES:
        content = _read(os.path.join(PROC_PRESSURE, resource))
        if not content:
            continue
        for line in content.splitlines():
            parts = line.split()
            if not parts:
                continue
            kind = parts[0]  # "some" or "full"
            for field in parts[1:]:
                if "=" not in field:
                    continue
                key, _, raw = field.partition("=")
                if key not in _PSI_WINDOWS:
                    continue
                try:
                    metrics[f"host.psi.{resource}.{kind}.{key}"] = float(raw)
                except ValueError:
                    continue
    return metrics


def read_loadavg() -> dict[str, float]:
    content = _read(PROC_LOADAVG)
    if not content:
        return {}
    parts = content.split()
    if len(parts) < 3:
        return {}
    try:
        metrics = {
            "host.load.1": float(parts[0]),
            "host.load.5": float(parts[1]),
            "host.load.15": float(parts[2]),
        }
    except ValueError:
        return {}
    # "running/total" processes, e.g. 3/1234
    if len(parts) >= 4 and "/" in parts[3]:
        running, _, total = parts[3].partition("/")
        try:
            metrics["host.procs.running"] = float(running)
            metrics["host.procs.total"] = float(total)
        except ValueError:
            pass
    return metrics


def read_meminfo() -> dict[str, float]:
    content = _read(PROC_MEMINFO)
    if not content:
        return {}
    values: dict[str, float] = {}
    for line in content.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        try:
            # /proc/meminfo is in kB.
            values[key] = float(fields[0]) * 1024.0
        except ValueError:
            continue

    total = values.get("MemTotal", 0.0)
    available = values.get("MemAvailable", values.get("MemFree", 0.0))
    swap_total = values.get("SwapTotal", 0.0)
    swap_free = values.get("SwapFree", 0.0)

    metrics = {
        "host.mem.total_bytes": total,
        "host.mem.available_bytes": available,
        "host.mem.free_bytes": values.get("MemFree", 0.0),
        "host.mem.cached_bytes": values.get("Cached", 0.0),
        "host.swap.total_bytes": swap_total,
        "host.swap.used_bytes": max(swap_total - swap_free, 0.0),
    }
    if total > 0:
        metrics["host.mem.used_pct"] = (total - available) / total * 100.0
    if swap_total > 0:
        metrics["host.swap.used_pct"] = (swap_total - swap_free) / swap_total * 100.0
    return metrics


def read_uptime() -> dict[str, float]:
    content = _read(PROC_UPTIME)
    if not content:
        return {}
    try:
        return {"host.uptime_seconds": float(content.split()[0])}
    except (ValueError, IndexError):
        return {}


def read_thermal() -> dict[str, float]:
    """CPU/board temperatures. Thermal shutdown is a real crash cause on x86."""
    metrics: dict[str, float] = {}
    hottest: float | None = None
    for zone in sorted(glob.glob(os.path.join(SYS_THERMAL, "thermal_zone*"))):
        raw = _read(os.path.join(zone, "temp"))
        if not raw:
            continue
        try:
            celsius = float(raw.strip()) / 1000.0
        except ValueError:
            continue
        # Ignore obviously bogus sensors rather than poisoning the max.
        if not -50.0 < celsius < 150.0:
            continue
        label = (_read(os.path.join(zone, "type")) or os.path.basename(zone)).strip()
        safe = "".join(c if c.isalnum() else "_" for c in label).strip("_").lower()
        metrics[f"host.temp.{safe}"] = celsius
        hottest = celsius if hottest is None else max(hottest, celsius)
    if hottest is not None:
        metrics["host.temp.max"] = hottest
    return metrics


def collect() -> dict[str, float]:
    """All host metrics for one sampling tick."""
    metrics: dict[str, float] = {}
    metrics.update(read_loadavg())
    metrics.update(read_meminfo())
    metrics.update(read_uptime())
    metrics.update(read_thermal())
    if psi_available():
        metrics.update(read_psi())
    return metrics


def describe_capabilities() -> dict[str, Any]:
    return {
        "psi": psi_available(),
        "loadavg": os.path.exists(PROC_LOADAVG),
        "meminfo": os.path.exists(PROC_MEMINFO),
        "thermal": bool(glob.glob(os.path.join(SYS_THERMAL, "thermal_zone*"))),
    }
