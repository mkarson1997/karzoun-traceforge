# Azure production profile

TraceForge M5 moves the proven privacy boundary from the local Docker reference stack into Azure without changing the core invariant: raw coding-agent telemetry is scrubbed before any centralized durable backend sees it.

## Production data path

```text
AI coding agent / edge collector
            |
            | OTLP/gRPC TLS :443
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

The gateway, collector, and production viewer use separate user-assigned managed identities. Key Vault is RBAC-enabled and remains the secret authority for runtime credentials such as an optional TraceForge tokenization key. The collector receives Storage Blob Data Contributor on the sanitized archive account and, when ADX is enabled, database Ingestor on the TraceForge Kusto database. The viewer receives only the database-level Kusto `Viewer` role.

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
- optional Azure Data Explorer cluster/database/schema and collector Ingestor assignment
- optional ADX-backed TraceForge viewer Container App
- database-level ADX `Viewer` assignment for the viewer identity
- Container Apps built-in Microsoft Entra authentication for the viewer

Private endpoints are deliberately not claimed as complete yet. The current M5 profile keeps Azure service public endpoints enabled while enforcing TLS, identity/RBAC, no public blob access, and Entra authentication on the viewer. Private networking is the next hardening slice after live Azure validation.

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

Create a Microsoft Entra application registration for the browser viewer and record its Application (client) ID. The current deployment uses Container Apps built-in authentication with the Entra provider, `RedirectToLoginPage`, HTTPS-only auth responses, and the configured client ID as the allowed audience.

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
- Central collector ingress is internal to the Container Apps environment.
- Gateway-to-collector traffic uses TLS.
- Production viewer ingress is HTTPS and protected by Container Apps built-in Entra authentication.
- Viewer-to-ADX access uses a dedicated managed identity with only database-level `Viewer` permissions.
- Storage disallows public blob access and shared-key authorization.
- Collector archive access uses managed identity and Azure RBAC.
- Kusto ingestion uses managed identity and database-level `Ingestor`, not cluster admin.
- Key Vault uses Azure RBAC and purge protection.
- Raw prompt/source/body fields are still removed by the same privacy kernel before the collector receives them.
- User-controlled viewer search values are passed to ADX as query parameters instead of being concatenated into KQL.

## Remaining M5 work

The Azure production profile now has the gateway, collector, Azure Monitor sink, optional ADX ingestion, optional archive sink, ADX-backed viewer, least-privilege viewer identity, and Entra edge authentication in code.

M5 is not considered fully production-validated until the remaining items are complete:

- deploy the template into a real Azure subscription and run end-to-end validation
- add private endpoints/VNet hardening and shut down unnecessary public service endpoints
- exercise viewer authentication and ADX permissions with real tenant identities
- document operational rollback and deployment verification output

M6 then adds inbound mTLS policy/certificate rotation, rate controls, audit policy, SBOM/scanning, and failure/load testing.
