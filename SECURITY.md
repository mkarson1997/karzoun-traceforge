# Security Policy

KARZOUN TraceForge handles telemetry that may contain credentials, source fragments, prompts, identifiers, and other sensitive developer context. Security and privacy reports are treated as high priority.

## Supported versions

TraceForge is currently an engineering preview. Security fixes are applied to the latest `main` branch until the first stable release line is published.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose secrets, private telemetry, authentication data, or a practical exploit path.

Use GitHub's private vulnerability reporting feature for this repository when available. Include:

- affected component and commit or version
- reproduction steps or proof of concept
- expected and observed behavior
- security or privacy impact
- any suggested remediation

If private vulnerability reporting is unavailable, contact the repository owner through a private channel before publishing technical details.

## Scope priorities

Reports are especially useful when they involve:

- privacy-gateway bypasses that allow prohibited raw data into durable storage
- secret or PII scrubbing bypasses
- OTLP client-certificate authorization bypasses
- authentication or authorization bypasses on the production viewer
- Kusto query injection or managed-identity privilege escalation
- cross-tenant or cross-session data exposure
- unsafe archive/export behavior
- denial-of-service paths that bypass admission controls
- dependency or container supply-chain compromise

## Security controls in CI

The repository runs Python tests and linting across supported Python versions, Azure/Bicep compilation, container builds, CodeQL analysis, resolved-dependency vulnerability auditing, CycloneDX SBOM generation, and committed-secret scanning.

Synthetic credentials are intentionally present in privacy regression fixtures. The secret-scanning allowlist is limited to those exact fake values and must not be expanded to broad paths or generic credential patterns.
