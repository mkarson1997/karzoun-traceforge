from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from traceforge.audit import emit_audit_event, stable_ref
from traceforge.policy import OrganizationPolicy, builtin_policy, load_policy, render_policy
from traceforge.tenant_auth import TenantTokenSigner

_API_KEY_ITERATIONS = 240_000
_ALLOWED_SCOPES = frozenset({"ingest", "policy:read", "viewer:read", "admin"})


@dataclass(frozen=True)
class TenantPrincipal:
    organization_id: str
    key_id: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class ControlPlaneSession:
    policy: OrganizationPolicy
    ingest_token: str


class TenantRegistry:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._initialize()

    def create_organization(
        self,
        display_name: str,
        *,
        organization_id: str | None = None,
        profile: str = "strict",
    ) -> OrganizationPolicy:
        name = display_name.strip()
        if not name:
            raise ValueError("display_name is required")
        org_id = organization_id or f"org_{secrets.token_hex(8)}"
        policy = builtin_policy(profile, org_id)
        now = _now()
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO organizations(
                        organization_id, display_name, status, policy_json, created_at, updated_at
                    ) VALUES(?, ?, 'active', ?, ?, ?)
                    """,
                    (org_id, name, _policy_json(policy), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"organization already exists: {org_id}") from exc
        emit_audit_event(
            "tenant.organization.create",
            "success",
            component="control-plane",
            actor_ref=stable_ref(org_id),
        )
        return policy

    def list_organizations(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT organization_id, display_name, status, created_at, updated_at
                FROM organizations
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [
            {
                "organization_id": str(row["organization_id"]),
                "display_name": str(row["display_name"]),
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def set_status(self, organization_id: str, status: str) -> None:
        normalized = status.strip().lower()
        if normalized not in {"active", "suspended"}:
            raise ValueError("status must be active or suspended")
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE organizations SET status = ?, updated_at = ?
                WHERE organization_id = ?
                """,
                (normalized, _now(), organization_id),
            )
            if result.rowcount != 1:
                raise KeyError(organization_id)
        emit_audit_event(
            "tenant.organization.status",
            "success",
            component="control-plane",
            actor_ref=stable_ref(organization_id),
            status=normalized,
        )

    def get_policy(self, organization_id: str) -> OrganizationPolicy:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT policy_json FROM organizations
                WHERE organization_id = ? AND status = 'active'
                """,
                (organization_id,),
            ).fetchone()
        if row is None:
            raise KeyError(organization_id)
        return _policy_from_payload(json.loads(row["policy_json"]))

    def set_policy(self, organization_id: str, policy: OrganizationPolicy) -> None:
        if policy.organization_id != organization_id:
            raise ValueError("policy organization_id does not match target organization")
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE organizations
                SET policy_json = ?, updated_at = ?
                WHERE organization_id = ?
                """,
                (_policy_json(policy), _now(), organization_id),
            )
            if result.rowcount != 1:
                raise KeyError(organization_id)
        emit_audit_event(
            "tenant.policy.update",
            "success",
            component="control-plane",
            actor_ref=stable_ref(organization_id),
            profile=policy.profile,
        )

    def issue_api_key(
        self,
        organization_id: str,
        *,
        label: str = "default",
        scopes: frozenset[str] = frozenset({"ingest", "policy:read"}),
    ) -> tuple[str, str]:
        normalized_scopes = _validate_scopes(scopes)
        self.get_policy(organization_id)
        key_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        digest = _hash_api_secret(secret, salt)
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO api_keys(
                    key_id, organization_id, label, salt, secret_hash, scopes_json,
                    created_at, revoked_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    key_id,
                    organization_id,
                    label.strip() or "default",
                    salt,
                    digest,
                    json.dumps(sorted(normalized_scopes)),
                    now,
                ),
            )
        emit_audit_event(
            "tenant.api_key.issue",
            "success",
            component="control-plane",
            actor_ref=stable_ref(organization_id),
            key_ref=stable_ref(key_id),
        )
        return key_id, f"tfk_{key_id}_{secret}"

    def revoke_api_key(self, key_id: str) -> None:
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE api_keys SET revoked_at = ?
                WHERE key_id = ? AND revoked_at IS NULL
                """,
                (_now(), key_id),
            )
            if result.rowcount != 1:
                raise KeyError(key_id)
        emit_audit_event(
            "tenant.api_key.revoke",
            "success",
            component="control-plane",
            actor_ref=stable_ref(key_id),
        )

    def authenticate_api_key(
        self,
        token: str,
        *,
        required_scope: str | None = None,
    ) -> TenantPrincipal:
        key_id, secret = _parse_api_key(token)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    k.organization_id, k.salt, k.secret_hash, k.scopes_json,
                    k.revoked_at, o.status
                FROM api_keys k
                JOIN organizations o ON o.organization_id = k.organization_id
                WHERE k.key_id = ?
                """,
                (key_id,),
            ).fetchone()
        if row is None or row["revoked_at"] is not None or row["status"] != "active":
            raise PermissionError("invalid API key")
        supplied = _hash_api_secret(secret, bytes(row["salt"]))
        if not hmac.compare_digest(supplied, bytes(row["secret_hash"])):
            raise PermissionError("invalid API key")
        scopes = frozenset(json.loads(row["scopes_json"]))
        if required_scope and required_scope not in scopes and "admin" not in scopes:
            raise PermissionError(f"API key missing required scope: {required_scope}")
        return TenantPrincipal(
            organization_id=str(row["organization_id"]),
            key_id=key_id,
            scopes=scopes,
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS organizations(
                    organization_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'suspended')),
                    policy_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_keys(
                    key_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    salt BLOB NOT NULL,
                    secret_hash BLOB NOT NULL,
                    scopes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(organization_id) REFERENCES organizations(organization_id)
                );

                CREATE INDEX IF NOT EXISTS idx_api_keys_org
                ON api_keys(organization_id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


class _ControlPlaneServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        registry: TenantRegistry,
        signer: TenantTokenSigner,
    ) -> None:
        super().__init__(address, _ControlPlaneHandler)
        self.registry = registry
        self.signer = signer


class _ControlPlaneHandler(BaseHTTPRequestHandler):
    server: _ControlPlaneServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        try:
            principal = self._principal("policy:read" if self.path == "/v1/policy" else None)
        except PermissionError as exc:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
            return

        if self.path == "/v1/me":
            self._json(
                HTTPStatus.OK,
                {
                    "organization_id": principal.organization_id,
                    "key_id": principal.key_id,
                    "scopes": sorted(principal.scopes),
                },
            )
            return
        if self.path == "/v1/policy":
            try:
                policy = self.server.registry.get_policy(principal.organization_id)
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "organization not found"})
                return
            self._json(HTTPStatus.OK, render_policy(policy))
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/ingest-token":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            principal = self._principal("ingest")
        except PermissionError as exc:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
            return
        token = self.server.signer.issue(
            principal.organization_id,
            {"ingest"},
            ttl_seconds=900,
        )
        emit_audit_event(
            "tenant.ingest_token.issue",
            "success",
            component="control-plane",
            actor_ref=stable_ref(principal.organization_id),
            key_ref=stable_ref(principal.key_id),
        )
        self._json(
            HTTPStatus.OK,
            {
                "organization_id": principal.organization_id,
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": 900,
            },
        )

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _principal(self, scope: str | None) -> TenantPrincipal:
        header = self.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            raise PermissionError("missing bearer API key")
        return self.server.registry.authenticate_api_key(
            header[7:].strip(),
            required_scope=scope,
        )

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        self.send_response(status.value, status.phrase)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def fetch_control_plane_session(
    base_url: str,
    api_key: str,
    *,
    timeout_seconds: float = 10.0,
) -> ControlPlaneSession:
    root = base_url.rstrip("/")
    policy_payload = _http_json(
        Request(
            f"{root}/v1/policy",
            headers={"Authorization": f"Bearer {api_key}"},
            method="GET",
        ),
        timeout_seconds,
    )
    token_payload = _http_json(
        Request(
            f"{root}/v1/ingest-token",
            headers={"Authorization": f"Bearer {api_key}"},
            data=b"{}",
            method="POST",
        ),
        timeout_seconds,
    )
    policy = _policy_from_payload(policy_payload)
    token = token_payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("control plane returned no ingest token")
    if token_payload.get("organization_id") != policy.organization_id:
        raise ValueError("control plane returned inconsistent organization identity")
    return ControlPlaneSession(policy=policy, ingest_token=token)


def tenants_main() -> int:
    parser = argparse.ArgumentParser(description="Manage TraceForge tenant organizations")
    parser.add_argument("--db", default="traceforge-tenants.db")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-org")
    create.add_argument("--name", required=True)
    create.add_argument("--id")
    create.add_argument("--profile", default="strict", choices=["minimal", "strict", "standard"])

    sub.add_parser("list-orgs")

    status = sub.add_parser("set-status")
    status.add_argument("--org", required=True)
    status.add_argument("--status", required=True, choices=["active", "suspended"])

    policy = sub.add_parser("set-policy")
    policy.add_argument("--org", required=True)
    policy.add_argument("--file", type=Path, required=True)

    issue = sub.add_parser("issue-key")
    issue.add_argument("--org", required=True)
    issue.add_argument("--label", default="default")
    issue.add_argument("--scopes", default="ingest,policy:read")

    revoke = sub.add_parser("revoke-key")
    revoke.add_argument("--key-id", required=True)

    args = parser.parse_args()
    registry = TenantRegistry(args.db)
    try:
        if args.command == "create-org":
            created = registry.create_organization(
                args.name,
                organization_id=args.id,
                profile=args.profile,
            )
            print(json.dumps(render_policy(created), indent=2, sort_keys=True))
        elif args.command == "list-orgs":
            print(json.dumps(registry.list_organizations(), indent=2, sort_keys=True))
        elif args.command == "set-status":
            registry.set_status(args.org, args.status)
            print(json.dumps({"organization_id": args.org, "status": args.status}))
        elif args.command == "set-policy":
            policy_value = load_policy(args.file)
            registry.set_policy(args.org, policy_value)
            print(json.dumps(render_policy(policy_value), indent=2, sort_keys=True))
        elif args.command == "issue-key":
            scopes = frozenset(part.strip() for part in args.scopes.split(",") if part.strip())
            key_id, token = registry.issue_api_key(
                args.org,
                label=args.label,
                scopes=scopes,
            )
            print(
                json.dumps(
                    {
                        "key_id": key_id,
                        "api_key": token,
                        "warning": "store this API key now; TraceForge does not store plaintext keys",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "revoke-key":
            registry.revoke_api_key(args.key_id)
            print(json.dumps({"key_id": args.key_id, "revoked": True}))
    except (KeyError, OSError, PermissionError, ValueError, json.JSONDecodeError) as exc:
        print(f"traceforge-tenants: {exc}")
        return 2
    return 0


def server_main() -> int:
    parser = argparse.ArgumentParser(description="Run the TraceForge multi-tenant control plane")
    parser.add_argument("--db", default=os.getenv("TRACEFORGE_TENANT_DB", "traceforge-tenants.db"))
    parser.add_argument(
        "--http-address",
        default=os.getenv("TRACEFORGE_CONTROL_HTTP_ADDRESS", "127.0.0.1:8090"),
    )
    parser.add_argument("--signing-key-file", type=Path)
    args = parser.parse_args()

    raw_key = (
        args.signing_key_file.read_bytes()
        if args.signing_key_file
        else os.getenv("TRACEFORGE_TENANT_SIGNING_KEY", "").encode()
    )
    if len(raw_key) < 32:
        raise SystemExit(
            "TRACEFORGE_TENANT_SIGNING_KEY or --signing-key-file must provide at least 32 bytes"
        )
    host, port_text = args.http_address.rsplit(":", 1)
    server = _ControlPlaneServer(
        (host, int(port_text)),
        TenantRegistry(args.db),
        TenantTokenSigner(raw_key),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _hash_api_secret(secret: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode(),
        salt,
        _API_KEY_ITERATIONS,
        dklen=32,
    )


def _parse_api_key(token: str) -> tuple[str, str]:
    if not token.startswith("tfk_"):
        raise PermissionError("invalid API key")
    parts = token.split("_", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        raise PermissionError("invalid API key")
    return parts[1], parts[2]


def _validate_scopes(scopes: frozenset[str]) -> frozenset[str]:
    normalized = frozenset(scope.strip() for scope in scopes if scope.strip())
    if not normalized:
        raise ValueError("at least one API key scope is required")
    unknown = normalized - _ALLOWED_SCOPES
    if unknown:
        raise ValueError(f"unknown API key scopes: {', '.join(sorted(unknown))}")
    return normalized


def _policy_json(policy: OrganizationPolicy) -> str:
    return json.dumps(render_policy(policy), separators=(",", ":"), sort_keys=True)


def _policy_from_payload(payload: dict[str, Any]) -> OrganizationPolicy:
    organization_id = str(payload.get("organization_id") or "")
    profile = str(payload.get("profile") or "strict")
    base = builtin_policy(profile, organization_id)
    allowed = payload.get("allowed_adapters")
    capture_model_names = payload.get("capture_model_names", base.capture_model_names)
    capture_tool_names = payload.get("capture_tool_names", base.capture_tool_names)
    capture_usage_metrics = payload.get(
        "capture_usage_metrics",
        base.capture_usage_metrics,
    )
    max_batch_size = payload.get("max_batch_size", base.max_batch_size)
    if not isinstance(allowed, list) or not allowed:
        allowed_set = base.allowed_adapters
    else:
        from traceforge.policy import normalize_adapter_name

        allowed_set = frozenset(normalize_adapter_name(str(item)) for item in allowed)
    if (
        not isinstance(capture_model_names, bool)
        or not isinstance(capture_tool_names, bool)
        or not isinstance(capture_usage_metrics, bool)
        or isinstance(max_batch_size, bool)
        or not isinstance(max_batch_size, int)
        or not 1 <= max_batch_size <= 1000
    ):
        raise ValueError("invalid control-plane policy payload")
    return OrganizationPolicy(
        organization_id=organization_id,
        profile=base.profile,
        allowed_adapters=allowed_set,
        capture_model_names=capture_model_names,
        capture_tool_names=capture_tool_names,
        capture_usage_metrics=capture_usage_metrics,
        max_batch_size=max_batch_size,
    )


def _http_json(request: Request, timeout_seconds: float) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise PermissionError(f"control-plane request failed: HTTP {exc.code} {detail}") from exc
    except URLError as exc:
        raise ConnectionError(f"control-plane request failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise ValueError("control-plane response must be a JSON object")
    return payload


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(server_main())
