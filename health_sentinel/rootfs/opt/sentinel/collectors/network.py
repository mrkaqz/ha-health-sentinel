"""Host network state, without elevated privileges.

A note on what is deliberately *not* here: byte, packet, error and drop
counters. Reading `/proc/net/dev` from inside the add-on would return the
container's own veth, not the host's NIC — unlike `/proc/loadavg` and
`/proc/pressure`, network statistics *are* namespaced per container. Getting the
host's real counters needs `host_network: true`, which would end the add-on's
"protection mode stays on" property. Not worth it.

What is available without that still answers the question that matters after a
cluster of integrations drops: *did the network change state at that moment?*
Link loss, a DHCP lease landing on a new address, a changed gateway or resolver,
and loss of internet reachability all show up here, and all of them will take
LAN or cloud integrations down with them.
"""

from __future__ import annotations

import logging
from typing import Any

from collectors.supervisor_api import SupervisorClient

_LOGGER = logging.getLogger(__name__)


def _snapshot(interface: dict[str, Any]) -> dict[str, Any]:
    ipv4 = interface.get("ipv4") or {}
    return {
        "interface": interface.get("interface"),
        "type": interface.get("type"),
        "enabled": bool(interface.get("enabled")),
        "connected": bool(interface.get("connected")),
        "primary": bool(interface.get("primary")),
        "method": ipv4.get("method"),
        "address": (ipv4.get("address") or [None])[0]
        if isinstance(ipv4.get("address"), list)
        else ipv4.get("address"),
        "gateway": ipv4.get("gateway"),
        "nameservers": ipv4.get("nameservers") or [],
    }


class NetworkWatcher:
    """Polls /network/info and reports state changes."""

    def __init__(self, client: SupervisorClient) -> None:
        self._client = client
        self._interfaces: dict[str, dict[str, Any]] = {}
        self._host_internet: bool | None = None
        self._supervisor_internet: bool | None = None
        self._primed = False

    @property
    def interfaces(self) -> list[dict[str, Any]]:
        return list(self._interfaces.values())

    @property
    def state(self) -> dict[str, Any]:
        return {
            "interfaces": self.interfaces,
            "host_internet": self._host_internet,
            "supervisor_internet": self._supervisor_internet,
        }

    async def poll(self) -> list[dict[str, Any]]:
        """Return changes since the last poll."""
        info = await self._client.try_get("/network/info")
        if not info:
            return []

        current = {
            snap["interface"]: snap
            for snap in (_snapshot(i) for i in info.get("interfaces") or [])
            if snap["interface"]
        }
        host_internet = info.get("host_internet")
        supervisor_internet = info.get("supervisor_internet")

        if not self._primed:
            self._interfaces = current
            self._host_internet = host_internet
            self._supervisor_internet = supervisor_internet
            self._primed = True
            _LOGGER.info("Network baseline: %d interfaces", len(current))
            return []

        changes: list[dict[str, Any]] = []

        for name, snap in current.items():
            previous = self._interfaces.get(name)
            if previous is None:
                changes.append(
                    {
                        "kind": "net_interface_added",
                        "severity": "info",
                        "message": f"Network interface {name} appeared",
                        "detail": snap,
                    }
                )
                continue

            if previous["connected"] != snap["connected"]:
                up = snap["connected"]
                changes.append(
                    {
                        "kind": "net_link_up" if up else "net_link_down",
                        # Losing the primary interface is what takes the whole
                        # instance offline; a secondary going down may be noise.
                        "severity": "info"
                        if up
                        else ("critical" if snap["primary"] else "warning"),
                        "message": (
                            f"Interface {name} link {'up' if up else 'DOWN'}"
                            f"{' (primary)' if snap['primary'] else ''}"
                        ),
                        "detail": snap,
                    }
                )

            if previous["address"] != snap["address"]:
                changes.append(
                    {
                        "kind": "net_address_changed",
                        "severity": "warning",
                        "message": (
                            f"Interface {name} address changed from "
                            f"{previous['address']} to {snap['address']}. A DHCP "
                            "lease landing on a new address drops every "
                            "integration that reaches this host by IP."
                        ),
                        "detail": snap,
                    }
                )

            if previous["gateway"] != snap["gateway"]:
                changes.append(
                    {
                        "kind": "net_gateway_changed",
                        "severity": "warning",
                        "message": (
                            f"Interface {name} gateway changed from "
                            f"{previous['gateway']} to {snap['gateway']}"
                        ),
                        "detail": snap,
                    }
                )

            if previous["nameservers"] != snap["nameservers"]:
                changes.append(
                    {
                        "kind": "net_dns_changed",
                        "severity": "warning",
                        "message": (
                            f"Interface {name} DNS servers changed from "
                            f"{previous['nameservers']} to {snap['nameservers']}. "
                            "If name resolution breaks, cloud integrations fail "
                            "together and look like a network outage."
                        ),
                        "detail": snap,
                    }
                )

        for name in self._interfaces:
            if name not in current:
                changes.append(
                    {
                        "kind": "net_interface_removed",
                        "severity": "warning",
                        "message": f"Network interface {name} disappeared",
                        "detail": {"interface": name},
                    }
                )

        for label, previous, now_value in (
            ("Host", self._host_internet, host_internet),
            ("Supervisor", self._supervisor_internet, supervisor_internet),
        ):
            if previous is not None and now_value is not None and previous != now_value:
                changes.append(
                    {
                        "kind": "net_internet_changed",
                        "severity": "warning" if not now_value else "info",
                        "message": (
                            f"{label} internet connectivity "
                            f"{'restored' if now_value else 'LOST'}"
                        ),
                        "detail": {"scope": label.lower(), "connected": now_value},
                    }
                )

        self._interfaces = current
        self._host_internet = host_internet
        self._supervisor_internet = supervisor_internet
        return changes

    def metrics(self) -> dict[str, float]:
        connected = sum(1 for i in self._interfaces.values() if i["connected"])
        out = {
            "net.interfaces.total": float(len(self._interfaces)),
            "net.interfaces.connected": float(connected),
        }
        if self._host_internet is not None:
            out["net.host_internet"] = 1.0 if self._host_internet else 0.0
        if self._supervisor_internet is not None:
            out["net.supervisor_internet"] = (
                1.0 if self._supervisor_internet else 0.0
            )
        return out
