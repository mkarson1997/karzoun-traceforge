# KARZOUN TraceForge

Privacy-first observability and trace collection for AI coding agents.

TraceForge is a vendor-neutral OpenTelemetry pipeline that captures coding-agent telemetry, removes secrets and personally identifiable information before durable storage, correlates task/session/trace activity, and exposes a searchable read-only trace viewer plus sanitized dataset exports.

> Status: engineering preview. M0-M4 are implemented in the local reference stack. M5 now includes the Azure infrastructure/runtime foundation and is moving into the ADX-backed production viewer and private-networking phase.

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
              v
TraceForge Trace Store
  defense-in-depth scrub -> SQLite WAL
              |
              v
Searchable read-only API + Trace Viewer
              |
              v
Sanitized JSONL export
```

The Azure production profile preserves the same privacy boundary while adding Azure Container Apps, separate managed identities, RBAC-enabled Key Vault, Application Insights, optional ADX/Kusto, and optional ADLS/Blob sanitized archives.

See [docs/architecture.md](docs/architecture.md), [docs/threat-model.md](docs/threat-model.md), [docs/data-model.md](docs/data-model.md), [docs/azure-deployment.md](docs/azure-deployment.md), and [docs/roadmap.md](docs/roadmap.md).

## Implemented

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
- Central OpenTelemetry Collector with queued/retried delivery to the local store
- SQLite WAL trace store with idempotent `(trace_id, span_id)` upserts
- Task -> session -> trace correlation
- Read-only JSON API and browser trace timeline
- Sanitized span search across IDs, names, agent/service fields, attributes, and events
- Service, agent, and status filters in the read-only API
- Sanitized JSONL export over HTTP and the `traceforge-export` CLI
- Synthetic coding-agent generator containing deliberate privacy test vectors
- Defense-in-depth scrub at storage ingress
- CI regression tests proving fake prompts, source code, email addresses, and credentials do not reach persistent storage or exports
- PowerShell and POSIX one-command demo bootstrap scripts
- Bicep Azure production foundation with Container Apps, managed identities, Key Vault, Log Analytics, Application Insights, ADLS Gen2, and optional ADX
- Pinned OpenTelemetry Collector Contrib Azure profiles for Monitor, ADX, and opt-in Blob archival

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

The viewer includes session browsing, span/attribute search, privacy finding totals, trace waterfalls, and a one-click sanitized JSONL export.

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
pytest
```

Run quality checks:

```bash
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

The expected path is:

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

For production transport, configure a trusted CA and client certificate/key for mTLS. Production images and collector artifacts are pinned or validated as part of the security track.

## Azure production profile

The M5 infrastructure entry point is `deploy/azure/main.bicep`. Build both runtime images first:

```bash
docker build -t <registry>/traceforge:0.1.0 .
docker build -f Dockerfile.collector -t <registry>/traceforge-collector:0.1.0 .
```

Validate the template:

```bash
az bicep build --file deploy/azure/main.bicep
```

Deploy the baseline, which uses Application Insights as the centralized backend:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --template-file deploy/azure/main.bicep \
  --parameters \
      gatewayImage=<registry>/traceforge:0.1.0 \
      collectorImage=<registry>/traceforge-collector:0.1.0
```

ADX is opt-in with `deployKusto=true`. Sanitized ADLS/Blob archival is independently opt-in with `enableBlobArchive=true`; it is not the default because the upstream Azure Blob exporter is still alpha. See [docs/azure-deployment.md](docs/azure-deployment.md) for deployment, cost, identity, and security details.

The local SQLite viewer is intentionally not presented as the Azure production query plane. The remaining M5 viewer work is an ADX-backed read-only repository followed by Microsoft Entra protection at the Container Apps edge.

## Delivery plan

M0-M4 are implemented in the reference stack. The first M5 Azure slice is implemented: IaC, Container Apps gateway/collector, managed identities, Key Vault, Azure Monitor, ADLS infrastructure, and optional ADX ingestion. M5 continues with the ADX-backed viewer, Entra authentication, private networking, and live Azure deployment validation. M6 then hardens security and reliability, and M7 packages adapters and organization controls for product use.

## Commercial status

This repository is public for engineering transparency and portfolio review. No open-source license is granted at this stage. Commercial licensing and distribution terms will be defined before the first production release.
