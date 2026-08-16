"""Per-integration availability tracking and multi-integration outage detection.

The add-on already knew how many entities were unavailable. That number cannot
answer the question people actually ask after an outage: *which* integrations
died, and did they die together? Several unrelated integrations dropping within
the same couple of minutes is the signature of a shared cause — the network, DNS,
a power blip — and no aggregate percentage can show it.

Two ideas do the work here.

**Chronic is not the same as a blip.** On a real system a large share of
entities are simply dead — a camera that was unplugged months ago, a bulb that
was thrown away. Those must be separated out, or they drown the signal and every
restart looks like a catastrophe. Anything unavailable longer than
`chronic_after_minutes` stops counting as an event and moves to a list of
standing problems.

**Only genuine drops cluster.** A drop is a transition from a healthy state into
unavailable. Re-reporting an already-dead entity is not a drop, which is what
keeps a restart from firing the detector.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

# States that mean "this entity is not reporting". `unknown` is included
# because integrations that lose their connection frequently land there rather
# than on `unavailable`.
DEAD_STATES = ("unavailable", "unknown")

# Entities whose integration we could not determine.
_UNKNOWN_PLATFORM = "unknown"


def parse_iso(value: str) -> int | None:
    """Parse a Home Assistant ISO timestamp into a unix time."""
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


class AvailabilityTracker:
    """Tracks entity availability, bucketed by integration."""

    def __init__(
        self,
        chronic_after_minutes: int = 60,
        cluster_window_seconds: int = 120,
        cluster_min_integrations: int = 3,
        cluster_min_entities: int = 2,
    ) -> None:
        self._chronic_after = chronic_after_minutes * 60
        self._window = cluster_window_seconds
        self._min_integrations = cluster_min_integrations
        self._min_entities = cluster_min_entities

        # entity_id -> integration name
        self._platforms: dict[str, str] = {}
        # entity_id -> {"state": str, "since": int}
        self._states: dict[str, dict[str, Any]] = {}
        # recent drops as (ts, entity_id, platform)
        self._drops: list[tuple[int, str, str]] = []
        # Suppresses repeat alerts for one continuous outage.
        self._last_cluster_ts = 0
        self._mapping_source = "none"

    # ------------------------------------------------------------- mapping

    def set_mapping(self, mapping: dict[str, str], source: str) -> None:
        """Install the entity_id -> integration map."""
        self._platforms = mapping
        self._mapping_source = source
        _LOGGER.info(
            "Entity mapping loaded: %d entities across %d integrations (via %s)",
            len(mapping),
            len(set(mapping.values())),
            source,
        )

    @property
    def mapping_source(self) -> str:
        return self._mapping_source

    @property
    def mapped_entities(self) -> int:
        return len(self._platforms)

    def platform_for(self, entity_id: str) -> str:
        if entity_id in self._platforms:
            return self._platforms[entity_id]
        return _UNKNOWN_PLATFORM

    # ------------------------------------------------------------ ingestion

    def seed(self, entity_id: str, state: str, since: int | None = None) -> None:
        """Record a starting state without treating it as a transition.

        Used when restoring from the database and when taking the initial
        census, so that startup does not look like a mass outage.
        """
        self._states[entity_id] = {
            "state": state,
            "since": since if since is not None else int(time.time()),
        }

    def observe(
        self, entity_id: str, new_state: str, old_state: str | None, ts: int | None = None
    ) -> dict[str, Any] | None:
        """Feed one state_changed event.

        Returns a drop record when this was a genuine transition into a dead
        state, otherwise None.
        """
        ts = ts if ts is not None else int(time.time())
        previous = self._states.get(entity_id)

        # Two sources disagree about the prior state after a websocket
        # reconnect: our tracked view can be stale because we missed events
        # while disconnected, while the event's own old_state is authoritative
        # for this transition. If *either* says the entity was already dead,
        # this is not a new drop. Erring toward "already dead" is deliberate —
        # the cost of a missed drop is one uncounted entity, while the cost of a
        # false one is a spurious outage alert on every reconnect.
        was_dead = bool(previous and previous["state"] in DEAD_STATES) or bool(
            old_state and old_state in DEAD_STATES
        )
        is_dead = new_state in DEAD_STATES

        if previous is None or previous["state"] != new_state:
            self._states[entity_id] = {"state": new_state, "since": ts}

        # Only a healthy -> dead transition counts. Anything else is either a
        # recovery, a no-op, or an already-dead entity being re-reported, which
        # is exactly what happens en masse on a restart.
        if not is_dead or was_dead:
            return None

        platform = self.platform_for(entity_id)
        self._drops.append((ts, entity_id, platform))
        self._prune(ts)
        return {"ts": ts, "entity_id": entity_id, "platform": platform}

    def _prune(self, now: int) -> None:
        cutoff = now - self._window
        self._drops = [d for d in self._drops if d[0] >= cutoff]

    # ------------------------------------------------------------ detection

    def check_cluster(self, now: int | None = None) -> dict[str, Any] | None:
        """Have enough unrelated integrations dropped at once?"""
        now = now if now is not None else int(time.time())
        self._prune(now)

        by_platform: dict[str, list[str]] = defaultdict(list)
        for ts, entity_id, platform in self._drops:
            # An entity we cannot attribute tells us nothing about which
            # integrations correlate, so it cannot contribute to a cluster.
            if platform == _UNKNOWN_PLATFORM:
                continue
            # A chronically dead entity flapping is not evidence of a new event.
            if self._is_chronic(entity_id, now):
                continue
            by_platform[platform].append(entity_id)

        qualifying = {
            platform: entities
            for platform, entities in by_platform.items()
            if len(entities) >= self._min_entities
        }
        if len(qualifying) < self._min_integrations:
            return None

        # One outage, one alert.
        if now - self._last_cluster_ts < self._window:
            return None
        self._last_cluster_ts = now

        total = sum(len(v) for v in qualifying.values())
        return {
            "ts": now,
            "window_seconds": self._window,
            "integrations": sorted(qualifying),
            "entity_count": total,
            "detail": {k: sorted(v) for k, v in sorted(qualifying.items())},
            "summary": (
                f"{len(qualifying)} integrations lost {total} entities within "
                f"{self._window}s: {', '.join(sorted(qualifying))}. Unrelated "
                "integrations failing together points at a shared cause — "
                "network, DNS or power — rather than at any one device."
            ),
        }

    def _is_chronic(self, entity_id: str, now: int) -> bool:
        record = self._states.get(entity_id)
        if not record or record["state"] not in DEAD_STATES:
            return False
        return (now - record["since"]) >= self._chronic_after

    # ------------------------------------------------------------- reporting

    def integration_health(self, now: int | None = None) -> list[dict[str, Any]]:
        """Per-integration totals, sorted worst first."""
        now = now if now is not None else int(time.time())
        totals: dict[str, dict[str, Any]] = {}

        for entity_id, platform in self._platforms.items():
            bucket = totals.setdefault(
                platform,
                {
                    "integration": platform,
                    "total": 0,
                    "unavailable": 0,
                    "chronic": 0,
                    "last_drop": None,
                },
            )
            bucket["total"] += 1
            record = self._states.get(entity_id)
            if record and record["state"] in DEAD_STATES:
                bucket["unavailable"] += 1
                if (now - record["since"]) >= self._chronic_after:
                    bucket["chronic"] += 1

        for ts, entity_id, platform in self._drops:
            bucket = totals.get(platform)
            if bucket and (bucket["last_drop"] is None or ts > bucket["last_drop"]):
                bucket["last_drop"] = ts

        rows = list(totals.values())
        for row in rows:
            row["unavailable_pct"] = (
                round(row["unavailable"] / row["total"] * 100.0, 1)
                if row["total"]
                else 0.0
            )
        rows.sort(key=lambda r: (-r["unavailable"], r["integration"]))
        return rows

    def chronic_entities(
        self, now: int | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Entities that have been dead long enough to be a standing problem."""
        now = now if now is not None else int(time.time())
        rows = [
            {
                "entity_id": entity_id,
                "platform": self.platform_for(entity_id),
                "state": record["state"],
                "since": record["since"],
                "dead_seconds": now - record["since"],
            }
            for entity_id, record in self._states.items()
            if record["state"] in DEAD_STATES
            and (now - record["since"]) >= self._chronic_after
        ]
        rows.sort(key=lambda r: r["dead_seconds"], reverse=True)
        return rows[:limit]

    def metrics(self, now: int | None = None) -> dict[str, float]:
        now = now if now is not None else int(time.time())
        health = self.integration_health(now)
        out: dict[str, float] = {
            "integrations.total": float(len(health)),
            "integrations.degraded": float(
                sum(1 for row in health if row["unavailable"] > 0)
            ),
            "integrations.recent_drops": float(len(self._drops)),
        }
        for row in health:
            name = row["integration"]
            out[f"integration.{name}.total"] = float(row["total"])
            out[f"integration.{name}.unavailable"] = float(row["unavailable"])
            out[f"integration.{name}.chronic"] = float(row["chronic"])
        return out

    def persistable_states(self) -> list[tuple[str, str, str, int]]:
        """Rows for storage: (entity_id, platform, state, since)."""
        return [
            (entity_id, self.platform_for(entity_id), record["state"], record["since"])
            for entity_id, record in self._states.items()
            if record["state"] in DEAD_STATES
        ]
