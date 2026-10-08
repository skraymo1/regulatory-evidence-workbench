using './applications.bicep'

var authenticationEnabled = json(toLower(trim(readEnvironmentVariable('AUTH_ENABLED', 'true'))))
param authEnabled = authenticationEnabled
param foundationName = readEnvironmentVariable('REGULATORY_WORKBENCH_FOUNDATION_NAME')
param containerAppsEnvironmentName = readEnvironmentVariable('AZURE_CONTAINER_APPS_ENVIRONMENT_NAME')
param location = 'northeurope'
param profile = readEnvironmentVariable('REGULATORY_WORKBENCH_PROFILE', 'dev')
param apiImage = readEnvironmentVariable('REGULATORY_WORKBENCH_API_IMAGE')
param uiImage = readEnvironmentVariable('REGULATORY_WORKBENCH_UI_IMAGE')
param projectEndpoint = readEnvironmentVariable('REGULATORY_WORKBENCH_PROJECT_ENDPOINT')
param embeddingEndpoint = readEnvironmentVariable('REGULATORY_WORKBENCH_EMBEDDING_ENDPOINT')
param embeddingDeployment = readEnvironmentVariable('AZURE_AI_EMBEDDING_MODEL')
param modelDeployment = readEnvironmentVariable('AZURE_AI_MODEL_DEPLOYMENT_NAME')
param modelVersion = readEnvironmentVariable('POC_MODEL_VERSION')
param agentVersion = readEnvironmentVariable('POC_AGENT_VERSION')
param chatAgentVersion = readEnvironmentVariable('POC_CHAT_AGENT_VERSION')
param tenantId = readEnvironmentVariable('REGULATORY_WORKBENCH_TENANT_ID')
// Environment reads are evaluated even in an unused branch; read the explicit mode instead when disabled.
param authClientId = authenticationEnabled ? readEnvironmentVariable(authenticationEnabled ? 'REGULATORY_WORKBENCH_AUTH_CLIENT_ID' : 'AUTH_ENABLED') : ''
param allowedObjectId = authenticationEnabled ? readEnvironmentVariable(authenticationEnabled ? 'REGULATORY_WORKBENCH_ALLOWED_OBJECT_ID' : 'AUTH_ENABLED') : ''
param authClientSecret = authenticationEnabled ? readEnvironmentVariable(authenticationEnabled ? 'REGULATORY_WORKBENCH_AUTH_CLIENT_SECRET' : 'AUTH_ENABLED') : ''
