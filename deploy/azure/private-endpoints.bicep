targetScope = 'resourceGroup'

@description('Whether to provision private endpoints and private DNS for TraceForge Azure PaaS dependencies.')
param enabled bool = false

@description('Short lowercase prefix used for Azure resource names.')
param prefix string

@description('Azure region for private endpoints and the ADX private DNS zone.')
param location string

@description('Virtual network linked to all TraceForge private DNS zones.')
param vnetId string

@description('Subnet reserved for Azure Private Endpoints.')
param privateEndpointSubnetId string

@description('Resource ID of the TraceForge Key Vault.')
param keyVaultId string

@description('Resource ID of the TraceForge ADLS Gen2 storage account.')
param storageAccountId string

@description('Whether Azure Data Explorer is deployed.')
param deployKusto bool = false

@description('Resource ID of the TraceForge Azure Data Explorer cluster when deployed.')
param kustoClusterId string = ''

var suffix = substring(uniqueString(resourceGroup().id), 0, 6)
var kustoPrivateEnabled = enabled && deployKusto && !empty(kustoClusterId)
var keyVaultZoneName = 'privatelink.vaultcore.azure.net'
var blobZoneName = 'privatelink.blob.core.windows.net'
var dfsZoneName = 'privatelink.dfs.core.windows.net'
var queueZoneName = 'privatelink.queue.core.windows.net'
var tableZoneName = 'privatelink.table.core.windows.net'
var kustoZoneName = 'privatelink.${location}.kusto.windows.net'

resource keyVaultZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (enabled) {
  name: keyVaultZoneName
  location: 'global'
}

resource blobZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (enabled) {
  name: blobZoneName
  location: 'global'
}

resource dfsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (enabled) {
  name: dfsZoneName
  location: 'global'
}

resource queueZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (kustoPrivateEnabled) {
  name: queueZoneName
  location: 'global'
}

resource tableZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (kustoPrivateEnabled) {
  name: tableZoneName
  location: 'global'
}

resource kustoZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (kustoPrivateEnabled) {
  name: kustoZoneName
  location: 'global'
}

resource keyVaultZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (enabled) {
  parent: keyVaultZone
  name: '${prefix}-kv-link-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource blobZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (enabled) {
  parent: blobZone
  name: '${prefix}-blob-link-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource dfsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (enabled) {
  parent: dfsZone
  name: '${prefix}-dfs-link-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource queueZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (kustoPrivateEnabled) {
  parent: queueZone
  name: '${prefix}-queue-link-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource tableZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (kustoPrivateEnabled) {
  parent: tableZone
  name: '${prefix}-table-link-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource kustoZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (kustoPrivateEnabled) {
  parent: kustoZone
  name: '${prefix}-kusto-link-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource keyVaultPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = if (enabled) {
  name: '${prefix}-kv-pe-${suffix}'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${prefix}-kv-connection'
        properties: {
          privateLinkServiceId: keyVaultId
          groupIds: [
            'vault'
          ]
          privateLinkServiceConnectionState: {
            status: 'Approved'
            description: 'TraceForge Key Vault private endpoint'
          }
        }
      }
    ]
  }
}

resource keyVaultDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = if (enabled) {
  parent: keyVaultPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'key-vault'
        properties: {
          privateDnsZoneId: keyVaultZone.id
        }
      }
    ]
  }
}

resource blobPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = if (enabled) {
  name: '${prefix}-blob-pe-${suffix}'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${prefix}-blob-connection'
        properties: {
          privateLinkServiceId: storageAccountId
          groupIds: [
            'blob'
          ]
          privateLinkServiceConnectionState: {
            status: 'Approved'
            description: 'TraceForge sanitized Blob archive private endpoint'
          }
        }
      }
    ]
  }
}

resource blobDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = if (enabled) {
  parent: blobPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: blobZone.id
        }
      }
    ]
  }
}

resource dfsPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = if (enabled) {
  name: '${prefix}-dfs-pe-${suffix}'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${prefix}-dfs-connection'
        properties: {
          privateLinkServiceId: storageAccountId
          groupIds: [
            'dfs'
          ]
          privateLinkServiceConnectionState: {
            status: 'Approved'
            description: 'TraceForge ADLS Gen2 DFS private endpoint'
          }
        }
      }
    ]
  }
}

resource dfsDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = if (enabled) {
  parent: dfsPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'dfs'
        properties: {
          privateDnsZoneId: dfsZone.id
        }
      }
    ]
  }
}

resource kustoPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = if (kustoPrivateEnabled) {
  name: '${prefix}-adx-pe-${suffix}'
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${prefix}-adx-connection'
        properties: {
          privateLinkServiceId: kustoClusterId
          groupIds: [
            'cluster'
          ]
          privateLinkServiceConnectionState: {
            status: 'Approved'
            description: 'TraceForge Azure Data Explorer private endpoint'
          }
        }
      }
    ]
  }
}

resource kustoDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = if (kustoPrivateEnabled) {
  parent: kustoPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'kusto'
        properties: {
          privateDnsZoneId: kustoZone.id
        }
      }
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: blobZone.id
        }
      }
      {
        name: 'queue'
        properties: {
          privateDnsZoneId: queueZone.id
        }
      }
      {
        name: 'table'
        properties: {
          privateDnsZoneId: tableZone.id
        }
      }
    ]
  }
}

output enabled bool = enabled
output keyVaultPrivateEndpointId string = enabled ? keyVaultPrivateEndpoint.id : ''
output blobPrivateEndpointId string = enabled ? blobPrivateEndpoint.id : ''
output dfsPrivateEndpointId string = enabled ? dfsPrivateEndpoint.id : ''
output kustoPrivateEndpointId string = kustoPrivateEnabled ? kustoPrivateEndpoint.id : ''
