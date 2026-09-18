from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from traceforge.demo import build_demo_request
from traceforge.store.repository import TraceRepository
from traceforge.store.server import ViewerServer
from traceforge.tenant_auth import TenantTokenSigner


def _kv(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _tenant_request(organization_id: str, trace_byte: int = 0xAA):
    request, identity = build_demo_request(
        trace_id=bytes([trace_byte]) * 16,
        session_id="shared-session",
        task_id="shared-task",
    )
    request.resource_spans[0].resource.attributes.append(
        _kv("traceforge.organization.id", organization_id)
    )
    return request, identity


def test_same_trace_and_span_ids_are_isolated_by_organization(tmp_path):
    repository = TraceRepository(tmp_path / "traceforge.db")
    request_a, identity = _tenant_request("org_alpha")
    request_b, _ = _tenant_request("org_beta")

    assert repository.ingest(request_a) == 4
    assert repository.ingest(request_b) == 4

    assert repository.stats()["spans"] == 8
    assert repository.stats(organization_id="org_alpha")["spans"] == 4
    assert repository.stats(organization_id="org_beta")["spans"] == 4

    alpha = repository.get_trace(identity.trace_id, organization_id="org_alpha")
    beta = repository.get_trace(identity.trace_id, organization_id="org_beta")
    assert alpha is not None
    assert beta is not None
    assert len(alpha["spans"]) == 4
    assert len(beta["spans"]) == 4
    assert {
        span["resource"]["traceforge.organization.id"] for span in alpha["spans"]
    } == {"org_alpha"}
    assert {
        span["resource"]["traceforge.organization.id"] for span in beta["spans"]
    } == {"org_beta"}

    assert len(repository.search_spans("tool.shell", organization_id="org_alpha")) == 1
    assert len(repository.search_spans("tool.shell", organization_id="org_beta")) == 1
    assert len(repository.export_spans(organization_id="org_alpha")) == 4
    assert len(repository.export_spans(organization_id="org_beta")) == 4


def test_viewer_api_requires_viewer_scope_and_filters_tenant(tmp_path):
    repository = TraceRepository(tmp_path / "traceforge.db")
    request_a, _ = _tenant_request("org_alpha", 0xAA)
    request_b, _ = _tenant_request("org_beta", 0xBB)
    repository.ingest(request_a)
    repository.ingest(request_b)

    signer = TenantTokenSigner(b"v" * 32)
    viewer = ViewerServer(
        ("127.0.0.1", 0),
        repository,
        tenant_token_signer=signer,
        tenant_auth_required=True,
    )
    thread = threading.Thread(target=viewer.serve_forever, daemon=True)
    thread.start()
    host, port = viewer.server_address
    base = f"http://{host}:{port}"

    try:
        try:
            urlopen(f"{base}/api/stats", timeout=2)
        except HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("missing viewer token should be rejected")

        ingest_token = signer.issue("org_alpha", {"ingest"})
        try:
            urlopen(
                Request(
                    f"{base}/api/stats",
                    headers={"Authorization": f"Bearer {ingest_token}"},
                ),
                timeout=2,
            )
        except HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("ingest-only token should not read viewer APIs")

        alpha_token = signer.issue("org_alpha", {"viewer:read"})
        alpha_stats = _get_json(base, "/api/stats", alpha_token)
        alpha_sessions = _get_json(base, "/api/sessions", alpha_token)
        assert alpha_stats["spans"] == 4
        assert len(alpha_sessions) == 1
        assert alpha_sessions[0]["span_count"] == 4

        beta_token = signer.issue("org_beta", {"viewer:read"})
        beta_stats = _get_json(base, "/api/stats", beta_token)
        assert beta_stats["spans"] == 4
    finally:
        viewer.shutdown()
        viewer.server_close()
        thread.join(timeout=2)


def _get_json(base: str, path: str, token: str):
    with urlopen(
        Request(
            f"{base}{path}",
            headers={"Authorization": f"Bearer {token}"},
        ),
        timeout=2,
    ) as response:
        return json.loads(response.read().decode())
