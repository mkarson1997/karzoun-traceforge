from __future__ import annotations

from traceforge.demo import build_demo_request


def test_demo_request_has_task_session_and_span_tree():
    request, identity = build_demo_request(
        trace_id=bytes.fromhex("ab" * 16),
        session_id="session-demo",
        task_id="task-demo",
        start_ns=10_000,
    )

    spans = request.resource_spans[0].scope_spans[0].spans
    assert len(spans) == 4
    assert identity.trace_id == "ab" * 16
    assert {span.trace_id.hex() for span in spans} == {identity.trace_id}
    assert spans[0].parent_span_id == b""
    assert {span.parent_span_id for span in spans[1:]} == {spans[0].span_id}
    assert all(span.end_time_unix_nano > span.start_time_unix_nano for span in spans)


def test_demo_request_intentionally_contains_privacy_test_vectors():
    request, _ = build_demo_request(start_ns=10_000)
    spans = request.resource_spans[0].scope_spans[0].spans
    keys = {item.key for span in spans for item in span.attributes}

    assert "user.email" in keys
    assert "agent.prompt" in keys
    assert "authorization" in keys
    assert "source.code" in keys
    assert "completion" in keys
