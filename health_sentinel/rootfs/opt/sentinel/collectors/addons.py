"""Add-on state tracking, restart counting and killed-vs-clean classification.

Reading Docker exit codes directly would mean `docker_api: true`, which is a
real privilege escalation and would force protection mode off. It buys one
thing: knowing whether a container was killed (137) or exited cleanly (143).

That same fact is recoverable without it. The kernel names its OOM victim in the
journal, including the cgroup, and the Supervisor reports the state transition.
Correlating the two in a short time window gives the same answer at no
privilege cost.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from collectors.supervisor_api import SupervisorClient, container_stats_to_row

_LOGGER = logging.getLogger(__name__)

# How far back to look for an OOM kill that could explain a stop.
_OOM_CORRELATION_WINDOW = 90

_RUNNING_STATES = ("started",)
_BAD_STATES = ("error", "unknown")


class AddonTracker:
    """Watches add-on states and collects per-container resource stats."""

    def __init__(self, client: SupervisorClient) -> None:
        self._client = client
        self._states: dict[str, str] = {}
        self._restarts: dict[str, int] = {}
        self._names: dict[str, str] = {}
        self._last_stats: dict[str, dict[str, Any]] = {}
        self._primed = False

    @property
    def restart_counts(self) -> dict[str, int]:
        return dict(self._restarts)

    @property
    def names(self) -> dict[str, str]:
        return dict(self._names)

    def name_for(self, slug: str) -> str:
        return self._names.get(slug, slug)

    async def poll_states(self) -> list[dict[str, Any]]:
        """Return add-on state transitions since the previous poll."""
        addons = await self._client.addons()
        if not addons:
            return []

        current: dict[str, str] = {}
        for addon in addons:
            slug = addon.get("slug")
            if not slug:
                continue
            current[slug] = addon.get("state") or "unknown"
            self._names[slug] = addon.get("name") or slug

        if not self._primed:
            self._states = current
            self._primed = True
            return []

        transitions: list[dict[str, Any]] = []
        for slug, state in current.items():
            previous = self._states.get(slug)
            if previous is None or previous == state:
                continue

            if previous in _RUNNING_STATES and state not in _RUNNING_STATES:
                self._restarts[slug] = self._restarts.get(slug, 0) + 1

            severity = "error" if state in _BAD_STATES else "info"
            transitions.append(
                {
                    "slug": slug,
                    "name": self.name_for(slug),
                    "from": previous,
                    "to": state,
                    "severity": severity,
                    "restarts": self._restarts.get(slug, 0),
                }
            )

        self._states = current
        return transitions

    async def collect_stats(self) -> list[dict[str, Any]]:
        """Per-container resource stats for every running add-on."""
        rows: list[dict[str, Any]] = []
        for slug, state in self._states.items():
            if state not in _RUNNING_STATES:
                continue
            stats = await self._client.addon_stats(slug)
            if not stats:
                continue
            row = container_stats_to_row(slug, stats)
            rows.append(row)
            self._last_stats[slug] = row
        return rows

    def snapshot(self) -> list[dict[str, Any]]:
        """Current view of every add-on — captured into incident bundles."""
        return [
            {
                "slug": slug,
                "name": self.name_for(slug),
                "state": state,
                "restarts": self._restarts.get(slug, 0),
                "stats": self._last_stats.get(slug),
            }
            for slug, state in sorted(self._states.items())
        ]

    def metrics(self) -> dict[str, float]:
        running = sum(1 for s in self._states.values() if s in _RUNNING_STATES)
        errored = sum(1 for s in self._states.values() if s in _BAD_STATES)
        return {
            "addons.total": float(len(self._states)),
            "addons.running": float(running),
            "addons.error": float(errored),
        }


def classify_stop(
    slug: str,
    name: str,
    stopped_ts: int,
    recent_oom_events: list[dict[str, Any]],
) -> tuple[str, str]:
    """Decide whether a stopped add-on was killed or exited cleanly.

    Returns (classification, human explanation).
    """
    needle_slug = slug.lower()
    needle_name = name.lower()

    for event in recent_oom_events:
        if abs(int(event.get("ts", 0)) - stopped_ts) > _OOM_CORRELATION_WINDOW:
            continue
        message = str(event.get("message", "")).lower()
        if needle_slug in message or needle_name in message:
            return (
                "oom_killed",
                f"{name} was killed by the kernel OOM killer at "
                f"{time.strftime('%H:%M:%S', time.localtime(int(event['ts'])))}.",
            )

    # An OOM kill anywhere on the box at the same moment is still relevant, even
    # if the cgroup name didn't match the add-on slug.
    for event in recent_oom_events:
        if abs(int(event.get("ts", 0)) - stopped_ts) <= _OOM_CORRELATION_WINDOW:
            return (
                "possibly_oom_killed",
                f"{name} stopped within seconds of a kernel OOM kill; memory "
                "exhaustion is the likely cause.",
            )

    return ("stopped", f"{name} stopped with no matching kernel kill event.")
