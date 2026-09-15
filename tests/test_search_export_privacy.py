from __future__ import annotations

import json

from traceforge.demo import build_demo_request
from traceforge.gateway.otel_scrub import attach_privacy_summary, scrub_trace_export_request
from traceforge.privacy import Scrubber
from traceforge.store.repository import TraceRepository


def _sanitized_request():
    request, identity = build_demo_request(
        trace_id=bytes.fromhex("44" * 16),
        session_id="session-search",
        task_id="task-search",
        start_ns=4_000_000_000,
    )
    stats = scrub_trace_export_request(request, Scrubber())
    attach_privacy_summary(request, stats, export_id="export-search-test")
    return request, identity, stats


def test_privacy_summary_is_persisted_without_matched_values(tmp_path):
    request, _, stats = _sanitized_request()
    repository = TraceRepository(tmp_path / "traceforge.db")
    repository.ingest(request)

    aggregate = repository.stats()
    assert aggregate["privacy_exports"] == 1
    assert aggregate["privacy_findings"] == stats.findings
    assert aggregate["privacy_attributes_removed"] == stats.attributes_removed
    assert aggregate["privacy_attributes_rewritten"] == stats.attributes_rewritten

    exports = repository.list_privacy_exports()
    assert exports[0]["export_id"] == "export-search-test"
    serialized = json.dumps(exports[0])
    assert "developer@example.com" not in serialized
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in serialized


def test_search_and_sanitized_export(tmp_path):
    request, identity, _ = _sanitized_request()
    repository = TraceRepository(tmp_path / "traceforge.db")
    repository.ingest(request)

    shell_matches = repository.search_spans("tool.shell")
    assert len(shell_matches) == 1
    assert shell_matches[0]["trace_id"] == identity.trace_id

    session_matches = repository.search_spans("session-search")
    assert len(session_matches) == 4

    exported = repository.export_spans(session_id=identity.session_id)
    assert len(exported) == 4
    serialized = json.dumps(exported, sort_keys=True)
    assert "developer@example.com" not in serialized
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in serialized
    assert "sensitive source code" not in serialized
    assert "private generated completion" not in serialized
    assert "[REDACTED:sensitive_key]" in serialized


def test_search_treats_like_wildcards_as_literal_text(tmp_path):
    request, _, _ = _sanitized_request()
    repository = TraceRepository(tmp_path / "traceforge.db")
    repository.ingest(request)

    # These would match the literal value ``task-search`` if SQL LIKE wildcards
    # were allowed to leak through unescaped.
    assert repository.search_spans("task%search") == []
    assert repository.search_spans("task_search") == []
