from __future__ import annotations

import json

from traceforge.demo import build_demo_request
from traceforge.gateway.otel_scrub import scrub_trace_export_request
from traceforge.privacy import Scrubber
from traceforge.store.repository import TraceRepository


def test_synthetic_secrets_never_reach_persistent_store(tmp_path):
    request, identity = build_demo_request(
        trace_id=bytes.fromhex("33" * 16),
        session_id="session-private",
        task_id="task-private",
        start_ns=3_000_000_000,
    )
    raw = request.SerializeToString()
    assert b"developer@example.com" in raw
    assert b"ghp_abcdefghijklmnopqrstuvwxyz123456" in raw
    assert b"sensitive source code" in raw

    sanitized = type(request)()
    sanitized.CopyFrom(request)
    stats = scrub_trace_export_request(sanitized, Scrubber())
    assert stats.findings >= 5
    assert stats.attributes_removed >= 4

    repository = TraceRepository(tmp_path / "traceforge.db")
    repository.ingest(sanitized)
    trace = repository.get_trace(identity.trace_id)
    assert trace is not None
    persisted = json.dumps(trace, sort_keys=True)

    assert "developer@example.com" not in persisted
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in persisted
    assert "Bearer abcdefghijklmnopqrstuvwxyz0123456789" not in persisted
    assert "sensitive source code" not in persisted
    assert "private generated completion" not in persisted
    assert "agent.prompt" not in persisted
    assert "source.code" not in persisted
    assert "completion" not in persisted
    assert "[REDACTED:sensitive_key]" in persisted
