# Multi-tenant control plane

TraceForge M7 adds a tenant control plane that separates organization policy and API-key identity
from the OTLP data plane.

The reference implementation is intentionally small and auditable:

- SQLite WAL organization registry for the single-instance reference deployment
- per-organization policy profiles
- scoped API keys stored only as PBKDF2-HMAC-SHA256 hashes
- revocable credentials
- short-lived HMAC-signed ingest and viewer tokens
- read-only policy delivery over HTTP
- authenticated organization identity enforced again at the OTLP gateway
- tenant identity overwrites any client-supplied organization attribute

The control plane never returns stored API-key plaintext because plaintext is never persisted.

## Trust flow

    administrator
        |
        | traceforge-tenants create-org / issue-key
        v
    Tenant Registry
        |
        | API key
        v
    Agent Adapter
        |
        | GET /v1/policy
        | POST /v1/ingest-token
        v
    Control Plane
        |
        | short-lived signed ingest token
        v
    TraceForge Privacy Gateway
        |
        | verifies token, requires ingest scope,
        | overwrites organization identity
        v
    sanitized OTLP storage / analytics

mTLS can still be enabled at the same time. In that profile the client must pass both the
certificate authorization boundary and tenant-token authorization.

## Create an organization

    traceforge-tenants --db ./traceforge-tenants.db create-org       --id org_acme       --name "Acme Engineering"       --profile strict

List organizations:

    traceforge-tenants --db ./traceforge-tenants.db list-orgs

Suspend an organization immediately:

    traceforge-tenants --db ./traceforge-tenants.db set-status       --org org_acme       --status suspended

Suspended organizations cannot authenticate API keys or receive new policy/ingest tokens.

## Issue a scoped API key

    traceforge-tenants --db ./traceforge-tenants.db issue-key       --org org_acme       --label developer-fleet       --scopes ingest,policy:read

The command prints the API key once. Save it in the endpoint secret store. TraceForge persists only
a salted PBKDF2 hash.

Revoke it by key ID:

    traceforge-tenants --db ./traceforge-tenants.db revoke-key       --key-id <key-id>

Supported reference scopes are:

- ingest
- policy:read
- viewer:read
- admin

The admin scope satisfies other control-plane scope checks.

## Run the control plane

Generate a long random signing secret and place it in a secret manager. For a local example:

    export TRACEFORGE_TENANT_SIGNING_KEY='<at-least-32-random-bytes>'
    traceforge-control-plane       --db ./traceforge-tenants.db       --http-address 127.0.0.1:8090

Container build:

    docker build -f Dockerfile.control-plane -t traceforge-control-plane:0.1.0 .

The HTTP surface is intentionally narrow:

    GET  /healthz
    GET  /v1/me
    GET  /v1/policy
    POST /v1/ingest-token
    POST /v1/viewer-token

Administrative mutations remain CLI-only in the reference release so the network service does not
expose organization creation, credential issuance, or credential revocation.

## Connect an adapter

    export TRACEFORGE_CONTROL_PLANE_URL=http://127.0.0.1:8090
    export TRACEFORGE_API_KEY='<tenant-api-key>'

    codex exec --json "inspect the repository" |
      traceforge-adapter codex         --endpoint 127.0.0.1:4317         --insecure

The adapter fetches its effective organization policy and a short-lived ingest token. The API key is
not sent to the OTLP gateway. Only the short-lived ingest token is attached as gRPC authorization
metadata.

A static --policy-file and --control-plane-url are mutually exclusive.

## Require tenant authorization at the gateway

Use the same signing secret in the gateway runtime:

    export TRACEFORGE_TENANT_SIGNING_KEY='<same-secret>'
    export TRACEFORGE_TENANT_AUTH_REQUIRED=true

The gateway verifies:

1. token signature
2. issuer
3. expiration
4. ingest scope

After privacy scrubbing, the gateway removes any client-supplied tenant organization fields and
writes the authenticated organization ID itself. This prevents an endpoint from placing traces in
another tenant simply by forging trace attributes.

The per-client rate limiter also keys on authenticated organization identity when tenant
authorization is active.

## Rotation

The signing key is a data-plane trust root. Rotate it through the deployment secret manager during a
maintenance window. The current reference signer accepts one active key at a time. Overlap-based
dual-key signing is a planned production hardening item if zero-downtime signing-key rotation is
required.

API keys are independent of the signing key and can be revoked per tenant at any time.

## Production persistence note

The included SQLite registry is the tested reference control-plane store and is suitable for a
single durable control-plane instance with a persistent volume. Do not scale this specific SQLite
image horizontally against independent local disks.

For a multi-replica commercial deployment, keep the same TenantRegistry behavior but move the
registry tables to a managed transactional database before scaling the control-plane service. The
OTLP tenant-token contract and gateway enforcement do not depend on SQLite.


## Tenant-isolated query plane

TraceForge keeps the authenticated organization boundary after ingestion instead of treating the
organization ID as display-only metadata.

The local SQLite store uses a composite identity:

    (organization_id, trace_id, span_id)

This prevents one organization from overwriting another organization even when an endpoint
deliberately reuses the same trace and span IDs. Existing pre-tenant databases are migrated into the
reserved `org_legacy` organization when the store starts.

Every local viewer query can be scoped by organization, including:

- aggregate stats
- sessions and traces
- trace lookup
- search
- privacy counters
- sanitized JSONL export

The ADX/Kusto repository applies the same organization constraint as a Kusto query parameter before
search, grouping, projection, or export. The organization value is never interpolated into KQL.

## Short-lived viewer tokens

A tenant API key with `viewer:read` can request a 15-minute viewer token:

    curl -sS -X POST \
      -H "Authorization: Bearer <viewer-api-key>" \
      http://127.0.0.1:8090/v1/viewer-token

The returned access token contains only the organization identity, the `viewer:read` scope, issue
and expiry times, and an opaque token ID. It does not contain the long-lived API-key secret.

When `TRACEFORGE_VIEWER_TENANT_AUTH_REQUIRED=true`, every `/api/*` viewer request must provide:

    Authorization: Bearer <short-lived-viewer-token>

The HTML shell and health endpoint remain non-sensitive, but the stats, sessions, traces, search,
privacy, and export APIs are blocked without a valid viewer token.

For the bundled browser viewer, open the page once with the token in the URL fragment:

    http://127.0.0.1:8081/#token=<short-lived-viewer-token>

The fragment is not sent in the HTTP request. The viewer moves it into browser session storage and
immediately removes it from the visible URL, then sends it only as an Authorization header for API
calls. Closing the browser session clears the stored token.

## Azure viewer organization scope

The Azure viewer remains protected by Microsoft Entra at the Container Apps edge. When a shared ADX
database stores more than one TraceForge organization, set `viewerOrganizationId` in Bicep for a
customer-specific viewer deployment.

The viewer process then adds the organization filter to every Kusto query. This is application-level
query isolation layered behind Entra authentication. A shared multi-customer SaaS deployment that
requires database-enforced row isolation should add an Azure data-plane isolation mechanism before
claiming database-level tenant isolation.
