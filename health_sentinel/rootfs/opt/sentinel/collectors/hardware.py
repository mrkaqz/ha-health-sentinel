"""Hardware inventory diffing.

The kernel journal says a USB device disconnected; this says *which* one, by its
stable by-id path. Together they turn "Zigbee stopped working overnight" into a
timestamped fact with a device name attached.

Serial devices get special attention because that is what Zigbee and Z-Wave
coordinators are, and losing one silently unavailables hundreds of entities.
"""

from __future__ import annotations

import logging
from typing import Any

from collectors.supervisor_api import SupervisorClient

_LOGGER = logging.getLogger(__name__)

# Subsystems worth tracking. Everything else on a busy host is noise.
_TRACKED_SUBSYSTEMS = ("tty", "usb", "block", "net")

# Losing one of these is an incident, not a curiosity.
_CRITICAL_SUBSYSTEMS = ("tty",)


def _identity(device: dict[str, Any]) -> str:
    """Stable identity for a device across re-enumeration.

    by_id survives replugging into a different port; dev_path does not. Prefer
    the former so a genuine re-plug isn't reported as a different device.
    """
    by_id = device.get("by_id")
    if by_id:
        return str(by_id)
    return str(device.get("dev_path") or device.get("sysfs") or device.get("name"))


def _describe(device: dict[str, Any]) -> str:
    name = device.get("name") or device.get("dev_path") or "unknown device"
    subsystem = device.get("subsystem") or "?"
    by_id = device.get("by_id")
    return f"{name} ({subsystem}){f' [{by_id}]' if by_id else ''}"


class HardwareWatcher:
    """Detects devices appearing and disappearing between polls."""

    def __init__(self, client: SupervisorClient) -> None:
        self._client = client
        self._known: dict[str, dict[str, Any]] = {}
        self._primed = False

    @property
    def devices(self) -> list[dict[str, Any]]:
        return list(self._known.values())

    @property
    def serial_devices(self) -> list[dict[str, Any]]:
        return [
            device
            for device in self._known.values()
            if device.get("subsystem") == "tty" and device.get("by_id")
        ]

    async def poll(self) -> list[dict[str, Any]]:
        """Return changes since the last poll.

        The first poll only establishes a baseline — otherwise starting the
        add-on would report every device on the system as newly attached.
        """
        info = await self._client.hardware_info()
        if not info:
            return []

        current: dict[str, dict[str, Any]] = {}
        for device in info.get("devices") or []:
            if device.get("subsystem") not in _TRACKED_SUBSYSTEMS:
                continue
            current[_identity(device)] = device

        if not self._primed:
            self._known = current
            self._primed = True
            _LOGGER.info(
                "Hardware baseline: %d tracked devices (%d serial)",
                len(current),
                len(self.serial_devices),
            )
            return []

        changes: list[dict[str, Any]] = []

        for identity, device in current.items():
            if identity not in self._known:
                changes.append(
                    {
                        "change": "attached",
                        "identity": identity,
                        "subsystem": device.get("subsystem"),
                        "description": _describe(device),
                        "severity": "info",
                    }
                )

        for identity, device in self._known.items():
            if identity not in current:
                subsystem = device.get("subsystem")
                changes.append(
                    {
                        "change": "removed",
                        "identity": identity,
                        "subsystem": subsystem,
                        "description": _describe(device),
                        # A serial device vanishing is how Zigbee dies.
                        "severity": (
                            "critical" if subsystem in _CRITICAL_SUBSYSTEMS else "warning"
                        ),
                    }
                )

        self._known = current
        return changes

    def metrics(self) -> dict[str, float]:
        return {
            "host.devices.tracked": float(len(self._known)),
            "host.devices.serial": float(len(self.serial_devices)),
        }
