# Roadmap

## M0 - Foundation

- [x] Architecture and threat model
- [x] Python package and test harness
- [x] Edge OpenTelemetry Collector configuration
- [x] CI quality gates across Python 3.11-3.13

## M1 - Privacy kernel

- [x] Sensitive-key policy
- [x] Known token and credential detection
- [x] PII detection
- [x] High-entropy token detection
- [x] Stable HMAC tokenization option
- [x] Recursive JSON/attribute scrubbing
- [x] Privacy regression tests

## M2 - OTLP privacy gateway

- [x] OTLP/gRPC TraceService receiver
- [x] Span, event, resource, scope, and link attribute scrubbing
- [x] Health and readiness endpoints
- [x] Upstream OTLP exporter with TLS/mTLS support
- [x] Request size limits and fail-closed upstream behavior
- [ ] Bounded ingress admission control and explicit backpressure metrics

## M3 - Local end-to-end demo

- [x] Docker Compose reference stack
- [x] Privacy gateway -> central collector -> persistent trace store
- [x] Synthetic coding-agent trace generator
- [x] Windows PowerShell and Linux/macOS shell bootstrap scripts
- [x] Demonstrable secret-leak prevention regression test

## M4 - Correlation and viewer

- [x] task -> session -> trace correlation model
- [x] SQLite WAL reference store with idempotent span upserts
- [x] Read-only JSON API
- [x] Trace timeline UI
- [x] Defense-in-depth scrubbing at storage ingress
- [x] Full-text/attribute search and filters
- [x] Persisted scrub-finding counters without matched values
- [x] Sanitized JSONL dataset export

## M5 - Azure production profile

- [x] Bicep infrastructure foundation
- [x] Azure Container Apps privacy gateway and internal collector
- [x] Separate managed identities and RBAC-enabled Key Vault
- [ ] Microsoft Entra ID authentication on the production viewer
- [x] Azure Monitor / workspace-based Application Insights OTLP export
- [x] Optional ADX/Kusto cluster, database, schema, and managed-identity ingestion
- [x] ADLS Gen2 archive containers and opt-in managed-identity Blob exporter profile
- [ ] ADX-backed read-only viewer repository
- [ ] Private endpoints, VNet hardening, and public-network shutdown
- [ ] Live deployment validation in an Azure subscription

## M6 - Security and reliability

- [ ] Inbound mTLS and certificate rotation
- [ ] Rate limiting and abuse controls
- [ ] Audit logging
- [ ] Retention policies
- [ ] SBOM, CodeQL, dependency scanning, and secret scanning
- [ ] Load tests, soak tests, failure injection, and privacy benchmarks

## M7 - Productization

- [ ] Pluggable adapters for Copilot CLI, Claude Code, Codex, and generic tools
- [ ] Policy profiles and organization controls
- [ ] Multi-tenant control plane
- [ ] Deployment guide and commercial packaging
