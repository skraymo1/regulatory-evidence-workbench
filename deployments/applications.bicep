targetScope = 'resourceGroup'

@description('Exact foundationName output from the foundation deployment in this resource group.')
param foundationName string
@description('Actual environmentName output from the foundation deployment.')
param containerAppsEnvironmentName string = 'cae-net-${foundationName}'
param location string = 'northeurope'
@allowed(['dev', 'prod'])
param profile string = 'dev'
@description('Immutable image reference already pushed to the foundation registry. No placeholder is deployed.')
@minLength(1)
param apiImage string
@minLength(1)
param uiImage string
param projectEndpoint string
param embeddingEndpoint string
param embeddingDeployment string
param modelDeployment string
param modelVersion string
param agentVersion string
param chatAgentVersion string
param tenantId string
param authEnabled bool = true
param authClientId string
@description('Object ID of the user allowed to access the application.')
param allowedObjectId string
@secure()
param authClientSecret string

resource environment 'Microsoft.App/managedEnvironments@2025-07-01' existing = {
  name: containerAppsEnvironmentName
}
resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' existing = {
  name: replace('cr${foundationName}', '-', '')
}
resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' existing = {
  name: replace('st${foundationName}', '-', '')
}
resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: 'srch-${foundationName}'
}
var services = [{ name: 'api', port: 8000, image: apiImage }, { name: 'ui', port: 8501, image: uiImage }]
resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = [for service in services: {
  name: 'id-${service.name}-${foundationName}'
}]
var variables = {
  POC_AGENT_MODE: 'foundry'
  POC_SEARCH_MODE: 'azure'
  FOUNDRY_PROJECT_ENDPOINT: projectEndpoint
  AZURE_AI_MODEL_DEPLOYMENT_NAME: modelDeployment
  POC_MODEL_VERSION: modelVersion
  AZURE_AI_SEARCH_ENDPOINT: 'https://${search.name}.search.windows.net'
  AZURE_AI_SEARCH_INDEX: 'fidelity-sources'
  AZURE_AI_SEARCH_SEMANTIC_CONFIGURATION: 'regulatory-semantic'
  AZURE_AI_EMBEDDING_ENDPOINT: embeddingEndpoint
  AZURE_AI_EMBEDDING_MODEL: embeddingDeployment
  POC_BLOB_ACCOUNT_URL: storage.properties.primaryEndpoints.blob
  POC_BLOB_CONTAINER: 'approved-documents'
  POC_REPORT_CONTAINER: 'comparison-reports'
  POC_STATE_CONTAINER: 'poc-state'
  POC_SOURCE_KB: 'fidelity-sources-kb'
  POC_REPORT_INDEX: 'fidelity-reports'
  POC_REPORT_KB: 'fidelity-reports-kb'
  POC_AGENT_NAME: 'security-requirements-analysis'
  POC_AGENT_VERSION: agentVersion
  POC_CHAT_AGENT_NAME: 'fidelity-evidence-chat'
  POC_CHAT_AGENT_VERSION: chatAgentVersion
  POC_AUTH_ENABLED: string(authEnabled)
  POC_ALLOWED_OID: authEnabled ? allowedObjectId : ''
  AZURE_TENANT_ID: tenantId
}
module apps './modules/application.bicep' = [for (service, i) in services: {
  name: 'regulatory-workbench-${service.name}'
  params: {
    name: 'ca-${service.name}-${foundationName}'
    service: service.name
    location: location
    profile: profile
    environmentId: environment.id
    registryServer: registry.properties.loginServer
    identityId: identities[i].id
    identityClientId: identities[i].properties.clientId
    image: service.image
    port: service.port
    variables: variables
    tenantId: tenantId
    authClientId: authClientId
    authClientSecret: authClientSecret
    allowedObjectId: allowedObjectId
    authEnabled: authEnabled
  }
}]
output apiUrl string = apps[0].outputs.url
output uiUrl string = apps[1].outputs.url
