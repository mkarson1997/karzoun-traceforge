targetScope = 'resourceGroup'

@description('Short lowercase prefix used for Azure resource names.')
@minLength(3)
@maxLength(12)
param prefix string = 'traceforge'

@description('Azure region for all regional resources.')
param location string = resourceGroup().location

@description('Container image for the TraceForge privacy gateway.')
param gatewayImage string

@description('Container image built from Dockerfile.collector.')
param collectorImage string

@description('Deploy Azure Data Explorer. Disabled by default because ADX has a material hourly cost.')
param deployKusto bool = false

@description('Enable sanitized OTLP JSON archives in ADLS/Blob. The OpenTelemetry azureblob exporter is alpha, so this is opt-in.')
param enableBlobArchive bool = false

@description('Gateway redaction mode: redact, drop, or tokenize.')
@allowed([
  'redact'
  'drop'
  'tokenize'
])
param redactionMode string = 'redact'

@description('Optional Key Vault secret URI containing TRACEFORGE_TOKENIZATION_KEY. Required when redactionMode=tokenize.')
param tokenizationSecretUri string = ''

@description('Kusto database retention in ISO-8601 duration format.')
param kustoSoftDeletePeriod string = 'P30D'

@description('Kusto hot cache period in ISO-8601 duration format.')
param kustoHotCachePeriod string = 'P7D'

var suffix = substring(uniqueString(resourceGroup().id), 0, 6)
var normalizedPrefix = toLower(replace(prefix, '-', ''))
var logAnalyticsName = '${prefix}-logs-${suffix}'
var appInsightsName = '${prefix}-appinsights-${suffix}'
var keyVaultName = '${normalizedPrefix}kv${suffix}'
var storageName = '${normalizedPrefix}st${suffix}'
var environmentName = '${prefix}-env-${suffix}'
var gatewayName = '${prefix}-gateway-${suffix}'
var collectorName = '${prefix}-collector-${suffix}'
var kustoClusterName = '${normalizedPrefix}adx${suffix}'
var kustoDatabaseName = 'traceforge'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var collectorConfig = deployKusto
  ? (enableBlobArchive ? '/etc/otelcol-contrib/azure-kusto-blob.yaml' : '/etc/otelcol-contrib/azure-kusto.yaml')
  : (enableBlobArchive ? '/etc/otelcol-contrib/azure-monitor-blob.yaml' : '/etc/otelcol-contrib/azure-monitor.yaml')

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: logAnalyticsName
  location: location
  sku: {
    name: 'PerGB2018'
  }
  properties: {
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    DisableLocalAuth: false
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2025-05-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    accessPolicies: []
    enableRbacAuthorization: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource archiveStorage 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: storageName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    isHnsEnabled: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    supportsHttpsTrafficOnly: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = {
  parent: archiveStorage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource sanitizedTraces 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobService
  name: 'sanitized-traces'
  properties: {
    publicAccess: 'None'
  }
}

resource sanitizedMetrics 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobService
  name: 'sanitized-metrics'
  properties: {
    publicAccess: 'None'
  }
}

resource sanitizedLogs 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobService
  name: 'sanitized-logs'
  properties: {
    publicAccess: 'None'
  }
}

resource collectorIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-collector-mi-${suffix}'
  location: location
}

resource gatewayIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-gateway-mi-${suffix}'
  location: location
}

resource collectorKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, collectorIdentity.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    principalId: collectorIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
  }
}

resource gatewayKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, gatewayIdentity.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    principalId: gatewayIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
  }
}

resource collectorArchiveRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(archiveStorage.id, collectorIdentity.id, storageBlobDataContributorRoleId)
  scope: archiveStorage
  properties: {
    principalId: collectorIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
  }
}

resource containerEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource kustoCluster 'Microsoft.Kusto/clusters@2025-02-14' = if (deployKusto) {
  name: kustoClusterName
  location: location
  sku: {
    name: 'Dev(No SLA)_Standard_D11_v2'
    tier: 'Basic'
    capacity: 1
  }
  properties: {
    enableAutoStop: true
    enablePurge: false
    enableStreamingIngest: false
    engineType: 'V2'
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: 'Disabled'
  }
}

resource kustoDatabase 'Microsoft.Kusto/clusters/databases@2025-02-14' = if (deployKusto) {
  parent: kustoCluster
  name: kustoDatabaseName
  location: location
  kind: 'ReadWrite'
  properties: {
    softDeletePeriod: kustoSoftDeletePeriod
    hotCachePeriod: kustoHotCachePeriod
  }
}

resource kustoSchema 'Microsoft.Kusto/clusters/databases/scripts@2025-02-14' = if (deployKusto) {
  parent: kustoDatabase
  name: 'traceforge-otel-schema'
  properties: {
    continueOnErrors: false
    forceUpdateTag: 'otel-schema-v1'
    scriptLevel: 'Database'
    scriptContent: '''
.create-merge table OTELLogs (Timestamp:datetime, ObservedTimestamp:datetime, TraceID:string, SpanID:string, SeverityText:string, SeverityNumber:int, Body:string, ResourceAttributes:dynamic, LogsAttributes:dynamic)
.create-merge table OTELMetrics (Timestamp:datetime, MetricName:string, MetricType:string, MetricUnit:string, MetricDescription:string, MetricValue:real, Host:string, ResourceAttributes:dynamic, MetricAttributes:dynamic)
.create-merge table OTELTraces (TraceID:string, SpanID:string, ParentID:string, SpanName:string, SpanStatus:string, SpanKind:string, StartTime:datetime, EndTime:datetime, ResourceAttributes:dynamic, TraceAttributes:dynamic, Events:dynamic, Links:dynamic)
.alter-merge table OTELTraces (SpanStatusMessage:string)
'''
  }
}

resource kustoIngestor 'Microsoft.Kusto/clusters/databases/principalAssignments@2025-02-14' = if (deployKusto) {
  parent: kustoDatabase
  name: guid(kustoDatabase.id, collectorIdentity.properties.clientId, 'ingestor')
  properties: {
    principalId: collectorIdentity.properties.clientId
    principalType: 'App'
    role: 'Ingestor'
    tenantId: tenant().tenantId
  }
}

var collectorEnv = [
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    secretRef: 'appinsights-connection-string'
  }
  {
    name: 'AZURE_CLIENT_ID'
    value: collectorIdentity.properties.clientId
  }
  {
    name: 'AZURE_STORAGE_BLOB_URL'
    value: 'https://${archiveStorage.name}.blob.${environment().suffixes.storage}'
  }
  {
    name: 'AZURE_DATA_EXPLORER_DATABASE'
    value: kustoDatabaseName
  }
  {
    name: 'AZURE_DATA_EXPLORER_CLUSTER_URI'
    value: deployKusto ? kustoCluster.properties.uri : ''
  }
]

resource collectorApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: collectorName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${collectorIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        allowInsecure: false
        targetPort: 4317
        transport: 'http2'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      secrets: [
        {
          name: 'appinsights-connection-string'
          value: appInsights.properties.ConnectionString
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'collector'
          image: collectorImage
          args: [
            '--config=${collectorConfig}'
          ]
          env: collectorEnv
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
  dependsOn: [
    collectorArchiveRole
    kustoSchema
    kustoIngestor
  ]
}

var gatewaySecrets = empty(tokenizationSecretUri) ? [] : [
  {
    name: 'tokenization-key'
    keyVaultUrl: tokenizationSecretUri
    identity: gatewayIdentity.id
  }
]

var gatewayEnv = concat([
  {
    name: 'TRACEFORGE_LISTEN_ADDRESS'
    value: '0.0.0.0:4317'
  }
  {
    name: 'TRACEFORGE_HEALTH_ADDRESS'
    value: '0.0.0.0:8080'
  }
  {
    name: 'TRACEFORGE_UPSTREAM_OTLP_ENDPOINT'
    value: '${collectorApp.properties.configuration.ingress.fqdn}:443'
  }
  {
    name: 'TRACEFORGE_UPSTREAM_INSECURE'
    value: 'false'
  }
  {
    name: 'TRACEFORGE_REDACTION_MODE'
    value: redactionMode
  }
  {
    name: 'TRACEFORGE_REDACT_PII'
    value: 'true'
  }
  {
    name: 'TRACEFORGE_DETECT_HIGH_ENTROPY'
    value: 'true'
  }
], empty(tokenizationSecretUri) ? [] : [
  {
    name: 'TRACEFORGE_TOKENIZATION_KEY'
    secretRef: 'tokenization-key'
  }
])

resource gatewayApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: gatewayName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${gatewayIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        clientCertificateMode: 'ignore'
        targetPort: 4317
        transport: 'http2'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      secrets: gatewaySecrets
    }
    template: {
      containers: [
        {
          name: 'privacy-gateway'
          image: gatewayImage
          command: [
            'traceforge-gateway'
          ]
          env: gatewayEnv
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 5
      }
    }
  }
  dependsOn: [
    gatewayKeyVaultRole
  ]
}

output gatewayFqdn string = gatewayApp.properties.configuration.ingress.fqdn
output gatewayOtlpEndpoint string = '${gatewayApp.properties.configuration.ingress.fqdn}:443'
output collectorFqdn string = collectorApp.properties.configuration.ingress.fqdn
output applicationInsightsName string = appInsights.name
output keyVaultUri string = keyVault.properties.vaultUri
output archiveStorageAccount string = archiveStorage.name
output kustoClusterUri string = deployKusto ? kustoCluster.properties.uri : ''
output kustoDatabase string = deployKusto ? kustoDatabase.name : ''
