"""Async client for the Supervisor REST API.

Every endpoint used here sits behind ROLE_MANAGER in the Supervisor's security
middleware, which is why config.yaml asks for `hassio_role: manager` and nothing
more. If that ever changes upstream, `probe_capabilities()` will say so on
startup instead of the dashboard silently drawing empty charts.

JSON endpoints answer with an envelope: {"result": "ok", "data": {...}}.
Log endpoints answer with plain text.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

import aiohttp

from config import SUPERVISOR_API

_LOGGER = logging.getLogger(__name__)

_JSON_TIMEOUT = aiohttp.ClientTimeout(total=15)
_LOG_TIMEOUT = aiohttp.ClientTimeout(total=60)

# Checked once at startup so permission problems surface immediately.
_CAPABILITY_ENDPOINTS = {
    "core_info": "/core/info",
    "core_stats": "/core/stats",
    "supervisor_info": "/supervisor/info",
    "supervisor_stats": "/supervisor/stats",
    "addons": "/addons",
    "host_info": "/host/info",
    "os_info": "/os/info",
    "hardware_info": "/hardware/info",
    "host_services": "/host/services",
    "resolution_info": "/resolution/info",
    "host_log_boots": "/host/logs/boots",
}


class SupervisorError(RuntimeError):
    """Raised when the Supervisor answers with something unusable."""


class SupervisorClient:
    def __init__(self, token: str, base_url: str = SUPERVISOR_API) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "SupervisorClient":
        self._session = aiohttp.ClientSession(headers=self._headers)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def base(self) -> str:
        return self._base

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session

    # ------------------------------------------------------------------ core

    async def get(self, path: str) -> dict[str, Any]:
        """GET a JSON endpoint and unwrap the Supervisor envelope."""
        url = f"{self._base}{path}"
        async with self.session.get(url, timeout=_JSON_TIMEOUT) as response:
            if response.status != 200:
                raise SupervisorError(f"{path} returned HTTP {response.status}")
            payload = await response.json()
        if isinstance(payload, dict) and "data" in payload:
            data = payload.get("data")
            return data if isinstance(data, dict) else {"value": data}
        return payload if isinstance(payload, dict) else {"value": payload}

    async def try_get(self, path: str) -> dict[str, Any] | None:
        """Same as get() but returns None instead of raising."""
        try:
            return await self.get(path)
        except (SupervisorError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Supervisor GET %s failed: %s", path, err)
            return None

    async def get_text(self, path: str, lines: int | None = None) -> str:
        """GET a log endpoint as plain text."""
        url = f"{self._base}{path}"
        headers = {"Accept": "text/plain"}
        if lines:
            # The journal endpoints take a Range header; older builds and the
            # container-log endpoints take ?lines=. Send both — the one that
            # isn't understood is ignored.
            headers["Range"] = f"entries=:-{lines}:{lines}"
            url = f"{url}?lines={lines}"
        async with self.session.get(url, headers=headers, timeout=_LOG_TIMEOUT) as resp:
            if resp.status not in (200, 206):
                raise SupervisorError(f"{path} returned HTTP {resp.status}")
            return await resp.text()

    async def try_get_text(self, path: str, lines: int | None = None) -> str:
        try:
            return await self.get_text(path, lines)
        except (SupervisorError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Supervisor log GET %s failed: %s", path, err)
            return ""

    async def stream_lines(self, path: str) -> AsyncIterator[str]:
        """Stream a /follow log endpoint line by line until it ends."""
        url = f"{self._base}{path}"
        headers = {"Accept": "text/plain"}
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        async with self.session.get(url, headers=headers, timeout=timeout) as response:
            if response.status not in (200, 206):
                raise SupervisorError(f"{path} returned HTTP {response.status}")
            async for raw in response.content:
                if raw:
                    yield raw.decode("utf-8", errors="replace").rstrip("\n")

    # -------------------------------------------------------------- accessors

    async def core_info(self) -> dict[str, Any] | None:
        return await self.try_get("/core/info")

    async def core_stats(self) -> dict[str, Any] | None:
        return await self.try_get("/core/stats")

    async def supervisor_stats(self) -> dict[str, Any] | None:
        return await self.try_get("/supervisor/stats")

    async def host_info(self) -> dict[str, Any] | None:
        return await self.try_get("/host/info")

    async def os_info(self) -> dict[str, Any] | None:
        return await self.try_get("/os/info")

    async def hardware_info(self) -> dict[str, Any] | None:
        return await self.try_get("/hardware/info")

    async def host_services(self) -> dict[str, Any] | None:
        return await self.try_get("/host/services")

    async def resolution_info(self) -> dict[str, Any] | None:
        return await self.try_get("/resolution/info")

    async def addons(self) -> list[dict[str, Any]]:
        data = await self.try_get("/addons")
        if not data:
            return []
        return data.get("addons", []) or []

    async def addon_stats(self, slug: str) -> dict[str, Any] | None:
        return await self.try_get(f"/addons/{slug}/stats")

    async def boots(self) -> dict[str, Any] | None:
        """Map of boot offset -> boot id, newest offset is 0."""
        return await self.try_get("/host/logs/boots")

    async def boot_log(self, boot_id: str, lines: int = 2000) -> str:
        return await self.try_get_text(f"/host/logs/boots/{boot_id}", lines)

    async def core_log(self, lines: int = 500) -> str:
        return await self.try_get_text("/core/logs", lines)

    async def supervisor_log(self, lines: int = 500) -> str:
        return await self.try_get_text("/supervisor/logs", lines)

    async def addon_log(self, slug: str, lines: int = 200) -> str:
        return await self.try_get_text(f"/addons/{slug}/logs", lines)

    # ------------------------------------------------------------ diagnostics

    async def probe_capabilities(self) -> dict[str, bool]:
        """Hit every endpoint once so a 403 is loud, not silent."""
        results: dict[str, bool] = {}
        for name, path in _CAPABILITY_ENDPOINTS.items():
            try:
                await self.get(path)
                results[name] = True
            except Exception as err:  # noqa: BLE001 - report, never crash
                results[name] = False
                _LOGGER.warning("Capability %s unavailable (%s): %s", name, path, err)
        ok = sum(1 for value in results.values() if value)
        _LOGGER.info("Supervisor capabilities: %d/%d available", ok, len(results))
        return results


def container_stats_to_row(slug: str, stats: dict[str, Any]) -> dict[str, Any]:
    """Normalise a Supervisor stats payload into a container_samples row."""
    return {
        "slug": slug,
        "cpu": stats.get("cpu_percent"),
        "mem_bytes": stats.get("memory_usage"),
        "mem_percent": stats.get("memory_percent"),
        "net_rx": stats.get("network_rx"),
        "net_tx": stats.get("network_tx"),
        "blk_read": stats.get("blk_read"),
        "blk_write": stats.get("blk_write"),
    }
