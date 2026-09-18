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

@description('Container image built from Dockerfile.viewer. Required with entraClientId to deploy the protected ADX viewer.')
param viewerImage string = ''

@description('Microsoft Entra application client ID used by Container Apps built-in authentication for the production viewer.')
param entraClientId string = ''

@description('Microsoft Entra tenant ID that owns the viewer app registration.')
param entraTenantId string = tenant().tenantId

@description('Optional organization ID that scopes every production viewer query. Recommended when a shared ADX database contains multiple organizations.')
param viewerOrganizationId string = ''

@description('SHA-256 client certificate thumbprints allowed to send OTLP. A non-empty list enables required mTLS at Container Apps ingress and gateway-side thumbprint authorization.')
param gatewayTrustedClientCertificateHashes array = []

@description('Maximum number of OTLP exports admitted concurrently by one gateway replica.')
@minValue(1)
param gatewayMaxInflightExports int = 64

@description('How long an OTLP export may wait for gateway admission capacity before RESOURCE_EXHAUSTED.')
param gatewayAdmissionTimeoutSeconds string = '0.25'

@description('Maximum OTLP exports accepted per client per minute by each gateway replica. Set 0 to disable.')
@minValue(0)
param gatewayClientRateLimitPerMinute int = 600

@description('Maximum client identities retained by per-client rate-limit bookkeeping.')
@minValue(1)
param gatewayRateLimitMaxClients int = 4096

@description('Deploy Azure Data Explorer. Disabled by default because ADX has a material hourly cost.')
param deployKusto bool = false

@description('Enable sanitized OTLP JSON archives in ADLS/Blob. The OpenTelemetry azureblob exporter is alpha, so this is opt-in.')
param enableBlobArchive bool = false

@description('Delete sanitized archive blobs after this many days when Blob archival is enabled.')
@minValue(1)
param archiveRetentionDays int = 30

@description('Create a dedicated VNet and attach the Container Apps environment to its delegated infrastructure subnet.')
param enableVnetIntegration bool = false

@description('Create private endpoints/private DNS for Key Vault, ADLS, and optional ADX, and disable their public network access. Requires enableVnetIntegration=true.')
param enablePrivateEndpoints bool = false

@description('Address space used when enableVnetIntegration=true.')
param vnetAddressPrefix string = '10.42.0.0/16'

@description('Dedicated Container Apps infrastructure subnet. /27 or larger is required for workload profiles.')
param infrastructureSubnetPrefix string = '10.42.0.0/23'

@description('Dedicated subnet reserved for Azure Private Endpoints.')
param privateEndpointSubnetPrefix string = '10.42.2.0/24'

@description('Gateway redaction mode: redact, drop, or tokenize.')
@allowed([
  'redact'
  'drop'
  'tokenize'
])
param redactionMode string = 'redact'

@description('Optional Key Vault secret URI containing TRACEFORGE_TOKENIZATION_KEY. Required when redactionMode=tokenize.')
param tokenizationSecretUri string = ''

@description('Optional Key Vault secret URI containing the tenant-token signing key. When supplied, the gateway requires signed tenant ingest tokens.')
param tenantSigningSecretUri string = ''

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
var viewerName = '${prefix}-viewer-${suffix}'
var kustoClusterName = '${normalizedPrefix}adx${suffix}'
var kustoDatabaseName = 'traceforge'
var deployViewer = deployKusto && !empty(viewerImage) && !empty(entraClientId)
var privateNetworkingEnabled = enableVnetIntegration && enablePrivateEndpoints
var gatewayMutualTlsEnabled = length(gatewayTrustedClientCertificateHashes) > 0
var gatewayTenantAuthEnabled = !empty(tenantSigningSecretUri)
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var collectorConfig = deployKusto
  ? (enableBlobArchive ? '/etc/otelcol-contrib/azure-kusto-blob.yaml' : '/etc/otelcol-contrib/azure-kusto.yaml')
  : (enableBlobArchive ? '/etc/otelcol-contrib/azure-monitor-blob.yaml' : '/etc/otelcol-contrib/azure-monitor.yaml')

module networking './networking.bicep' = {
  name: 'traceforge-networking'
  params: {
    enabled: enableVnetIntegration
    prefix: prefix
    location: location
    vnetAddressPrefix: vnetAddressPrefix
    infrastructureSubnetPrefix: infrastructureSubnetPrefix
    privateEndpointSubnetPrefix: privateEndpointSubnetPrefix
  }
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
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
    publicNetworkAccess: privateNetworkingEnabled ? 'Disabled' : 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: privateNetworkingEnabled ? 'Deny' : 'Allow'
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
    publicNetworkAccess: privateNetworkingEnabled ? 'Disabled' : 'Enabled'
    supportsHttpsTrafficOnly: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: privateNetworkingEnabled ? 'Deny' : 'Allow'
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

resource archiveLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2024-01-01' = if (enableBlobArchive) {
  parent: archiveStorage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          enabled: true
          name: 'expire-sanitized-archives'
          type: 'Lifecycle'
          definition: {
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: archiveRetentionDays
                }
              }
            }
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                'sanitized-traces/'
                'sanitized-metrics/'
                'sanitized-logs/'
              ]
            }
          }
        }
      ]
    }
  }
  dependsOn: [
    sanitizedTraces
    sanitizedMetrics
    sanitizedLogs
  ]
}

resource collectorIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-collector-mi-${suffix}'
  location: location
}

resource gatewayIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-gateway-mi-${suffix}'
  location: location
}

resource viewerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (deployViewer) {
  name: '${prefix}-viewer-mi-${suffix}'
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

var environmentProperties = union(
  {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  },
  enableVnetIntegration
    ? {
        vnetConfiguration: {
          infrastructureSubnetId: networking.outputs.infrastructureSubnetId
          internal: false
        }
      }
    : {}
)

resource containerEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' = {
  name: environmentName
  location: location
  properties: environmentProperties
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
    publicNetworkAccess: privateNetworkingEnabled ? 'Disabled' : 'Enabled'
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
  name: guid(kustoDatabase.id, collectorIdentity.id, 'ingestor')
  properties: {
    principalId: collectorIdentity.properties.clientId
    principalType: 'App'
    role: 'Ingestor'
    tenantId: tenant().tenantId
  }
}

resource kustoViewer 'Microsoft.Kusto/clusters/databases/principalAssignments@2025-02-14' = if (deployViewer) {
  parent: kustoDatabase
  name: guid(kustoDatabase.id, viewerIdentity.id, 'viewer')
  properties: {
    principalId: viewerIdentity.properties.clientId
    principalType: 'App'
    role: 'Viewer'
    tenantId: tenant().tenantId
  }
}

module privateEndpoints './private-endpoints.bicep' = {
  name: 'traceforge-private-endpoints'
  params: {
    enabled: privateNetworkingEnabled
    prefix: prefix
    location: location
    vnetId: networking.outputs.vnetId
    privateEndpointSubnetId: networking.outputs.privateEndpointSubnetId
    keyVaultId: keyVault.id
    storageAccountId: archiveStorage.id
    deployKusto: deployKusto
    kustoClusterId: deployKusto ? kustoCluster.id : ''
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
    archiveLifecycle
    privateEndpoints
  ]
}

var gatewaySecrets = concat(
  empty(tokenizationSecretUri) ? [] : [
    {
      name: 'tokenization-key'
      keyVaultUrl: tokenizationSecretUri
      identity: gatewayIdentity.id
    }
  ],
  empty(tenantSigningSecretUri) ? [] : [
    {
      name: 'tenant-signing-key'
      keyVaultUrl: tenantSigningSecretUri
      identity: gatewayIdentity.id
    }
  ]
)

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
  {
    name: 'TRACEFORGE_MAX_INFLIGHT_EXPORTS'
    value: string(gatewayMaxInflightExports)
  }
  {
    name: 'TRACEFORGE_ADMISSION_TIMEOUT_SECONDS'
    value: gatewayAdmissionTimeoutSeconds
  }
  {
    name: 'TRACEFORGE_TRUSTED_CLIENT_CERT_HASHES'
    value: join(gatewayTrustedClientCertificateHashes, ',')
  }
  {
    name: 'TRACEFORGE_CLIENT_RATE_LIMIT_PER_MINUTE'
    value: string(gatewayClientRateLimitPerMinute)
  }
  {
    name: 'TRACEFORGE_RATE_LIMIT_MAX_CLIENTS'
    value: string(gatewayRateLimitMaxClients)
  }
],
empty(tokenizationSecretUri) ? [] : [
  {
    name: 'TRACEFORGE_TOKENIZATION_KEY'
    secretRef: 'tokenization-key'
  }
],
empty(tenantSigningSecretUri) ? [] : [
  {
    name: 'TRACEFORGE_TENANT_SIGNING_KEY'
    secretRef: 'tenant-signing-key'
  }
  {
    name: 'TRACEFORGE_TENANT_AUTH_REQUIRED'
    value: 'true'
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
        clientCertificateMode: gatewayMutualTlsEnabled ? 'require' : 'ignore'
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
    privateEndpoints
  ]
}

resource viewerApp 'Microsoft.App/containerApps@2026-01-01' = if (deployViewer) {
  name: viewerName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${viewerIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8081
        transport: 'http'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
    }
    template: {
      containers: [
        {
          name: 'viewer'
          image: viewerImage
          env: [
            {
              name: 'TRACEFORGE_KUSTO_CLUSTER_URI'
              value: kustoCluster.properties.uri
            }
            {
              name: 'TRACEFORGE_KUSTO_DATABASE'
              value: kustoDatabaseName
            }
            {
              name: 'TRACEFORGE_KUSTO_AUTH'
              value: 'managed_identity'
            }
            {
              name: 'TRACEFORGE_VIEWER_HTTP_ADDRESS'
              value: '0.0.0.0:8081'
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: viewerIdentity.properties.clientId
            }
            {
              name: 'TRACEFORGE_VIEWER_ORGANIZATION_ID'
              value: viewerOrganizationId
            }
          ]
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
    kustoSchema
    kustoViewer
    privateEndpoints
  ]
}

resource viewerAuth 'Microsoft.App/containerApps/authConfigs@2026-01-01' = if (deployViewer) {
  parent: viewerApp
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            entraClientId
          ]
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

output gatewayFqdn string = gatewayApp.properties.configuration.ingress.fqdn
output gatewayOtlpEndpoint string = '${gatewayApp.properties.configuration.ingress.fqdn}:443'
output gatewayMutualTlsEnabled bool = gatewayMutualTlsEnabled
output gatewayTenantAuthEnabled bool = gatewayTenantAuthEnabled
output gatewayClientRateLimitPerMinute int = gatewayClientRateLimitPerMinute
output collectorFqdn string = collectorApp.properties.configuration.ingress.fqdn
output viewerFqdn string = deployViewer ? viewerApp.properties.configuration.ingress.fqdn : ''
output viewerUrl string = deployViewer ? 'https://${viewerApp.properties.configuration.ingress.fqdn}' : ''
output viewerOrganizationId string = viewerOrganizationId
output viewerCallbackUrl string = deployViewer ? 'https://${viewerApp.properties.configuration.ingress.fqdn}/.auth/login/aad/callback' : ''
output virtualNetworkId string = enableVnetIntegration ? networking.outputs.vnetId : ''
output infrastructureSubnetId string = enableVnetIntegration ? networking.outputs.infrastructureSubnetId : ''
output privateEndpointSubnetId string = enableVnetIntegration ? networking.outputs.privateEndpointSubnetId : ''
output privateEndpointsEnabled bool = privateNetworkingEnabled
output applicationInsightsName string = appInsights.name
output keyVaultUri string = keyVault.properties.vaultUri
output archiveStorageAccount string = archiveStorage.name
output archiveRetentionDays int = archiveRetentionDays
output kustoClusterUri string = deployKusto ? kustoCluster.properties.uri : ''
output kustoDatabase string = deployKusto ? kustoDatabase.name : ''
