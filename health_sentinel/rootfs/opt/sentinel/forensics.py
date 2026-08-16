"""Boot forensics — reconstructing what happened while we were not running.

Runs once, at startup. Compares what the previous run left in the state file
against what the host looks like now, decides what kind of death occurred, and
then goes and finds the evidence for it.

The single most valuable source is the *previous boot's* journal, which the
Supervisor will hand over via /host/logs/boots. If the machine died badly, the
reason is usually in the last few lines before that journal stops.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import bundle
from collectors import host_events
from collectors.supervisor_api import SupervisorClient
from config import BUNDLE_DIR, HA_LOG_PREVIOUS, PROC_BOOT_ID, PROC_UPTIME, Config
from state import StateFile, read_host_boot_id, read_host_uptime
from storage import Storage

_LOGGER = logging.getLogger(__name__)

# Lines that prove someone asked the machine to stop, rather than it just
# stopping. Their absence in the previous boot's tail is the tell for power loss.
_SHUTDOWN_MARKERS = (
    "systemd-shutdown",
    "Reached target Shutdown",
    "Reached target Power-Off",
    "Powering off",
    "Power down",
    "System is powering down",
    "Rebooting",
    "Unmounting",
    "Deactivating swap",
)

CLASSIFICATIONS = {
    "first_run": "First run — nothing to compare against yet.",
    "clean_restart": "Clean restart. The add-on was stopped in an orderly way.",
    "addon_restart": "The add-on restarted, but the host kept running.",
    "core_only": "Home Assistant Core went away while the host stayed up.",
    "host_reboot": "The host rebooted in an orderly way.",
    "host_power_loss": "The host stopped without shutting down — power loss, "
    "hard reset, kernel panic or thermal cutoff.",
}


def human_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours, rest = divmod(seconds, 3600)
    if hours < 24:
        return f"{hours}h {rest // 60}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _local(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))


class BootForensics:
    def __init__(
        self,
        client: SupervisorClient,
        storage: Storage,
        state: StateFile,
        config: Config,
    ) -> None:
        self._client = client
        self._storage = storage
        self._state = state
        self._config = config

    async def analyse(self) -> dict[str, Any]:
        """Classify the gap since the last run and capture the evidence."""
        now = int(time.time())
        boot_id = read_host_boot_id(PROC_BOOT_ID)
        uptime = read_host_uptime(PROC_UPTIME) or 0.0
        host_boot_ts = int(now - uptime)

        previous = self._state.previous
        last_heartbeat = previous.get("last_heartbeat")
        previous_boot_id = previous.get("boot_id")
        clean_shutdown = bool(previous.get("clean_shutdown"))

        if boot_id:
            self._storage.record_boot(boot_id, host_boot_ts, now, clean_shutdown)

        # Record where we are now before anything else can go wrong.
        self._state.update(boot_id=boot_id, host_boot_ts=host_boot_ts, started_ts=now)
        self._state.clear_shutdown_marker()

        verdict = self._classify(
            now=now,
            uptime=uptime,
            boot_id=boot_id,
            previous_boot_id=previous_boot_id,
            last_heartbeat=last_heartbeat,
            clean_shutdown=clean_shutdown,
        )

        if verdict["classification"] in ("first_run", "clean_restart"):
            _LOGGER.info("Boot forensics: %s", verdict["summary"])
            self._storage.add_event(
                kind="boot",
                message=verdict["summary"],
                severity="info",
                source="forensics",
                detail=verdict,
                ts=now,
            )
            return verdict

        await self._gather_evidence(verdict, previous_boot_id)
        self._record_incident(verdict, now)
        _LOGGER.warning("Boot forensics: %s", verdict["summary"])
        return verdict

    # -------------------------------------------------------- classification

    def _classify(
        self,
        *,
        now: int,
        uptime: float,
        boot_id: str | None,
        previous_boot_id: str | None,
        last_heartbeat: Any,
        clean_shutdown: bool,
    ) -> dict[str, Any]:
        verdict: dict[str, Any] = {
            "boot_id": boot_id,
            "previous_boot_id": previous_boot_id,
            "host_uptime_seconds": uptime,
            "detected_ts": now,
            "last_heartbeat_ts": last_heartbeat,
            "evidence": [],
            "logs_captured": [],
        }

        if not last_heartbeat:
            verdict.update(
                classification="first_run",
                summary=CLASSIFICATIONS["first_run"],
                gap_seconds=0,
            )
            return verdict

        gap = max(now - int(last_heartbeat), 0)
        verdict["gap_seconds"] = gap
        verdict["gap_human"] = human_duration(gap)
        verdict["last_seen_local"] = _local(last_heartbeat)

        if clean_shutdown:
            verdict.update(
                classification="clean_restart",
                summary=(
                    f"{CLASSIFICATIONS['clean_restart']} Down for "
                    f"{human_duration(gap)}."
                ),
            )
            return verdict

        host_rebooted = bool(
            boot_id and previous_boot_id and boot_id != previous_boot_id
        )
        verdict["host_rebooted"] = host_rebooted

        if not host_rebooted:
            # The host never went away, so whatever happened was above it.
            verdict.update(
                classification="addon_restart",
                summary=(
                    f"The add-on stopped without a clean shutdown for "
                    f"{human_duration(gap)}, but the host stayed up "
                    f"(uptime {human_duration(uptime)}). Either the add-on was "
                    "restarted, or it was killed."
                ),
            )
            return verdict

        # Host rebooted. Whether it was asked to is decided by the evidence,
        # which _gather_evidence fills in; assume the worse case until proven.
        verdict.update(
            classification="host_power_loss",
            summary=(
                f"The host rebooted after being unreachable for "
                f"{human_duration(gap)}. Checking the previous boot's journal "
                "for a shutdown sequence."
            ),
        )
        return verdict

    # ------------------------------------------------------------- evidence

    async def _gather_evidence(
        self, verdict: dict[str, Any], previous_boot_id: str | None
    ) -> None:
        logs: dict[str, str] = {}

        if verdict.get("host_rebooted"):
            previous_journal = await self._previous_boot_journal(previous_boot_id)
            if previous_journal:
                logs["previous-boot-journal.log"] = previous_journal
                findings = host_events.scan_text(previous_journal)
                verdict["evidence"].extend(findings)

                if _looks_like_ordered_shutdown(previous_journal):
                    verdict["classification"] = "host_reboot"
                    verdict["summary"] = (
                        f"{CLASSIFICATIONS['host_reboot']} The previous boot's "
                        "journal contains a normal shutdown sequence, so this "
                        "was not a power loss."
                    )
                else:
                    verdict["summary"] = (
                        f"{CLASSIFICATIONS['host_power_loss']} The previous "
                        "boot's journal has no shutdown sequence — it simply "
                        "stops."
                    )

                headline = host_events.summarise(findings)
                if headline:
                    verdict["summary"] += f" Kernel evidence: {headline}"
            else:
                verdict["summary"] += (
                    " The previous boot's journal could not be retrieved, so the "
                    "cause of the reboot is unconfirmed."
                )

        core_log = await self._client.core_log(lines=800)
        if core_log:
            logs["core.log"] = core_log
        supervisor_log = await self._client.supervisor_log(lines=800)
        if supervisor_log:
            logs["supervisor.log"] = supervisor_log

        verdict["logs_captured"] = sorted(logs)
        verdict["_logs"] = logs

        files: dict[str, str] = {}
        if os.path.exists(HA_LOG_PREVIOUS):
            # HA rotates the previous run's log here on restart. Grab it before
            # the next restart overwrites it.
            files["home-assistant.log.1"] = HA_LOG_PREVIOUS
            verdict["logs_captured"].append("home-assistant.log.1")
        verdict["_files"] = files

    async def _previous_boot_journal(self, previous_boot_id: str | None) -> str:
        """Fetch the journal of the boot that ended, by id or by offset."""
        candidates: list[str] = []
        if previous_boot_id:
            candidates.append(previous_boot_id)
        # Offset -1 is "the boot before this one" and works even when we never
        # recorded an id (for example after the add-on was reinstalled).
        candidates.append("-1")

        for candidate in candidates:
            text = await self._client.boot_log(candidate, lines=3000)
            if text.strip():
                _LOGGER.info("Retrieved previous boot journal via '%s'", candidate)
                return text

        boots = await self._client.boots()
        if boots:
            _LOGGER.debug("Available boots: %s", boots)
        return ""

    # ------------------------------------------------------------- recording

    def _record_incident(self, verdict: dict[str, Any], now: int) -> None:
        started = int(verdict.get("last_heartbeat_ts") or now)
        logs = verdict.pop("_logs", {})
        files = verdict.pop("_files", {})

        severity = (
            "critical"
            if verdict["classification"] in ("host_power_loss", "core_only")
            else "warning"
        )
        self._storage.add_event(
            kind="boot",
            message=verdict["summary"],
            severity=severity,
            source="forensics",
            detail={k: v for k, v in verdict.items() if not k.startswith("_")},
            ts=now,
        )

        incident_id = self._storage.open_incident(
            started_ts=started,
            classification=verdict["classification"],
            summary=verdict["summary"],
            detail=verdict,
        )

        summary_doc = dict(verdict)
        summary_doc.update(
            {
                "incident_id": incident_id,
                "started_local": _local(started),
                "ended_local": _local(now),
                "outage_human": verdict.get("gap_human", ""),
            }
        )

        # The metric window from before the gap is already on disk — it is the
        # tape the flight recorder wrote before it lost power.
        window = self._storage.window(started - self._config.ring_buffer_minutes * 60, now)
        events = self._storage.query(
            "SELECT * FROM events WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (started - self._config.ring_buffer_minutes * 60, now),
        )

        path = bundle.build(
            BUNDLE_DIR,
            incident_id,
            summary=summary_doc,
            metrics=window,
            events=events,
            logs=logs,
            files=files,
        )
        bundle.prune(BUNDLE_DIR)

        self._storage.close_incident(
            incident_id,
            ended_ts=now,
            bundle_path=path,
        )
        verdict["incident_id"] = incident_id
        verdict["bundle_path"] = path


def _looks_like_ordered_shutdown(journal: str) -> bool:
    """Did the previous boot end with someone asking it to stop?

    Only the tail matters — a shutdown marker from an earlier point in that
    boot's life says nothing about how it ended.
    """
    tail = "\n".join(journal.splitlines()[-400:])
    return any(marker.lower() in tail.lower() for marker in _SHUTDOWN_MARKERS)
