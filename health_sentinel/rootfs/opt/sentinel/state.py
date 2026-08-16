"""Crash-survivable state file.

This is deliberately not in SQLite. The database runs WAL with
synchronous=NORMAL, so a power cut can lose the most recent commits — exactly
the commits that matter when reconstructing the moment of death.

This file is tiny and rewritten atomically with an fsync, so whatever it says
after a hard power loss is true. `last_heartbeat` is therefore the timestamp we
trust for "when did the machine stop being alive".
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)


class StateFile:
    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict[str, Any] = {}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._load()
        # Capture what the previous run left behind before anything overwrites
        # it — boot forensics compares against this.
        self.snapshot_previous()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                self._data = json.load(handle)
        except FileNotFoundError:
            self._data = {}
        except (OSError, ValueError) as err:
            # A torn state file is itself evidence of an unclean stop; don't die
            # on it, just start fresh and let boot forensics note the gap.
            _LOGGER.warning("State file unreadable (%s); starting fresh", err)
            self._data = {}

    @property
    def previous(self) -> dict[str, Any]:
        """Snapshot as it was when the add-on started. Never mutated."""
        return self._previous

    def snapshot_previous(self) -> None:
        self._previous = dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, **values: Any) -> None:
        self._data.update(values)

    def flush(self) -> None:
        """Atomically replace the file and fsync both file and directory."""
        directory = os.path.dirname(self._path)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
            # Without fsyncing the directory the rename itself can be lost.
            dir_fd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as err:
            _LOGGER.error("Failed to persist state: %s", err)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def heartbeat(self, **extra: Any) -> None:
        self._data["last_heartbeat"] = int(time.time())
        if extra:
            self._data.update(extra)
        self.flush()

    def mark_clean_shutdown(self) -> None:
        self._data["clean_shutdown"] = True
        self._data["shutdown_ts"] = int(time.time())
        self.flush()

    def clear_shutdown_marker(self) -> None:
        self._data["clean_shutdown"] = False
        self._data.pop("shutdown_ts", None)
        self.flush()


def read_host_boot_id(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def read_host_uptime(path: str) -> float | None:
    """Seconds since the host booted. Not namespaced, so this is the host's."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
