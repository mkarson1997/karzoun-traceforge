from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    name TEXT NOT NULL,
    kind INTEGER NOT NULL,
    start_ns INTEGER NOT NULL,
    end_ns INTEGER NOT NULL,
    duration_ns INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    status_message TEXT NOT NULL,
    task_id TEXT,
    session_id TEXT,
    agent_name TEXT,
    service_name TEXT,
    resource_json TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    events_json TEXT NOT NULL,
    links_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (trace_id, span_id)
);

CREATE INDEX IF NOT EXISTS idx_spans_session_start
ON spans(session_id, start_ns DESC);

CREATE INDEX IF NOT EXISTS idx_spans_task_start
ON spans(task_id, start_ns DESC);

CREATE INDEX IF NOT EXISTS idx_spans_trace_start
ON spans(trace_id, start_ns ASC);
"""


class TraceRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def ingest(self, request: ExportTraceServiceRequest) -> int:
        rows: list[tuple[Any, ...]] = []
        for resource_spans in request.resource_spans:
            resource = _key_values(resource_spans.resource.attributes)
            service_name = _as_text(resource.get("service.name"))
            resource_json = _dump_json(resource)

            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    attributes = _key_values(span.attributes)
                    task_id = _first_text(
                        attributes,
                        "traceforge.task.id",
                        "task.id",
                        "gen_ai.request.id",
                    )
                    session_id = _first_text(
                        attributes,
                        "traceforge.session.id",
                        "session.id",
                        "gen_ai.conversation.id",
                    )
                    agent_name = _first_text(
                        attributes,
                        "traceforge.agent.name",
                        "agent.name",
                        "gen_ai.system",
                    )
                    events = [
                        {
                            "name": event.name,
                            "time_unix_nano": event.time_unix_nano,
                            "attributes": _key_values(event.attributes),
                        }
                        for event in span.events
                    ]
                    links = [
                        {
                            "trace_id": link.trace_id.hex(),
                            "span_id": link.span_id.hex(),
                            "attributes": _key_values(link.attributes),
                        }
                        for link in span.links
                    ]
                    start_ns = int(span.start_time_unix_nano)
                    end_ns = int(span.end_time_unix_nano)
                    rows.append(
                        (
                            span.trace_id.hex(),
                            span.span_id.hex(),
                            span.parent_span_id.hex() or None,
                            span.name,
                            int(span.kind),
                            start_ns,
                            end_ns,
                            max(0, end_ns - start_ns),
                            int(span.status.code),
                            span.status.message,
                            task_id,
                            session_id,
                            agent_name,
                            service_name,
                            resource_json,
                            _dump_json(attributes),
                            _dump_json(events),
                            _dump_json(links),
                        )
                    )

        if not rows:
            return 0

        statement = """
        INSERT INTO spans (
            trace_id, span_id, parent_span_id, name, kind, start_ns, end_ns,
            duration_ns, status_code, status_message, task_id, session_id,
            agent_name, service_name, resource_json, attributes_json,
            events_json, links_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trace_id, span_id) DO UPDATE SET
            parent_span_id=excluded.parent_span_id,
            name=excluded.name,
            kind=excluded.kind,
            start_ns=excluded.start_ns,
            end_ns=excluded.end_ns,
            duration_ns=excluded.duration_ns,
            status_code=excluded.status_code,
            status_message=excluded.status_message,
            task_id=excluded.task_id,
            session_id=excluded.session_id,
            agent_name=excluded.agent_name,
            service_name=excluded.service_name,
            resource_json=excluded.resource_json,
            attributes_json=excluded.attributes_json,
            events_json=excluded.events_json,
            links_json=excluded.links_json,
            ingested_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """
        with self._connect() as connection:
            connection.executemany(statement, rows)
        return len(rows)

    def stats(self) -> dict[str, int | str | None]:
        query = """
        SELECT
            COUNT(*) AS spans,
            COUNT(DISTINCT trace_id) AS traces,
            COUNT(DISTINCT NULLIF(session_id, '')) AS sessions,
            COUNT(DISTINCT NULLIF(task_id, '')) AS tasks,
            MAX(ingested_at) AS last_ingested_at
        FROM spans
        """
        with self._connect() as connection:
            row = connection.execute(query).fetchone()
        assert row is not None
        return dict(row)

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        query = """
        SELECT
            COALESCE(NULLIF(session_id, ''), 'unassigned') AS session_id,
            MAX(task_id) AS task_id,
            MAX(agent_name) AS agent_name,
            MAX(service_name) AS service_name,
            MIN(start_ns) AS start_ns,
            MAX(end_ns) AS end_ns,
            COUNT(*) AS span_count,
            COUNT(DISTINCT trace_id) AS trace_count
        FROM spans
        GROUP BY COALESCE(NULLIF(session_id, ''), 'unassigned')
        ORDER BY MAX(end_ns) DESC
        LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(query, (max(1, min(limit, 200)),)).fetchall()
        return [dict(row) for row in rows]

    def list_traces_for_session(self, session_id: str) -> list[dict[str, Any]]:
        if session_id == "unassigned":
            condition = "session_id IS NULL OR session_id = ''"
            params: tuple[Any, ...] = ()
        else:
            condition = "session_id = ?"
            params = (session_id,)
        query = f"""
        SELECT
            trace_id,
            MIN(start_ns) AS start_ns,
            MAX(end_ns) AS end_ns,
            COUNT(*) AS span_count,
            MAX(task_id) AS task_id,
            MAX(agent_name) AS agent_name,
            MAX(service_name) AS service_name
        FROM spans
        WHERE {condition}
        GROUP BY trace_id
        ORDER BY MIN(start_ns) DESC
        """
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        query = """
        SELECT *
        FROM spans
        WHERE trace_id = ?
        ORDER BY start_ns ASC, span_id ASC
        """
        with self._connect() as connection:
            rows = connection.execute(query, (trace_id.lower(),)).fetchall()
        if not rows:
            return None
        spans = [_decode_span_row(row) for row in rows]
        return {
            "trace_id": trace_id.lower(),
            "start_ns": min(span["start_ns"] for span in spans),
            "end_ns": max(span["end_ns"] for span in spans),
            "spans": spans,
        }


def _decode_span_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for field in ("resource_json", "attributes_json", "events_json", "links_json"):
        value[field.removesuffix("_json")] = json.loads(value.pop(field))
    return value


def _key_values(values: Any) -> dict[str, Any]:
    return {item.key: _any_value(item.value) for item in values}


def _any_value(value: AnyValue) -> Any:
    field = value.WhichOneof("value")
    if field is None:
        return None
    if field == "array_value":
        return [_any_value(item) for item in value.array_value.values]
    if field == "kvlist_value":
        return _key_values(value.kvlist_value.values)
    if field == "bytes_value":
        encoded = base64.b64encode(value.bytes_value).decode("ascii")
        return f"base64:{encoded}"
    return getattr(value, field)


def _first_text(attributes: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _as_text(attributes.get(key))
        if value:
            return value
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dump_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
