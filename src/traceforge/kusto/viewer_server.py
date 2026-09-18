from __future__ import annotations

import argparse
import logging
import os

from traceforge.kusto.repository import KustoTraceRepository
from traceforge.store.server import ViewerServer, _split_host_port
from traceforge.tenant_auth import TenantTokenSigner

LOGGER = logging.getLogger("traceforge.kusto.viewer")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the TraceForge read-only Azure Data Explorer viewer"
    )
    parser.add_argument(
        "--cluster-uri",
        default=os.getenv("TRACEFORGE_KUSTO_CLUSTER_URI", ""),
    )
    parser.add_argument(
        "--database",
        default=os.getenv("TRACEFORGE_KUSTO_DATABASE", "traceforge"),
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("AZURE_CLIENT_ID", ""),
        help="User-assigned managed identity client ID",
    )
    parser.add_argument(
        "--auth",
        choices=["managed_identity", "interactive"],
        default=os.getenv("TRACEFORGE_KUSTO_AUTH", "managed_identity"),
    )
    parser.add_argument(
        "--http-address",
        default=os.getenv("TRACEFORGE_VIEWER_HTTP_ADDRESS", "0.0.0.0:8081"),
    )
    parser.add_argument(
        "--organization-id",
        default=os.getenv("TRACEFORGE_VIEWER_ORGANIZATION_ID", ""),
        help="Optional fixed organization scope for every viewer query",
    )
    parser.add_argument(
        "--tenant-auth-required",
        action="store_true",
        default=os.getenv("TRACEFORGE_VIEWER_TENANT_AUTH_REQUIRED", "").lower()
        in {"1", "true", "yes", "on"},
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.cluster_uri:
        raise SystemExit("TRACEFORGE_KUSTO_CLUSTER_URI or --cluster-uri is required")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    repository = KustoTraceRepository(
        args.cluster_uri,
        args.database,
        client_id=args.client_id or None,
        auth=args.auth,
    )
    raw_signing_key = os.getenv("TRACEFORGE_TENANT_SIGNING_KEY", "").encode()
    tenant_signer = (
        TenantTokenSigner(raw_signing_key)
        if len(raw_signing_key) >= 32
        else None
    )
    viewer = ViewerServer(
        _split_host_port(args.http_address),
        repository,
        tenant_token_signer=tenant_signer,
        tenant_auth_required=args.tenant_auth_required,
        fixed_organization_id=args.organization_id or None,
    )
    LOGGER.info(
        "read-only Kusto viewer=http://%s cluster=%s database=%s organization=%s",
        args.http_address,
        args.cluster_uri,
        args.database,
        args.organization_id or "all",
    )
    try:
        viewer.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("shutdown requested")
    finally:
        viewer.shutdown()
        viewer.server_close()
        repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
