from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

_BASE_EXTEND = r'''
| extend task_id = tostring(coalesce(
    TraceAttributes["traceforge.task.id"],
    TraceAttributes["task.id"],
    TraceAttributes["gen_ai.request.id"]
  ))
| extend session_id = tostring(coalesce(
    TraceAttributes["traceforge.session.id"],
    TraceAttributes["session.id"],
    TraceAttributes["gen_ai.conversation.id"]
  ))
| extend agent_name = tostring(coalesce(
    TraceAttributes["traceforge.agent.name"],
    TraceAttributes["agent.name"],
    TraceAttributes["gen_ai.system"]
  ))
| extend service_name = tostring(ResourceAttributes["service.name"])
| extend organization_id = tostring(ResourceAttributes["traceforge.organization.id"])
| extend status_code = case(
    toupper(SpanStatus) contains "ERROR", 2,
    toupper(SpanStatus) contains "OK", 1,
    0
  )
'''


class KustoTraceRepository:
    """Read-only repository backed by the OTELTraces table in Azure Data Explorer.

    User-controlled values are passed as Kusto query parameters rather than interpolated
    into KQL. This keeps the production viewer query-only and avoids turning search
    inputs into executable query fragments.
    """

    def __init__(
        self,
        cluster_uri: str,
        database: str,
        *,
        client_id: str | None = None,
        auth: str = "managed_identity",
        client: Any | None = None,
        request_properties_factory: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.cluster_uri = cluster_uri.rstrip("/")
        self.database = database
        self.client_id = client_id
        self.auth = auth

        if client is None:
            client, default_factory = _build_client(
                self.cluster_uri,
                client_id=client_id,
                auth=auth,
            )
            self._request_properties_factory = default_factory
        else:
            self._request_properties_factory = request_properties_factory
        self._client = client

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def stats(
        self,
        *,
        organization_id: str | None = None,
    ) -> dict[str, int | str | None]:
        org_filter = (
            "| where organization_id == tf_org\n" if organization_id is not None else ""
        )
        org_declaration = (
            "declare query_parameters(tf_org:string);\n"
            if organization_id is not None
            else ""
        )
        org_parameters = {"tf_org": organization_id} if organization_id is not None else None
        trace_rows = self._execute(
            org_declaration
            + """
OTELTraces
"""
            + _BASE_EXTEND
            + org_filter
            + """
| summarize
    spans=count(),
    traces=dcount(TraceID),
    sessions=dcountif(session_id, isnotempty(session_id)),
    tasks=dcountif(task_id, isnotempty(task_id)),
    last_ingested_at=max(EndTime)
""",
            org_parameters,
        )
        privacy_rows = self._execute(
            org_declaration
            + """
OTELTraces
| extend organization_id=tostring(ResourceAttributes["traceforge.organization.id"])
| extend export_id=tostring(ResourceAttributes["traceforge.privacy.export_id"])
| extend findings=tolong(ResourceAttributes["traceforge.privacy.findings"])
| extend attributes_removed=tolong(ResourceAttributes["traceforge.privacy.attributes_removed"])
| extend attributes_rewritten=tolong(ResourceAttributes["traceforge.privacy.attributes_rewritten"])
"""
            + org_filter
            + f"""
| where isnotempty(export_id)
| summarize
    findings=max(findings),
    attributes_removed=max(attributes_removed),
    attributes_rewritten=max(attributes_rewritten)
  by export_id
| summarize
    privacy_exports=count(),
    privacy_findings=sum(findings),
    privacy_attributes_removed=sum(attributes_removed),
    privacy_attributes_rewritten=sum(attributes_rewritten)
""",
            org_parameters,
        )
        base = trace_rows[0] if trace_rows else {}
        privacy = privacy_rows[0] if privacy_rows else {}
        return {
            "spans": _as_int(base.get("spans")),
            "traces": _as_int(base.get("traces")),
            "sessions": _as_int(base.get("sessions")),
            "tasks": _as_int(base.get("tasks")),
            "last_ingested_at": _iso_datetime(base.get("last_ingested_at")),
            "privacy_exports": _as_int(privacy.get("privacy_exports")),
            "privacy_findings": _as_int(privacy.get("privacy_findings")),
            "privacy_attributes_removed": _as_int(
                privacy.get("privacy_attributes_removed")
            ),
            "privacy_attributes_rewritten": _as_int(
                privacy.get("privacy_attributes_rewritten")
            ),
        }

    def list_sessions(
        self,
        limit: int = 50,
        *,
        organization_id: str | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 200))
        org_declaration = (
            "declare query_parameters(tf_org:string);\n"
            if organization_id is not None
            else ""
        )
        org_filter = (
            "| where organization_id == tf_org\n" if organization_id is not None else ""
        )
        rows = self._execute(
            org_declaration
            + """
OTELTraces
"""
            + _BASE_EXTEND
            + org_filter
            + f"""
| extend session_key=iff(isempty(session_id), "unassigned", session_id)
| summarize
    task_id=take_any(task_id),
    agent_name=take_any(agent_name),
    service_name=take_any(service_name),
    start_time=min(StartTime),
    end_time=max(EndTime),
    span_count=count(),
    trace_count=dcount(TraceID)
  by session_id=session_key
| top {safe_limit} by end_time desc
""",
            {"tf_org": organization_id} if organization_id is not None else None,
        )
        return [
            {
                "session_id": str(row.get("session_id") or "unassigned"),
                "task_id": _optional_text(row.get("task_id")),
                "agent_name": _optional_text(row.get("agent_name")),
                "service_name": _optional_text(row.get("service_name")),
                "start_ns": _datetime_to_ns(row.get("start_time")),
                "end_ns": _datetime_to_ns(row.get("end_time")),
                "span_count": _as_int(row.get("span_count")),
                "trace_count": _as_int(row.get("trace_count")),
            }
            for row in rows
        ]

    def list_traces_for_session(
        self,
        session_id: str,
        *,
        organization_id: str | None = None,
    ) -> list[dict[str, Any]]:
        declaration = (
            "declare query_parameters(tf_session:string, tf_org:string);\n"
            if organization_id is not None
            else "declare query_parameters(tf_session:string);\n"
        )
        org_filter = (
            "| where organization_id == tf_org\n" if organization_id is not None else ""
        )
        query = (
            declaration
            + "OTELTraces\n"
            + _BASE_EXTEND
            + org_filter
            + """
| where (tf_session == "unassigned" and isempty(session_id))
    or (tf_session != "unassigned" and session_id == tf_session)
| summarize
    start_time=min(StartTime),
    end_time=max(EndTime),
    span_count=count(),
    task_id=take_any(task_id),
    agent_name=take_any(agent_name),
    service_name=take_any(service_name)
  by trace_id=TraceID
| order by start_time desc
"""
        )
        parameters: dict[str, Any] = {"tf_session": session_id}
        if organization_id is not None:
            parameters["tf_org"] = organization_id
        rows = self._execute(query, parameters)
        return [
            {
                "trace_id": str(row.get("trace_id") or "").lower(),
                "start_ns": _datetime_to_ns(row.get("start_time")),
                "end_ns": _datetime_to_ns(row.get("end_time")),
                "span_count": _as_int(row.get("span_count")),
                "task_id": _optional_text(row.get("task_id")),
                "agent_name": _optional_text(row.get("agent_name")),
                "service_name": _optional_text(row.get("service_name")),
            }
            for row in rows
        ]

    def get_trace(
        self,
        trace_id: str,
        *,
        organization_id: str | None = None,
    ) -> dict[str, Any] | None:
        declaration = (
            "declare query_parameters(tf_trace:string, tf_org:string);\n"
            if organization_id is not None
            else "declare query_parameters(tf_trace:string);\n"
        )
        org_filter = (
            "| where organization_id == tf_org\n" if organization_id is not None else ""
        )
        query = (
            declaration
            + "OTELTraces\n"
            + _BASE_EXTEND
            + org_filter
            + """
| where tolower(TraceID) == tolower(tf_trace)
| project
    trace_id=TraceID,
    span_id=SpanID,
    parent_span_id=ParentID,
    name=SpanName,
    span_kind=SpanKind,
    start_time=StartTime,
    end_time=EndTime,
    status_code,
    status_message=SpanStatusMessage,
    task_id,
    session_id,
    agent_name,
    service_name,
    resource=ResourceAttributes,
    attributes=TraceAttributes,
    events=Events,
    links=Links
| order by start_time asc, span_id asc
"""
        )
        parameters: dict[str, Any] = {"tf_trace": trace_id}
        if organization_id is not None:
            parameters["tf_org"] = organization_id
        rows = self._execute(query, parameters)
        if not rows:
            return None
        spans = [_span_from_row(row) for row in rows]
        return {
            "trace_id": str(rows[0].get("trace_id") or trace_id).lower(),
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
        safe_limit = max(1, min(limit, 500))
        declaration = (
            "declare query_parameters(tf_query:string, tf_service:string, "
            "tf_agent:string, tf_status:long, tf_org:string);\n"
            if organization_id is not None
            else "declare query_parameters(tf_query:string, tf_service:string, "
            "tf_agent:string, tf_status:long);\n"
        )
        org_filter = (
            "| where organization_id == tf_org\n" if organization_id is not None else ""
        )
        kql = (
            declaration
            + "OTELTraces\n"
            + _BASE_EXTEND
            + org_filter
            + f"""
| where isempty(tf_query)
    or tolower(TraceID) contains tolower(tf_query)
    or tolower(SpanID) contains tolower(tf_query)
    or tolower(SpanName) contains tolower(tf_query)
    or tolower(task_id) contains tolower(tf_query)
    or tolower(session_id) contains tolower(tf_query)
    or tolower(agent_name) contains tolower(tf_query)
    or tolower(service_name) contains tolower(tf_query)
    or tolower(tostring(TraceAttributes)) contains tolower(tf_query)
    or tolower(tostring(Events)) contains tolower(tf_query)
| where isempty(tf_service) or service_name == tf_service
| where isempty(tf_agent) or agent_name == tf_agent
| where tf_status < 0 or status_code == tf_status
| project
    trace_id=TraceID,
    span_id=SpanID,
    parent_span_id=ParentID,
    name=SpanName,
    span_kind=SpanKind,
    start_time=StartTime,
    end_time=EndTime,
    status_code,
    status_message=SpanStatusMessage,
    task_id,
    session_id,
    agent_name,
    service_name,
    resource=ResourceAttributes,
    attributes=TraceAttributes,
    events=Events,
    links=Links
| top {safe_limit} by start_time desc
"""
        )
        rows = self._execute(
            kql,
            {
                "tf_query": query.strip(),
                "tf_service": service or "",
                "tf_agent": agent or "",
                "tf_status": status_code if status_code is not None else -1,
                **({"tf_org": organization_id} if organization_id is not None else {}),
            },
        )
        return [_span_from_row(row) for row in rows]

    def list_privacy_exports(
        self,
        limit: int = 50,
        *,
        organization_id: str | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        org_declaration = (
            "declare query_parameters(tf_org:string);\n"
            if organization_id is not None
            else ""
        )
        org_filter = (
            "| where organization_id == tf_org\n" if organization_id is not None else ""
        )
        rows = self._execute(
            org_declaration
            + """
OTELTraces
| extend organization_id=tostring(ResourceAttributes["traceforge.organization.id"])
| extend export_id=tostring(ResourceAttributes["traceforge.privacy.export_id"])
| extend findings=tolong(ResourceAttributes["traceforge.privacy.findings"])
| extend attributes_removed=tolong(ResourceAttributes["traceforge.privacy.attributes_removed"])
| extend attributes_rewritten=tolong(ResourceAttributes["traceforge.privacy.attributes_rewritten"])
| extend spans_seen=tolong(ResourceAttributes["traceforge.privacy.spans_seen"])
| extend events_seen=tolong(ResourceAttributes["traceforge.privacy.events_seen"])
| extend links_seen=tolong(ResourceAttributes["traceforge.privacy.links_seen"])
"""
            + org_filter
            + """
| where isnotempty(export_id)
| summarize
    findings=max(findings),
    attributes_removed=max(attributes_removed),
    attributes_rewritten=max(attributes_rewritten),
    spans_seen=max(spans_seen),
    events_seen=max(events_seen),
    links_seen=max(links_seen),
    ingested_at=max(EndTime)
  by export_id
| top {safe_limit} by ingested_at desc
""",
            {"tf_org": organization_id} if organization_id is not None else None,
        )
        return [
            {
                "export_id": str(row.get("export_id") or ""),
                "findings": _as_int(row.get("findings")),
                "attributes_removed": _as_int(row.get("attributes_removed")),
                "attributes_rewritten": _as_int(row.get("attributes_rewritten")),
                "spans_seen": _as_int(row.get("spans_seen")),
                "events_seen": _as_int(row.get("events_seen")),
                "links_seen": _as_int(row.get("links_seen")),
                "ingested_at": _iso_datetime(row.get("ingested_at")),
            }
            for row in rows
        ]

    def export_spans(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        limit: int = 10_000,
        organization_id: str | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 100_000))
        declaration = (
            "declare query_parameters(tf_session:string, tf_task:string, tf_org:string);\n"
            if organization_id is not None
            else "declare query_parameters(tf_session:string, tf_task:string);\n"
        )
        org_filter = (
            "| where organization_id == tf_org\n" if organization_id is not None else ""
        )
        query = (
            declaration
            + "OTELTraces\n"
            + _BASE_EXTEND
            + org_filter
            + f"""
| where isempty(tf_session) or session_id == tf_session
| where isempty(tf_task) or task_id == tf_task
| project
    trace_id=TraceID,
    span_id=SpanID,
    parent_span_id=ParentID,
    name=SpanName,
    span_kind=SpanKind,
    start_time=StartTime,
    end_time=EndTime,
    status_code,
    status_message=SpanStatusMessage,
    task_id,
    session_id,
    agent_name,
    service_name,
    resource=ResourceAttributes,
    attributes=TraceAttributes,
    events=Events,
    links=Links
| top {safe_limit} by start_time asc
"""
        )
        rows = self._execute(
            query,
            {
                "tf_session": session_id or "",
                "tf_task": task_id or "",
                **({"tf_org": organization_id} if organization_id is not None else {}),
            },
        )
        return [_span_from_row(row) for row in rows]

    def _execute(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        properties = None
        if parameters and self._request_properties_factory is not None:
            properties = self._request_properties_factory(parameters)

        if properties is None:
            response = self._client.execute(self.database, query)
        else:
            response = self._client.execute(self.database, query, properties)

        table = response.primary_results[0]
        names = [column.column_name for column in table.columns]
        return [{name: row[index] for index, name in enumerate(names)} for row in table]


def _build_client(
    cluster_uri: str,
    *,
    client_id: str | None,
    auth: str,
) -> tuple[Any, Callable[[Mapping[str, Any]], Any]]:
    try:
        from azure.kusto.data import (
            ClientRequestProperties,
            KustoClient,
            KustoConnectionStringBuilder,
        )
    except ImportError as exc:  # pragma: no cover - exercised only without optional extra
        raise RuntimeError(
            "Azure viewer support requires the 'azure' extra: "
            "pip install 'karzoun-traceforge[azure]'"
        ) from exc

    if auth == "interactive":
        connection = KustoConnectionStringBuilder.with_interactive_login(cluster_uri)
    elif auth == "managed_identity":
        connection = KustoConnectionStringBuilder.with_aad_managed_service_identity_authentication(
            cluster_uri,
            client_id=client_id,
        )
    else:
        raise ValueError("auth must be 'managed_identity' or 'interactive'")

    def request_properties(parameters: Mapping[str, Any]) -> Any:
        props = ClientRequestProperties()
        for name, value in parameters.items():
            props.set_parameter(name, str(value))
        return props

    return KustoClient(connection), request_properties


def _span_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    start_ns = _datetime_to_ns(row.get("start_time"))
    end_ns = _datetime_to_ns(row.get("end_time"))
    return {
        "trace_id": str(row.get("trace_id") or "").lower(),
        "span_id": str(row.get("span_id") or "").lower(),
        "parent_span_id": _optional_text(row.get("parent_span_id")),
        "name": str(row.get("name") or ""),
        "kind": row.get("span_kind"),
        "start_ns": start_ns,
        "end_ns": end_ns,
        "duration_ns": max(0, end_ns - start_ns),
        "status_code": _as_int(row.get("status_code")),
        "status_message": str(row.get("status_message") or ""),
        "task_id": _optional_text(row.get("task_id")),
        "session_id": _optional_text(row.get("session_id")),
        "agent_name": _optional_text(row.get("agent_name")),
        "service_name": _optional_text(row.get("service_name")),
        "resource": _dynamic(row.get("resource"), {}),
        "attributes": _dynamic(row.get("attributes"), {}),
        "events": _dynamic(row.get("events"), []),
        "links": _dynamic(row.get("links"), []),
    }


def _dynamic(value: Any, default: Any) -> Any:
    return default if value is None else value


def _datetime_to_ns(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
        return (
            (delta.days * 86_400 + delta.seconds) * 1_000_000_000
            + delta.microseconds * 1_000
        )
    if isinstance(value, (int, float)):
        return int(float(value) * 1_000_000_000)
    text = str(value).strip()
    if not text:
        return 0
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _datetime_to_ns(parsed)


def _iso_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    return text or None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
