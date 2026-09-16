from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from traceforge.audit import emit_audit_event


class RetentionResult(TypedDict):
    spans_deleted: int
    privacy_exports_deleted: int
    cutoff: str


class RetentionSweeper:
    """Periodically removes locally persisted telemetry older than the policy window."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        retention_days: int = 30,
        sweep_interval_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if retention_days < 0:
            raise ValueError("retention_days must be zero or greater")
        if sweep_interval_seconds <= 0:
            raise ValueError("sweep_interval_seconds must be greater than 0")
        self.database_path = Path(database_path)
        self.retention_days = retention_days
        self.sweep_interval_seconds = sweep_interval_seconds
        self._clock = clock
        self._now = now
        self._next_sweep = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.retention_days > 0

    def maybe_sweep(self, *, force: bool = False) -> RetentionResult | None:
        if not self.enabled:
            return None
        current = self._clock()
        if not force and current < self._next_sweep:
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            current = self._clock()
            if not force and current < self._next_sweep:
                return None
            result = self._sweep()
            self._next_sweep = current + self.sweep_interval_seconds
            return result
        finally:
            self._lock.release()

    def _sweep(self) -> RetentionResult:
        cutoff = self._now().astimezone(UTC) - timedelta(days=self.retention_days)
        cutoff_text = _sqlite_timestamp(cutoff)
        with sqlite3.connect(self.database_path, timeout=10) as connection:
            spans_cursor = connection.execute(
                "DELETE FROM spans WHERE ingested_at < ?",
                (cutoff_text,),
            )
            privacy_cursor = connection.execute(
                "DELETE FROM privacy_exports WHERE ingested_at < ?",
                (cutoff_text,),
            )
            spans_deleted = max(0, spans_cursor.rowcount)
            privacy_exports_deleted = max(0, privacy_cursor.rowcount)

        result: RetentionResult = {
            "spans_deleted": spans_deleted,
            "privacy_exports_deleted": privacy_exports_deleted,
            "cutoff": cutoff_text,
        }
        emit_audit_event(
            "retention.prune",
            "success",
            component="store",
            spans_deleted=spans_deleted,
            privacy_exports_deleted=privacy_exports_deleted,
            retention_days=self.retention_days,
        )
        return result


def _sqlite_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
