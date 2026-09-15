# KARZOUN TraceForge

Privacy-first observability and trace collection for AI coding agents.

TraceForge is building a vendor-neutral pipeline that captures developer-tool telemetry with OpenTelemetry, removes secrets and personally identifiable information before centralized persistence, and supports secure trace correlation and review.

> Status: early engineering preview. The privacy kernel is implemented first because centralized storage must never become the first place where sensitive data is filtered.

## Why TraceForge

AI coding tools can produce valuable operational traces, but those traces can also contain source code, prompts, shell arguments, access tokens, cookies, user identifiers, local paths, and other sensitive context. TraceForge treats the developer endpoint as a high-sensitivity trust zone and makes privacy enforcement a mandatory hop before durable centralized storage.

## Architecture

```text
AI coding agent
      |
      | OTLP on localhost
      v
Edge OpenTelemetry Collector
      |
      | OTLP over TLS/mTLS
      v
TraceForge Privacy Gateway
      |
      | sanitized OTLP only
      v
Central OpenTelemetry Collector
      |-------------------|------------------|
      v                   v                  v
Azure Monitor         ADX / Kusto       Blob / ADLS
      \___________________|__________________/
                          |
                    Read-only Viewer
                          |
                    Microsoft Entra ID
```

See [docs/architecture.md](docs/architecture.md) for the full target design and [docs/threat-model.md](docs/threat-model.md) for the security model.

## Implemented now

- Recursive telemetry/JSON scrubbing
- Known credential detection for GitHub tokens, AWS access keys, JWTs, bearer tokens, and generic secret assignments
- Email, IPv4, and phone-number redaction
- High-entropy opaque-token detection
- Sensitive-key policy with drop-by-default rules for prompts, completions, source content, request/response bodies, and database statements
- Optional stable HMAC tokenization for privacy-preserving equality correlation
- CLI for local scrubbing experiments
- Edge OpenTelemetry Collector configuration bound to loopback
- Unit tests covering privacy-critical behavior
- OTLP/gRPC privacy gateway with fail-closed upstream forwarding
- Trace, event, link, resource, and instrumentation-scope attribute scrubbing
- Lightweight `/healthz` and `/readyz` endpoints
- Local Docker Compose path from gateway to a central OpenTelemetry Collector

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -e .
```

Scrub a JSON file:

```bash
traceforge-scrub sample.json --show-findings
```

Or pipe JSON through stdin:

```bash
echo '{"user.email":"dev@example.com","service.name":"demo"}' | traceforge-scrub
```

Example output:

```json
{
  "user.email": "[REDACTED:sensitive_key]",
  "service.name": "demo"
}
```

Tokenization mode uses HMAC so repeated sensitive values can correlate without persisting plaintext:

```bash
export TRACEFORGE_TOKENIZATION_KEY='use-a-real-secret-from-a-secret-manager'
traceforge-scrub sample.json --mode tokenize
```

## Tests

```bash
pytest
```


## Run the privacy gateway locally

The gateway accepts OTLP/gRPC on port `4317`, scrubs the request in memory, and forwards only the sanitized request to an upstream OTLP collector.

```bash
docker compose up --build
```

Health endpoints:

```text
http://localhost:8080/healthz
http://localhost:8080/readyz
```

For a production network path, set `TRACEFORGE_UPSTREAM_INSECURE=false` and provide a CA file. Client certificate and key environment variables enable mTLS.

## OpenTelemetry edge collector

The first edge configuration lives at `deploy/otel/edge-collector.yaml`. It listens on loopback only and forwards traces to the privacy gateway.

```bash
export TRACEFORGE_GATEWAY_OTLP_ENDPOINT=privacy-gateway.example:4317
export TRACEFORGE_GATEWAY_INSECURE=false
otelcol-contrib --config deploy/otel/edge-collector.yaml
```

The reference collector distribution is OpenTelemetry Collector Contrib. Production deployment will pin and verify release artifacts.

## Delivery plan

The project is intentionally staged so privacy is proven before storage and UI work. See [docs/roadmap.md](docs/roadmap.md).

The OTLP/gRPC privacy gateway is now implemented and covered by an in-process forwarding test. The next engineering milestone is the synthetic coding-agent generator and local trace viewer, followed by the Azure production profile.

## Commercial status

This repository is public for engineering transparency and portfolio review. No open-source license is granted at this stage. Commercial licensing and distribution terms will be defined before the first production release.
