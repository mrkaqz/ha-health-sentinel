"""Home Assistant Core liveness and latency probe.

This is the crash detector. Two distinct questions get asked, and the
difference between their answers is diagnostic:

  * `/core/info` is the *Supervisor's* opinion of Core (running/stopped/error).
  * `/core/api/` goes through the proxy to Core itself and actually waits for
    Core's event loop to answer.

Core can be "running" as far as the Supervisor is concerned while its event loop
is wedged solid. Only the second probe notices that, and rising latency on it is
the classic signature in the minutes before a hang.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from collectors.supervisor_api import SupervisorClient

_LOGGER = logging.getLogger(__name__)

# Deliberately short. A probe that takes longer than this is itself the finding.
_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=10)
_TEMPLATE_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Resolving every integration's entity list is a big render; give it room.
_MAP_TIMEOUT = aiohttp.ClientTimeout(total=120)

# One round trip that makes Core do real work across its state machine.
_CENSUS_TEMPLATE = (
    "{{ states | list | count }}|"
    "{{ states | selectattr('state', 'in', ['unavailable', 'unknown']) "
    "| list | count }}|"
    "{{ states.automation | selectattr('state', 'eq', 'on') | list | count }}"
)


@dataclass
class ProbeResult:
    ts: int
    reachable: bool
    latency_ms: float | None = None
    supervisor_state: str | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def metrics(self) -> dict[str, float]:
        out: dict[str, float] = {"core.reachable": 1.0 if self.reachable else 0.0}
        if self.latency_ms is not None:
            out["core.latency_ms"] = self.latency_ms
        return out


class CoreProbe:
    def __init__(self, client: SupervisorClient) -> None:
        self._client = client
        self._base = client.base

    async def probe(self) -> ProbeResult:
        """One liveness check. Never raises."""
        ts = int(time.time())
        supervisor_state: str | None = None

        info = await self._client.try_get("/core/info")
        if info:
            supervisor_state = info.get("state")

        started = time.monotonic()
        try:
            async with self._client.session.get(
                f"{self._base}/core/api/", timeout=_PROBE_TIMEOUT
            ) as response:
                await response.read()
                latency_ms = (time.monotonic() - started) * 1000.0
                if response.status == 200:
                    return ProbeResult(
                        ts=ts,
                        reachable=True,
                        latency_ms=latency_ms,
                        supervisor_state=supervisor_state,
                    )
                return ProbeResult(
                    ts=ts,
                    reachable=False,
                    latency_ms=latency_ms,
                    supervisor_state=supervisor_state,
                    error=f"HTTP {response.status}",
                )
        except asyncio.TimeoutError:
            return ProbeResult(
                ts=ts,
                reachable=False,
                latency_ms=(time.monotonic() - started) * 1000.0,
                supervisor_state=supervisor_state,
                error="timeout",
            )
        except aiohttp.ClientError as err:
            return ProbeResult(
                ts=ts,
                reachable=False,
                latency_ms=None,
                supervisor_state=supervisor_state,
                error=str(err) or err.__class__.__name__,
            )

    async def integration_entity_map(self) -> dict[str, str]:
        """Fallback entity -> integration mapping, needing no admin rights.

        Used when `config/entity_registry/list` is refused. Asks Core for its
        loaded components, then has Core itself resolve `integration_entities()`
        for each one, so the mapping never has to be guessed from entity ids.
        """
        components = await self._loaded_integrations()
        if not components:
            return {}

        # Core does the work; we get back "integration=entity,entity,..." lines.
        listing = json.dumps(sorted(components))
        template = (
            "{%- set ints = " + listing + " -%}"
            "{%- for i in ints -%}"
            "{%- set e = integration_entities(i) -%}"
            "{%- if e %}{{ i }}={{ e | join(',') }}\n{% endif -%}"
            "{%- endfor -%}"
        )

        try:
            async with self._client.session.post(
                f"{self._base}/core/api/template",
                json={"template": template},
                timeout=_MAP_TIMEOUT,
            ) as response:
                if response.status != 200:
                    _LOGGER.warning(
                        "Integration mapping template failed: HTTP %s", response.status
                    )
                    return {}
                rendered = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Integration mapping template failed: %s", err)
            return {}

        mapping: dict[str, str] = {}
        for line in rendered.splitlines():
            integration, _, entities = line.partition("=")
            if not integration or not entities:
                continue
            for entity_id in entities.split(","):
                entity_id = entity_id.strip()
                if entity_id:
                    mapping[entity_id] = integration.strip()
        return mapping

    async def _loaded_integrations(self) -> list[str]:
        """Integration domains currently loaded by Core."""
        try:
            async with self._client.session.get(
                f"{self._base}/core/api/config", timeout=_PROBE_TIMEOUT
            ) as response:
                if response.status != 200:
                    return []
                config = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.debug("Could not read Core config: %s", err)
            return []

        # `components` mixes integration domains with "domain.platform" entries;
        # only the bare domains are integrations.
        return sorted(
            {c for c in (config.get("components") or []) if c and "." not in c}
        )

    async def entity_census(self) -> dict[str, float]:
        """Entity counts via one template render.

        Fetching /core/api/states would pull every attribute of ~4,000 entities
        on every slow tick. Rendering a template makes Core do the counting and
        return a few bytes.
        """
        try:
            async with self._client.session.post(
                f"{self._base}/core/api/template",
                json={"template": _CENSUS_TEMPLATE},
                timeout=_TEMPLATE_TIMEOUT,
            ) as response:
                if response.status != 200:
                    return {}
                rendered = (await response.text()).strip()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Entity census failed: %s", err)
            return {}

        parts = rendered.split("|")
        if len(parts) < 3:
            return {}
        try:
            total = float(parts[0])
            unavailable = float(parts[1])
            automations_on = float(parts[2])
        except ValueError:
            return {}

        metrics = {
            "core.entities.total": total,
            "core.entities.unavailable": unavailable,
            "core.automations.enabled": automations_on,
        }
        if total > 0:
            metrics["core.entities.unavailable_pct"] = unavailable / total * 100.0
        return metrics
