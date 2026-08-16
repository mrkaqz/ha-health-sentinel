"""Health Sentinel entrypoint.

Runs several loops at different cadences, plus two long-lived streams:

  probe      every  5 s   Core liveness and latency — the crash detector
  sample     every 15 s   host PSI/memory/thermal and per-container stats
  slow       every  5 m   disk, hardware inventory, add-on states, entity census
  maintain   every 15 m   rollup and retention
  journal    streaming    host kernel events
  eventbus   streaming    Core state_changed, for recorder pressure

Each loop is independently supervised: one failing must never take the others
down, because the whole point is to still be watching when things go wrong.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any

from aiohttp import web as aioweb

import web
from collectors import host_psi
from collectors.addons import AddonTracker, classify_stop
from collectors.availability import AvailabilityTracker
from collectors.core_probe import CoreProbe
from collectors.hardware import HardwareWatcher
from collectors.host_events import HostEventTail
from collectors.network import NetworkWatcher
from collectors.recorder import RecorderWatcher
from collectors.supervisor_api import SupervisorClient, container_stats_to_row
from config import DB_PATH, STATE_PATH, WEB_PORT, Config, setup_logging
from detector import IncidentDetector
from forensics import BootForensics, human_duration
from notify import Notifier
from state import StateFile
from storage import Storage

_LOGGER = logging.getLogger("sentinel")

_HEARTBEAT_INTERVAL = 30
_MAINTENANCE_INTERVAL = 900


class Sentinel:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.storage = Storage(DB_PATH)
        self.state = StateFile(STATE_PATH)
        self.client = SupervisorClient(config.supervisor_token)
        self.notifier = Notifier(config, self.storage)

        self.addons = AddonTracker(self.client)
        self.hardware = HardwareWatcher(self.client)
        self.network = NetworkWatcher(self.client)
        self.probe = CoreProbe(self.client)
        self.availability = AvailabilityTracker(
            chronic_after_minutes=config.chronic_after_minutes,
            cluster_window_seconds=config.cluster_window_seconds,
            cluster_min_integrations=config.cluster_min_integrations,
            cluster_min_entities=config.cluster_min_entities,
        )
        # One websocket serves both recorder ranking and availability tracking.
        self.recorder = RecorderWatcher(
            self.client,
            config.supervisor_token,
            availability=self.availability,
            on_cluster=self._on_integration_cluster,
        )
        self.detector = IncidentDetector(
            self.storage, config, self.client, self.addons, self.notifier
        )
        self.journal = HostEventTail(self.client, self._on_host_event)

        self.started_ts = int(time.time())
        self.capabilities: dict[str, Any] = {}
        self.boot_verdict: dict[str, Any] = {}
        self.live: dict[str, Any] = {}

        self._last_heartbeat = 0.0
        self._stop = asyncio.Event()
        self._runner: aioweb.AppRunner | None = None

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        _LOGGER.info("Health Sentinel starting")

        self.capabilities = await self.client.probe_capabilities()
        self.capabilities.update(
            {f"proc_{k}": v for k, v in host_psi.describe_capabilities().items()}
        )
        if not host_psi.psi_available():
            _LOGGER.warning(
                "PSI is unavailable on this kernel; pressure metrics will be "
                "missing but everything else still works"
            )

        # Prime trackers so the first real poll reports changes, not everything.
        await self.addons.poll_states()
        await self.hardware.poll()
        await self.network.poll()
        self._restore_availability()

        await self._run_forensics()
        await self._start_web()

        tasks = [
            asyncio.create_task(self._supervise("probe", self._probe_loop)),
            asyncio.create_task(self._supervise("sample", self._sample_loop)),
            asyncio.create_task(self._supervise("slow", self._slow_loop)),
            asyncio.create_task(self._supervise("maintain", self._maintenance_loop)),
            asyncio.create_task(self._supervise("journal", self.journal.run)),
            asyncio.create_task(self._supervise("eventbus", self.recorder.run)),
            asyncio.create_task(self._supervise("mapping", self._mapping_loop)),
        ]

        await self._stop.wait()
        _LOGGER.info("Shutting down")

        self.journal.stop()
        self.recorder.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._shutdown()

    def request_stop(self) -> None:
        self._stop.set()

    async def _shutdown(self) -> None:
        # The marker is what tells the next run this was orderly rather than a
        # crash. Write it before anything else can fail.
        self.state.mark_clean_shutdown()

        if self._runner is not None:
            await self._runner.cleanup()
        await self.notifier.close()
        await self.client.close()
        self.storage.close()
        _LOGGER.info("Stopped cleanly")

    async def _supervise(self, name: str, coro_factory: Any) -> None:
        """Keep one loop alive; log and restart it if it ever raises."""
        while not self._stop.is_set():
            try:
                await coro_factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Loop '%s' crashed; restarting in 30s", name)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=30)
                    return
                except asyncio.TimeoutError:
                    continue

    # -------------------------------------------------------------- forensics

    async def _run_forensics(self) -> None:
        forensics = BootForensics(self.client, self.storage, self.state, self.config)
        try:
            self.boot_verdict = await forensics.analyse()
        except Exception:  # noqa: BLE001 - never block startup on forensics
            _LOGGER.exception("Boot forensics failed")
            self.boot_verdict = {"classification": "unknown", "summary": "n/a"}
            return

        classification = self.boot_verdict.get("classification")
        summary = self.boot_verdict.get("summary", "")
        if classification in ("first_run", "clean_restart"):
            return

        severity = "critical" if classification == "host_power_loss" else "warning"
        await self.notifier.alert(
            f"Home Assistant restarted — {classification.replace('_', ' ')}",
            summary,
            severity=severity,
        )

    # ------------------------------------------------------------------ loops

    async def _probe_loop(self) -> None:
        while not self._stop.is_set():
            result = await self.probe.probe()
            await self.detector.observe(result)
            await self.storage.awrite_samples(result.ts, result.metrics)

            self.live["core"] = {
                "reachable": result.reachable,
                "latency_ms": result.latency_ms,
                "supervisor_state": result.supervisor_state,
                "error": result.error,
                "ts": result.ts,
            }

            now = time.monotonic()
            if now - self._last_heartbeat >= _HEARTBEAT_INTERVAL:
                self._last_heartbeat = now
                # Fsynced, so it survives a power cut. This timestamp is what
                # the next boot uses as the moment of death.
                await asyncio.to_thread(
                    self.state.heartbeat, core_reachable=result.reachable
                )

            await self._sleep(self.config.probe_interval)

    async def _sample_loop(self) -> None:
        while not self._stop.is_set():
            ts = int(time.time())
            metrics: dict[str, float] = {}
            metrics.update(await asyncio.to_thread(host_psi.collect))
            metrics.update(self.recorder.metrics())
            metrics.update(self.hardware.metrics())
            metrics.update(self.addons.metrics())

            rows: list[dict[str, Any]] = []
            core_stats = await self.client.core_stats()
            if core_stats:
                rows.append(container_stats_to_row("homeassistant", core_stats))
                metrics["core.cpu_percent"] = core_stats.get("cpu_percent") or 0.0
                metrics["core.memory_bytes"] = core_stats.get("memory_usage") or 0.0
                metrics["core.memory_percent"] = core_stats.get("memory_percent") or 0.0

            supervisor_stats = await self.client.supervisor_stats()
            if supervisor_stats:
                rows.append(container_stats_to_row("supervisor", supervisor_stats))

            rows.extend(await self.addons.collect_stats())

            await self.storage.awrite_samples(ts, metrics)
            await self.storage.awrite_container_samples(ts, rows)

            self.live["metrics"] = metrics
            self.live["containers"] = rows
            self.live["ts"] = ts

            await self._check_thresholds(metrics)
            await self._sleep(self.config.sample_interval)

    async def _slow_loop(self) -> None:
        while not self._stop.is_set():
            ts = int(time.time())
            metrics: dict[str, float] = {}

            host_info = await self.client.host_info()
            if host_info:
                metrics.update(_disk_metrics(host_info))
                self.live["host"] = host_info

            os_info = await self.client.os_info()
            if os_info:
                await self._check_os_change(os_info)
                self.live["os"] = os_info

            resolution = await self.client.resolution_info()
            if resolution:
                issues = resolution.get("issues") or []
                metrics["supervisor.issues"] = float(len(issues))
                self.live["resolution"] = resolution

            services = await self.client.host_services()
            if services:
                self.live["services"] = services.get("services", [])

            metrics.update(await self.probe.entity_census())
            metrics["sentinel.db_size_bytes"] = float(
                await asyncio.to_thread(self.storage.db_size_bytes)
            )

            await self._poll_addon_states()
            await self._poll_hardware()
            await self._poll_network()

            metrics.update(self.network.metrics())
            metrics.update(self.availability.metrics())

            await self.storage.awrite_samples(ts, metrics)
            await asyncio.to_thread(
                self.storage.save_availability,
                self.availability.persistable_states(),
            )

            self.live.setdefault("metrics", {}).update(metrics)
            self.live["top_writers"] = self.recorder.top_writers()
            self.live["network"] = self.network.state
            self.live["integrations"] = self.availability.integration_health()

            await self._sleep(self.config.slow_interval)

    async def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(_MAINTENANCE_INTERVAL)
            if self._stop.is_set():
                return
            try:
                await self.storage.amaintain(
                    self.config.retention_raw_days,
                    self.config.retention_rollup_days,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Maintenance pass failed")

    async def _mapping_loop(self) -> None:
        """Keep the entity -> integration map fresh.

        The websocket asks for the entity registry on connect. If that command
        is refused — it may require admin, which the add-on's token might not
        have — this falls back to asking Core to resolve `integration_entities()`
        for every loaded integration, which needs no elevated rights.
        """
        while not self._stop.is_set():
            # Give the websocket a chance to answer before deciding to fall back.
            await self._sleep(45)
            if self._stop.is_set():
                return

            if self.recorder.registry_available is False:
                mapping = await self.probe.integration_entity_map()
                if mapping:
                    self.availability.set_mapping(mapping, "template_fallback")
                else:
                    _LOGGER.warning(
                        "Could not build an entity-to-integration map by either "
                        "route; per-integration health will be unavailable"
                    )
            await self._sleep(3600)

    def _restore_availability(self) -> None:
        """Reload dead-entity history so 'broken since' survives a restart."""
        try:
            rows = self.storage.load_availability()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not restore availability history")
            return
        for row in rows:
            self.availability.seed(
                row["entity_id"], row["state"], row.get("since_ts")
            )
        if rows:
            _LOGGER.info("Restored availability history for %d entities", len(rows))

    async def _on_integration_cluster(self, cluster: dict[str, Any]) -> None:
        """Several unrelated integrations dropped at once."""
        detail = dict(cluster)
        # Network state at the moment of the drop is the first thing anyone
        # will want to check, so capture it into the event rather than making
        # someone correlate two timelines by hand.
        detail["network"] = self.network.state

        await self.storage.aadd_event(
            "multi_integration_outage",
            cluster["summary"],
            "critical",
            "availability",
            detail,
            ts=cluster["ts"],
        )
        _LOGGER.error("Multi-integration outage: %s", cluster["summary"])

        lines = [cluster["summary"], ""]
        for integration, entities in cluster["detail"].items():
            lines.append(f"  {integration}: {len(entities)} entities")
        if self.network.state.get("host_internet") is False:
            lines.append("")
            lines.append("Host internet connectivity is also down.")

        await self.notifier.alert(
            "Multiple integrations went offline together",
            "\n".join(lines),
            severity="critical",
        )

    # --------------------------------------------------------------- handlers

    async def _on_host_event(
        self, kind: str, message: str, severity: str, detail: dict[str, Any]
    ) -> None:
        """A classified line arrived from the host journal."""
        await self.storage.aadd_event(
            kind, message, severity, "host_journal", detail
        )
        _LOGGER.log(
            logging.ERROR if severity == "critical" else logging.INFO,
            "Host event [%s] %s",
            kind,
            message[:200],
        )
        if severity == "critical":
            await self.notifier.alert(
                f"Kernel event: {kind.replace('_', ' ')}",
                f"{detail.get('explanation', '')}\n\n{message}",
                severity="critical",
            )

    async def _poll_addon_states(self) -> None:
        transitions = await self.addons.poll_states()
        if not transitions:
            return

        recent_oom = await self.storage.aquery(
            "SELECT ts, message FROM events WHERE kind IN ('oom', 'oom_victim') "
            "AND ts >= ? ORDER BY ts DESC LIMIT 20",
            (int(time.time()) - 300,),
        )

        for change in transitions:
            message = (
                f"Add-on {change['name']} went {change['from']} -> {change['to']}"
            )
            detail = dict(change)

            if change["to"] not in ("started",):
                classification, explanation = classify_stop(
                    change["slug"],
                    change["name"],
                    int(time.time()),
                    recent_oom,
                )
                detail["stop_classification"] = classification
                message = f"{message}. {explanation}"

            await self.storage.aadd_event(
                "addon_state", message, change["severity"], "supervisor", detail
            )

            if change["to"] == "error":
                await self.notifier.alert(
                    f"Add-on error: {change['name']}", message, severity="warning"
                )

    async def _poll_hardware(self) -> None:
        for change in await self.hardware.poll():
            message = (
                f"Device {change['change']}: {change['description']}"
            )
            await self.storage.aadd_event(
                f"device_{change['change']}",
                message,
                change["severity"],
                "hardware",
                change,
            )
            if change["severity"] == "critical":
                await self.notifier.alert(
                    "Serial device disappeared",
                    f"{message}\n\nThis is how a Zigbee or Z-Wave coordinator "
                    "drops off, and it will unavailable every device behind it.",
                    severity="critical",
                )

    async def _poll_network(self) -> None:
        for change in await self.network.poll():
            await self.storage.aadd_event(
                change["kind"],
                change["message"],
                change["severity"],
                "network",
                change.get("detail"),
            )
            if change["severity"] == "critical":
                await self.notifier.alert(
                    "Network link lost", change["message"], severity="critical"
                )

    async def _check_thresholds(self, metrics: dict[str, float]) -> None:
        memory_pct = metrics.get("host.mem.used_pct")
        if memory_pct is not None and memory_pct >= self.config.memory_warn_pct:
            await self.notifier.alert(
                "Host memory is nearly exhausted",
                f"Memory used is {memory_pct:.1f}% (threshold "
                f"{self.config.memory_warn_pct}%). An OOM kill may follow.",
                severity="warning",
            )

        # PSI full-memory stall means everything on the box is waiting on RAM.
        stalled = metrics.get("host.psi.memory.full.avg60")
        if stalled is not None and stalled >= 10.0:
            await self.notifier.alert(
                "Host is stalling on memory",
                f"Memory pressure (full, 60s) is {stalled:.1f}%. The machine is "
                "spending that share of its time waiting for RAM.",
                severity="critical",
            )

    async def _check_os_change(self, os_info: dict[str, Any]) -> None:
        for key, label in (("version", "HAOS version"), ("boot_slot", "boot slot")):
            value = os_info.get(key)
            if value is None:
                continue
            stored_key = f"os_{key}"
            previous = await asyncio.to_thread(self.storage.get_meta, stored_key)
            if previous is not None and previous != str(value):
                message = f"{label} changed from {previous} to {value}"
                await self.storage.aadd_event(
                    "os_change", message, "warning", "supervisor", {key: value}
                )
                _LOGGER.warning(message)
            if previous != str(value):
                await asyncio.to_thread(self.storage.set_meta, stored_key, str(value))

    # ------------------------------------------------------------------- misc

    async def _start_web(self) -> None:
        app = web.create_app(self)
        self._runner = aioweb.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = aioweb.TCPSite(self._runner, "0.0.0.0", WEB_PORT)
        await site.start()
        _LOGGER.info("Dashboard listening on port %d", WEB_PORT)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately on shutdown."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def uptime(self) -> str:
        return human_duration(time.time() - self.started_ts)


def _disk_metrics(host_info: dict[str, Any]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    total = host_info.get("disk_total")
    used = host_info.get("disk_used")
    free = host_info.get("disk_free")
    for key, value in (
        ("host.disk.total_gb", total),
        ("host.disk.used_gb", used),
        ("host.disk.free_gb", free),
    ):
        if isinstance(value, (int, float)):
            metrics[key] = float(value)
    if isinstance(total, (int, float)) and isinstance(free, (int, float)) and total:
        metrics["host.disk.free_pct"] = float(free) / float(total) * 100.0

    life = host_info.get("disk_life_time")
    if isinstance(life, (int, float)):
        metrics["host.disk.life_time_pct"] = float(life)
    return metrics


async def _main() -> None:
    config = Config.from_env()
    setup_logging(config)

    if not config.supervisor_token:
        _LOGGER.error(
            "No Supervisor token. The add-on needs hassio_api: true — this is a "
            "packaging problem, not a configuration one."
        )
        return

    sentinel = Sentinel(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, sentinel.request_stop)
        except NotImplementedError:
            pass

    await sentinel.run()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
