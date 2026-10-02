targetScope = 'resourceGroup'

@minLength(2)
@maxLength(16)
param environmentName string
param location string = 'swedencentral'
@allowed(['dev', 'prod'])
param profile string = 'dev'
@description('Object ID of the approved bootstrap operator, not an application/client ID.')
@minLength(36)
@maxLength(36)
param operatorObjectId string
@description('Existing Foundry account, project and model deployments are reused, not provisioned here.')
param foundryResourceGroup string
param foundryAccountName string
@allowed(['basic', 'standard'])
param searchSku string = 'basic'
@minValue(1)
@maxValue(3)
param searchReplicas int = profile == 'prod' ? 2 : 1
@minValue(1)
@maxValue(3)
param searchPartitions int = 1

var name = '${environmentName}-${take(uniqueString(resourceGroup().id, environmentName), 6)}'
var tags = { environment: environmentName, purpose: 'regulatory-evidence-workbench', profile: profile }
var services = ['api', 'ui']

resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: replace('st${name}', '-', '')
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    publicNetworkAccess: 'Enabled'
  }
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: { enabled: true, days: 7 }
  }
}
resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = [for container in [
  'approved-documents'
  'comparison-reports'
  'poc-state'
]: {
  parent: blobs
  name: container
  properties: { publicAccess: 'None' }
}]
resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' = {
  name: replace('cr${name}', '-', '')
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}
resource logs 'Microsoft.OperationalInsights/workspaces@2025-02-01' = {
  name: 'log-${name}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: profile == 'prod' ? 1 : json('0.1') }
    features: { enableLogAccessUsingOnlyResourcePermissions: true }
  }
}
resource appEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: 'cae-${name}'
  location: location
  tags: tags
  properties: {
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}
resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: 'srch-${name}'
  location: location
  tags: tags
  sku: { name: searchSku }
  identity: { type: 'SystemAssigned' }
  properties: {
    replicaCount: searchReplicas
    partitionCount: searchPartitions
    hostingMode: 'Default'
    publicNetworkAccess: 'enabled'
    disableLocalAuth: true
    semanticSearch: 'free'
  }
}
resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = [for service in services: {
  name: 'id-${service}-${name}'
  location: location
  tags: tags
}]
module access './modules/access.bicep' = {
  name: 'regulatory-workbench-app-access'
  params: {
    acrName: registry.name
    searchName: search.name
    storageAccountName: storage.name
    principals: [for (service, i) in services: identities[i].properties.principalId]
  }
  dependsOn: [containers]
}
module foundry './modules/foundry-access.bicep' = {
  name: 'regulatory-workbench-foundry-access'
  scope: resourceGroup(foundryResourceGroup)
  params: {
    accountName: foundryAccountName
    appPrincipals: [for (service, i) in services: identities[i].properties.principalId]
    searchPrincipal: search.identity.principalId
  }
}
resource operatorSearchRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in [
  '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
]: {
  name: guid(search.id, operatorObjectId, role)
  scope: search
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
  }
}]
resource operatorBlobRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (container, i) in [
  'approved-documents'
  'comparison-reports'
  'poc-state'
]: {
  name: guid(containers[i].id, operatorObjectId, 'blob-data')
  scope: containers[i]
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}]

output foundationName string = name
output registryName string = registry.name
output registryEndpoint string = registry.properties.loginServer
output storageAccountName string = storage.name
output searchName string = search.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output environmentId string = appEnvironment.id
output identityNames array = [for (service, i) in services: identities[i].name]
output applicationNames array = [for service in services: 'ca-${service}-${name}']
