from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

_LEGACY_ORGANIZATION_ID = "org_legacy"

_TABLE_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS spans (
    organization_id TEXT NOT NULL,
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
    PRIMARY KEY (organization_id, trace_id, span_id)
);

CREATE TABLE IF NOT EXISTS privacy_exports (
    organization_id TEXT NOT NULL,
    export_id TEXT NOT NULL,
    findings INTEGER NOT NULL,
    attributes_removed INTEGER NOT NULL,
    attributes_rewritten INTEGER NOT NULL,
    spans_seen INTEGER NOT NULL,
    events_seen INTEGER NOT NULL,
    links_seen INTEGER NOT NULL,
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (organization_id, export_id)
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_spans_org_session_start
ON spans(organization_id, session_id, start_ns DESC);

CREATE INDEX IF NOT EXISTS idx_spans_org_task_start
ON spans(organization_id, task_id, start_ns DESC);

CREATE INDEX IF NOT EXISTS idx_spans_org_trace_start
ON spans(organization_id, trace_id, start_ns ASC);

CREATE INDEX IF NOT EXISTS idx_spans_org_service_start
ON spans(organization_id, service_name, start_ns DESC);

CREATE INDEX IF NOT EXISTS idx_spans_org_agent_start
ON spans(organization_id, agent_name, start_ns DESC);
"""


class TraceRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_TABLE_SCHEMA)
            _migrate_tenant_schema(connection)
            connection.executescript(_INDEX_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def ingest(self, request: ExportTraceServiceRequest) -> int:
        rows: list[tuple[Any, ...]] = []
        privacy_rows: list[tuple[Any, ...]] = []
        for resource_spans in request.resource_spans:
            resource = _key_values(resource_spans.resource.attributes)
            organization_id = (
                _as_text(resource.get("traceforge.organization.id"))
                or _LEGACY_ORGANIZATION_ID
            )
            privacy_summary = _privacy_summary(resource, organization_id)
            if privacy_summary is not None:
                privacy_rows.append(privacy_summary)

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
                            organization_id,
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

        if not rows and not privacy_rows:
            return 0

        span_statement = """
        INSERT INTO spans (
            organization_id, trace_id, span_id, parent_span_id, name, kind,
            start_ns, end_ns, duration_ns, status_code, status_message, task_id,
            session_id, agent_name, service_name, resource_json, attributes_json,
            events_json, links_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(organization_id, trace_id, span_id) DO UPDATE SET
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
        privacy_statement = """
        INSERT INTO privacy_exports (
            organization_id, export_id, findings, attributes_removed,
            attributes_rewritten, spans_seen, events_seen, links_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(organization_id, export_id) DO UPDATE SET
            findings=excluded.findings,
            attributes_removed=excluded.attributes_removed,
            attributes_rewritten=excluded.attributes_rewritten,
            spans_seen=excluded.spans_seen,
            events_seen=excluded.events_seen,
            links_seen=excluded.links_seen,
            ingested_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """
        with self._connect() as connection:
            if rows:
                connection.executemany(span_statement, rows)
            if privacy_rows:
                connection.executemany(privacy_statement, privacy_rows)
        return len(rows)

    def stats(
        self,
        *,
        organization_id: str | None = None,
    ) -> dict[str, int | str | None]:
        span_where, span_params = _organization_where(organization_id)
        span_query = f"""
        SELECT
            COUNT(*) AS spans,
            COUNT(DISTINCT trace_id) AS traces,
            COUNT(DISTINCT NULLIF(session_id, '')) AS sessions,
            COUNT(DISTINCT NULLIF(task_id, '')) AS tasks,
            MAX(ingested_at) AS last_ingested_at
        FROM spans
        {span_where}
        """
        privacy_where, privacy_params = _organization_where(organization_id)
        privacy_query = f"""
        SELECT
            COUNT(*) AS privacy_exports,
            COALESCE(SUM(findings), 0) AS privacy_findings,
            COALESCE(SUM(attributes_removed), 0) AS privacy_attributes_removed,
            COALESCE(SUM(attributes_rewritten), 0) AS privacy_attributes_rewritten
        FROM privacy_exports
        {privacy_where}
        """
        with self._connect() as connection:
            span_row = connection.execute(span_query, span_params).fetchone()
            privacy_row = connection.execute(privacy_query, privacy_params).fetchone()
        assert span_row is not None
        assert privacy_row is not None
        return {**dict(span_row), **dict(privacy_row)}

    def list_sessions(
        self,
        limit: int = 50,
        *,
        organization_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = _organization_where(organization_id)
        query = f"""
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
        {where}
        GROUP BY COALESCE(NULLIF(session_id, ''), 'unassigned')
        ORDER BY MAX(end_ns) DESC
        LIMIT ?
        """
        params.append(max(1, min(limit, 200)))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_traces_for_session(
        self,
        session_id: str,
        *,
        organization_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if organization_id is not None:
            clauses.append("organization_id = ?")
            params.append(organization_id)
        if session_id == "unassigned":
            clauses.append("(session_id IS NULL OR session_id = '')")
        else:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = "WHERE " + " AND ".join(clauses)
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
        {where}
        GROUP BY trace_id
        ORDER BY MIN(start_ns) DESC
        """
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_trace(
        self,
        trace_id: str,
        *,
        organization_id: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["trace_id = ?"]
        params: list[Any] = [trace_id.lower()]
        if organization_id is not None:
            clauses.append("organization_id = ?")
            params.append(organization_id)
        query = f"""
        SELECT *
        FROM spans
        WHERE {" AND ".join(clauses)}
        ORDER BY start_ns ASC, span_id ASC
        """
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        if not rows:
            return None
        spans = [_decode_span_row(row) for row in rows]
        return {
            "trace_id": trace_id.lower(),
            "start_ns": min(span["start_ns"] for span in spans),
            "end_ns": max(span["end_ns"] for span in spans),
            "spans": spans,
        }

    def search_spans(
        self,
        query: str = "",
        *,
        service: str | None = None,
        agent: str | None = None,
        status_code: int | None = None,
        limit: int = 100,
        organization_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if organization_id is not None:
            clauses.append("organization_id = ?")
            params.append(organization_id)
        query = query.strip()
        if query:
            pattern = f"%{_escape_like(query.lower())}%"
            search_columns = (
                "trace_id",
                "span_id",
                "name",
                "COALESCE(task_id, '')",
                "COALESCE(session_id, '')",
                "COALESCE(agent_name, '')",
                "COALESCE(service_name, '')",
                "attributes_json",
                "events_json",
            )
            clauses.append(
                "("
                + " OR ".join(
                    f"LOWER({column}) LIKE ? ESCAPE '\\'" for column in search_columns
                )
                + ")"
            )
            params.extend([pattern] * len(search_columns))
        if service:
            clauses.append("service_name = ?")
            params.append(service)
        if agent:
            clauses.append("agent_name = ?")
            params.append(agent)
        if status_code is not None:
            clauses.append("status_code = ?")
            params.append(status_code)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 500)))
        sql = f"""
        SELECT *
        FROM spans
        {where}
        ORDER BY start_ns DESC
        LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_decode_span_row(row) for row in rows]

    def list_privacy_exports(
        self,
        limit: int = 50,
        *,
        organization_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = _organization_where(organization_id)
        params.append(max(1, min(limit, 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM privacy_exports
                {where}
                ORDER BY ingested_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def export_spans(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        limit: int = 10_000,
        organization_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if organization_id is not None:
            clauses.append("organization_id = ?")
            params.append(organization_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 100_000)))
        sql = f"""
        SELECT *
        FROM spans
        {where}
        ORDER BY start_ns ASC, trace_id ASC, span_id ASC
        LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_decode_span_row(row) for row in rows]


def _migrate_tenant_schema(connection: sqlite3.Connection) -> None:
    span_info = connection.execute("PRAGMA table_info(spans)").fetchall()
    span_columns = {str(row["name"]) for row in span_info}
    span_pk = [
        str(row["name"])
        for row in sorted(span_info, key=lambda row: int(row["pk"]))
        if int(row["pk"]) > 0
    ]
    if "organization_id" not in span_columns or span_pk != [
        "organization_id",
        "trace_id",
        "span_id",
    ]:
        connection.execute("ALTER TABLE spans RENAME TO spans_pre_tenant")
        connection.executescript(_TABLE_SCHEMA.split("CREATE TABLE IF NOT EXISTS privacy_exports")[0])
        connection.execute(
            """
            INSERT INTO spans (
                organization_id, trace_id, span_id, parent_span_id, name, kind,
                start_ns, end_ns, duration_ns, status_code, status_message, task_id,
                session_id, agent_name, service_name, resource_json, attributes_json,
                events_json, links_json, ingested_at
            )
            SELECT
                ?, trace_id, span_id, parent_span_id, name, kind,
                start_ns, end_ns, duration_ns, status_code, status_message, task_id,
                session_id, agent_name, service_name, resource_json, attributes_json,
                events_json, links_json, ingested_at
            FROM spans_pre_tenant
            """,
            (_LEGACY_ORGANIZATION_ID,),
        )
        connection.execute("DROP TABLE spans_pre_tenant")

    privacy_info = connection.execute("PRAGMA table_info(privacy_exports)").fetchall()
    privacy_columns = {str(row["name"]) for row in privacy_info}
    privacy_pk = [
        str(row["name"])
        for row in sorted(privacy_info, key=lambda row: int(row["pk"]))
        if int(row["pk"]) > 0
    ]
    if "organization_id" not in privacy_columns or privacy_pk != [
        "organization_id",
        "export_id",
    ]:
        connection.execute("ALTER TABLE privacy_exports RENAME TO privacy_exports_pre_tenant")
        connection.execute(
            """
            CREATE TABLE privacy_exports (
                organization_id TEXT NOT NULL,
                export_id TEXT NOT NULL,
                findings INTEGER NOT NULL,
                attributes_removed INTEGER NOT NULL,
                attributes_rewritten INTEGER NOT NULL,
                spans_seen INTEGER NOT NULL,
                events_seen INTEGER NOT NULL,
                links_seen INTEGER NOT NULL,
                ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY (organization_id, export_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO privacy_exports (
                organization_id, export_id, findings, attributes_removed,
                attributes_rewritten, spans_seen, events_seen, links_seen, ingested_at
            )
            SELECT
                ?, export_id, findings, attributes_removed,
                attributes_rewritten, spans_seen, events_seen, links_seen, ingested_at
            FROM privacy_exports_pre_tenant
            """,
            (_LEGACY_ORGANIZATION_ID,),
        )
        connection.execute("DROP TABLE privacy_exports_pre_tenant")


def _privacy_summary(
    resource: dict[str, Any],
    organization_id: str,
) -> tuple[Any, ...] | None:
    export_id = _as_text(resource.get("traceforge.privacy.export_id"))
    if not export_id:
        return None
    return (
        organization_id,
        export_id,
        _as_int(resource.get("traceforge.privacy.findings")),
        _as_int(resource.get("traceforge.privacy.attributes_removed")),
        _as_int(resource.get("traceforge.privacy.attributes_rewritten")),
        _as_int(resource.get("traceforge.privacy.spans_seen")),
        _as_int(resource.get("traceforge.privacy.events_seen")),
        _as_int(resource.get("traceforge.privacy.links_seen")),
    )


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


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _organization_where(organization_id: str | None) -> tuple[str, list[Any]]:
    if organization_id is None:
        return "", []
    return "WHERE organization_id = ?", [organization_id]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _dump_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
