from __future__ import annotations

import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from traceforge.control_plane import TenantRegistry, _ControlPlaneServer
from traceforge.demo import build_demo_request
from traceforge.operations import (
    _write_private_json,
    bootstrap_organization,
    create_backup,
    doctor,
    restore_backup,
    verify_backup,
)
from traceforge.store.repository import TraceRepository
from traceforge.store.server import ViewerServer
from traceforge.tenant_auth import TenantTokenSigner


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def _tenant_trace(repository: TraceRepository, organization_id: str, trace_byte: int) -> str:
    request, identity = build_demo_request(trace_id=bytes([trace_byte]) * 16)
    request.resource_spans[0].resource.attributes.append(
        KeyValue(
            key="traceforge.organization.id",
            value=AnyValue(string_value=organization_id),
        )
    )
    repository.ingest(request)
    return identity.trace_id


def test_bootstrap_organization_creates_separate_scoped_keys(tmp_path):
    payload = bootstrap_organization(
        tmp_path / "tenants.db",
        name="Alpha",
        organization_id="org_alpha",
        profile="strict",
    )

    assert payload["organization_id"] == "org_alpha"
    assert payload["ingest"]["scopes"] == ["ingest", "policy:read"]
    assert payload["viewer"]["scopes"] == ["viewer:read"]
    assert payload["ingest"]["api_key"].startswith("tfk_")
    assert payload["viewer"]["api_key"].startswith("tfk_")
    assert payload["ingest"]["api_key"] != payload["viewer"]["api_key"]


def test_backup_verify_and_restore_roundtrip(tmp_path):
    tenant_db = tmp_path / "tenants.db"
    trace_db = tmp_path / "traceforge.db"
    backup_dir = tmp_path / "backup"

    registry = TenantRegistry(tenant_db)
    registry.create_organization("Alpha", organization_id="org_alpha")
    trace_repository = TraceRepository(trace_db)
    original_trace = _tenant_trace(trace_repository, "org_alpha", 0xAA)

    manifest = create_backup(tenant_db, trace_db, backup_dir)
    assert manifest["schema"] == "traceforge.backup.v1"
    verified = verify_backup(backup_dir)
    assert verified["verified"] is True

    registry.create_organization("Beta", organization_id="org_beta")
    _tenant_trace(trace_repository, "org_beta", 0xBB)
    assert len(registry.list_organizations()) == 2
    assert trace_repository.stats()["spans"] == 8

    restore_backup(
        backup_dir,
        tenant_db,
        trace_db,
        confirm="RESTORE",
    )

    restored_registry = TenantRegistry(tenant_db)
    restored_traces = TraceRepository(trace_db)
    assert [item["organization_id"] for item in restored_registry.list_organizations()] == [
        "org_alpha"
    ]
    assert restored_traces.stats()["spans"] == 4
    assert restored_traces.get_trace(
        original_trace,
        organization_id="org_alpha",
    ) is not None
    assert restored_traces.stats(organization_id="org_beta")["spans"] == 0


def test_backup_verification_rejects_tampering(tmp_path):
    tenant_db = tmp_path / "tenants.db"
    trace_db = tmp_path / "traceforge.db"
    backup_dir = tmp_path / "backup"

    registry = TenantRegistry(tenant_db)
    registry.create_organization("Alpha", organization_id="org_alpha")
    TraceRepository(trace_db)
    create_backup(tenant_db, trace_db, backup_dir)

    with (backup_dir / "traceforge.db").open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_backup(backup_dir)


def test_restore_requires_explicit_confirmation(tmp_path):
    tenant_db = tmp_path / "tenants.db"
    trace_db = tmp_path / "traceforge.db"
    backup_dir = tmp_path / "backup"

    registry = TenantRegistry(tenant_db)
    registry.create_organization("Alpha", organization_id="org_alpha")
    TraceRepository(trace_db)
    create_backup(tenant_db, trace_db, backup_dir)

    with pytest.raises(ValueError, match="--confirm RESTORE"):
        restore_backup(
            backup_dir,
            tenant_db,
            trace_db,
            confirm="no",
        )


def test_private_credential_file_is_created_with_restrictive_mode(tmp_path):
    path = tmp_path / "credentials.json"
    _write_private_json(path, {"api_key": "synthetic-key"})

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["api_key"] == "synthetic-key"

    with pytest.raises(FileExistsError):
        _write_private_json(path, {"api_key": "replacement"})


def test_doctor_validates_control_plane_gateway_and_tenant_viewer(tmp_path):
    tenant_db = tmp_path / "tenants.db"
    trace_db = tmp_path / "traceforge.db"
    registry = TenantRegistry(tenant_db)
    registry.create_organization("Alpha", organization_id="org_alpha")
    _, ingest_api_key = registry.issue_api_key(
        "org_alpha",
        scopes=frozenset({"ingest", "policy:read"}),
    )
    _, viewer_api_key = registry.issue_api_key(
        "org_alpha",
        scopes=frozenset({"viewer:read"}),
    )

    signer = TenantTokenSigner(b"d" * 32)
    control_plane = _ControlPlaneServer(("127.0.0.1", 0), registry, signer)
    control_thread = threading.Thread(target=control_plane.serve_forever, daemon=True)
    control_thread.start()

    trace_repository = TraceRepository(trace_db)
    _tenant_trace(trace_repository, "org_alpha", 0xAA)
    viewer = ViewerServer(
        ("127.0.0.1", 0),
        trace_repository,
        tenant_token_signer=signer,
        tenant_auth_required=True,
    )
    viewer_thread = threading.Thread(target=viewer.serve_forever, daemon=True)
    viewer_thread.start()

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()

    try:
        control_host, control_port = control_plane.server_address
        viewer_host, viewer_port = viewer.server_address
        gateway_host, gateway_port = gateway.server_address

        results = doctor(
            control_plane_url=f"http://{control_host}:{control_port}",
            gateway_health_url=f"http://{gateway_host}:{gateway_port}",
            viewer_url=f"http://{viewer_host}:{viewer_port}",
            api_key=ingest_api_key,
            viewer_api_key=viewer_api_key,
            timeout_seconds=2,
        )
    finally:
        gateway.shutdown()
        gateway.server_close()
        gateway_thread.join(timeout=2)
        viewer.shutdown()
        viewer.server_close()
        viewer_thread.join(timeout=2)
        control_plane.shutdown()
        control_plane.server_close()
        control_thread.join(timeout=2)

    assert results
    assert all(item.ok for item in results)
    assert {item.name for item in results} == {
        "gateway-health",
        "control-plane-health",
        "control-plane-policy",
        "viewer-health",
        "viewer-token",
        "viewer-tenant-query",
    }
