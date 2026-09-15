# KARZOUN TraceForge

Privacy-first observability and trace collection for AI coding agents.

TraceForge is a vendor-neutral OpenTelemetry pipeline that captures coding-agent telemetry, removes secrets and personally identifiable information before durable storage, correlates task/session/trace activity, and exposes a read-only trace viewer.

> Status: engineering preview. The local end-to-end reference stack is working; the Azure production profile is the next major delivery track.

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
Read-only API + Trace Viewer
```

The production profile will preserve the same privacy boundary while adding Azure Container Apps, Managed Identity/Key Vault, Azure Monitor, ADX/Kusto, Blob/ADLS, private networking, RBAC, and Microsoft Entra ID.

See [docs/architecture.md](docs/architecture.md), [docs/threat-model.md](docs/threat-model.md), [docs/data-model.md](docs/data-model.md), and [docs/roadmap.md](docs/roadmap.md).

## Implemented

- Recursive telemetry/JSON scrubbing
- GitHub token, AWS access key, JWT, bearer token, and generic secret detection
- Email, IPv4, and phone-number redaction
- High-entropy opaque-token detection
- Drop-by-default policy for prompts, completions, source code, request/response bodies, and database statements
- Optional stable HMAC tokenization for privacy-preserving equality correlation
- OTLP/gRPC privacy gateway with upstream TLS/mTLS support and fail-closed forwarding
- Trace, event, link, resource, and instrumentation-scope attribute scrubbing
- `/healthz` and `/readyz` gateway endpoints
- Central OpenTelemetry Collector with queued/retried delivery to the local store
- SQLite WAL trace store with idempotent `(trace_id, span_id)` upserts
- Task -> session -> trace correlation
- Read-only JSON API and browser trace timeline
- Synthetic coding-agent generator containing deliberate privacy test vectors
- Defense-in-depth scrub at storage ingress
- CI regression test proving fake prompts, source code, email addresses, and credentials do not reach persistent storage
- PowerShell and POSIX one-command demo bootstrap scripts

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

## Privacy invariant

Persistent storage is never intended to be the first scrub point.

The expected path is:

```text
agent -> privacy gateway -> collector -> store
```

The store still scrubs again as defense in depth. CI contains a synthetic leak-prevention test that starts with raw fake secrets and verifies that the resulting persisted trace does not contain them.

## OpenTelemetry edge collector

`deploy/otel/edge-collector.yaml` is the endpoint-side reference configuration. It binds OTLP receivers to loopback and forwards toward the privacy gateway.

```bash
export TRACEFORGE_GATEWAY_OTLP_ENDPOINT=privacy-gateway.example:4317
export TRACEFORGE_GATEWAY_INSECURE=false
otelcol-contrib --config deploy/otel/edge-collector.yaml
```

For production transport, configure a trusted CA and client certificate/key for mTLS. Production images and collector artifacts will be pinned and verified as part of the security milestone.

## Delivery plan

M0-M3 are implemented. The core of M4 is also present: correlation, persistent local storage, read-only APIs, and the trace timeline. Remaining M4 work is search/filtering, privacy-finding counters, and sanitized dataset export. M5 then moves the reference design into the Azure production profile.

## Commercial status

This repository is public for engineering transparency and portfolio review. No open-source license is granted at this stage. Commercial licensing and distribution terms will be defined before the first production release.
