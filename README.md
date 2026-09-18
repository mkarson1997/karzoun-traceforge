# KARZOUN TraceForge

Privacy-first observability and trace collection for AI coding agents.

TraceForge is a vendor-neutral OpenTelemetry pipeline that captures coding-agent telemetry, removes secrets and personally identifiable information before durable storage, correlates task/session/trace activity, and exposes a searchable read-only trace viewer plus sanitized dataset exports.

> Status: engineering preview. M0-M4 and M6-M8 are implemented. The remaining production gate is M5 live Azure subscription validation of networking, identity, ingestion, and rollback behavior.

## Why TraceForge

AI coding tools can produce valuable operational traces, but those traces may also contain prompts, source code, shell arguments, access tokens, cookies, user identifiers, database statements, request bodies, and other sensitive context. TraceForge treats privacy as a mandatory network boundary rather than a cleanup job performed after collection.

## Reference architecture

```text
AI coding agent / synthetic generator
              |
              | OTLP
              v
TraceForge Privacy Gateway
  secret + PII + high-entropy scrubbing
              |
              | sanitized OTLP only
              v
Central OpenTelemetry Collector
              |
              +----> Azure Monitor / Application Insights
              +----> Azure Data Explorer / Kusto
              +----> sanitized ADLS / Blob archive
                           |
                           v
                Entra-protected Trace Viewer
```

The local profile uses SQLite WAL behind the same read-only viewer contract. The hardened Azure profile can place Container Apps inside a dedicated VNet and route Key Vault, ADLS, and ADX through Azure Private Link.

See [docs/architecture.md](docs/architecture.md), [docs/threat-model.md](docs/threat-model.md), [docs/data-model.md](docs/data-model.md), [docs/adapters.md](docs/adapters.md), [docs/policies.md](docs/policies.md), [docs/multi-tenancy.md](docs/multi-tenancy.md), [docs/azure-deployment.md](docs/azure-deployment.md), [docs/deployment-guide.md](docs/deployment-guide.md), [docs/commercial-packaging.md](docs/commercial-packaging.md), and [docs/roadmap.md](docs/roadmap.md).

## Implemented

### Privacy and OTLP

- Recursive telemetry/JSON scrubbing
- GitHub token, AWS access key, JWT, bearer token, and generic secret detection
- Email, IPv4, and phone-number redaction
- High-entropy opaque-token detection
- Drop-by-default policy for prompts, completions, source code, request/response bodies, and database statements
- Optional stable HMAC tokenization for privacy-preserving equality correlation
- OTLP/gRPC privacy gateway with upstream TLS/mTLS support and fail-closed forwarding
- Trace, event, link, resource, and instrumentation-scope attribute scrubbing
- Non-sensitive privacy audit counters propagated with an opaque export ID
- `/healthz` and `/readyz` gateway endpoints

### Correlation and local viewer

- Central OpenTelemetry Collector with queued/retried delivery
- SQLite WAL reference trace store with tenant-safe `(organization_id, trace_id, span_id)` upserts
- task -> session -> trace correlation
- Read-only JSON API and browser trace timeline
- Sanitized span search across IDs, names, agent/service fields, attributes, and events
- Service, agent, and status filters
- Sanitized JSONL export over HTTP and the `traceforge-export` CLI
- Defense-in-depth scrubbing at storage ingress
- Synthetic coding-agent generator containing deliberate privacy test vectors
- CI regression tests proving fake prompts, source code, email addresses, and credentials do not reach persistent storage or exports

### Coding-agent adapters

- Vendor-neutral adapter event model
- Codex JSON Lines normalization
- Claude Code stream-json normalization
- GitHub Copilot hook payload normalization
- Generic JSON/JSONL adapter contract
- Adapter-side privacy scrubbing before OTLP serialization
- Bounded metadata capture that excludes prompts, commands, tool payloads, and assistant text
- TLS/mTLS export support through the traceforge-adapter CLI
- Built-in minimal, strict, and standard organization policy profiles
- Per-organization adapter allowlists, capture controls, and batch limits

See [docs/adapters.md](docs/adapters.md) for usage and the generic event contract, and [docs/policies.md](docs/policies.md) for organization policy controls.

### Multi-tenant control plane

- Organization registry with active/suspended lifecycle
- Per-organization effective policy delivery
- Salted PBKDF2-hashed API keys with scoped access and revocation
- Short-lived HMAC-signed ingest and viewer tokens
- Adapter bootstrap through policy and ingest-token endpoints
- Gateway-side token verification and authenticated organization enforcement
- Client-supplied organization IDs are overwritten at the gateway
- Authenticated organization identity participates in per-tenant rate limiting
- Tenant-scoped viewer APIs and sanitized exports
- Parameterized tenant filters in the ADX/Kusto query layer
- Cross-tenant trace/span collision protection in the local store
- Non-root control-plane container image with persistent reference volume

The reference control-plane database is SQLite WAL for a single durable instance. The tenant-token
contract and gateway enforcement are backend-independent, so a commercial horizontally scaled
deployment can replace the registry persistence layer with a managed transactional database.

See [docs/multi-tenancy.md](docs/multi-tenancy.md).

### Self-hosted product profile

A tenant-aware single-node product profile is available at `deploy/compose/product.yml`.

Create a private environment file from the example, replace the signing key, then start the stack:

```bash
cp deploy/compose/product.env.example .traceforge.env
docker compose --env-file .traceforge.env -f deploy/compose/product.yml up --build -d
```

By default the control plane, health endpoint, and local viewer bind to loopback while OTLP ingress
uses port 4317. The self-hosted profile requires an external TLS/mTLS boundary before public
Internet exposure.

See [docs/deployment-guide.md](docs/deployment-guide.md) and
[docs/multi-tenancy.md](docs/multi-tenancy.md).

## Release packaging

The `.github/workflows/package.yml` workflow builds Python distributions, validates every container
image and the product Compose profile, generates dependency/SBOM evidence, creates SHA-256
checksums, and assembles a versioned technical handoff artifact. It does not automatically publish
commercial images to a public registry.

See [docs/commercial-packaging.md](docs/commercial-packaging.md), [SECURITY.md](SECURITY.md), and
[SUPPORT.md](SUPPORT.md).

## Azure production profile

- Bicep infrastructure with Azure Container Apps
- Separate gateway, collector, and viewer managed identities
- RBAC-enabled Key Vault with purge protection
- Workspace-based Application Insights
- Optional ADX/Kusto ingestion with database-level `Ingestor`
- ADX-backed viewer using parameterized KQL
- Dedicated viewer managed identity with database-level `Viewer`
- Microsoft Entra built-in authentication in front of the production viewer
- ADLS Gen2 sanitized archive containers
- Pinned OpenTelemetry Collector Contrib Azure exporter profiles
- Optional dedicated VNet with delegated Container Apps infrastructure subnet
- Dedicated subnet for Private Endpoints
- Linked Azure Private DNS zones
- Key Vault `vault` Private Endpoint
- Storage `blob` and `dfs` Private Endpoints
- Optional ADX `cluster` Private Endpoint plus required Kusto/Blob/Queue/Table private DNS zones
- Public network disabled for Key Vault, ADLS, and ADX when private mode is enabled

## One-command local demo

Requirements: Docker with Compose support.

Windows PowerShell:

```powershell
./scripts/demo.ps1
```

Linux/macOS:

```bash
chmod +x scripts/demo.sh
./scripts/demo.sh
```

The bootstrap builds the stack, waits for the privacy gateway and trace store, then sends three synthetic coding-agent sessions through the real OTLP pipeline.

Open the viewer at:

```text
http://localhost:8081
```

Gateway health endpoints:

```text
http://localhost:8080/healthz
http://localhost:8080/readyz
```

The synthetic generator deliberately injects a fake email, prompt, GitHub-style token, bearer token, request body, source-code value, and completion. Those raw values must disappear before persistent storage.

Stop the stack:

```bash
docker compose down
```

Remove the local trace volume as well:

```bash
docker compose down -v
```

## Python development

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e '.[dev]'
ruff check .
pytest
```

Scrub a JSON file directly:

```bash
traceforge-scrub sample.json --show-findings
```

Send synthetic traces to a running gateway:

```bash
traceforge-demo --endpoint 127.0.0.1:4317 --count 3
```

Run the local store/viewer directly:

```bash
traceforge-store --db ./traceforge.db --otlp-address 127.0.0.1:4320 --http-address 127.0.0.1:8081
```

Export a local sanitized store as JSONL:

```bash
traceforge-export --db ./traceforge.db --output ./sanitized-spans.jsonl
```

Read-only API examples:

```text
GET /api/stats
GET /api/sessions
GET /api/search?q=tool.shell
GET /api/privacy
GET /api/export.jsonl?session=session-id
GET /api/traces/{trace_id}
```

## Privacy invariant

Persistent storage is never intended to be the first scrub point.

```text
agent -> privacy gateway -> collector -> storage/analytics
```

The gateway attaches only an opaque export ID and aggregate privacy counters after scrubbing. The local store scrubs again as defense in depth. CI starts with synthetic raw secrets and verifies that the persisted trace and exported JSONL contain none of those raw values.

## OpenTelemetry edge collector

`deploy/otel/edge-collector.yaml` is the endpoint-side reference configuration. It binds OTLP receivers to loopback and forwards toward the privacy gateway.

```bash
export TRACEFORGE_GATEWAY_OTLP_ENDPOINT=privacy-gateway.example:4317
export TRACEFORGE_GATEWAY_INSECURE=false
otelcol-contrib --config deploy/otel/edge-collector.yaml
```

For production transport, configure a trusted CA and client certificate/key for mTLS.

## Azure production profile

Build the runtime images:

```bash
docker build -t <registry>/traceforge:0.1.0 .
docker build -f Dockerfile.collector -t <registry>/traceforge-collector:0.1.0 .
docker build -f Dockerfile.viewer -t <registry>/traceforge-viewer:0.1.0 .
docker build -f Dockerfile.control-plane -t <registry>/traceforge-control-plane:0.1.0 .
```

Validate the complete Bicep graph:

```bash
az bicep build --file deploy/azure/main.bicep
```

Low-cost baseline:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --template-file deploy/azure/main.bicep \
  --parameters \
      gatewayImage=<registry>/traceforge:0.1.0 \
      collectorImage=<registry>/traceforge-collector:0.1.0
```

Protected ADX viewer:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --template-file deploy/azure/main.bicep \
  --parameters \
      gatewayImage=<registry>/traceforge:0.1.0 \
      collectorImage=<registry>/traceforge-collector:0.1.0 \
      viewerImage=<registry>/traceforge-viewer:0.1.0 \
      deployKusto=true \
      entraClientId=<application-client-id>
```

Full private-network profile:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --template-file deploy/azure/main.bicep \
  --parameters \
      gatewayImage=<registry>/traceforge:0.1.0 \
      collectorImage=<registry>/traceforge-collector:0.1.0 \
      viewerImage=<registry>/traceforge-viewer:0.1.0 \
      deployKusto=true \
      enableBlobArchive=true \
      entraClientId=<application-client-id> \
      enableVnetIntegration=true \
      enablePrivateEndpoints=true
```

The protected viewer is deployed only when ADX is enabled and both `viewerImage` and `entraClientId` are supplied. Private Endpoints are effective only together with VNet integration. See [docs/azure-deployment.md](docs/azure-deployment.md) for redirect URI setup, network topology, identity details, and deployment notes.

## Delivery plan

M0-M4 are complete. The M5 implementation now covers the Azure application path and private-networking code. The final M5 gate is validation in a real Azure subscription, including Private DNS, Entra callback behavior, managed-identity access, sanitized OTLP ingestion, and rollback/redeployment checks.

M6 security and reliability hardening, M7 productization, and M8 tenant query isolation are complete in the reference implementation. The remaining production gate is M5 live Azure validation.

## Commercial status

This repository is public for engineering transparency and portfolio review. No open-source license is granted at this stage. Commercial licensing and distribution terms will be defined before the first production release.
