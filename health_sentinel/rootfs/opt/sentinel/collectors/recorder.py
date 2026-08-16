"""Recorder pressure: database size and which entities are actually causing it.

Finding the entities that bloat the recorder normally means SQL access to the
recorder database, and an add-on has no credentials for the user's MariaDB. So
this measures the cause instead of the symptom: it subscribes to `state_changed`
on Core's event bus and counts changes per entity.

Every one of those events is a recorder write. Ranking entities by state-change
rate answers "what is filling my database" directly, live, and without touching
the database at all.

The WebSocket connection doubles as a second, independent liveness signal — if
Core dies, this socket drops, and it drops for a different reason than the HTTP
probe times out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from typing import Any

import aiohttp

from collectors.supervisor_api import SupervisorClient

_LOGGER = logging.getLogger(__name__)

_WS_PATH = "/core/websocket"
_RECONNECT_DELAY = 15
_MAX_RECONNECT_DELAY = 300
# Ranking is only meaningful over a decent window; an hour matches how people
# think about "changes per hour".
_WINDOW_SECONDS = 3600


class RecorderWatcher:
    """Counts state changes per entity and tracks recorder database size."""

    def __init__(self, client: SupervisorClient, token: str) -> None:
        self._client = client
        self._token = token
        self._ws_url = client.base.replace("http://", "ws://") + _WS_PATH

        self._counts: Counter[str] = Counter()
        self._window_started = time.time()
        self._total_changes = 0
        self._last_window: list[tuple[str, int]] = []
        self._last_window_rate = 0.0

        self._db_size_bytes: float | None = None
        self._connected = False
        self._msg_id = 0
        self._stop = asyncio.Event()

    # ------------------------------------------------------------- properties

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def db_size_bytes(self) -> float | None:
        return self._db_size_bytes

    def top_writers(self, limit: int = 25) -> list[dict[str, Any]]:
        """Highest state-change entities in the current or last full window."""
        elapsed = max(time.time() - self._window_started, 1.0)
        if self._counts:
            source = self._counts.most_common(limit)
            hours = elapsed / 3600.0
        else:
            source = self._last_window[:limit]
            hours = 1.0
        return [
            {
                "entity_id": entity_id,
                "changes": count,
                "per_hour": round(count / hours, 1) if hours > 0 else 0.0,
            }
            for entity_id, count in source
        ]

    def metrics(self) -> dict[str, float]:
        elapsed = max(time.time() - self._window_started, 1.0)
        out: dict[str, float] = {
            "recorder.ws_connected": 1.0 if self._connected else 0.0,
            "recorder.state_changes_per_min": (self._total_changes / elapsed) * 60.0,
        }
        if self._db_size_bytes is not None:
            out["recorder.db_size_bytes"] = self._db_size_bytes
        return out

    # ------------------------------------------------------------------ loop

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Stay connected to the event bus, reconnecting with backoff."""
        delay = _RECONNECT_DELAY
        while not self._stop.is_set():
            try:
                await self._session()
                delay = _RECONNECT_DELAY
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - never kill the watcher
                _LOGGER.debug("Recorder watcher disconnected: %s", err)
            finally:
                self._connected = False

            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, _MAX_RECONNECT_DELAY)

    async def _session(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        async with self._client.session.ws_connect(
            self._ws_url, timeout=timeout, heartbeat=30
        ) as ws:
            if not await self._authenticate(ws):
                return
            self._connected = True
            _LOGGER.info("Recorder watcher connected to Core event bus")

            await self._send(ws, {"type": "subscribe_events", "event_type": "state_changed"})
            await self._request_db_size(ws)

            last_health = time.time()
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    break
                self._handle(json.loads(message.data))

                self._maybe_roll_window()
                if time.time() - last_health > 300:
                    last_health = time.time()
                    await self._request_db_size(ws)

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        greeting = json.loads(await ws.receive_str())
        if greeting.get("type") != "auth_required":
            _LOGGER.warning("Unexpected websocket greeting: %s", greeting.get("type"))
            return False
        await ws.send_json({"type": "auth", "access_token": self._token})
        result = json.loads(await ws.receive_str())
        if result.get("type") != "auth_ok":
            _LOGGER.warning("Websocket auth rejected: %s", result)
            return False
        return True

    async def _send(
        self, ws: aiohttp.ClientWebSocketResponse, payload: dict[str, Any]
    ) -> int:
        self._msg_id += 1
        payload["id"] = self._msg_id
        await ws.send_json(payload)
        return self._msg_id

    async def _request_db_size(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await self._send(ws, {"type": "system_health/info"})

    # -------------------------------------------------------------- handling

    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "event":
            self._handle_event(message.get("event") or {})
        elif kind == "result" and message.get("success"):
            self._handle_system_health(message.get("result") or {})

    def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("event_type") != "state_changed":
            return
        entity_id = (event.get("data") or {}).get("entity_id")
        if not entity_id:
            return
        self._counts[entity_id] += 1
        self._total_changes += 1

    def _handle_system_health(self, result: dict[str, Any]) -> None:
        recorder = (result.get("recorder") or {}).get("info") or {}
        raw = recorder.get("estimated_db_size")
        if not raw:
            return
        # Reported as a human string like "82363.42 MiB".
        try:
            number, _, unit = str(raw).strip().partition(" ")
            size = float(number)
        except ValueError:
            return
        multiplier = {
            "kib": 1024,
            "mib": 1024**2,
            "gib": 1024**3,
            "kb": 1000,
            "mb": 1000**2,
            "gb": 1000**3,
        }.get(unit.strip().lower(), 1024**2)
        self._db_size_bytes = size * multiplier

    def _maybe_roll_window(self) -> None:
        if time.time() - self._window_started < _WINDOW_SECONDS:
            return
        self._last_window = self._counts.most_common(100)
        self._last_window_rate = self._total_changes / _WINDOW_SECONDS
        self._counts.clear()
        self._total_changes = 0
        self._window_started = time.time()
