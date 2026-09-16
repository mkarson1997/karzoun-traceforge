from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from traceforge.demo import build_demo_request
from traceforge.store.repository import TraceRepository
from traceforge.store.retention import RetentionSweeper


def test_retention_sweeper_removes_only_expired_rows(tmp_path) -> None:
    database = tmp_path / "traceforge.db"
    repository = TraceRepository(database)
    old_request, old_identity = build_demo_request(
        trace_id=bytes.fromhex("31" * 16),
        session_id="session-old",
        task_id="task-old",
        start_ns=1_000_000_000,
    )
    recent_request, recent_identity = build_demo_request(
        trace_id=bytes.fromhex("32" * 16),
        session_id="session-recent",
        task_id="task-recent",
        start_ns=2_000_000_000,
    )
    repository.ingest(old_request)
    repository.ingest(recent_request)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE spans SET ingested_at = ? WHERE trace_id = ?",
            ("2026-07-01T00:00:00.000Z", old_identity.trace_id),
        )
        connection.execute(
            "UPDATE spans SET ingested_at = ? WHERE trace_id = ?",
            ("2026-09-15T00:00:00.000Z", recent_identity.trace_id),
        )
        connection.execute(
            """
            INSERT INTO privacy_exports (
                export_id, findings, attributes_removed, attributes_rewritten,
                spans_seen, events_seen, links_seen, ingested_at
            ) VALUES (?, 1, 1, 0, 4, 0, 0, ?)
            """,
            ("privacy-old", "2026-07-01T00:00:00.000Z"),
        )
        connection.execute(
            """
            INSERT INTO privacy_exports (
                export_id, findings, attributes_removed, attributes_rewritten,
                spans_seen, events_seen, links_seen, ingested_at
            ) VALUES (?, 1, 1, 0, 4, 0, 0, ?)
            """,
            ("privacy-recent", "2026-09-15T00:00:00.000Z"),
        )

    sweeper = RetentionSweeper(
        database,
        retention_days=30,
        now=lambda: datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
    )
    result = sweeper.maybe_sweep(force=True)

    assert result is not None
    assert result["spans_deleted"] == 4
    assert result["privacy_exports_deleted"] == 1
    assert repository.get_trace(old_identity.trace_id) is None
    assert repository.get_trace(recent_identity.trace_id) is not None
    exports = repository.list_privacy_exports()
    assert [row["export_id"] for row in exports] == ["privacy-recent"]


def test_retention_sweeper_is_interval_throttled(tmp_path) -> None:
    database = tmp_path / "traceforge.db"
    TraceRepository(database)
    clock = [10.0]
    sweeper = RetentionSweeper(
        database,
        retention_days=30,
        sweep_interval_seconds=60,
        clock=lambda: clock[0],
        now=lambda: datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
    )

    assert sweeper.maybe_sweep() is not None
    assert sweeper.maybe_sweep() is None
    clock[0] += 60.0
    assert sweeper.maybe_sweep() is not None


def test_zero_retention_days_disables_pruning(tmp_path) -> None:
    database = tmp_path / "traceforge.db"
    TraceRepository(database)
    sweeper = RetentionSweeper(database, retention_days=0)

    assert sweeper.maybe_sweep(force=True) is None
