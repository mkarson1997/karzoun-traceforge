# Deployment guide

TraceForge ships two reference deployment paths:

1. a single-node self-hosted product profile for evaluation, pilots, and controlled internal use
2. the Azure production profile in deploy/azure/main.bicep for managed cloud infrastructure

Both preserve the same privacy invariant: sensitive coding-agent telemetry is scrubbed at the
privacy gateway before centralized durable storage.

## 1. Single-node self-hosted product profile

Requirements:

- Docker Engine with Docker Compose
- a private high-entropy tenant signing key
- a durable host with regular volume backups

Copy the environment template and replace the signing key:

    cp deploy/compose/product.env.example .traceforge.env

Start the stack:

    docker compose       --env-file .traceforge.env       -f deploy/compose/product.yml       up --build -d

The profile starts:

- tenant-control-plane
- privacy-gateway
- central-collector
- trace-store and local viewer

Default exposure:

- OTLP gateway: 0.0.0.0:4317
- gateway health/metrics: 127.0.0.1:8080
- local viewer: 127.0.0.1:8081
- tenant control plane: 127.0.0.1:8090

The viewer and control plane are intentionally loopback-bound by default. Do not publish them
directly to an untrusted network.

### Bootstrap the first organization

Run the tenant administration CLI inside the control-plane container:

    docker compose       --env-file .traceforge.env       -f deploy/compose/product.yml       exec tenant-control-plane       traceforge-tenants --db /data/traceforge-tenants.db create-org         --id org_demo         --name "Demo Engineering"         --profile strict

Issue an endpoint API key:

    docker compose       --env-file .traceforge.env       -f deploy/compose/product.yml       exec tenant-control-plane       traceforge-tenants --db /data/traceforge-tenants.db issue-key         --org org_demo         --label developer-fleet         --scopes ingest,policy:read

Store the printed API key immediately in the endpoint secret store. TraceForge stores only a salted
hash and cannot recover the plaintext later.

### Connect an adapter

On the developer endpoint:

    export TRACEFORGE_CONTROL_PLANE_URL=http://<control-plane-host>:8090
    export TRACEFORGE_API_KEY='<tenant-api-key>'

Then stream one supported coding-agent event source into traceforge-adapter. The adapter obtains the
effective organization policy and a short-lived signed ingest token. The long-lived tenant API key
is never sent to the OTLP gateway.

### Network perimeter

The self-hosted product compose profile does not terminate public TLS itself. If OTLP traffic crosses
an untrusted network, place the gateway behind a trusted TLS/mTLS reverse proxy, service mesh, VPN,
or private network boundary. Do not expose raw port 4317 to the public Internet without an encrypted
transport boundary.

The gateway can additionally enforce the forwarded client-certificate allowlist when deployed
behind a compatible TLS-terminating ingress.

### Backups

Back up both named volumes:

- traceforge-tenant-data
- traceforge-trace-data

The tenant volume contains organization policy, hashed API keys, and credential state. The trace
volume contains sanitized reference telemetry. Backups inherit the sensitivity of those datasets
and must be encrypted and access controlled.

Before a destructive upgrade, stop writes and snapshot both volumes.

### Upgrade

Recommended sequence:

1. back up tenant and trace volumes
2. pull or build the new release images
3. run the release CI/test evidence for the target version
4. restart the control plane first
5. restart the privacy gateway and collector
6. verify /healthz, /readyz, and /metrics
7. send a synthetic tenant-authenticated trace
8. verify the organization ID and privacy counters in the viewer

If an upgrade fails, restore the previous images while preserving the volumes. Database schema
changes must include an explicit migration/rollback note before a production release.

## 2. Azure production profile

The Azure profile is defined in deploy/azure/main.bicep and documented in
docs/azure-deployment.md.

It can provision:

- Azure Container Apps
- managed identities
- RBAC-enabled Key Vault
- Log Analytics and Application Insights
- ADLS Gen2 sanitized archive storage
- optional Azure Data Explorer
- an Entra-protected production viewer
- optional VNet integration, Private DNS, and Private Endpoints

Validate IaC before deployment:

    az bicep build --file deploy/azure/main.bicep

The remaining M5 gate is real-subscription validation of DNS, Entra callback behavior,
managed-identity access, end-to-end sanitized ingestion, and rollback/redeployment behavior.

The current M7 tenant control plane is packaged as a separate container. Its included SQLite
registry is a single-instance reference persistence layer. Do not horizontally scale multiple
control-plane replicas with independent local SQLite disks. For a multi-replica commercial Azure
deployment, replace the registry persistence implementation with a shared managed transactional
database while preserving the existing API-key, policy, and signed-ingest-token contracts.

## Production secrets

At minimum protect:

- TRACEFORGE_TENANT_SIGNING_KEY
- optional TraceForge tokenization key
- endpoint tenant API keys
- TLS/mTLS private keys
- Entra application secrets, if a deployment uses secrets rather than managed identity

Do not commit these values to Git. In Azure, use Key Vault and managed identity wherever supported.

## Readiness checklist

Before treating a deployment as production-ready, verify:

- privacy regression tests pass
- dependency audit, CodeQL, Gitleaks, and SBOM jobs pass
- tenant spoofing test passes
- gateway rejects missing/expired/invalid tenant tokens when tenant auth is required
- retention is configured for every durable store
- viewer authentication and authorization are enabled
- external transport is TLS-protected
- backups and restore procedure are tested
- rate limits and capacity limits are appropriate for expected traffic
- organization suspension and API-key revocation are tested
- incident owner and rollback procedure are documented
