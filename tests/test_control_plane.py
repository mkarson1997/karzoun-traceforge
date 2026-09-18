from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request

import pytest

from traceforge.control_plane import (
    TenantRegistry,
    _ControlPlaneServer,
    _http_json,
    fetch_control_plane_session,
)
from traceforge.policy import builtin_policy
from traceforge.tenant_auth import TenantTokenSigner


def test_registry_issues_hashed_api_key_and_revokes_it(tmp_path):
    registry = TenantRegistry(tmp_path / "tenants.db")
    registry.create_organization("Demo", organization_id="org_demo")
    key_id, api_key = registry.issue_api_key("org_demo")

    principal = registry.authenticate_api_key(api_key, required_scope="ingest")
    assert principal.organization_id == "org_demo"
    assert principal.key_id == key_id
    assert "ingest" in principal.scopes

    registry.revoke_api_key(key_id)
    with pytest.raises(PermissionError, match="invalid API key"):
        registry.authenticate_api_key(api_key)


def test_suspended_organization_cannot_authenticate(tmp_path):
    registry = TenantRegistry(tmp_path / "tenants.db")
    registry.create_organization("Demo", organization_id="org_demo")
    _, api_key = registry.issue_api_key("org_demo")
    registry.set_status("org_demo", "suspended")

    with pytest.raises(PermissionError, match="invalid API key"):
        registry.authenticate_api_key(api_key)


def test_registry_rejects_cross_organization_policy_update(tmp_path):
    registry = TenantRegistry(tmp_path / "tenants.db")
    registry.create_organization("Demo", organization_id="org_demo")

    with pytest.raises(ValueError, match="does not match"):
        registry.set_policy("org_demo", builtin_policy("strict", "org_other"))


def test_control_plane_delivers_policy_and_short_lived_ingest_token(tmp_path):
    registry = TenantRegistry(tmp_path / "tenants.db")
    registry.create_organization("Demo", organization_id="org_demo", profile="strict")
    _, api_key = registry.issue_api_key("org_demo")
    signer = TenantTokenSigner(b"s" * 32)
    server = _ControlPlaneServer(("127.0.0.1", 0), registry, signer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        session = fetch_control_plane_session(
            f"http://{host}:{port}",
            api_key,
            timeout_seconds=2,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert session.policy.organization_id == "org_demo"
    assert session.policy.profile == "strict"
    claims = signer.verify(session.ingest_token, required_scope="ingest")
    assert claims.organization_id == "org_demo"



class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:1/credential-capture")
        self.end_headers()

    def log_message(self, format, *args):
        return


def test_control_plane_client_refuses_http_redirects():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        request = Request(
            f"http://{host}:{port}/v1/policy",
            headers={"Authorization": "Bearer tfk_sensitive"},
        )
        with pytest.raises(PermissionError, match="HTTP 302"):
            _http_json(request, 2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
