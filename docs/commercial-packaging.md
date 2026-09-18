# Commercial packaging

TraceForge is currently an engineering-preview product. This document defines how a release is
packaged for evaluation, self-hosted delivery, and enterprise deployment without implying a
production SLA or general-availability status.

## Product deliverables

A versioned TraceForge delivery can contain:

- Python wheel and source distribution
- privacy-gateway / local-store runtime image
- pinned OpenTelemetry Collector image definition
- ADX viewer image
- tenant control-plane image
- single-node Docker Compose product profile
- Azure Bicep infrastructure profile
- architecture, threat model, deployment, retention, and multi-tenancy documentation
- CycloneDX SBOM
- dependency-audit evidence
- SHA-256 release checksums

The product does not require customers to send raw prompts, source code, command strings, tool
arguments, or assistant responses to the centralized telemetry plane. The adapters intentionally
use a bounded metadata model, and the gateway scrubs again before durable storage.

## Packaging profiles

### Evaluation

Use the normal local Docker Compose stack or the product compose profile on a trusted workstation.
This profile is intended for technical evaluation and proof-of-concept work.

### Self-hosted single node

Use deploy/compose/product.yml with durable Docker volumes. Tenant policy, API keys, signed ingest
tokens, mandatory gateway tenant identity, retention, audit events, and the sanitized local viewer
are available in this profile.

The included control-plane SQLite registry is intentionally a single-instance persistence model.
It is not represented as horizontally scalable.

### Enterprise Azure

Use deploy/azure/main.bicep for the telemetry/data plane and the Entra-protected ADX viewer. The
profile supports managed identities, Key Vault, optional ADX, ADLS, Application Insights, VNet
integration, Private DNS, and Private Endpoints.

Before a commercial multi-replica control-plane deployment, replace the reference SQLite tenant
registry with a shared managed transactional store while keeping the same tenant API and
short-lived ingest-token contract.

## Release contract

TraceForge uses semantic versioning for packaged releases:

    MAJOR.MINOR.PATCH

Before a release is promoted, the release candidate should have:

- green CI on Python 3.11, 3.12, and 3.13
- successful infrastructure compilation and image builds
- successful CodeQL, Gitleaks, dependency audit, and SBOM generation
- privacy benchmark with zero known-marker leaks
- gateway load/soak and deterministic failure-injection evidence
- successful multi-tenant spoofing and credential lifecycle tests
- a reviewed changelog entry
- documented upgrade and rollback notes

The packaging workflow produces build artifacts and checksums but does not automatically publish
commercial images to a public registry.

## Customer-specific configuration

Keep customer configuration outside the source tree. At minimum, customer deployment material
should define:

- organization IDs and policy profiles
- retention windows
- tenant API-key ownership and rotation policy
- tenant signing-key ownership and rotation policy
- network ingress policy
- mTLS certificate lifecycle if used
- viewer identity/access groups
- storage region and residency requirements
- backup and restore policy
- incident contacts

Never place real API keys, signing keys, private certificates, or customer data in example files.

## Support boundary

The repository documents a tested reference architecture. Customer-specific infrastructure,
identity providers, network appliances, custom data residency, and managed-database migrations
require deployment-specific validation.

Do not promise uptime, support response times, regulatory certification, or compliance status from
the repository alone. Those obligations belong in a separate signed commercial agreement if they
are offered.

## Licensing position

This repository intentionally does not grant an open-source license. Source visibility does not by
itself grant redistribution, modification, hosting, or commercial-use rights. Any commercial
license terms should be supplied separately for the customer and release in question.

Third-party components and dependencies retain their own licenses and notices.

## Handoff package

A commercial technical handoff should include:

1. release version and commit SHA
2. SHA-256 checksums
3. SBOM and dependency-audit evidence
4. deployment profile and environment inventory
5. tenant bootstrap procedure
6. secret ownership/rotation procedure
7. backup/restore procedure
8. upgrade/rollback procedure
9. known limitations
10. support and escalation contacts from the applicable commercial agreement

This keeps the engineering artifact, security evidence, and contractual promises separate and
unambiguous.
