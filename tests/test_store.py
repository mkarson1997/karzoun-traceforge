from __future__ import annotations

import json

from traceforge.demo import build_demo_request
from traceforge.store.repository import TraceRepository


def test_repository_ingests_correlated_trace(tmp_path):
    request, identity = build_demo_request(
        trace_id=bytes.fromhex("11" * 16),
        session_id="session-test",
        task_id="task-test",
        start_ns=1_000_000_000,
    )
    repository = TraceRepository(tmp_path / "traceforge.db")

    assert repository.ingest(request) == 4
    stats = repository.stats()
    assert stats["spans"] == 4
    assert stats["traces"] == 1
    assert stats["sessions"] == 1
    assert stats["tasks"] == 1

    sessions = repository.list_sessions()
    assert sessions[0]["session_id"] == identity.session_id
    assert sessions[0]["task_id"] == identity.task_id
    assert sessions[0]["span_count"] == 4

    traces = repository.list_traces_for_session(identity.session_id)
    assert traces[0]["trace_id"] == identity.trace_id
    assert traces[0]["span_count"] == 4

    trace = repository.get_trace(identity.trace_id)
    assert trace is not None
    assert [span["name"] for span in trace["spans"]] == [
        "agent.task",
        "agent.plan",
        "tool.shell",
        "agent.complete",
    ]
    assert json.dumps(trace).find("session-test") >= 0


def test_repository_upserts_same_span(tmp_path):
    request, identity = build_demo_request(
        trace_id=bytes.fromhex("22" * 16),
        session_id="session-upsert",
        task_id="task-upsert",
        start_ns=2_000_000_000,
    )
    repository = TraceRepository(tmp_path / "traceforge.db")

    assert repository.ingest(request) == 4
    assert repository.ingest(request) == 4
    assert repository.stats()["spans"] == 4
    assert repository.get_trace(identity.trace_id) is not None
