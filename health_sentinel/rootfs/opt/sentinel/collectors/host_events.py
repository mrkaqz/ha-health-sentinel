"""Live host journal tail with kernel event pattern matching.

Core crashing is usually a symptom. The cause tends to live one layer down, in
the kernel, and the kernel says so plainly in the journal — but only if someone
is listening at the time. So this tails `/host/logs/follow` continuously rather
than reading the journal only after a reboot.

The same matcher is reused by forensics.py against the *previous boot's* journal,
so a crash that took the whole machine down gets classified by exactly the same
rules as one caught live.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from collectors.supervisor_api import SupervisorClient

_LOGGER = logging.getLogger(__name__)

_RECONNECT_DELAY = 10
_MAX_RECONNECT_DELAY = 300
# The journal repeats itself; don't record the same line twice in this window.
_DEDUPE_SECONDS = 120
# A failing disk can emit thousands of identical lines a second. Cap the damage.
_MAX_EVENTS_PER_MINUTE = 60

_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class Pattern:
    kind: str
    severity: str
    regex: re.Pattern[str]
    explanation: str


def _p(kind: str, severity: str, pattern: str, explanation: str) -> Pattern:
    return Pattern(kind, severity, re.compile(pattern, re.IGNORECASE), explanation)


# Ordered most-diagnostic first; the first match wins so a line is classified
# once, by its most serious interpretation.
PATTERNS: tuple[Pattern, ...] = (
    _p(
        "oom",
        "critical",
        r"\boom-killer\b|\bOut of memory: Kill|oom_reaper",
        "The kernel ran out of memory and killed a process to survive.",
    ),
    _p(
        "oom_victim",
        "critical",
        r"Killed process \d+ \([^)]+\)",
        "Names the process the OOM killer chose, with its memory footprint.",
    ),
    _p(
        "hardware_error",
        "critical",
        r"\bmce:|Machine Check|Hardware Error|\bEDAC\b|CPU\d+: Core temperature above",
        "Machine check / ECC error — failing RAM or CPU.",
    ),
    _p(
        "kernel_fault",
        "critical",
        r"Kernel panic|\bBUG: |soft lockup|hung task|watchdog: BUG|general protection fault",
        "Kernel-level fault or stall.",
    ),
    _p(
        "filesystem",
        "critical",
        r"EXT4-fs error|Remounting filesystem read-only|xfs.*corruption|btrfs.*error",
        "Filesystem error. A read-only remount makes HA look like it is hanging.",
    ),
    _p(
        "power",
        "critical",
        r"Under-voltage detected|voltage normalised",
        "Power supply problem.",
    ),
    _p(
        "disk_io",
        "error",
        r"\bI/O error\b|blk_update_request|critical medium error|ata\d+.*failed command",
        "Block device I/O failure.",
    ),
    _p(
        "usb_enumeration_error",
        "error",
        r"device not accepting address \d+"
        r"|unable to enumerate USB device"
        r"|device descriptor read/\d+, error"
        r"|over-current (?:change|condition)"
        r"|Cannot enable port \d+",
        "A USB device failed to enumerate. This normally precedes the device "
        "disappearing, so it is early warning that a coordinator is failing, "
        "not just confirmation after the fact.",
    ),
    _p(
        "usb_reset",
        "warning",
        r"reset (?:full|high|low|super)[- ]speed USB device",
        "A USB device was reset. Occasional resets are routine; repeated ones "
        "on the same port indicate a failing device, cable or power supply.",
    ),
    _p(
        "usb_disconnect",
        "warning",
        r"usb [\d.-]+: USB disconnect",
        "A USB device vanished — commonly a Zigbee or Z-Wave coordinator.",
    ),
    _p(
        "usb_connect",
        "info",
        r"usb [\d.-]+: new (?:full|high|low|super)[- ]speed USB device",
        "A USB device enumerated.",
    ),
    _p(
        "thermal",
        "warning",
        r"temperature above threshold|thermal throttl|clock throttled|cpu clock throttled",
        "Thermal throttling or overheating.",
    ),
    _p(
        "network",
        "warning",
        r"Link is Down|NETDEV WATCHDOG|link down|carrier lost",
        "Network interface flap.",
    ),
    _p(
        "service_failure",
        "error",
        r"systemd\[\d+\]:.*(?:Failed to start|entered failed state)",
        "A host systemd unit failed.",
    ),
)

# Severities that are worth waking someone up for.
ALERT_SEVERITIES = ("critical",)

EventCallback = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]


def match_line(line: str) -> Pattern | None:
    """Classify one journal line, or None if it is unremarkable."""
    for pattern in PATTERNS:
        if pattern.regex.search(line):
            return pattern
    return None


def scan_text(text: str, limit: int = 200) -> list[dict[str, Any]]:
    """Classify a block of journal text.

    Used by forensics against the previous boot's log, where the interesting
    lines are usually the last ones before everything went quiet.
    """
    findings: list[dict[str, Any]] = []
    for line in text.splitlines():
        pattern = match_line(line)
        if pattern is None:
            continue
        findings.append(
            {
                "kind": pattern.kind,
                "severity": pattern.severity,
                "explanation": pattern.explanation,
                "line": line.strip()[:500],
            }
        )
        if len(findings) >= limit:
            break
    return findings


def summarise(findings: Iterable[dict[str, Any]]) -> str | None:
    """One plain sentence describing the most serious thing found."""
    order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    worst = sorted(findings, key=lambda f: order.get(f["severity"], 9))
    if not worst:
        return None
    top = worst[0]
    return f"{top['explanation']} ({top['line'][:200]})"


class HostEventTail:
    """Follows the host journal and records classified events."""

    def __init__(
        self,
        client: SupervisorClient,
        on_event: EventCallback,
    ) -> None:
        self._client = client
        self._on_event = on_event
        self._stop = asyncio.Event()
        self._recent: dict[str, float] = {}
        self._minute_started = time.monotonic()
        self._minute_count = 0
        self._suppressed = 0
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        delay = _RECONNECT_DELAY
        while not self._stop.is_set():
            try:
                await self._follow()
                delay = _RECONNECT_DELAY
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - the tail must never die
                _LOGGER.debug("Host journal tail dropped: %s", err)
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

    async def _follow(self) -> None:
        _LOGGER.info("Following host journal")
        self._connected = True
        async for line in self._client.stream_lines("/host/logs/follow"):
            if self._stop.is_set():
                return
            if not line.strip():
                continue
            pattern = match_line(line)
            if pattern is None:
                continue
            if not self._admit(pattern.kind, line):
                continue
            await self._on_event(
                pattern.kind,
                line.strip()[:500],
                pattern.severity,
                {"explanation": pattern.explanation, "source": "host_journal"},
            )

    def _admit(self, kind: str, line: str) -> bool:
        """Dedupe and rate-limit so one sick device can't flood the database."""
        now = time.monotonic()

        if now - self._minute_started >= 60:
            if self._suppressed:
                _LOGGER.warning(
                    "Suppressed %d host journal events in the last minute",
                    self._suppressed,
                )
            self._minute_started = now
            self._minute_count = 0
            self._suppressed = 0

        if self._minute_count >= _MAX_EVENTS_PER_MINUTE:
            self._suppressed += 1
            return False

        # Strip timestamps and PIDs so repeats of "the same" line collapse.
        normalised = _DIGITS.sub("#", line)[:200]
        fingerprint = f"{kind}:{normalised}"
        last_seen = self._recent.get(fingerprint)
        if last_seen is not None and now - last_seen < _DEDUPE_SECONDS:
            return False

        self._recent[fingerprint] = now
        if len(self._recent) > 2000:
            cutoff = now - _DEDUPE_SECONDS
            self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}

        self._minute_count += 1
        return True
