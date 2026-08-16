"""Incident bundles — everything about one crash in a single downloadable file.

The point of a bundle is that it stays useful after the system it came from has
been rebooted, upgraded or reinstalled. So it carries the evidence itself, not
references to it.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import tarfile
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Logs get big and bundles should stay mailable.
_MAX_LOG_BYTES = 4 * 1024 * 1024


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes, ts: int) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mtime = ts
    archive.addfile(info, io.BytesIO(payload))


def _add_text(archive: tarfile.TarFile, name: str, text: str, ts: int) -> None:
    if not text:
        return
    payload = text.encode("utf-8", errors="replace")
    if len(payload) > _MAX_LOG_BYTES:
        # Keep the tail — the interesting part of a crash log is the end.
        payload = b"[truncated]\n" + payload[-_MAX_LOG_BYTES:]
    _add_bytes(archive, name, payload, ts)


def _add_json(archive: tarfile.TarFile, name: str, data: Any, ts: int) -> None:
    _add_bytes(
        archive, name, json.dumps(data, indent=2, default=str).encode("utf-8"), ts
    )


def _copy_file(archive: tarfile.TarFile, name: str, path: str, ts: int) -> bool:
    """Copy a host file into the bundle, tail-truncated if oversized."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > _MAX_LOG_BYTES:
                handle.seek(size - _MAX_LOG_BYTES)
                payload = b"[truncated]\n" + handle.read()
            else:
                payload = handle.read()
    except OSError as err:
        _LOGGER.debug("Could not include %s in bundle: %s", path, err)
        return False
    _add_bytes(archive, name, payload, ts)
    return True


def build(
    directory: str,
    incident_id: int,
    summary: dict[str, Any],
    metrics: dict[str, list[tuple]] | None = None,
    events: list[dict[str, Any]] | None = None,
    logs: dict[str, str] | None = None,
    files: dict[str, str] | None = None,
    addons: list[dict[str, Any]] | None = None,
) -> str | None:
    """Write one incident bundle and return its path."""
    os.makedirs(directory, exist_ok=True)
    ts = int(time.time())
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
    path = os.path.join(directory, f"incident-{incident_id:05d}-{stamp}.tar.gz")

    try:
        with tarfile.open(path, "w:gz") as archive:
            _add_json(archive, "summary.json", summary, ts)

            # A human-readable version so the bundle is useful without tooling.
            _add_text(archive, "README.txt", _render_readme(summary), ts)

            if metrics:
                _add_json(archive, "metrics.json", metrics, ts)
            if events:
                _add_json(archive, "events.json", events, ts)
            if addons:
                _add_json(archive, "addons.json", addons, ts)

            for name, text in (logs or {}).items():
                _add_text(archive, f"logs/{name}", text, ts)

            for name, source in (files or {}).items():
                _copy_file(archive, f"logs/{name}", source, ts)
    except OSError as err:
        _LOGGER.error("Failed to write incident bundle: %s", err)
        return None

    _LOGGER.info("Wrote incident bundle %s", path)
    return path


def _render_readme(summary: dict[str, Any]) -> str:
    lines = [
        "Home Assistant Health Sentinel — incident bundle",
        "=" * 48,
        "",
        f"Verdict:        {summary.get('classification', 'unknown')}",
        f"Summary:        {summary.get('summary', '')}",
        f"Started:        {summary.get('started_local', '')}",
        f"Ended:          {summary.get('ended_local', '') or 'still open'}",
        f"Outage:         {summary.get('outage_human', 'unknown')}",
        "",
    ]

    evidence = summary.get("evidence") or []
    if evidence:
        lines.append("Evidence found in logs")
        lines.append("-" * 48)
        for item in evidence:
            lines.append(f"  [{item.get('severity')}] {item.get('explanation')}")
            lines.append(f"      {item.get('line')}")
        lines.append("")

    lines.extend(
        [
            "Contents",
            "-" * 48,
            "  summary.json   full machine-readable verdict and context",
            "  metrics.json   metric samples around the event window",
            "  events.json    classified host and add-on events",
            "  addons.json    add-on states at the moment of the incident",
            "  logs/          core, supervisor, host journal and previous HA log",
            "",
        ]
    )
    return "\n".join(lines)


def prune(directory: str, keep: int = 50) -> None:
    """Keep only the newest bundles so /data can't fill up."""
    try:
        entries = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.startswith("incident-") and name.endswith(".tar.gz")
        ]
    except OSError:
        return
    if len(entries) <= keep:
        return
    entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for stale in entries[keep:]:
        try:
            os.unlink(stale)
        except OSError:
            pass


def export_to_share(path: str, share_dir: str = "/share/health_sentinel") -> str | None:
    """Copy a bundle somewhere the user can reach over Samba."""
    try:
        os.makedirs(share_dir, exist_ok=True)
        target = os.path.join(share_dir, os.path.basename(path))
        shutil.copy2(path, target)
        return target
    except OSError as err:
        _LOGGER.warning("Could not export bundle to share: %s", err)
        return None
