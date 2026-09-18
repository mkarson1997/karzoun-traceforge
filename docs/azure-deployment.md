# Azure production profile

TraceForge M5 moves the proven privacy boundary from the local Docker reference stack into Azure without changing the core invariant: raw coding-agent telemetry is scrubbed before any centralized durable backend sees it.

## Production data path

```text
AI coding agent / edge collector
            |
            | OTLP/gRPC TLS :443
            | optional required client certificate
            | optional short-lived tenant ingest token
            v
Azure Container Apps
TraceForge Privacy Gateway
            |
            | sanitized OTLP/gRPC TLS
            v
Internal OTel Collector
       |          |             |
       v          v             v
Application   ADX/Kusto      ADLS Gen2
Insights      (optional)     (optional archive)
                  |
                  | read-only managed identity
                  v
         Entra-protected TraceForge Viewer
```

The gateway, collector, and production viewer use separate user-assigned managed identities. Key Vault is RBAC-enabled and remains the secret authority for runtime credentials such as an optional TraceForge tokenization key and tenant-token signing key. The collector receives Storage Blob Data Contributor on the sanitized archive account and, when ADX is enabled, database Ingestor on the TraceForge Kusto database. The viewer receives only the database-level Kusto `Viewer` role.

## What the Bicep deployment creates

`deploy/azure/main.bicep` provisions:

- Log Analytics workspace
- workspace-based Application Insights
- RBAC-enabled Key Vault with purge protection
- ADLS Gen2 storage account with private `sanitized-traces`, `sanitized-metrics`, and `sanitized-logs` containers
- user-assigned identities for gateway and collector
- optional user-assigned identity for the production viewer
- Azure Container Apps environment
- internal OpenTelemetry Collector app
- externally reachable HTTP/2 OTLP privacy gateway
- optional required client-certificate mode on the OTLP gateway ingress
- optional required signed tenant-token authorization on the OTLP gateway
- optional Azure Data Explorer cluster/database/schema and collector Ingestor assignment
- optional ADX-backed TraceForge viewer Container App
- database-level ADX `Viewer` assignment for the viewer identity
- Container Apps built-in Microsoft Entra authentication for the viewer
- optional dedicated VNet with a delegated Container Apps infrastructure subnet
- optional private-endpoint subnet
- optional Private DNS + Private Endpoints for Key Vault, Storage Blob/DFS, and ADX

The production networking controls are opt-in so the low-cost engineering baseline remains easy to deploy. When both `enableVnetIntegration=true` and `enablePrivateEndpoints=true`, public network access is disabled for Key Vault, the ADLS account, and ADX, and the Container Apps environment resolves those services through linked Azure Private DNS zones.

## Runtime images

Build the privacy gateway:

```bash
docker build -t <registry>/traceforge:0.1.0 .
docker push <registry>/traceforge:0.1.0
```

Build the pinned collector image from `Dockerfile.collector`:

```bash
docker build -f Dockerfile.collector -t <registry>/traceforge-collector:0.1.0 .
docker push <registry>/traceforge-collector:0.1.0
```

Build the ADX-backed viewer image from `Dockerfile.viewer`:

```bash
docker build -f Dockerfile.viewer -t <registry>/traceforge-viewer:0.1.0 .
docker push <registry>/traceforge-viewer:0.1.0
```

The collector image is based on OpenTelemetry Collector Contrib `0.160.0` and contains four profiles:

```text
/etc/otelcol-contrib/azure-monitor.yaml
/etc/otelcol-contrib/azure-monitor-blob.yaml
/etc/otelcol-contrib/azure-kusto.yaml
/etc/otelcol-contrib/azure-kusto-blob.yaml
```

Application Insights is always enabled. ADX and Blob/ADLS archival are independent opt-in switches in Bicep. ADX is disabled by default because it has a material hourly cost. Blob archival is disabled by default because the upstream OpenTelemetry Azure Blob exporter is currently alpha; TraceForge therefore treats it as an explicit preview feature instead of silently making it part of the production baseline.

## Deploy the baseline

Prerequisites:

- Azure CLI with Bicep
- a resource group
- gateway and collector images reachable by Azure Container Apps
- permission to create role assignments in the target subscription/resource group

Validate locally before deploying:

```bash
az bicep build --file deploy/azure/main.bicep
```

Deploy the lowest-cost baseline, which sends sanitized traces to Application Insights only:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --template-file deploy/azure/main.bicep \
  --parameters \
      gatewayImage=<registry>/traceforge:0.1.0 \
      collectorImage=<registry>/traceforge-collector:0.1.0
```

Enable ADX/Kusto ingestion:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --template-file deploy/azure/main.bicep \
  --parameters \
      gatewayImage=<registry>/traceforge:0.1.0 \
      collectorImage=<registry>/traceforge-collector:0.1.0 \
      deployKusto=true
```

Enable the opt-in sanitized ADLS/Blob trace archive as well:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --template-file deploy/azure/main.bicep \
  --parameters \
      gatewayImage=<registry>/traceforge:0.1.0 \
      collectorImage=<registry>/traceforge-collector:0.1.0 \
      deployKusto=true \
      enableBlobArchive=true
```

The deployment outputs `gatewayOtlpEndpoint`. Configure edge collectors or agents to send OTLP/gRPC to that endpoint with TLS enabled.

## OTLP client mTLS and certificate rotation

TraceForge can require a client certificate on the public OTLP ingress and authorize specific certificate SHA-256 thumbprints in the privacy gateway.

Set `gatewayTrustedClientCertificateHashes` to one or more 64-character SHA-256 certificate thumbprints. When the list is non-empty, the Bicep template changes Azure Container Apps ingress to `clientCertificateMode=require`. Container Apps performs the client-certificate handshake and forwards certificate metadata through `X-Forwarded-Client-Cert`. The gateway then extracts only the forwarded `Hash` field and compares it against the configured allowlist using constant-time comparison before admission or privacy processing.

A parameter file is the least error-prone way to supply the array:

```json
{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "gatewayImage": { "value": "<registry>/traceforge:0.1.0" },
    "collectorImage": { "value": "<registry>/traceforge-collector:0.1.0" },
    "gatewayTrustedClientCertificateHashes": {
      "value": [
        "<64-hex-sha256-thumbprint>"
      ]
    }
  }
}
```

Deploy it with:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --template-file deploy/azure/main.bicep \
  --parameters @production.parameters.json
```

Certificate rotation is intentionally overlap-based and does not require downtime:

1. Deploy `[old, new]` thumbprints.
2. Roll clients from the old certificate to the new certificate.
3. Confirm all clients are using the new certificate.
4. Deploy `[new]` and retire the old certificate.

An empty allowlist leaves the engineering-preview ingress in certificate-ignore mode for backwards compatibility. Production deployments should provide an explicit allowlist.

This design does not claim that the Python process independently validates the full X.509 chain. Azure Container Apps handles the client-certificate handshake at ingress; TraceForge performs application-level authorization against the forwarded SHA-256 thumbprint.

## Tenant token authorization

The Azure gateway can enforce the M7 multi-tenant ingest boundary without passing the tenant signing
key as a normal deployment parameter.

1. Store the same high-entropy signing key used by the tenant control plane in Key Vault.
2. Pass the Key Vault secret URI as `tenantSigningSecretUri`.
3. The gateway managed identity reads the secret through the existing Key Vault Secrets User role.
4. Bicep injects the secret as `TRACEFORGE_TENANT_SIGNING_KEY` and sets
   `TRACEFORGE_TENANT_AUTH_REQUIRED=true`.

Example:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --template-file deploy/azure/main.bicep \
  --parameters \
      gatewayImage=<registry>/traceforge:0.1.0 \
      collectorImage=<registry>/traceforge-collector:0.1.0 \
      tenantSigningSecretUri=https://<vault>.vault.azure.net/secrets/traceforge-tenant-signing-key
```

When enabled, the gateway rejects missing, invalid, expired, or wrong-scope tenant tokens before
privacy processing. After scrubbing, it removes any organization identity supplied by the endpoint
and writes the organization ID from the verified token. The deployment output
`gatewayTenantAuthEnabled` confirms whether this mode is active.

The tenant control plane may be deployed separately from the telemetry data plane. Both components
must use the same signing key. The reference control-plane store remains single-instance SQLite;
multi-replica commercial control-plane deployments should use a shared managed transactional store.

Tenant authorization and client-certificate authorization are independent controls and may be
enabled together.

## Gateway admission control and metrics

Each gateway replica bounds simultaneous OTLP export processing before it copies or scrubs request payloads. The Azure defaults are:

```text
gatewayMaxInflightExports = 64
gatewayAdmissionTimeoutSeconds = 0.25
```

The matching runtime environment variables are:

```text
TRACEFORGE_MAX_INFLIGHT_EXPORTS
TRACEFORGE_ADMISSION_TIMEOUT_SECONDS
```

If all admission slots remain occupied past the timeout, the gateway returns gRPC `RESOURCE_EXHAUSTED` and clients should retry with backoff.

The health listener now exposes:

```text
/healthz
/readyz
/metrics
```

`/metrics` returns Prometheus text-format counters/gauges for current and maximum in-flight exports, backpressure saturation, accepted/rejected exports, client-certificate authorization rejections, upstream failures, and aggregate scrub findings/removal/rewrite counts. No matched secret value or certificate body is emitted in these metrics.

## ADX-backed production viewer

The production viewer is intentionally separate from the local SQLite reference store. It uses `KustoTraceRepository` to issue read-only, parameterized KQL against `OTELTraces`, preserving the same API shape used by the existing TraceForge browser UI.

The viewer is deployed only when all three of these conditions are true:

```text
deployKusto=true
viewerImage is not empty
entraClientId is not empty
```

This prevents the public viewer ingress from being created accidentally without both a durable production backend and authentication configuration.

The viewer Container App receives its own user-assigned managed identity. That identity gets the database-level ADX `Viewer` role and no ingestion role. The application authenticates to ADX using that managed identity through `AZURE_CLIENT_ID`.

## Microsoft Entra setup for the viewer

Create a Microsoft Entra application registration for the browser viewer and record its Application (client) ID. The deployment uses Container Apps built-in authentication with the Entra provider, `RedirectToLoginPage`, HTTPS-only auth responses, and the configured client ID as the allowed audience.

For the app registration:

1. Create a Web application registration in the tenant that will access TraceForge.
2. Enable ID tokens for the Web platform when using the secretless browser sign-in flow.
3. Deploy TraceForge with the client ID supplied as `entraClientId`.
4. Read the `viewerCallbackUrl` deployment output.
5. Add that exact URL as a Web redirect URI on the Entra application registration.

The callback has the form:

```text
https://<traceforge-viewer-fqdn>/.auth/login/aad/callback
```

Deploy the protected viewer:

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

Use `entraTenantId=<tenant-id>` only when the app registration belongs to a tenant other than the deployment tenant. Otherwise the Bicep template defaults to the current Azure tenant.

Important: the first deployment can create the protected viewer before its callback URL has been added to the app registration. In that interim state the viewer remains behind authentication, but sign-in will fail until the exact `viewerCallbackUrl` output is registered. This is safer than creating an anonymous viewer merely to discover its hostname.

## Private networking mode

`deploy/azure/networking.bicep` creates a dedicated VNet with two subnets when `enableVnetIntegration=true`:

- `aca-infrastructure`, delegated to `Microsoft.App/environments`
- `private-endpoints`, reserved for Azure Private Endpoints

Default address ranges are deliberately roomy for a demo/production-preview environment:

```text
VNet:                    10.42.0.0/16
Container Apps subnet:   10.42.0.0/23
Private Endpoint subnet: 10.42.2.0/24
```

They can be overridden with `vnetAddressPrefix`, `infrastructureSubnetPrefix`, and `privateEndpointSubnetPrefix`.

`deploy/azure/private-endpoints.bicep` creates and links the Azure Private DNS zones required by the services TraceForge uses, then attaches Private Endpoints to the reserved subnet:

- Key Vault `vault` -> `privatelink.vaultcore.azure.net`
- Storage Blob `blob` -> `privatelink.blob.core.windows.net`
- ADLS Gen2 `dfs` -> `privatelink.dfs.core.windows.net`
- ADX `cluster` -> `privatelink.<region>.kusto.windows.net`
- ADX supporting zones -> `privatelink.blob.core.windows.net`, `privatelink.queue.core.windows.net`, and `privatelink.table.core.windows.net`

Enable the complete private profile with:

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

`enablePrivateEndpoints=true` is effective only when `enableVnetIntegration=true`. When the private profile is active, the template switches these resources to public-network disabled/deny mode:

- Key Vault
- TraceForge ADLS Gen2 account
- Azure Data Explorer, when deployed

The gateway and Entra-protected viewer still have deliberate external Container Apps ingress because they are the product's public entry surfaces. The collector remains internal-only.

The deployment returns `virtualNetworkId`, `infrastructureSubnetId`, `privateEndpointSubnetId`, `privateEndpointsEnabled`, `gatewayMutualTlsEnabled`, and `gatewayTenantAuthEnabled` so operators can verify the selected topology and OTLP client-certificate mode after deployment.

## ADX schema

When `deployKusto=true`, Bicep creates the upstream OpenTelemetry exporter schema before the collector starts:

- `OTELTraces`
- `OTELMetrics`
- `OTELLogs`

The collector uses queued ingestion and authenticates with its user-assigned managed identity, not a client secret. The database assignment grants only `Ingestor`.

The viewer has a separate principal assignment with role `Viewer`. It can query the database but cannot ingest or alter telemetry through its application identity.

## Optional tokenization mode

Redaction is the default and needs no secret. To enable stable HMAC tokenization:

1. Put a strong random value in Key Vault.
2. Pass its secret URI through `tokenizationSecretUri`.
3. Set `redactionMode=tokenize`.

Example:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --template-file deploy/azure/main.bicep \
  --parameters \
      gatewayImage=<registry>/traceforge:0.1.0 \
      collectorImage=<registry>/traceforge-collector:0.1.0 \
      redactionMode=tokenize \
      tokenizationSecretUri=https://<vault>.vault.azure.net/secrets/traceforge-tokenization-key
```

The gateway identity receives Key Vault Secrets User. The secret value is never a normal Bicep parameter and never appears in TraceForge configuration.

## Security notes

- Gateway is the only externally exposed OTLP component.
- A non-empty client-certificate thumbprint allowlist makes Container Apps require a certificate and makes the gateway authorize its forwarded SHA-256 hash.
- A non-empty `tenantSigningSecretUri` makes the gateway require short-lived signed tenant ingest tokens and overwrite client-claimed organization identity.
- Central collector ingress is internal to the Container Apps environment.
- Gateway-to-collector traffic uses TLS.
- Gateway admission is bounded and emits explicit backpressure metrics.
- Production viewer ingress is HTTPS and protected by Container Apps built-in Entra authentication.
- Viewer-to-ADX access uses a dedicated managed identity with only database-level `Viewer` permissions.
- Storage disallows public blob access and shared-key authorization.
- Collector archive access uses managed identity and Azure RBAC.
- Kusto ingestion uses managed identity and database-level `Ingestor`, not cluster admin.
- Key Vault uses Azure RBAC and purge protection.
- Private mode disables public access to Key Vault, ADLS, and ADX and resolves them through linked Private DNS zones.
- Raw prompt/source/body fields are still removed by the same privacy kernel before the collector receives them.
- User-controlled viewer search values are passed to ADX as query parameters instead of being concatenated into KQL.

## Live validation CLI

`traceforge-azure-validate` turns the final M5 checks into a repeatable operator command. It can
read deployment outputs through Azure CLI, assert the hardened controls selected for the
deployment, bootstrap a tenant token from the control plane, and send a TLS OTLP synthetic trace.

Example for the hardened profile:

```bash
export TRACEFORGE_CONTROL_PLANE_URL=https://<control-plane-host>
export TRACEFORGE_API_KEY='<tenant-api-key>'

traceforge-azure-validate \
  --resource-group <resource-group> \
  --deployment-name <deployment-name> \
  --require-private \
  --require-mtls \
  --require-tenant-auth \
  --require-viewer \
  --client-cert-file ./client.crt \
  --client-key-file ./client.key
```

For a deployment without the tenant control plane, a short-lived token can instead be supplied
through `TRACEFORGE_TENANT_TOKEN`. Avoid placing API keys or tenant tokens directly in shell
history.

The CLI checks deployment outputs and verifies that the public OTLP gateway accepts the synthetic
request over TLS with the selected authentication controls. Persisted-data inspection, Private DNS
resolution from inside the Container Apps environment, Entra browser callback behavior, and
rollback/redeployment remain explicit live-subscription checks because they require access to the
deployed Azure resources.

## Remaining M5 validation

The Azure production profile now has the gateway, collector, Azure Monitor sink, optional ADX ingestion, optional archive sink, ADX-backed viewer, least-privilege viewer identity, Entra edge authentication, optional client-certificate mTLS authorization, VNet integration, Private DNS, and service Private Endpoints in code.

M5 is not marked fully production-validated until a real subscription deployment verifies:

- Bicep deployment success for the selected Azure region and subscription policies
- private DNS resolution from the Container Apps environment
- Key Vault, Storage, and ADX connectivity with public access disabled
- viewer Entra sign-in and callback configuration
- OTLP client-certificate handshake and thumbprint authorization
- signed tenant-token issuance, rejection paths, and authenticated organization overwrite
- end-to-end sanitized OTLP ingestion and ADX query behavior
- rollback/redeployment behavior

M6 security and reliability hardening and M7 reference productization are complete in code. The remaining gate is live Azure validation of the production profile.
