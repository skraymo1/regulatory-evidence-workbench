targetScope = 'subscription'

@minLength(2)
@maxLength(16)
param environmentName string
param location string = 'northeurope'
param principalId string
param allowedObjectId string = ''
@allowed(['dev', 'prod'])
param profile string = 'dev'
param foundryLocation string = 'swedencentral'
param modelName string = 'gpt-5.4-mini'
param modelVersion string = '2026-03-17'
@minValue(1)
param modelCapacity int = 250
@minValue(1)
param embeddingCapacity int = 10

resource group 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: { 'azd-env-name': environmentName }
}
module workbench './workbench.bicep' = {
  name: 'regulatory-workbench-${environmentName}'
  scope: group
  params: {
    environmentName: environmentName
    location: location
    profile: profile
    operatorObjectId: principalId
    foundryLocation: empty(foundryLocation) ? 'swedencentral' : foundryLocation
    modelName: modelName
    modelVersion: modelVersion
    modelCapacity: modelCapacity
    embeddingCapacity: embeddingCapacity
  }
}
output AZURE_RESOURCE_GROUP string = group.name
output AZURE_LOCATION string = location
output AZURE_FOUNDRY_LOCATION string = empty(foundryLocation) ? 'swedencentral' : foundryLocation
output AZURE_CONTAINER_REGISTRY_NAME string = workbench.outputs.registryName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = workbench.outputs.registryEndpoint
output AZURE_CONTAINER_APPS_ENVIRONMENT_ID string = workbench.outputs.environmentId
output AZURE_CONTAINER_APPS_ENVIRONMENT_NAME string = workbench.outputs.environmentName
output AZURE_KEY_VAULT_NAME string = workbench.outputs.vaultName
output AZURE_KEY_VAULT_ID string = workbench.outputs.vaultId
output AZURE_KEY_VAULT_ENDPOINT string = workbench.outputs.vaultUrl
output REGULATORY_WORKBENCH_FOUNDATION_NAME string = workbench.outputs.foundationName
output FOUNDRY_PROJECT_ENDPOINT string = workbench.outputs.projectEndpoint
output AZURE_AI_EMBEDDING_ENDPOINT string = workbench.outputs.embeddingEndpoint
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = workbench.outputs.modelDeployment
output POC_MODEL_VERSION string = workbench.outputs.modelVersion
output AZURE_AI_EMBEDDING_MODEL string = workbench.outputs.embeddingDeployment
output AZURE_AI_SEARCH_ENDPOINT string = workbench.outputs.searchEndpoint
output SERVICE_API_RESOURCE_NAME string = workbench.outputs.apiName
output SERVICE_UI_RESOURCE_NAME string = workbench.outputs.uiName
output REGULATORY_WORKBENCH_ALLOWED_OBJECT_ID string = empty(allowedObjectId) ? principalId : allowedObjectId
output REGULATORY_WORKBENCH_PROFILE string = profile
output REGULATORY_WORKBENCH_AUTH_CLIENT_ID string = workbench.outputs.authClientId
output REGULATORY_WORKBENCH_AUTH_APPLICATION_OBJECT_ID string = workbench.outputs.authApplicationObjectId
output REGULATORY_WORKBENCH_AUTH_SERVICE_PRINCIPAL_OBJECT_ID string = workbench.outputs.authServicePrincipalObjectId
