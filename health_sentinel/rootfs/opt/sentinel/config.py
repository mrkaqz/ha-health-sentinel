"""Add-on options and well-known paths.

Options arrive as environment variables exported by run.sh, which reads them
from the Supervisor via bashio. Nothing here touches the network.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

# The Supervisor is always reachable at this name from inside an add-on.
SUPERVISOR_API = "http://supervisor"

# /data is the add-on's persistent volume. It survives add-on restarts, add-on
# updates and host reboots — which is the whole point, because the crash we are
# investigating destroys everything that lived only in Core's memory.
DB_PATH = "/data/sentinel.db"
STATE_PATH = "/data/state.json"
BUNDLE_DIR = "/data/bundles"

# Previous run's Core log, via the read-only homeassistant_config mapping.
HA_LOG_PREVIOUS = "/homeassistant/home-assistant.log.1"
HA_LOG_CURRENT = "/homeassistant/home-assistant.log"

# Host-global files. These are NOT namespaced per container, so an ordinary
# unprivileged add-on reads the host's real values straight out of procfs.
PROC_PRESSURE = "/proc/pressure"
PROC_LOADAVG = "/proc/loadavg"
PROC_MEMINFO = "/proc/meminfo"
PROC_UPTIME = "/proc/uptime"
PROC_BOOT_ID = "/proc/sys/kernel/random/boot_id"
SYS_THERMAL = "/sys/class/thermal"

WEB_PORT = 8099

# Set from the BUILD_VERSION build arg. Used to bust browser caches on the
# dashboard's assets and shown in the UI, so "which build am I actually
# looking at" is answerable without guessing.
VERSION = os.environ.get("SENTINEL_VERSION", "").strip() or "dev"

# bashio log levels -> python. "trace" and "notice" have no stdlib equivalent.
_LOG_LEVELS = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "on")


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    # bashio renders an unset optional option as the literal "null".
    if value in ("", "null"):
        return default
    return value


@dataclass(frozen=True)
class Config:
    """Resolved add-on options."""

    probe_interval: int
    sample_interval: int
    slow_interval: int
    ring_buffer_minutes: int
    alert_core_down_after: int
    retention_raw_days: int
    retention_rollup_days: int
    disk_free_warn_pct: int
    memory_warn_pct: int
    chronic_after_minutes: int
    cluster_window_seconds: int
    cluster_min_integrations: int
    cluster_min_entities: int
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    webhook_url: str
    log_level: str
    supervisor_token: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            probe_interval=_env_int("SENTINEL_PROBE_INTERVAL", 5),
            sample_interval=_env_int("SENTINEL_SAMPLE_INTERVAL", 15),
            slow_interval=_env_int("SENTINEL_SLOW_INTERVAL", 300),
            ring_buffer_minutes=_env_int("SENTINEL_RING_BUFFER_MINUTES", 30),
            alert_core_down_after=_env_int("SENTINEL_ALERT_CORE_DOWN_AFTER", 90),
            retention_raw_days=_env_int("SENTINEL_RETENTION_RAW_DAYS", 7),
            retention_rollup_days=_env_int("SENTINEL_RETENTION_ROLLUP_DAYS", 90),
            disk_free_warn_pct=_env_int("SENTINEL_DISK_FREE_WARN_PCT", 10),
            memory_warn_pct=_env_int("SENTINEL_MEMORY_WARN_PCT", 90),
            chronic_after_minutes=_env_int("SENTINEL_CHRONIC_AFTER_MINUTES", 60),
            cluster_window_seconds=_env_int("SENTINEL_CLUSTER_WINDOW_SECONDS", 120),
            cluster_min_integrations=_env_int(
                "SENTINEL_CLUSTER_MIN_INTEGRATIONS", 3
            ),
            cluster_min_entities=_env_int("SENTINEL_CLUSTER_MIN_ENTITIES", 2),
            telegram_enabled=_env_bool("SENTINEL_TELEGRAM_ENABLED"),
            telegram_bot_token=_env_str("SENTINEL_TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env_str("SENTINEL_TELEGRAM_CHAT_ID"),
            webhook_url=_env_str("SENTINEL_WEBHOOK_URL"),
            log_level=_env_str("SENTINEL_LOG_LEVEL", "info").lower(),
            supervisor_token=_env_str("SENTINEL_SUPERVISOR_TOKEN")
            or _env_str("SUPERVISOR_TOKEN"),
        )

    @property
    def telegram_ready(self) -> bool:
        return bool(
            self.telegram_enabled and self.telegram_bot_token and self.telegram_chat_id
        )

    @property
    def python_log_level(self) -> int:
        return _LOG_LEVELS.get(self.log_level, logging.INFO)


def setup_logging(config: Config) -> None:
    logging.basicConfig(
        level=config.python_log_level,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # aiohttp logs every ingress request at INFO; that would drown the signal.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
