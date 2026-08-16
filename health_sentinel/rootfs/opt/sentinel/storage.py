"""SQLite persistence for metrics, events, incidents and boot history.

Deliberately a separate database from Home Assistant's recorder. The sentinel
must never become a victim of the problem it is diagnosing, and on this
instance the recorder is already ~80 GB.

Write volume is tiny (a few dozen rows per 15 s), so a single connection behind
a lock is plenty. Async callers go through the `a*` wrappers, which hop to a
worker thread so the event loop never blocks on disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Iterable, Sequence

_LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts      INTEGER NOT NULL,
    metric  TEXT    NOT NULL,
    value   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_metric_ts ON samples (metric, ts);
CREATE INDEX IF NOT EXISTS idx_samples_ts        ON samples (ts);

CREATE TABLE IF NOT EXISTS samples_1m (
    ts           INTEGER NOT NULL,
    metric       TEXT    NOT NULL,
    avg_value    REAL,
    min_value    REAL,
    max_value    REAL,
    sample_count INTEGER,
    PRIMARY KEY (ts, metric)
);

CREATE TABLE IF NOT EXISTS container_samples (
    ts          INTEGER NOT NULL,
    slug        TEXT    NOT NULL,
    cpu         REAL,
    mem_bytes   INTEGER,
    mem_percent REAL,
    net_rx      INTEGER,
    net_tx      INTEGER,
    blk_read    INTEGER,
    blk_write   INTEGER,
    PRIMARY KEY (ts, slug)
);
CREATE INDEX IF NOT EXISTS idx_container_slug_ts ON container_samples (slug, ts);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    kind     TEXT    NOT NULL,
    severity TEXT    NOT NULL,
    source   TEXT,
    message  TEXT,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts       ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts  ON events (kind, ts);

CREATE TABLE IF NOT EXISTS incidents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts     INTEGER NOT NULL,
    ended_ts       INTEGER,
    classification TEXT,
    summary        TEXT,
    detail         TEXT,
    bundle_path    TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_started ON incidents (started_ts);

CREATE TABLE IF NOT EXISTS boots (
    boot_id        TEXT PRIMARY KEY,
    host_boot_ts   INTEGER,
    detected_ts    INTEGER,
    clean_shutdown INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Dead entities and when they died. Persisted so that "how long has this been
-- broken" survives an add-on restart; without it every long-dead entity would
-- look freshly broken on startup and drown the outage detector.
CREATE TABLE IF NOT EXISTS entity_availability (
    entity_id TEXT PRIMARY KEY,
    platform  TEXT,
    state     TEXT,
    since_ts  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_availability_platform
    ON entity_availability (platform);
"""

# Events at or above this severity are never purged — they are the evidence.
_PERMANENT_SEVERITIES = ("critical", "error")


class Storage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL lets the web UI read while the collectors write. NORMAL keeps SSD
        # wear down (this box is already at 17% lifetime); durability of the
        # exact moment of death is handled by the fsynced state file instead.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- writes

    def write_samples(self, ts: int, metrics: dict[str, float]) -> None:
        rows = [(ts, k, float(v)) for k, v in metrics.items() if v is not None]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO samples (ts, metric, value) VALUES (?, ?, ?)", rows
            )
            self._conn.commit()

    def write_container_samples(self, ts: int, stats: Iterable[dict[str, Any]]) -> None:
        rows = [
            (
                ts,
                s["slug"],
                s.get("cpu"),
                s.get("mem_bytes"),
                s.get("mem_percent"),
                s.get("net_rx"),
                s.get("net_tx"),
                s.get("blk_read"),
                s.get("blk_write"),
            )
            for s in stats
        ]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                """INSERT OR REPLACE INTO container_samples
                   (ts, slug, cpu, mem_bytes, mem_percent,
                    net_rx, net_tx, blk_read, blk_write)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            self._conn.commit()

    def add_event(
        self,
        kind: str,
        message: str,
        severity: str = "info",
        source: str | None = None,
        detail: dict[str, Any] | None = None,
        ts: int | None = None,
    ) -> int:
        ts = ts if ts is not None else int(time.time())
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO events (ts, kind, severity, source, message, detail)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    kind,
                    severity,
                    source,
                    message,
                    json.dumps(detail) if detail else None,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    # ------------------------------------------------------------- incidents

    def open_incident(
        self,
        started_ts: int,
        classification: str,
        summary: str,
        detail: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO incidents
                   (started_ts, classification, summary, detail)
                   VALUES (?, ?, ?, ?)""",
                (
                    started_ts,
                    classification,
                    summary,
                    json.dumps(detail) if detail else None,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def close_incident(
        self,
        incident_id: int,
        ended_ts: int,
        summary: str | None = None,
        classification: str | None = None,
        detail: dict[str, Any] | None = None,
        bundle_path: str | None = None,
    ) -> None:
        sets = ["ended_ts = ?"]
        args: list[Any] = [ended_ts]
        for column, value in (
            ("summary", summary),
            ("classification", classification),
            ("bundle_path", bundle_path),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                args.append(value)
        if detail is not None:
            sets.append("detail = ?")
            args.append(json.dumps(detail))
        args.append(incident_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE incidents SET {', '.join(sets)} WHERE id = ?", args
            )
            self._conn.commit()

    def set_incident_bundle(self, incident_id: int, path: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE incidents SET bundle_path = ? WHERE id = ?",
                (path, incident_id),
            )
            self._conn.commit()

    def open_incidents(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM incidents WHERE ended_ts IS NULL")

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            self._conn.commit()

    def record_boot(
        self, boot_id: str, host_boot_ts: int, detected_ts: int, clean: bool = False
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO boots
                   (boot_id, host_boot_ts, detected_ts, clean_shutdown)
                   VALUES (?, ?, ?, ?)""",
                (boot_id, host_boot_ts, detected_ts, 1 if clean else 0),
            )
            self._conn.commit()

    # ---------------------------------------------------------- availability

    def save_availability(self, rows: Sequence[tuple[str, str, str, int]]) -> None:
        """Replace the dead-entity snapshot."""
        with self._lock:
            self._conn.execute("DELETE FROM entity_availability")
            if rows:
                self._conn.executemany(
                    """INSERT OR REPLACE INTO entity_availability
                       (entity_id, platform, state, since_ts) VALUES (?, ?, ?, ?)""",
                    rows,
                )
            self._conn.commit()

    def load_availability(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT entity_id, platform, state, since_ts FROM entity_availability"
        )

    # ----------------------------------------------------------------- reads

    def query(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return [dict(row) for row in cur.fetchall()]

    def series(self, metric: str, since: int, until: int | None = None) -> list[tuple]:
        """Raw points for one metric, falling back to rollups for old windows."""
        until = until if until is not None else int(time.time())
        raw = self.query(
            "SELECT ts, value FROM samples WHERE metric = ? AND ts >= ? AND ts <= ? "
            "ORDER BY ts",
            (metric, since, until),
        )
        if raw:
            return [(r["ts"], r["value"]) for r in raw]
        rolled = self.query(
            "SELECT ts, avg_value AS value FROM samples_1m "
            "WHERE metric = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (metric, since, until),
        )
        return [(r["ts"], r["value"]) for r in rolled]

    def latest(self, metric: str) -> float | None:
        rows = self.query(
            "SELECT value FROM samples WHERE metric = ? ORDER BY ts DESC LIMIT 1",
            (metric,),
        )
        return rows[0]["value"] if rows else None

    def window(self, since: int, until: int) -> dict[str, list[tuple]]:
        """Every metric in a time window — used to freeze the flight recorder."""
        rows = self.query(
            "SELECT ts, metric, value FROM samples WHERE ts >= ? AND ts <= ? "
            "ORDER BY ts",
            (since, until),
        )
        out: dict[str, list[tuple]] = {}
        for row in rows:
            out.setdefault(row["metric"], []).append((row["ts"], row["value"]))
        return out

    def memory_slope(self, slug: str, since: int) -> float | None:
        """Least-squares MB/hour for one container. Positive means leaking."""
        rows = self.query(
            "SELECT ts, mem_bytes FROM container_samples "
            "WHERE slug = ? AND ts >= ? AND mem_bytes IS NOT NULL ORDER BY ts",
            (slug, since),
        )
        if len(rows) < 10:
            return None
        n = len(rows)
        t0 = rows[0]["ts"]
        xs = [(r["ts"] - t0) / 3600.0 for r in rows]
        ys = [r["mem_bytes"] / (1024.0 * 1024.0) for r in rows]
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom == 0:
            return None
        return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom

    # ------------------------------------------------------ rollup & retention

    def rollup(self, now: int | None = None) -> int:
        """Fold complete minutes of raw samples into samples_1m."""
        now = now if now is not None else int(time.time())
        last = int(self.get_meta("last_rollup_ts") or 0)
        # Only aggregate minutes that have certainly finished.
        end = ((now - 60) // 60) * 60
        if end <= last:
            return 0
        start = last if last else end - 86400
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR REPLACE INTO samples_1m
                       (ts, metric, avg_value, min_value, max_value, sample_count)
                   SELECT (ts / 60) * 60 AS bucket, metric,
                          AVG(value), MIN(value), MAX(value), COUNT(*)
                     FROM samples
                    WHERE ts >= ? AND ts < ?
                 GROUP BY bucket, metric""",
                (start, end),
            )
            self._conn.commit()
            written = cur.rowcount
        self.set_meta("last_rollup_ts", str(end))
        return max(written, 0)

    def purge(self, raw_days: int, rollup_days: int, now: int | None = None) -> None:
        now = now if now is not None else int(time.time())
        raw_cutoff = now - raw_days * 86400
        rollup_cutoff = now - rollup_days * 86400
        placeholders = ",".join("?" * len(_PERMANENT_SEVERITIES))
        with self._lock:
            self._conn.execute("DELETE FROM samples WHERE ts < ?", (raw_cutoff,))
            self._conn.execute(
                "DELETE FROM container_samples WHERE ts < ?", (raw_cutoff,)
            )
            self._conn.execute("DELETE FROM samples_1m WHERE ts < ?", (rollup_cutoff,))
            # Incidents are never purged, and neither is anything that might
            # explain one.
            self._conn.execute(
                f"DELETE FROM events WHERE ts < ? AND severity NOT IN ({placeholders})",
                (rollup_cutoff, *_PERMANENT_SEVERITIES),
            )
            self._conn.commit()

    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")

    def db_size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self._path + suffix)
            except OSError:
                pass
        return total

    # ----------------------------------------------------- async convenience

    async def awrite_samples(self, ts: int, metrics: dict[str, float]) -> None:
        await asyncio.to_thread(self.write_samples, ts, metrics)

    async def awrite_container_samples(
        self, ts: int, stats: Iterable[dict[str, Any]]
    ) -> None:
        await asyncio.to_thread(self.write_container_samples, ts, list(stats))

    async def aadd_event(self, *args: Any, **kwargs: Any) -> int:
        return await asyncio.to_thread(self.add_event, *args, **kwargs)

    async def aquery(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.query, sql, args)

    async def amaintain(self, raw_days: int, rollup_days: int) -> None:
        await asyncio.to_thread(self.rollup)
        await asyncio.to_thread(self.purge, raw_days, rollup_days)
