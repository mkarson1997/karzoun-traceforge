from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from traceforge.kusto.repository import KustoTraceRepository


class _FakeColumn:
    def __init__(self, name: str) -> None:
        self.column_name = name


class _FakeTable:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        names = list(rows[0]) if rows else []
        self.columns = [_FakeColumn(name) for name in names]

    def __iter__(self):
        for row in self._rows:
            yield [row[column.column_name] for column in self.columns]


class _FakeClient:
    def __init__(self, responses: list[list[dict[str, object]]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, object | None]] = []

    def execute(self, database: str, query: str, properties=None):
        self.calls.append((database, query, properties))
        rows = self.responses.pop(0)
        return SimpleNamespace(primary_results=[_FakeTable(rows)])


def _repo(client: _FakeClient) -> KustoTraceRepository:
    return KustoTraceRepository(
        "https://example.kusto.windows.net",
        "traceforge",
        client=client,
        request_properties_factory=lambda params: dict(params),
    )


def test_search_uses_query_parameters_instead_of_interpolating_user_input():
    client = _FakeClient([[]])
    repository = _repo(client)
    malicious = "x') | take 999999 //"

    assert repository.search_spans(malicious, service="svc", agent="codex") == []

    _, query, properties = client.calls[0]
    assert malicious not in query
    assert properties == {
        "tf_query": malicious,
        "tf_service": "svc",
        "tf_agent": "codex",
        "tf_status": -1,
    }
    assert "declare query_parameters" in query


def test_kusto_trace_rows_match_local_viewer_shape():
    start = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    end = datetime(2026, 9, 15, 12, 0, 0, 250000, tzinfo=UTC)
    client = _FakeClient(
        [
            [
                {
                    "trace_id": "AA" * 16,
                    "span_id": "BB" * 8,
                    "parent_span_id": "",
                    "name": "agent.run",
                    "span_kind": "SPAN_KIND_INTERNAL",
                    "start_time": start,
                    "end_time": end,
                    "status_code": 1,
                    "status_message": "",
                    "task_id": "task-1",
                    "session_id": "session-1",
                    "agent_name": "codex",
                    "service_name": "traceforge-demo",
                    "resource": {"service.name": "traceforge-demo"},
                    "attributes": {"traceforge.task.id": "task-1"},
                    "events": [],
                    "links": [],
                }
            ]
        ]
    )
    repository = _repo(client)

    trace = repository.get_trace("aa" * 16)

    assert trace is not None
    assert trace["trace_id"] == "aa" * 16
    assert len(trace["spans"]) == 1
    span = trace["spans"][0]
    assert span["span_id"] == "bb" * 8
    assert span["duration_ns"] == 250_000_000
    assert span["session_id"] == "session-1"
    assert span["attributes"] == {"traceforge.task.id": "task-1"}


def test_stats_deduplicate_privacy_exports_server_side():
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    client = _FakeClient(
        [
            [
                {
                    "spans": 8,
                    "traces": 2,
                    "sessions": 2,
                    "tasks": 2,
                    "last_ingested_at": now,
                }
            ],
            [
                {
                    "privacy_exports": 2,
                    "privacy_findings": 7,
                    "privacy_attributes_removed": 3,
                    "privacy_attributes_rewritten": 4,
                }
            ],
        ]
    )
    repository = _repo(client)

    stats = repository.stats()

    assert stats["spans"] == 8
    assert stats["privacy_exports"] == 2
    assert stats["privacy_findings"] == 7
    assert stats["last_ingested_at"] == "2026-09-15T12:00:00Z"
    privacy_query = client.calls[1][1]
    assert "summarize" in privacy_query
    assert "by export_id" in privacy_query
