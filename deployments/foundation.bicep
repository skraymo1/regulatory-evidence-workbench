targetScope = 'resourceGroup'

@minLength(2)
@maxLength(16)
param environmentName string
param location string = 'northeurope'
@allowed(['dev', 'prod'])
param profile string = 'dev'
@description('Object ID of the approved bootstrap operator, not an application/client ID.')
@minLength(36)
@maxLength(36)
param operatorObjectId string
param foundryLocation string = 'swedencentral'
param modelName string = 'gpt-5.4-mini'
param modelVersion string = '2026-03-17'
param modelDeploymentName string = modelName
@minValue(1)
param modelCapacity int = 250
param embeddingDeploymentName string = 'text-embedding-3-small'
@minValue(1)
param embeddingCapacity int = 10
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

resource vault 'Microsoft.KeyVault/vaults@2025-05-01' = {
  name: 'kv-${take(environmentName, 10)}-${take(uniqueString(resourceGroup().id), 10)}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enabledForTemplateDeployment: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
  }
}
resource operatorVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, operatorObjectId, 'key-vault-secrets-officer')
  scope: vault
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  }
}
resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'ai-${name}'
  location: foundryLocation
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: 'ai-${name}'
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}
resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundryAccount
  name: 'regulatory-workbench'
  location: foundryLocation
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Regulatory Evidence Workbench'
    description: 'Provisional regulatory evidence analysis requiring expert review.'
  }
}
resource generation 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: modelDeploymentName
  sku: { name: 'GlobalStandard', capacity: modelCapacity }
  properties: {
    model: { format: 'OpenAI', name: modelName, version: modelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  // Project and model writes share an account-level operation lock.
  dependsOn: [foundryProject]
}
resource embeddings 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: embeddingDeploymentName
  sku: { name: 'GlobalStandard', capacity: embeddingCapacity }
  properties: {
    model: { format: 'OpenAI', name: 'text-embedding-3-small', version: '1' }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [generation]
}
resource operatorFoundryRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in [
  '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
]: {
  name: guid(foundryAccount.id, operatorObjectId, role)
  scope: foundryAccount
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
  }
}]
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
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
    }
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
module network './modules/network.bicep' = {
  name: 'regulatory-workbench-network'
  params: {
    name: name
    location: location
    tags: tags
    storageAccountName: storage.name
  }
}
resource appEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: 'cae-net-${name}'
  location: location
  tags: tags
  properties: {
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    vnetConfiguration: {
      infrastructureSubnetId: network.outputs.appSubnetId
      internal: false
    }
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
  params: {
    accountName: foundryAccount.name
    appPrincipals: [for (service, i) in services: identities[i].properties.principalId]
    searchPrincipal: search.identity.principalId
    projectPrincipal: foundryProject.identity.principalId
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
output environmentName string = appEnvironment.name
output environmentDefaultDomain string = appEnvironment.properties.defaultDomain
output identityNames array = [for (service, i) in services: identities[i].name]
output applicationNames array = [for service in services: 'ca-${service}-${name}']
output vaultName string = vault.name
output vaultId string = vault.id
output vaultUrl string = vault.properties.vaultUri
output foundryAccountName string = foundryAccount.name
output projectEndpoint string = foundryProject.properties.endpoints['AI Foundry API']
output embeddingEndpoint string = 'https://${foundryAccount.properties.customSubDomainName}.openai.azure.com/'
output modelDeployment string = generation.name
output modelVersion string = generation.properties.model.version
output embeddingDeployment string = embeddings.name
