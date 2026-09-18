# Operational readiness

TraceForge includes an operator CLI named `traceforge-ops` for the repeatable tasks that become
important once the product moves beyond a demo:

- one-command organization bootstrap
- consistent SQLite backups
- checksum and SQLite-integrity verification
- explicit restore with confirmation
- end-to-end service health checks
- tenant viewer-token and query-path validation

The tools use only the existing TraceForge package and Python standard library.

## Bootstrap an organization

Create an organization plus separate ingest/policy and viewer credentials:

    traceforge-ops bootstrap-org       --db ./traceforge-tenants.db       --id org_acme       --name "Acme Engineering"       --profile strict

For a handoff file that is created with owner-only mode on POSIX systems:

    traceforge-ops bootstrap-org       --db ./traceforge-tenants.db       --id org_acme       --name "Acme Engineering"       --profile strict       --output ./org-acme-credentials.json

The file contains plaintext API keys and must be moved immediately into the intended secret store,
then securely deleted from the operator workstation.

The command creates two independent credentials:

- developer-fleet: ingest + policy:read
- viewer: viewer:read

Do not reuse the viewer credential for developer endpoints.

## Create a consistent backup

The backup command uses SQLite's online backup API rather than copying database files byte-for-byte.
This means a consistent snapshot can be taken even when WAL mode is active.

    traceforge-ops backup       --tenant-db ./traceforge-tenants.db       --trace-db ./traceforge.db       --output-dir ./backups/traceforge-2026-09-18

The directory contains:

    manifest.json
    traceforge-tenants.db
    traceforge.db

The manifest records SHA-256 and file size for each database. The command also runs
`PRAGMA integrity_check` on each snapshot before declaring success.

## Verify a backup

Always verify before moving a backup into long-term storage and again before restoring:

    traceforge-ops verify-backup       --backup-dir ./backups/traceforge-2026-09-18

Verification checks:

1. manifest schema
2. expected file inventory
3. file size
4. SHA-256 checksum
5. SQLite integrity

A modified or truncated database fails verification.

## Restore

Stop every service that can write the tenant or trace database before restoring.

Restore is intentionally gated by an explicit confirmation string:

    traceforge-ops restore       --backup-dir ./backups/traceforge-2026-09-18       --tenant-db ./traceforge-tenants.db       --trace-db ./traceforge.db       --confirm RESTORE

The backup is verified first. Each database is restored through SQLite into a temporary file and then
atomically moved into place.

After restore:

1. start the control plane and trace store
2. run `traceforge-ops doctor`
3. verify the expected organization list
4. send one synthetic trace
5. verify tenant-scoped viewer results

## Docker Compose operations profile

The self-hosted product Compose file includes an on-demand `operations` profile. It mounts both
persistent data volumes plus a host backup directory.

Set in the private environment file:

    TRACEFORGE_BACKUP_DIR=./traceforge-backups

Create a backup:

    docker compose       --env-file .traceforge.env       -f deploy/compose/product.yml       --profile ops run --rm operations       backup       --tenant-db /tenant-data/traceforge-tenants.db       --trace-db /trace-data/traceforge.db       --output-dir /backups/backup-001

Verify it:

    docker compose       --env-file .traceforge.env       -f deploy/compose/product.yml       --profile ops run --rm operations       verify-backup       --backup-dir /backups/backup-001

For restore, stop normal product services first while preserving named volumes:

    docker compose       --env-file .traceforge.env       -f deploy/compose/product.yml       stop tenant-control-plane privacy-gateway central-collector trace-store

Then restore:

    docker compose       --env-file .traceforge.env       -f deploy/compose/product.yml       --profile ops run --rm operations       restore       --backup-dir /backups/backup-001       --tenant-db /tenant-data/traceforge-tenants.db       --trace-db /trace-data/traceforge.db       --confirm RESTORE

Restart the product stack only after the restore command succeeds.

## Doctor

The doctor command checks the surfaces that an operator actually depends on:

- gateway health
- control-plane health
- authenticated policy lookup
- viewer health
- viewer-token issuance
- tenant-authenticated viewer stats query

Credentials can be supplied through environment variables to avoid putting them directly in the
command line:

    export TRACEFORGE_CONTROL_PLANE_URL=http://127.0.0.1:8090
    export TRACEFORGE_GATEWAY_HEALTH_URL=http://127.0.0.1:8080
    export TRACEFORGE_VIEWER_URL=http://127.0.0.1:8081
    export TRACEFORGE_API_KEY='<ingest-policy-api-key>'
    export TRACEFORGE_VIEWER_API_KEY='<viewer-api-key>'

    traceforge-ops doctor

The result is JSON and exits non-zero when any configured check fails. It is suitable for a manual
release gate or a controlled deployment pipeline.

The doctor does not print either API key or the issued viewer token.

## Backup security

Trace backups contain sanitized telemetry, not raw prompts by design, but they are still operational
data and may contain identifiers or metadata. Tenant backups contain hashed API-key material and
organization policy.

Treat backup directories as confidential:

- encrypt at rest
- restrict filesystem/cloud access
- apply finite retention
- test restore regularly
- do not commit backups or manifests into the repository
- keep copies in a failure domain separate from the runtime host

A successful backup command proves snapshot consistency at creation time. It does not replace a
tested off-host retention and disaster-recovery policy.
