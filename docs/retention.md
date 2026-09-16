# Retention policy

TraceForge treats retention as part of the privacy boundary. Sanitized telemetry should not live forever merely because storage is available.

## Default policy

The reference policy keeps operational telemetry for **30 days** unless an operator explicitly chooses another window.

| Store | Default | Enforcement |
| --- | ---: | --- |
| Local SQLite reference store | 30 days | ingestion-time pruning sweep |
| Log Analytics / Application Insights | 30 days | workspace retention |
| Azure Data Explorer | 30 days | database soft-delete period |
| ADX hot cache | 7 days | database hot-cache period |
| Sanitized ADLS archive | 30 days | Storage lifecycle deletion rule |

Key Vault's 90-day soft-delete setting protects secret recovery and is not a telemetry-retention control.

## Local SQLite retention

`traceforge-store` runs a retention sweep at startup and then throttles additional sweeps while telemetry is ingested. Rows are expired by their `ingested_at` timestamp so delayed/offline telemetry receives a predictable retention window after it reaches the store.

Environment variables:

```text
TRACEFORGE_STORE_RETENTION_DAYS=30
TRACEFORGE_STORE_RETENTION_SWEEP_SECONDS=3600
```

Equivalent CLI flags:

```text
--retention-days 30
--retention-sweep-seconds 3600
```

Set `TRACEFORGE_STORE_RETENTION_DAYS=0` only when an operator intentionally wants to disable automatic local pruning. Production-like profiles should keep a finite retention value.

Each completed prune emits a privacy-safe structured audit event with only deletion counts and the configured window. Trace/session IDs and deleted telemetry values are never placed in that event.

## Azure Data Explorer retention

The Bicep parameters are:

```text
kustoSoftDeletePeriod=P30D
kustoHotCachePeriod=P7D
```

`kustoSoftDeletePeriod` controls how long ADX keeps queryable telemetry. `kustoHotCachePeriod` controls the faster cache window and does not extend the soft-delete lifetime.

## Sanitized ADLS archive retention

When `enableBlobArchive=true`, `deploy/azure/main.bicep` creates a Storage lifecycle rule named `expire-sanitized-archives`. It deletes block blobs under:

```text
sanitized-traces/
sanitized-metrics/
sanitized-logs/
```

after `archiveRetentionDays`, which defaults to 30 days.

Example override:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --template-file deploy/azure/main.bicep \
  --parameters \
      gatewayImage=<registry>/traceforge:0.1.0 \
      collectorImage=<registry>/traceforge-collector:0.1.0 \
      enableBlobArchive=true \
      archiveRetentionDays=14
```

The seven-day Blob/container soft-delete settings are recovery controls for recently deleted data. The lifecycle rule is the actual archive expiration policy.

## Operational guidance

Use the shortest retention window that still satisfies the organization's debugging, security, and compliance needs. If a policy change shortens retention, validate the new cutoff in a non-production environment first and record the change through normal infrastructure review. Long-term research datasets should be generated through the sanitized export path and governed separately rather than silently extending production telemetry retention.
