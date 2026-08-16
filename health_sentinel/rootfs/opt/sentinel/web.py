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
            aioweb.get("/api/recorder", handle_recorder),
            aioweb.get("/api/logs", handle_logs),
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


async def handle_test_alert(request: aioweb.Request) -> aioweb.Response:
    sentinel: "Sentinel" = request.app["sentinel"]
    return _json(await sentinel.notifier.test())


async def handle_index(request: aioweb.Request) -> aioweb.StreamResponse:
    return aioweb.FileResponse(os.path.join(WWW_DIR, "index.html"))
