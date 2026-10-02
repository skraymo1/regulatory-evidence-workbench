targetScope = 'resourceGroup'

@description('Suffix shared with the other resources, for example reference-dev-abc123.')
param name string
param location string = resourceGroup().location
param tags object = {}
@description('Existing storage account that receives the blob private endpoint.')
param storageAccountName string
param addressPrefix string = '10.60.0.0/16'
@description('Container Apps workload-profile subnet (minimum /27), delegated to Microsoft.App/environments.')
param appSubnetPrefix string = '10.60.0.0/23'
param privateEndpointSubnetPrefix string = '10.60.2.0/27'

// Workload-profile environments with public ingress only need the default NSG rules:
// public traffic arrives through the environment's public endpoint, not the subnet.
resource appNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-aca-${name}'
  location: location
  tags: tags
  properties: { securityRules: [] }
}
resource endpointNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-pe-${name}'
  location: location
  tags: tags
  properties: { securityRules: [] }
}
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${name}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [addressPrefix] }
    subnets: [
      {
        name: 'snet-aca'
        properties: {
          addressPrefix: appSubnetPrefix
          networkSecurityGroup: { id: appNsg.id }
          delegations: [{ name: 'aca', properties: { serviceName: 'Microsoft.App/environments' } }]
        }
      }
      {
        name: 'snet-pe'
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          networkSecurityGroup: { id: endpointNsg.id }
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}
resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' existing = {
  name: storageAccountName
}
resource blobZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}
resource blobZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: blobZone
  name: 'link-${name}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}
resource blobEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-blob-${storageAccountName}'
  location: location
  tags: tags
  properties: {
    subnet: { id: vnet.properties.subnets[1].id }
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: ['blob']
        }
      }
    ]
  }
}
resource blobZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: blobEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{ name: 'blob', properties: { privateDnsZoneId: blobZone.id } }]
  }
}

output vnetId string = vnet.id
output appSubnetId string = vnet.properties.subnets[0].id
output privateEndpointId string = blobEndpoint.id
