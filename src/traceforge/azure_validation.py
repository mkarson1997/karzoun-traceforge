from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc import TraceServiceStub

from traceforge.control_plane import fetch_control_plane_session
from traceforge.demo import build_demo_request


def validate_deployment_outputs(
    outputs: dict[str, Any],
    *,
    require_private: bool = False,
    require_mtls: bool = False,
    require_tenant_auth: bool = False,
    require_viewer: bool = False,
) -> list[str]:
    failures: list[str] = []
    endpoint = _output_value(outputs, "gatewayOtlpEndpoint")
    if not isinstance(endpoint, str) or not endpoint.strip():
        failures.append("gatewayOtlpEndpoint is missing")

    checks = (
        ("privateEndpointsEnabled", require_private),
        ("gatewayMutualTlsEnabled", require_mtls),
        ("gatewayTenantAuthEnabled", require_tenant_auth),
    )
    for name, required in checks:
        if required and _output_value(outputs, name) is not True:
            failures.append(f"{name} is not enabled")

    if require_viewer:
        viewer_url = _output_value(outputs, "viewerUrl")
        callback = _output_value(outputs, "viewerCallbackUrl")
        if not isinstance(viewer_url, str) or not viewer_url.startswith("https://"):
            failures.append("viewerUrl is missing")
        if not isinstance(callback, str) or "/.auth/login/aad/callback" not in callback:
            failures.append("viewerCallbackUrl is missing or invalid")
    return failures


def deployment_outputs(resource_group: str, deployment_name: str) -> dict[str, Any]:
    command = [
        "az",
        "deployment",
        "group",
        "show",
        "--resource-group",
        resource_group,
        "--name",
        deployment_name,
        "--query",
        "properties.outputs",
        "--output",
        "json",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Azure CLI 'az' is not installed") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"Azure deployment lookup failed: {detail}") from exc
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("Azure deployment outputs are not a JSON object")
    return payload


def send_validation_trace(
    endpoint: str,
    *,
    authorization_token: str | None = None,
    ca_file: Path | None = None,
    client_cert_file: Path | None = None,
    client_key_file: Path | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, str]:
    if bool(client_cert_file) != bool(client_key_file):
        raise ValueError("client certificate and key must be supplied together")

    request, identity = build_demo_request()
    resource = request.resource_spans[0].resource
    spoofed = resource.attributes.add()
    spoofed.key = "traceforge.organization.id"
    spoofed.value.string_value = "org_client_spoof_attempt"

    credentials = grpc.ssl_channel_credentials(
        root_certificates=ca_file.read_bytes() if ca_file else None,
        private_key=client_key_file.read_bytes() if client_key_file else None,
        certificate_chain=client_cert_file.read_bytes() if client_cert_file else None,
    )
    metadata = (
        (("authorization", f"Bearer {authorization_token}"),)
        if authorization_token
        else None
    )
    with grpc.secure_channel(endpoint, credentials) as channel:
        TraceServiceStub(channel).Export(
            request,
            timeout=timeout_seconds,
            metadata=metadata,
        )
    return {
        "trace_id": identity.trace_id,
        "session_id": identity.session_id,
        "task_id": identity.task_id,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a deployed TraceForge Azure gateway and deployment outputs"
    )
    parser.add_argument("--resource-group")
    parser.add_argument("--deployment-name")
    parser.add_argument("--gateway-endpoint")
    parser.add_argument("--control-plane-url", default=os.getenv("TRACEFORGE_CONTROL_PLANE_URL"))
    parser.add_argument("--api-key", default=os.getenv("TRACEFORGE_API_KEY"))
    parser.add_argument("--tenant-token", default=os.getenv("TRACEFORGE_TENANT_TOKEN"))
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--client-cert-file", type=Path)
    parser.add_argument("--client-key-file", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--require-private", action="store_true")
    parser.add_argument("--require-mtls", action="store_true")
    parser.add_argument("--require-tenant-auth", action="store_true")
    parser.add_argument("--require-viewer", action="store_true")
    parser.add_argument("--skip-export", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if bool(args.resource_group) != bool(args.deployment_name):
        raise SystemExit("--resource-group and --deployment-name must be supplied together")
    if args.control_plane_url and args.tenant_token:
        raise SystemExit("--control-plane-url and --tenant-token are mutually exclusive")
    if args.control_plane_url and not args.api_key:
        raise SystemExit("--api-key is required with --control-plane-url")

    outputs: dict[str, Any] = {}
    if args.resource_group:
        try:
            outputs = deployment_outputs(args.resource_group, args.deployment_name)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "stage": "deployment", "error": str(exc)}))
            return 2

    failures = validate_deployment_outputs(
        outputs,
        require_private=args.require_private,
        require_mtls=args.require_mtls,
        require_tenant_auth=args.require_tenant_auth,
        require_viewer=args.require_viewer,
    ) if outputs else []

    endpoint = args.gateway_endpoint or _output_value(outputs, "gatewayOtlpEndpoint")
    if not args.skip_export and not endpoint:
        failures.append("gateway endpoint is required for export validation")

    authorization_token = args.tenant_token
    organization_id: str | None = None
    if args.control_plane_url:
        try:
            session = fetch_control_plane_session(
                args.control_plane_url,
                args.api_key,
                timeout_seconds=args.timeout_seconds,
            )
        except (ConnectionError, OSError, PermissionError, ValueError) as exc:
            failures.append(f"control-plane bootstrap failed: {exc}")
        else:
            authorization_token = session.ingest_token
            organization_id = session.policy.organization_id

    exported: dict[str, str] | None = None
    if not failures and not args.skip_export:
        try:
            exported = send_validation_trace(
                str(endpoint),
                authorization_token=authorization_token,
                ca_file=args.ca_file,
                client_cert_file=args.client_cert_file,
                client_key_file=args.client_key_file,
                timeout_seconds=args.timeout_seconds,
            )
        except (OSError, ValueError, grpc.RpcError) as exc:
            failures.append(f"OTLP export failed: {exc}")

    report = {
        "ok": not failures,
        "gateway_endpoint": endpoint,
        "tenant_organization_id": organization_id,
        "deployment_checks": {
            "private_endpoints": _output_value(outputs, "privateEndpointsEnabled"),
            "mutual_tls": _output_value(outputs, "gatewayMutualTlsEnabled"),
            "tenant_auth": _output_value(outputs, "gatewayTenantAuthEnabled"),
            "viewer_url": _output_value(outputs, "viewerUrl"),
        },
        "exported_trace": exported,
        "failures": failures,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failures else 1


def _output_value(outputs: dict[str, Any], key: str) -> Any:
    value = outputs.get(key)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
