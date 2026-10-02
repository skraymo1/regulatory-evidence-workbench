using './applications.bicep'

param foundationName = readEnvironmentVariable('REGULATORY_WORKBENCH_FOUNDATION_NAME')
param location = 'swedencentral'
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
param authClientId = readEnvironmentVariable('REGULATORY_WORKBENCH_AUTH_CLIENT_ID')
param allowedObjectId = readEnvironmentVariable('REGULATORY_WORKBENCH_ALLOWED_OBJECT_ID')
param authClientSecret = readEnvironmentVariable('REGULATORY_WORKBENCH_AUTH_CLIENT_SECRET')
