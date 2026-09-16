targetScope = 'resourceGroup'

@description('Whether to create and attach the TraceForge production VNet.')
param enabled bool = false

@description('Short lowercase prefix used for Azure resource names.')
param prefix string

@description('Azure region for the virtual network.')
param location string

@description('Address space for the TraceForge VNet.')
param vnetAddressPrefix string = '10.42.0.0/16'

@description('Dedicated delegated subnet for the Container Apps environment. /27 or larger is required for workload profiles.')
param infrastructureSubnetPrefix string = '10.42.0.0/23'

@description('Dedicated subnet reserved for Azure Private Endpoints.')
param privateEndpointSubnetPrefix string = '10.42.2.0/24'

var suffix = substring(uniqueString(resourceGroup().id), 0, 6)
var vnetName = '${prefix}-vnet-${suffix}'
var infrastructureSubnetName = 'aca-infrastructure'
var privateEndpointSubnetName = 'private-endpoints'

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = if (enabled) {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: infrastructureSubnetName
        properties: {
          addressPrefix: infrastructureSubnetPrefix
          delegations: [
            {
              name: 'container-apps-environment'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: privateEndpointSubnetName
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

output vnetId string = enabled ? resourceId('Microsoft.Network/virtualNetworks', vnetName) : ''
output infrastructureSubnetId string = enabled ? resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, infrastructureSubnetName) : ''
output privateEndpointSubnetId string = enabled ? resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, privateEndpointSubnetName) : ''
