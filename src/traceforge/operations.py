from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from traceforge.control_plane import TenantRegistry, fetch_control_plane_viewer_token

_BACKUP_SCHEMA = "traceforge.backup.v1"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def backup_sqlite(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)

    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
    destination_connection = sqlite3.connect(destination, timeout=10)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
        _assert_integrity(destination_connection, destination.name)
    finally:
        destination_connection.close()
        source_connection.close()


def create_backup(
    tenant_db: Path,
    trace_db: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"backup directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "tenant_db": ("traceforge-tenants.db", tenant_db),
        "trace_db": ("traceforge.db", trace_db),
    }
    manifest_files: dict[str, dict[str, Any]] = {}
    for logical_name, (filename, source) in files.items():
        destination = output_dir / filename
        backup_sqlite(source, destination)
        manifest_files[logical_name] = _file_metadata(destination)

    manifest = {
        "schema": _BACKUP_SCHEMA,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "files": manifest_files,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    manifest_path = backup_dir / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != _BACKUP_SCHEMA:
        raise ValueError("unsupported or invalid TraceForge backup manifest")
    files = payload.get("files")
    if not isinstance(files, dict):
        raise ValueError("backup manifest has no files map")

    expected_names = {"tenant_db", "trace_db"}
    if set(files) != expected_names:
        raise ValueError("backup manifest must contain tenant_db and trace_db")

    verified: dict[str, Any] = {}
    for logical_name in sorted(expected_names):
        metadata = files[logical_name]
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid backup metadata for {logical_name}")
        filename = metadata.get("filename")
        expected_sha = metadata.get("sha256")
        expected_size = metadata.get("size_bytes")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not isinstance(expected_sha, str)
            or not isinstance(expected_size, int)
        ):
            raise ValueError(f"invalid backup metadata for {logical_name}")
        path = backup_dir / filename
        actual = _file_metadata(path)
        if actual["sha256"] != expected_sha:
            raise ValueError(f"checksum mismatch for {filename}")
        if actual["size_bytes"] != expected_size:
            raise ValueError(f"size mismatch for {filename}")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            _assert_integrity(connection, filename)
        finally:
            connection.close()
        verified[logical_name] = actual

    return {
        "schema": _BACKUP_SCHEMA,
        "created_at": payload.get("created_at"),
        "files": verified,
        "verified": True,
    }


def restore_backup(
    backup_dir: Path,
    tenant_db: Path,
    trace_db: Path,
    *,
    confirm: str,
) -> None:
    if confirm != "RESTORE":
        raise ValueError("restore requires --confirm RESTORE")
    verified = verify_backup(backup_dir)
    files = verified["files"]
    targets = {
        "tenant_db": tenant_db,
        "trace_db": trace_db,
    }

    for logical_name, target in targets.items():
        source = backup_dir / str(files[logical_name]["filename"])
        _restore_sqlite_file(source, target)


def doctor(
    *,
    control_plane_url: str | None = None,
    gateway_health_url: str | None = None,
    viewer_url: str | None = None,
    api_key: str | None = None,
    viewer_api_key: str | None = None,
    timeout_seconds: float = 5.0,
) -> list[CheckResult]:
    checks: list[CheckResult] = []

    if gateway_health_url:
        checks.append(
            _check_http(
                "gateway-health",
                _join_url(gateway_health_url, "/healthz"),
                timeout_seconds=timeout_seconds,
            )
        )

    if control_plane_url:
        checks.append(
            _check_http(
                "control-plane-health",
                _join_url(control_plane_url, "/healthz"),
                timeout_seconds=timeout_seconds,
            )
        )
        if api_key:
            checks.append(
                _check_http(
                    "control-plane-policy",
                    _join_url(control_plane_url, "/v1/policy"),
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout_seconds=timeout_seconds,
                )
            )

    if viewer_url:
        checks.append(
            _check_http(
                "viewer-health",
                _join_url(viewer_url, "/healthz"),
                timeout_seconds=timeout_seconds,
            )
        )
        if viewer_api_key:
            if not control_plane_url:
                checks.append(
                    CheckResult(
                        "viewer-tenant-query",
                        False,
                        "viewer API key requires --control-plane-url",
                    )
                )
            else:
                try:
                    organization_id, token = fetch_control_plane_viewer_token(
                        control_plane_url,
                        viewer_api_key,
                        timeout_seconds=timeout_seconds,
                    )
                except (ConnectionError, OSError, PermissionError, ValueError) as exc:
                    checks.append(
                        CheckResult(
                            "viewer-token",
                            False,
                            _safe_error(exc),
                        )
                    )
                else:
                    checks.append(
                        CheckResult(
                            "viewer-token",
                            True,
                            f"organization={organization_id}",
                        )
                    )
                    checks.append(
                        _check_http(
                            "viewer-tenant-query",
                            _join_url(viewer_url, "/api/stats"),
                            headers={"Authorization": f"Bearer {token}"},
                            timeout_seconds=timeout_seconds,
                        )
                    )

    if not checks:
        checks.append(CheckResult("configuration", False, "no endpoints were supplied"))
    return checks


def bootstrap_organization(
    db_path: Path,
    *,
    name: str,
    organization_id: str | None,
    profile: str,
) -> dict[str, Any]:
    registry = TenantRegistry(db_path)
    policy = registry.create_organization(
        name,
        organization_id=organization_id,
        profile=profile,
    )
    ingest_key_id, ingest_api_key = registry.issue_api_key(
        policy.organization_id,
        label="developer-fleet",
        scopes=frozenset({"ingest", "policy:read"}),
    )
    viewer_key_id, viewer_api_key = registry.issue_api_key(
        policy.organization_id,
        label="viewer",
        scopes=frozenset({"viewer:read"}),
    )
    return {
        "organization_id": policy.organization_id,
        "profile": policy.profile,
        "ingest": {
            "key_id": ingest_key_id,
            "api_key": ingest_api_key,
            "scopes": ["ingest", "policy:read"],
        },
        "viewer": {
            "key_id": viewer_key_id,
            "api_key": viewer_api_key,
            "scopes": ["viewer:read"],
        },
        "warning": (
            "Store these API keys now. TraceForge persists only salted hashes "
            "and cannot recover plaintext keys."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TraceForge operational readiness toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap-org")
    bootstrap.add_argument("--db", default="traceforge-tenants.db")
    bootstrap.add_argument("--name", required=True)
    bootstrap.add_argument("--id")
    bootstrap.add_argument(
        "--profile",
        default="strict",
        choices=["minimal", "strict", "standard"],
    )
    bootstrap.add_argument(
        "--output",
        type=Path,
        help="Optional JSON credential handoff file, created with mode 0600",
    )

    backup = sub.add_parser("backup")
    backup.add_argument("--tenant-db", required=True, type=Path)
    backup.add_argument("--trace-db", required=True, type=Path)
    backup.add_argument("--output-dir", required=True, type=Path)

    verify = sub.add_parser("verify-backup")
    verify.add_argument("--backup-dir", required=True, type=Path)

    restore = sub.add_parser("restore")
    restore.add_argument("--backup-dir", required=True, type=Path)
    restore.add_argument("--tenant-db", required=True, type=Path)
    restore.add_argument("--trace-db", required=True, type=Path)
    restore.add_argument("--confirm", required=True)

    health = sub.add_parser("doctor")
    health.add_argument(
        "--control-plane-url",
        default=os.getenv("TRACEFORGE_CONTROL_PLANE_URL"),
    )
    health.add_argument(
        "--gateway-health-url",
        default=os.getenv("TRACEFORGE_GATEWAY_HEALTH_URL"),
    )
    health.add_argument(
        "--viewer-url",
        default=os.getenv("TRACEFORGE_VIEWER_URL"),
    )
    health.add_argument("--api-key", default=os.getenv("TRACEFORGE_API_KEY"))
    health.add_argument(
        "--viewer-api-key",
        default=os.getenv("TRACEFORGE_VIEWER_API_KEY"),
    )
    health.add_argument("--timeout-seconds", type=float, default=5.0)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "bootstrap-org":
            payload = bootstrap_organization(
                Path(args.db),
                name=args.name,
                organization_id=args.id,
                profile=args.profile,
            )
            if args.output:
                _write_private_json(args.output, payload)
                print(
                    json.dumps(
                        {
                            "organization_id": payload["organization_id"],
                            "credential_file": str(args.output),
                            "created": True,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        if args.command == "backup":
            payload = create_backup(
                args.tenant_db,
                args.trace_db,
                args.output_dir,
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        if args.command == "verify-backup":
            payload = verify_backup(args.backup_dir)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        if args.command == "restore":
            restore_backup(
                args.backup_dir,
                args.tenant_db,
                args.trace_db,
                confirm=args.confirm,
            )
            print(
                json.dumps(
                    {
                        "restored": True,
                        "tenant_db": str(args.tenant_db),
                        "trace_db": str(args.trace_db),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if args.command == "doctor":
            results = doctor(
                control_plane_url=args.control_plane_url,
                gateway_health_url=args.gateway_health_url,
                viewer_url=args.viewer_url,
                api_key=args.api_key,
                viewer_api_key=args.viewer_api_key,
                timeout_seconds=args.timeout_seconds,
            )
            payload = {
                "ok": all(item.ok for item in results),
                "checks": [asdict(item) for item in results],
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if payload["ok"] else 1
    except (
        FileExistsError,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        PermissionError,
        sqlite3.DatabaseError,
        ValueError,
    ) as exc:
        print(f"traceforge-ops: {_safe_error(exc)}", file=sys.stderr)
        return 2

    return 2


def _restore_sqlite_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.restore-",
        dir=target.parent,
    )
    os.close(file_descriptor)
    temp = Path(temp_name)
    try:
        temp.unlink()
        backup_sqlite(source, temp)
        os.replace(temp, target)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{target}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
    finally:
        if temp.exists():
            temp.unlink()


def _assert_integrity(connection: sqlite3.Connection, label: str) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    values = [str(row[0]) for row in rows]
    if values != ["ok"]:
        raise sqlite3.DatabaseError(f"SQLite integrity check failed for {label}: {values}")


def _file_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return {
        "filename": path.name,
        "size_bytes": size,
        "sha256": digest.hexdigest(),
    }


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    file_descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _check_http(
    name: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float,
) -> CheckResult:
    opener = build_opener(_RejectRedirects())
    request = Request(url, headers=headers or {}, method="GET")
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            response.read(4096)
    except HTTPError as exc:
        return CheckResult(name, False, f"HTTP {exc.code}")
    except URLError as exc:
        return CheckResult(name, False, f"connection failed: {exc.reason}")
    except OSError as exc:
        return CheckResult(name, False, _safe_error(exc))
    if 200 <= status < 300:
        return CheckResult(name, True, f"HTTP {status}")
    return CheckResult(name, False, f"HTTP {status}")


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}{path}"


def _safe_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ").strip() or type(exc).__name__


if __name__ == "__main__":
    raise SystemExit(main())
