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
from typing import Any, Awaitable, Callable

import aiohttp

from collectors.availability import AvailabilityTracker, parse_iso
from collectors.supervisor_api import SupervisorClient

_LOGGER = logging.getLogger(__name__)

ClusterCallback = Callable[[dict[str, Any]], Awaitable[None]]

_WS_PATH = "/core/websocket"
_RECONNECT_DELAY = 15
_MAX_RECONNECT_DELAY = 300
# Ranking is only meaningful over a decent window; an hour matches how people
# think about "changes per hour".
_WINDOW_SECONDS = 3600


class RecorderWatcher:
    """Counts state changes per entity and tracks recorder database size."""

    def __init__(
        self,
        client: SupervisorClient,
        token: str,
        availability: "AvailabilityTracker | None" = None,
        on_cluster: ClusterCallback | None = None,
    ) -> None:
        self._client = client
        self._token = token
        self._ws_url = client.base.replace("http://", "ws://") + _WS_PATH
        self._availability = availability
        self._on_cluster = on_cluster
        # Result messages carry only an id, so remember what each id asked for.
        self._pending: dict[int, str] = {}
        # system_health/info and subscribe_events are *subscriptions*, not
        # request/response: their confirming "result" carries no payload, and
        # the real data streams in afterward as "event" messages reusing the
        # same id — exactly like subscribe_events -> state_changed. Without
        # this map, every incoming "event" was routed to the state_changed
        # handler unconditionally, so system_health's actual data (arriving
        # as its own "event") was silently discarded no matter how the
        # "result" message itself was parsed.
        self._subscriptions: dict[int, str] = {}
        self._registry_available: bool | None = None

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
        # Old subscription ids meant nothing to a fresh connection anyway, but
        # a stale entry (e.g. from a health request whose "finish" never
        # arrived before a reconnect) would otherwise sit there forever.
        self._subscriptions = {}

        async with self._client.session.ws_connect(
            self._ws_url, timeout=timeout, heartbeat=30
        ) as ws:
            if not await self._authenticate(ws):
                return
            self._connected = True
            _LOGGER.info("Recorder watcher connected to Core event bus")

            await self._send(
                ws, {"type": "subscribe_events", "event_type": "state_changed"},
                "subscribe",
            )
            if self._availability is not None:
                # Registry gives entity -> integration; get_states gives the
                # current value and, crucially, last_changed — so chronic vs
                # blip is correct immediately after a restart rather than
                # treating every long-dead entity as freshly broken.
                await self._send(
                    ws, {"type": "config/entity_registry/list"}, "registry"
                )
                await self._send(ws, {"type": "get_states"}, "states")
            await self._request_db_size(ws)

            last_health = time.time()
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    break
                await self._handle(ws, json.loads(message.data))

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
        self,
        ws: aiohttp.ClientWebSocketResponse,
        payload: dict[str, Any],
        kind: str,
    ) -> int:
        self._msg_id += 1
        payload["id"] = self._msg_id
        self._pending[self._msg_id] = kind
        await ws.send_json(payload)
        return self._msg_id

    async def _request_db_size(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await self._send(ws, {"type": "system_health/info"}, "health")

    # -------------------------------------------------------------- handling

    async def _handle(
        self, ws: aiohttp.ClientWebSocketResponse, message: dict[str, Any]
    ) -> None:
        kind = message.get("type")
        msg_id = message.get("id")

        if kind == "event":
            # subscribe_events and system_health/info are both subscriptions:
            # their confirming "result" is empty, and the real payload streams
            # in afterward as "event" messages that reuse the request's id.
            # Route by which subscription that id belongs to — an id this
            # connection never subscribed under (e.g. left over after a
            # reconnect) is simply ignored.
            sub_kind = self._subscriptions.get(msg_id)
            if sub_kind == "state_changed":
                await self._handle_event(message.get("event") or {})
            elif sub_kind == "health":
                await self._handle_health_event(ws, msg_id, message.get("event") or {})
            return

        if kind != "result":
            return

        what = self._pending.pop(msg_id, None)
        if not message.get("success"):
            if what == "registry":
                # Registry commands may require admin. Not fatal — main.py has a
                # template-based fallback that needs no elevated rights.
                self._registry_available = False
                _LOGGER.info(
                    "Entity registry not available to this token (%s); "
                    "integration mapping will use the template fallback",
                    (message.get("error") or {}).get("code", "unknown"),
                )
            elif what is not None:
                # health/states/subscribe failing wholesale — log every case,
                # not just the one we happen to have a fallback for.
                _LOGGER.warning(
                    "Websocket request %r failed: %s",
                    what,
                    message.get("error"),
                )
            return

        # subscribe_events and system_health/info confirm with an EMPTY
        # result — send_result(msg["id"]) with no second argument on the HA
        # side. There is nothing to parse here; the id just becomes a live
        # subscription that future "event" messages will be routed through.
        if what in ("subscribe", "health"):
            self._subscriptions[msg_id] = (
                "state_changed" if what == "subscribe" else "health"
            )
            return

        result = message.get("result")
        if what == "registry":
            self._handle_registry(result or [])
        elif what == "states":
            self._handle_states(result or [])

    async def _handle_health_event(
        self, ws: aiohttp.ClientWebSocketResponse, msg_id: int, event: dict[str, Any]
    ) -> None:
        """One message from the system_health/info subscription's stream.

        Sequence per subscription: exactly one "initial" event carrying every
        domain's synchronously-available data (recorder's is — it is a plain
        dict, never a coroutine, so it is always present here, never only in
        a later "update"), zero or more "update" events as slow domains like
        solcast_solar or hacs finish their own network checks, then "finish".
        """
        etype = event.get("type")
        if etype == "initial":
            self._handle_system_health(event)
        elif etype == "finish":
            # A fresh subscription id is requested every _request_db_size()
            # call (every 300s in the read loop); leaving each one open on
            # Core's side after we're done with it would leak subscriptions
            # over a long-running connection.
            self._subscriptions.pop(msg_id, None)
            await self._send(
                ws, {"type": "unsubscribe_events", "subscription": msg_id}, "unsub"
            )
        # "update" events (a slow domain's own check finishing) carry nothing
        # about the recorder, which is never one of those — nothing to do.

    def _handle_registry(self, entries: list[dict[str, Any]]) -> None:
        if self._availability is None:
            return
        mapping = {
            entry["entity_id"]: entry["platform"]
            for entry in entries
            if entry.get("entity_id") and entry.get("platform")
        }
        if not mapping:
            self._registry_available = False
            return
        self._registry_available = True
        self._availability.set_mapping(mapping, "entity_registry")

    def _handle_states(self, states: list[dict[str, Any]]) -> None:
        """Seed current availability from a full state snapshot."""
        if self._availability is None:
            return
        now = int(time.time())
        seeded = 0
        for state in states:
            entity_id = state.get("entity_id")
            value = state.get("state")
            if not entity_id or value is None:
                continue
            since = now
            changed = state.get("last_changed")
            if changed:
                parsed = parse_iso(changed)
                if parsed:
                    since = parsed
            self._availability.seed(entity_id, value, since)
            seeded += 1
        _LOGGER.info("Seeded availability state for %d entities", seeded)

    @property
    def registry_available(self) -> bool | None:
        """True/False once known, None while still unanswered."""
        return self._registry_available

    async def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("event_type") != "state_changed":
            return
        data = event.get("data") or {}
        entity_id = data.get("entity_id")
        if not entity_id:
            return
        self._counts[entity_id] += 1
        self._total_changes += 1

        if self._availability is None:
            return

        new_state = (data.get("new_state") or {}).get("state")
        if new_state is None:
            # Entity removed rather than changed.
            return
        old_state = (data.get("old_state") or {}).get("state")

        if self._availability.observe(entity_id, new_state, old_state) is None:
            return
        cluster = self._availability.check_cluster()
        if cluster and self._on_cluster:
            await self._on_cluster(cluster)

    def _handle_system_health(self, result: dict[str, Any]) -> None:
        """Parse a system_health/info result.

        The WS command does not return the component dicts at the top level —
        it wraps them as {"type": "initial", "data": {<component>: {...}}},
        with slower components filled in later via separate update events. Every
        branch here logs on the way out, because the previous version of this
        function read `result["recorder"]` directly (always absent, since the
        real key is `result["data"]["recorder"]`) and returned silently on
        every call. Stable connection, no exceptions, size forever null — a
        wrong-shape bug is otherwise indistinguishable from no bug at all.
        """
        data = result.get("data")
        if not isinstance(data, dict):
            # Older/newer HA could plausibly flatten this; fall back to the
            # unwrapped shape rather than assume the current one is permanent.
            data = result if "recorder" in result else {}
            if not data:
                _LOGGER.warning(
                    "system_health/info result has neither 'data' nor "
                    "'recorder' at the top level; shape=%s",
                    sorted(result.keys()) if isinstance(result, dict) else type(result),
                )
                return

        recorder = (data.get("recorder") or {}).get("info")
        if recorder is None:
            _LOGGER.info(
                "system_health/info has no 'recorder' component yet "
                "(components so far: %s) — it may arrive in a later update",
                sorted(data.keys()),
            )
            return

        raw = recorder.get("estimated_db_size")
        if not raw:
            _LOGGER.info(
                "Recorder health has no estimated_db_size (keys: %s)",
                sorted(recorder.keys()),
            )
            return

        # Reported as a human string like "82363.42 MiB".
        try:
            number, _, unit = str(raw).strip().partition(" ")
            size = float(number)
        except ValueError:
            _LOGGER.warning("Could not parse estimated_db_size value: %r", raw)
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
        _LOGGER.info(
            "Recorder database size: %.1f MiB (engine: %s)",
            self._db_size_bytes / (1024**2),
            recorder.get("database_engine", "unknown"),
        )

    def _maybe_roll_window(self) -> None:
        if time.time() - self._window_started < _WINDOW_SECONDS:
            return
        self._last_window = self._counts.most_common(100)
        self._last_window_rate = self._total_changes / _WINDOW_SECONDS
        self._counts.clear()
        self._total_changes = 0
        self._window_started = time.time()
