"""Ingress dashboard and JSON API.

Served by the add-on itself rather than as a Lovelace dashboard, because it has
to stay usable exactly when Home Assistant is not — during a restart, a hang, or
a full outage. Nothing here talks to Core to render a page.

All assets are local. A dashboard that fetches a charting library from a CDN is
useless during the network-shaped outages it is supposed to help diagnose.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web as aioweb

from config import BUNDLE_DIR

if TYPE_CHECKING:
    from main import Sentinel

_LOGGER = logging.getLogger(__name__)

WWW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")

_LOG_SOURCES = {
    "core": "/core/logs",
    "supervisor": "/supervisor/logs",
    "host": "/host/logs",
}


def _json(data: Any, status: int = 200) -> aioweb.Response:
    return aioweb.json_response(data, status=status, dumps=_dumps)


def _dumps(data: Any) -> str:
    return json.dumps(data, default=str)


def create_app(sentinel: "Sentinel") -> aioweb.Application:
    app = aioweb.Application()
    app["sentinel"] = sentinel

    app.add_routes(
        [
            aioweb.get("/health", handle_health),
            aioweb.get("/api/status", handle_status),
            aioweb.get("/api/series", handle_series),
            aioweb.get("/api/events", handle_events),
            aioweb.get("/api/incidents", handle_incidents),
            aioweb.get("/api/incidents/{incident_id}", handle_incident),
            aioweb.get("/api/incidents/{incident_id}/bundle", handle_bundle),
            aioweb.get("/api/containers", handle_containers),
            aioweb.get("/api/host", handle_host),
            aioweb.get("/api/integrations", handle_integrations),
            aioweb.get("/api/recorder", handle_recorder),
            aioweb.get("/api/logs", handle_logs),
            aioweb.get("/api/logs/export", handle_logs_export),
            aioweb.post("/api/test-alert", handle_test_alert),
            aioweb.get("/", handle_index),
        ]
    )
    app.router.add_static("/static/", WWW_DIR, name="static")
    return app


# ----------------------------------------------------------------- endpoints


async def handle_health(request: aioweb.Request) -> aioweb.Response:
    """Backs the add-on's own Supervisor watchdog.

    Deliberately dumb: it proves the web server and event loop are alive. It
    must not depend on Core, or a Core outage would make the Supervisor restart
    the very thing recording the outage.
    """
    sentinel: "Sentinel" = request.app["sentinel"]
    return _json({"ok": True, "uptime": sentinel.uptime()})


async def handle_status(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    storage = sentinel.storage

    open_incidents = await asyncio.to_thread(storage.open_incidents)
    recent_events = await storage.aquery(
        "SELECT * FROM events ORDER BY ts DESC LIMIT 25"
    )

    return _json(
        {
            "now": int(time.time()),
            "sentinel_uptime": sentinel.uptime(),
            "sentinel_started": sentinel.started_ts,
            "core": sentinel.live.get("core", {}),
            "metrics": sentinel.live.get("metrics", {}),
            "detector": sentinel.detector.status(),
            "boot_verdict": sentinel.boot_verdict,
            "capabilities": sentinel.capabilities,
            "notifications": sentinel.notifier.describe(),
            "open_incidents": open_incidents,
            "recent_events": recent_events,
            "streams": {
                "host_journal": sentinel.journal.connected,
                "event_bus": sentinel.recorder.connected,
            },
        }
    )


async def handle_series(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    metrics = request.query.get("metric", "")
    hours = float(request.query.get("hours", "6"))
    since = int(time.time() - hours * 3600)

    out: dict[str, list] = {}
    for metric in [m for m in metrics.split(",") if m]:
        out[metric] = await asyncio.to_thread(sentinel.storage.series, metric, since)
    return _json({"since": since, "series": out})


async def handle_events(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    limit = min(int(request.query.get("limit", "200")), 1000)
    kind = request.query.get("kind")
    severity = request.query.get("severity")

    sql = "SELECT * FROM events WHERE 1=1"
    args: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if severity:
        sql += " AND severity = ?"
        args.append(severity)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)

    return _json({"events": await sentinel.storage.aquery(sql, args)})


async def handle_incidents(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    rows = await sentinel.storage.aquery(
        "SELECT id, started_ts, ended_ts, classification, summary, bundle_path "
        "FROM incidents ORDER BY started_ts DESC LIMIT 200"
    )
    for row in rows:
        if row.get("ended_ts"):
            row["duration_seconds"] = row["ended_ts"] - row["started_ts"]
    return _json({"incidents": rows})


async def handle_incident(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    incident_id = request.match_info["incident_id"]

    rows = await sentinel.storage.aquery(
        "SELECT * FROM incidents WHERE id = ?", (incident_id,)
    )
    if not rows:
        return _json({"error": "not found"}, status=404)

    incident = rows[0]
    if incident.get("detail"):
        try:
            incident["detail"] = json.loads(incident["detail"])
        except (TypeError, ValueError):
            pass

    started = incident["started_ts"]
    ended = incident.get("ended_ts") or int(time.time())
    padding = sentinel.config.ring_buffer_minutes * 60

    incident["events"] = await sentinel.storage.aquery(
        "SELECT * FROM events WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (started - padding, ended + 300),
    )
    incident["window"] = {"from": started - padding, "to": ended + 300}
    return _json(incident)


async def handle_bundle(request: aioweb.Request) -> aioweb.StreamResponse:
    sentinel: "Sentinel" = request.app["sentinel"]
    incident_id = request.match_info["incident_id"]

    rows = await sentinel.storage.aquery(
        "SELECT bundle_path FROM incidents WHERE id = ?", (incident_id,)
    )
    if not rows or not rows[0].get("bundle_path"):
        return _json({"error": "no bundle for this incident"}, status=404)

    path = rows[0]["bundle_path"]
    # Never serve a path that escaped the bundle directory.
    if os.path.commonpath([os.path.abspath(path), BUNDLE_DIR]) != BUNDLE_DIR:
        return _json({"error": "invalid bundle path"}, status=400)
    if not os.path.exists(path):
        return _json({"error": "bundle file is gone"}, status=404)

    return aioweb.FileResponse(
        path,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{os.path.basename(path)}"'
            )
        },
    )


async def handle_containers(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    since = int(time.time() - 6 * 3600)

    latest = await sentinel.storage.aquery(
        """SELECT cs.* FROM container_samples cs
           JOIN (SELECT slug, MAX(ts) AS ts FROM container_samples
                  WHERE ts >= ? GROUP BY slug) newest
             ON cs.slug = newest.slug AND cs.ts = newest.ts""",
        (since,),
    )

    restarts = sentinel.addons.restart_counts
    names = sentinel.addons.names
    for row in latest:
        slug = row["slug"]
        row["name"] = names.get(slug, slug)
        row["restarts"] = restarts.get(slug, 0)
        row["memory_slope_mb_per_hour"] = await asyncio.to_thread(
            sentinel.storage.memory_slope, slug, since
        )

    latest.sort(key=lambda r: r.get("mem_bytes") or 0, reverse=True)
    return _json({"containers": latest})


async def handle_host(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    kernel_events = await sentinel.storage.aquery(
        "SELECT * FROM events WHERE source IN ('host_journal', 'hardware') "
        "ORDER BY ts DESC LIMIT 100"
    )
    return _json(
        {
            "host": sentinel.live.get("host", {}),
            "os": sentinel.live.get("os", {}),
            "services": sentinel.live.get("services", []),
            "resolution": sentinel.live.get("resolution", {}),
            "devices": sentinel.hardware.devices,
            "serial_devices": sentinel.hardware.serial_devices,
            "kernel_events": kernel_events,
            "psi_available": sentinel.capabilities.get("proc_psi", False),
            "network": sentinel.network.state,
        }
    )


async def handle_integrations(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    clusters = await sentinel.storage.aquery(
        "SELECT * FROM events WHERE kind = 'multi_integration_outage' "
        "ORDER BY ts DESC LIMIT 50"
    )
    for row in clusters:
        if row.get("detail"):
            try:
                row["detail"] = json.loads(row["detail"])
            except (TypeError, ValueError):
                pass

    return _json(
        {
            "integrations": await asyncio.to_thread(
                sentinel.availability.integration_health
            ),
            "chronic": await asyncio.to_thread(
                sentinel.availability.chronic_entities
            ),
            "clusters": clusters,
            "mapping_source": sentinel.availability.mapping_source,
            "mapped_entities": sentinel.availability.mapped_entities,
            "chronic_after_minutes": sentinel.config.chronic_after_minutes,
        }
    )


async def handle_recorder(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    since = int(time.time() - 7 * 86400)
    return _json(
        {
            "db_size_bytes": sentinel.recorder.db_size_bytes,
            "connected": sentinel.recorder.connected,
            "top_writers": sentinel.recorder.top_writers(limit=40),
            "size_history": await asyncio.to_thread(
                sentinel.storage.series, "recorder.db_size_bytes", since
            ),
            "change_rate": await asyncio.to_thread(
                sentinel.storage.series, "recorder.state_changes_per_min", since
            ),
        }
    )


async def handle_logs(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    source = request.query.get("source", "core")
    lines = min(int(request.query.get("lines", "300")), 5000)
    search = request.query.get("search", "").strip()

    if source.startswith("addon:"):
        text = await sentinel.client.addon_log(source.split(":", 1)[1], lines)
    elif source in _LOG_SOURCES:
        text = await sentinel.client.try_get_text(_LOG_SOURCES[source], lines)
    else:
        return _json({"error": "unknown source"}, status=400)

    if search:
        needle = search.lower()
        text = "\n".join(
            line for line in text.splitlines() if needle in line.lower()
        )

    return _json({"source": source, "text": text})


async def handle_logs_export(request: aioweb.Request) -> aioweb.Response:
    """Download logs as one plain-text file, ready to hand to an AI.

    Plain text rather than the tar.gz incident bundle on purpose: a single file
    can be uploaded or pasted straight into a chat, with no extraction step.

    `full=1` gathers every source plus the system context an analyst would
    otherwise have to ask for — versions, current metrics, incident history and
    the sentinel's own classified kernel events. Raw logs alone lack the context
    to interpret them.
    """
    sentinel: "Sentinel" = request.app["sentinel"]
    source = request.query.get("source", "core")
    full = request.query.get("full", "") in ("1", "true", "yes")
    lines = min(int(request.query.get("lines", "2000")), 20000)
    search = request.query.get("search", "").strip()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    if full:
        text = await _build_full_export(sentinel, lines)
        filename = f"ha-diagnostic-{stamp}.txt"
    else:
        text = await _build_single_export(sentinel, source, lines, search)
        filename = f"ha-{source.replace(':', '-')}-{stamp}.log"

    return aioweb.Response(
        text=text,
        content_type="text/plain",
        charset="utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _section(title: str) -> str:
    return f"\n\n{'=' * 78}\n== {title}\n{'=' * 78}\n"


async def _fetch_log(sentinel: "Sentinel", source: str, lines: int) -> str:
    if source.startswith("addon:"):
        return await sentinel.client.addon_log(source.split(":", 1)[1], lines)
    if source in _LOG_SOURCES:
        return await sentinel.client.try_get_text(_LOG_SOURCES[source], lines)
    return ""


async def _build_single_export(
    sentinel: "Sentinel", source: str, lines: int, search: str
) -> str:
    text = await _fetch_log(sentinel, source, lines)
    if search:
        needle = search.lower()
        text = "\n".join(l for l in text.splitlines() if needle in l.lower())

    header = [
        f"# Home Assistant log export — {source}",
        f"# Generated {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"# Last {lines} lines" + (f", filtered by '{search}'" if search else ""),
        "",
    ]
    return "\n".join(header) + text


async def _build_full_export(sentinel: "Sentinel", lines: int) -> str:
    now = int(time.time())
    out: list[str] = []

    out.append(
        "HOME ASSISTANT DIAGNOSTIC EXPORT\n"
        f"Generated {time.strftime('%Y-%m-%d %H:%M:%S %z')} by Health Sentinel\n"
        "\n"
        "This file is intended to be read by a person or an AI assistant to\n"
        "diagnose instability. Suggested order of reading:\n"
        "\n"
        "  1. INCIDENTS      - what already went wrong, and the verdict reached\n"
        "  2. KERNEL EVENTS  - OOM kills, hardware errors, USB drops, disk faults.\n"
        "                      A kernel OOM kill here explains a Core death that\n"
        "                      the Core log itself will show no reason for.\n"
        "  3. INTEGRATIONS   - which integrations are degraded, and whether any\n"
        "                      went offline together. Several unrelated ones\n"
        "                      failing within a couple of minutes points at a\n"
        "                      shared cause (network, DNS, power), not at any\n"
        "                      one device.\n"
        "  4. NETWORK        - link state, addresses and connectivity, to check\n"
        "                      against the timing of any integration outage\n"
        "  5. SYSTEM         - versions, memory, disk, pressure at export time\n"
        "  6. CONTAINERS     - which add-on is consuming or leaking memory\n"
        "  7. LOGS           - Core, Supervisor and host journal\n"
        "\n"
        "Note on ordering: Core's log ends when Core dies, so the cause of a hard\n"
        "crash is usually NOT in it. Check the host journal and kernel events for\n"
        "the same timestamp."
    )

    # ---- system ----------------------------------------------------------
    host = sentinel.live.get("host", {}) or {}
    os_info = sentinel.live.get("os", {}) or {}
    metrics = sentinel.live.get("metrics", {}) or {}
    core = sentinel.live.get("core", {}) or {}

    out.append(_section("SYSTEM"))
    facts = [
        ("Hostname", host.get("hostname")),
        ("Operating system", host.get("operating_system")),
        ("HAOS version", os_info.get("version")),
        ("Boot slot", os_info.get("boot")),
        ("Board", os_info.get("board")),
        ("Kernel", host.get("kernel")),
        ("Disk used / total (GB)", f"{host.get('disk_used')} / {host.get('disk_total')}"),
        ("Disk lifetime used (%)", host.get("disk_life_time")),
        ("Host uptime (s)", metrics.get("host.uptime_seconds")),
        ("Core reachable", core.get("reachable")),
        ("Core latency (ms)", core.get("latency_ms")),
        ("Sentinel uptime", sentinel.uptime()),
        ("PSI available", sentinel.capabilities.get("proc_psi")),
    ]
    for label, value in facts:
        if value not in (None, ""):
            out.append(f"{label:<26} {value}")

    out.append("\nCurrent metrics:")
    for key in sorted(metrics):
        out.append(f"  {key:<40} {metrics[key]}")

    # ---- incidents -------------------------------------------------------
    out.append(_section("INCIDENTS (most recent first)"))
    incidents = await sentinel.storage.aquery(
        "SELECT id, started_ts, ended_ts, classification, summary FROM incidents "
        "ORDER BY started_ts DESC LIMIT 30"
    )
    if not incidents:
        out.append("None recorded.")
    for incident in incidents:
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(incident["started_ts"]))
        duration = (
            f"{incident['ended_ts'] - incident['started_ts']}s"
            if incident.get("ended_ts")
            else "ONGOING"
        )
        out.append(
            f"\n#{incident['id']}  {started}  [{incident['classification']}]  "
            f"duration={duration}\n    {incident['summary']}"
        )

    # ---- classified events ----------------------------------------------
    out.append(_section("KERNEL, HARDWARE AND ADD-ON EVENTS (most recent first)"))
    events = await sentinel.storage.aquery(
        "SELECT ts, kind, severity, source, message FROM events "
        "WHERE severity IN ('critical', 'error', 'warning') "
        "ORDER BY ts DESC LIMIT 400"
    )
    if not events:
        out.append("None recorded.")
    for event in events:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["ts"]))
        out.append(
            f"{when}  {event['severity'].upper():<8} {event['kind']:<18} "
            f"{event['message']}"
        )

    # ---- integrations ----------------------------------------------------
    out.append(_section("INTEGRATION HEALTH"))
    health = await asyncio.to_thread(sentinel.availability.integration_health)
    if not health:
        out.append(
            "No integration mapping available — per-integration health could "
            "not be determined."
        )
    else:
        out.append(
            f"Mapping source: {sentinel.availability.mapping_source} "
            f"({sentinel.availability.mapped_entities} entities)\n"
        )
        out.append(
            f"{'integration':<26}{'total':>8}{'unavail':>9}{'chronic':>9}{'dead %':>9}"
        )
        for row in health:
            out.append(
                f"{row['integration'][:25]:<26}{row['total']:>8}"
                f"{row['unavailable']:>9}{row['chronic']:>9}"
                f"{row['unavailable_pct']:>9.1f}"
            )
        out.append(
            "\n'chronic' means unavailable for longer than "
            f"{sentinel.config.chronic_after_minutes} minutes — a standing "
            "problem rather than part of a sudden outage."
        )

    clusters = await sentinel.storage.aquery(
        "SELECT ts, message FROM events WHERE kind = 'multi_integration_outage' "
        "ORDER BY ts DESC LIMIT 20"
    )
    out.append("\nMulti-integration outages:")
    if not clusters:
        out.append("  None detected.")
    for row in clusters:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"]))
        out.append(f"  {when}  {row['message']}")

    # ---- network ---------------------------------------------------------
    out.append(_section("NETWORK"))
    net = sentinel.network.state
    out.append(f"Host internet:       {net.get('host_internet')}")
    out.append(f"Supervisor internet: {net.get('supervisor_internet')}\n")
    out.append(
        f"{'interface':<14}{'type':<12}{'connected':>10}{'primary':>9}  "
        f"{'address':<20}{'gateway':<16}"
    )
    for interface in net.get("interfaces") or []:
        out.append(
            f"{str(interface.get('interface'))[:13]:<14}"
            f"{str(interface.get('type'))[:11]:<12}"
            f"{str(interface.get('connected')):>10}"
            f"{str(interface.get('primary')):>9}  "
            f"{str(interface.get('address') or '-'):<20}"
            f"{str(interface.get('gateway') or '-'):<16}"
        )
    out.append(
        "\nTraffic counters are not available: they are namespaced per "
        "container, and reading the host's would require privileges this "
        "add-on deliberately does not take."
    )

    # ---- containers ------------------------------------------------------
    out.append(_section("CONTAINER RESOURCE USE"))
    since = now - 6 * 3600
    containers = await sentinel.storage.aquery(
        """SELECT cs.* FROM container_samples cs
           JOIN (SELECT slug, MAX(ts) AS ts FROM container_samples
                  WHERE ts >= ? GROUP BY slug) newest
             ON cs.slug = newest.slug AND cs.ts = newest.ts""",
        (since,),
    )
    out.append(
        f"{'add-on':<34}{'cpu%':>8}{'memory MB':>12}{'mem%':>8}{'MB/h':>10}"
        f"{'restarts':>10}"
    )
    restarts = sentinel.addons.restart_counts
    for row in sorted(containers, key=lambda r: r.get("mem_bytes") or 0, reverse=True):
        slope = await asyncio.to_thread(
            sentinel.storage.memory_slope, row["slug"], since
        )
        megabytes = (row.get("mem_bytes") or 0) / (1024 * 1024)
        out.append(
            f"{sentinel.addons.name_for(row['slug'])[:33]:<34}"
            f"{row.get('cpu') or 0:>8.2f}{megabytes:>12.1f}"
            f"{row.get('mem_percent') or 0:>8.1f}"
            f"{(slope if slope is not None else 0):>10.1f}"
            f"{restarts.get(row['slug'], 0):>10}"
        )

    # ---- logs ------------------------------------------------------------
    for source, label in (
        ("core", "HOME ASSISTANT CORE LOG"),
        ("supervisor", "SUPERVISOR LOG"),
        ("host", "HOST JOURNAL"),
    ):
        out.append(_section(f"{label} (last {lines} lines)"))
        text = await _fetch_log(sentinel, source, lines)
        out.append(text.strip() or "(empty or unavailable)")

    # Only add-ons that are currently unhealthy — including all 30-odd logs
    # would bury the signal and make the file unusable.
    unhealthy = [
        addon
        for addon in sentinel.addons.snapshot()
        if addon["state"] not in ("started", "stopped")
    ]
    for addon in unhealthy[:5]:
        out.append(_section(f"ADD-ON LOG: {addon['name']} (state={addon['state']})"))
        text = await _fetch_log(sentinel, f"addon:{addon['slug']}", 300)
        out.append(text.strip() or "(empty or unavailable)")

    return "\n".join(out) + "\n"


async def handle_test_alert(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    return _json(await sentinel.notifier.test())


async def handle_index(request: aioweb.Request) -> aioweb.StreamResponse:
    return aioweb.FileResponse(os.path.join(WWW_DIR, "index.html"))
