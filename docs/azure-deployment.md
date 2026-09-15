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
```

The gateway and collector use separate user-assigned managed identities. Key Vault is RBAC-enabled and created as the secret authority for runtime credentials such as an optional TraceForge tokenization key. The collector receives Storage Blob Data Contributor on the sanitized archive account and, when ADX is enabled, database Ingestor on the TraceForge Kusto database.

## What the Bicep deployment creates

`deploy/azure/main.bicep` provisions:

- Log Analytics workspace
- workspace-based Application Insights
- RBAC-enabled Key Vault with purge protection
- ADLS Gen2 storage account with private `sanitized-traces`, `sanitized-metrics`, and `sanitized-logs` containers
- user-assigned identities for gateway and collector
- Azure Container Apps environment
- internal OpenTelemetry Collector app
- externally reachable HTTP/2 OTLP privacy gateway
- optional Azure Data Explorer cluster/database/schema and collector Ingestor assignment

Private endpoints are deliberately not claimed as complete yet. The first M5 profile keeps Azure service public endpoints enabled while enforcing TLS, identity/RBAC, and no public blob access. Private networking is the next hardening slice after this deployment is validated in a real Azure subscription.

## Collector image

Build the pinned collector image from `Dockerfile.collector`:

```bash
docker build -f Dockerfile.collector -t <registry>/traceforge-collector:0.1.0 .
docker push <registry>/traceforge-collector:0.1.0
```

The image is based on OpenTelemetry Collector Contrib `0.160.0` and contains four profiles:

```text
/etc/otelcol-contrib/azure-monitor.yaml
/etc/otelcol-contrib/azure-monitor-blob.yaml
/etc/otelcol-contrib/azure-kusto.yaml
/etc/otelcol-contrib/azure-kusto-blob.yaml
```

Application Insights is always enabled. ADX and Blob/ADLS archival are independent opt-in switches in Bicep. ADX is disabled by default because it has a material hourly cost. Blob archival is disabled by default because the upstream OpenTelemetry Azure Blob exporter is currently alpha; TraceForge therefore treats it as an explicit preview feature instead of silently making it part of the production baseline.

## Gateway image

Build the normal TraceForge runtime image:

```bash
docker build -t <registry>/traceforge:0.1.0 .
docker push <registry>/traceforge:0.1.0
```

The Bicep template overrides the command to `traceforge-gateway` and configures the collector's internal Container Apps FQDN as its upstream. The gateway uses TLS for that hop.

## Deploy

Prerequisites:

- Azure CLI with Bicep
- a resource group
- two container images reachable by Azure Container Apps
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

Enable ADX/Kusto:

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

## ADX schema

When `deployKusto=true`, Bicep creates the upstream OpenTelemetry exporter schema before the collector starts:

- `OTELTraces`
- `OTELMetrics`
- `OTELLogs`

The collector uses queued ingestion and authenticates with its user-assigned managed identity, not a client secret. The database assignment grants only `Ingestor`.

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

## Entra-protected production viewer

The local viewer still uses the SQLite reference repository. Deploying that implementation as the Azure production query plane would be the wrong durability model, so M5 does not disguise it as production-ready. The next slice replaces the viewer repository with an ADX-backed read-only repository and then places Container Apps built-in Microsoft Entra authentication in front of it.

Until that ADX viewer lands, use Application Insights or ADX directly for Azure trace review.

## Security notes

- Gateway is the only externally exposed OTLP component.
- Central collector ingress is internal to the Container Apps environment.
- Gateway-to-collector traffic uses TLS.
- Storage disallows public blob access and shared-key authorization.
- Collector archive access uses managed identity and Azure RBAC.
- Kusto ingestion uses managed identity and database-level `Ingestor`, not cluster admin.
- Key Vault uses Azure RBAC and purge protection.
- Raw prompt/source/body fields are still removed by the same privacy kernel before the collector receives them.

M6 will add inbound mTLS policy/certificate rotation, private endpoints/VNet hardening, rate controls, audit policy, SBOM/scanning, and failure/load testing.
