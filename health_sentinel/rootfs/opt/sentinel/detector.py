"""Live incident detection — watching Core die in real time.

This is the part that only works because an add-on outlives Core. When Core
stops answering, this add-on is still running, still sampling, and still holding
the last half hour of high-resolution metrics that Core never got to write down.

The bundle is built the moment the incident opens, not when it closes. If the
host is on its way down, there may not be a "when it closes".
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import bundle
from collectors.addons import AddonTracker
from collectors.core_probe import ProbeResult
from collectors.supervisor_api import SupervisorClient
from config import BUNDLE_DIR, Config
from forensics import human_duration
from storage import Storage

_LOGGER = logging.getLogger(__name__)

# Consecutive slow probes before we say Core is struggling. Rising latency is
# the classic signature in the minutes before a hang.
_SLOW_PROBE_THRESHOLD_MS = 2500
_SLOW_PROBE_COUNT = 4


class Notifier(Protocol):
    async def alert(
        self, title: str, message: str, severity: str = "warning"
    ) -> None: ...


class IncidentDetector:
    def __init__(
        self,
        storage: Storage,
        config: Config,
        client: SupervisorClient,
        addons: AddonTracker,
        notifier: Notifier,
    ) -> None:
        self._storage = storage
        self._config = config
        self._client = client
        self._addons = addons
        self._notifier = notifier

        self._first_failure_ts: int | None = None
        self._incident_id: int | None = None
        self._incident_started: int | None = None
        self._slow_streak = 0
        self._warned_slow = False
        self._last_error: str | None = None

    # ------------------------------------------------------------- accessors

    @property
    def incident_open(self) -> bool:
        return self._incident_id is not None

    @property
    def current_incident_id(self) -> int | None:
        return self._incident_id

    def status(self) -> dict[str, Any]:
        return {
            "incident_open": self.incident_open,
            "incident_id": self._incident_id,
            "incident_started": self._incident_started,
            "down_since": self._first_failure_ts,
            "last_error": self._last_error,
        }

    # ---------------------------------------------------------------- observe

    async def observe(self, probe: ProbeResult) -> None:
        """Feed one probe result into the state machine."""
        if probe.reachable:
            await self._on_reachable(probe)
        else:
            await self._on_unreachable(probe)

    async def _on_reachable(self, probe: ProbeResult) -> None:
        if self._incident_id is not None:
            await self._close_incident(probe.ts)

        self._first_failure_ts = None
        self._last_error = None
        await self._check_latency(probe)

    async def _on_unreachable(self, probe: ProbeResult) -> None:
        self._last_error = probe.error
        if self._first_failure_ts is None:
            self._first_failure_ts = probe.ts
            _LOGGER.warning(
                "Core stopped responding (%s); watching", probe.error or "no reason"
            )

        if self._incident_id is not None:
            return

        down_for = probe.ts - self._first_failure_ts
        if down_for < self._config.alert_core_down_after:
            return

        await self._open_incident(probe, down_for)

    # --------------------------------------------------------------- incident

    async def _open_incident(self, probe: ProbeResult, down_for: int) -> None:
        started = self._first_failure_ts or probe.ts
        self._incident_started = started

        supervisor_state = probe.supervisor_state or "unknown"
        summary = (
            f"Home Assistant Core stopped responding at "
            f"{time.strftime('%H:%M:%S', time.localtime(started))} "
            f"({probe.error or 'no response'}). The Supervisor reports Core as "
            f"'{supervisor_state}'."
        )

        detail = {
            "error": probe.error,
            "supervisor_state": supervisor_state,
            "down_for_seconds": down_for,
            "detected_by": "live_probe",
        }

        incident_id = self._storage.open_incident(
            started_ts=started,
            classification="core_unreachable",
            summary=summary,
            detail=detail,
        )
        self._incident_id = incident_id

        self._storage.add_event(
            kind="core_down",
            message=summary,
            severity="critical",
            source="detector",
            ts=started,
        )
        _LOGGER.error("Incident #%d opened: %s", incident_id, summary)

        await self._freeze(incident_id, started, summary, detail)
        await self._notifier.alert(
            "Home Assistant Core is not responding",
            f"{summary}\n\nDown for {human_duration(down_for)} so far.",
            severity="critical",
        )

    async def _freeze(
        self, incident_id: int, started: int, summary: str, detail: dict[str, Any]
    ) -> None:
        """Preserve the flight recorder tape and Core's last words.

        Done immediately. If this turns out to be the start of a full host
        failure, there will be no later opportunity.
        """
        now = int(time.time())
        window_start = started - self._config.ring_buffer_minutes * 60

        try:
            metrics = self._storage.window(window_start, now)
            events = self._storage.query(
                "SELECT * FROM events WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (window_start, now),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Could not read metric window: %s", err)
            metrics, events = {}, []

        logs: dict[str, str] = {}
        core_log = await self._client.core_log(lines=1000)
        if core_log:
            logs["core.log"] = core_log
        supervisor_log = await self._client.supervisor_log(lines=600)
        if supervisor_log:
            logs["supervisor.log"] = supervisor_log
        host_journal = await self._client.try_get_text("/host/logs", lines=600)
        if host_journal:
            logs["host-journal.log"] = host_journal

        summary_doc = {
            "incident_id": incident_id,
            "classification": "core_unreachable",
            "summary": summary,
            "started_local": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(started)
            ),
            "ended_local": "",
            "outage_human": "still open at capture time",
            "detail": detail,
            "ring_buffer_minutes": self._config.ring_buffer_minutes,
        }

        path = bundle.build(
            BUNDLE_DIR,
            incident_id,
            summary=summary_doc,
            metrics=metrics,
            events=events,
            logs=logs,
            addons=self._addons.snapshot(),
        )
        bundle.prune(BUNDLE_DIR)
        if path:
            self._storage.set_incident_bundle(incident_id, path)

    async def _close_incident(self, ended_ts: int) -> None:
        incident_id = self._incident_id
        started = self._incident_started or ended_ts
        outage = max(ended_ts - started, 0)

        summary = (
            f"Home Assistant Core recovered after {human_duration(outage)} "
            f"(down from {time.strftime('%H:%M:%S', time.localtime(started))} "
            f"to {time.strftime('%H:%M:%S', time.localtime(ended_ts))})."
        )

        assert incident_id is not None
        self._storage.close_incident(
            incident_id,
            ended_ts=ended_ts,
            summary=summary,
            detail={
                "outage_seconds": outage,
                "outage_human": human_duration(outage),
                "last_error": self._last_error,
                "detected_by": "live_probe",
            },
        )
        self._storage.add_event(
            kind="core_up",
            message=summary,
            severity="warning",
            source="detector",
            ts=ended_ts,
        )
        _LOGGER.warning("Incident #%d closed: %s", incident_id, summary)

        self._incident_id = None
        self._incident_started = None

        await self._notifier.alert(
            "Home Assistant Core recovered",
            summary,
            severity="info",
        )

    # ---------------------------------------------------------------- latency

    async def _check_latency(self, probe: ProbeResult) -> None:
        """Warn when Core is answering, but slowly."""
        if probe.latency_ms is None:
            return

        if probe.latency_ms < _SLOW_PROBE_THRESHOLD_MS:
            # Back to normal — rearm so the next episode alerts again.
            self._slow_streak = 0
            self._warned_slow = False
            return

        self._slow_streak += 1

        if self._slow_streak >= _SLOW_PROBE_COUNT and not self._warned_slow:
            self._warned_slow = True
            message = (
                f"Core is responding slowly: {probe.latency_ms:.0f} ms for the "
                f"last {self._slow_streak} probes. This often precedes a hang."
            )
            self._storage.add_event(
                kind="core_slow",
                message=message,
                severity="warning",
                source="detector",
                detail={"latency_ms": probe.latency_ms},
            )
            _LOGGER.warning(message)
            await self._notifier.alert(
                "Home Assistant Core is slow", message, severity="warning"
            )
