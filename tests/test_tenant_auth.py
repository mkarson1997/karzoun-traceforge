from __future__ import annotations

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from traceforge.tenant_auth import (
    TenantTokenError,
    TenantTokenSigner,
    bearer_token_from_metadata,
    enforce_tenant_identity,
)


def test_tenant_token_roundtrip_and_scope():
    signer = TenantTokenSigner(b"k" * 32)
    token = signer.issue("org_demo", {"ingest", "policy:read"}, ttl_seconds=300, now=1000)

    claims = signer.verify(token, required_scope="ingest", now=1100)

    assert claims.organization_id == "org_demo"
    assert claims.scopes == frozenset({"ingest", "policy:read"})
    assert claims.issued_at == 1000
    assert claims.expires_at == 1300


def test_tenant_token_rejects_tampering_expiry_and_missing_scope():
    signer = TenantTokenSigner(b"k" * 32)
    token = signer.issue("org_demo", {"ingest"}, ttl_seconds=60, now=1000)

    with pytest.raises(TenantTokenError, match="signature"):
        signer.verify(token[:-1] + ("A" if token[-1] != "A" else "B"), now=1010)

    with pytest.raises(TenantTokenError, match="expired"):
        signer.verify(token, now=1100, leeway_seconds=0)

    with pytest.raises(TenantTokenError, match="required scope"):
        signer.verify(token, required_scope="viewer:read", now=1010)


def test_bearer_token_metadata_parser():
    assert bearer_token_from_metadata((("authorization", "Bearer abc"),)) == "abc"
    assert bearer_token_from_metadata((("x-other", "value"),)) is None


def test_authenticated_tenant_identity_overwrites_spoofed_resource_attribute():
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add().resource
    original = resource.attributes.add()
    original.key = "traceforge.organization.id"
    original.value.string_value = "org_spoofed"
    safe = resource.attributes.add()
    safe.key = "service.name"
    safe.value.string_value = "demo"

    enforce_tenant_identity(request, "org_authenticated")

    values = {
        item.key: item.value.string_value
        for item in request.resource_spans[0].resource.attributes
        if item.value.WhichOneof("value") == "string_value"
    }
    assert values["traceforge.organization.id"] == "org_authenticated"
    assert values["service.name"] == "demo"
    assert list(values).count("traceforge.organization.id") == 1
