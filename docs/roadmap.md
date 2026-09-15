# Roadmap

## M0 - Foundation

- Architecture and threat model
- Python package and test harness
- Edge OpenTelemetry Collector configuration
- CI quality gates

## M1 - Privacy kernel

- Sensitive-key policy
- Known token and credential detection
- PII detection
- High-entropy token detection
- Stable HMAC tokenization option
- Recursive JSON/attribute scrubbing
- Privacy regression corpus

## M2 - OTLP privacy gateway

- OTLP/gRPC TraceService receiver
- Span, event, resource, and link attribute scrubbing
- Health and readiness endpoints
- Upstream OTLP exporter with TLS/mTLS
- Backpressure, retry, request size limits, and fail-closed behavior

## M3 - Local end-to-end demo

- Docker Compose
- Edge collector -> privacy gateway -> central collector -> Jaeger or Tempo
- Synthetic coding-agent trace generator
- Windows PowerShell and Linux/macOS shell bootstrap
- Demonstrable secret-leak prevention test

## M4 - Correlation and viewer

- task -> session -> trace model
- Read-only API
- Trace timeline and search UI
- Scrub finding counts without storing matched secret values
- Export of sanitized datasets

## M5 - Azure production profile

- Infrastructure as code
- Azure Container Apps
- Managed identity and Key Vault
- Microsoft Entra ID authentication
- Azure Monitor / Application Insights OTLP export
- ADX/Kusto query store
- Blob/ADLS sanitized archive
- Private networking and RBAC

## M6 - Security and reliability

- mTLS and certificate rotation
- Rate limiting and abuse controls
- Audit logging
- Retention policies
- SBOM, CodeQL, dependency scanning, secret scanning
- Load tests, soak tests, failure injection, and privacy benchmarks

## M7 - Productization

- Pluggable adapters for Copilot CLI, Claude Code, Codex, and generic tools
- Policy profiles and organization controls
- Multi-tenant control plane
- Deployment guide and commercial packaging
