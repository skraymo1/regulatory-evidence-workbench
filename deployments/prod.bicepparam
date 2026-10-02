using './foundation.bicep'

param environmentName = 'regwork-prod'
param location = 'swedencentral'
param profile = 'prod'
param searchSku = 'basic'
// Query availability for a static corpus; use 3 when query + indexing SLA is required.
param searchReplicas = 2
param searchPartitions = 1
param operatorObjectId = readEnvironmentVariable('REGULATORY_WORKBENCH_OPERATOR_OBJECT_ID')
param foundryResourceGroup = readEnvironmentVariable('REGULATORY_WORKBENCH_FOUNDRY_RESOURCE_GROUP')
param foundryAccountName = readEnvironmentVariable('REGULATORY_WORKBENCH_FOUNDRY_ACCOUNT_NAME')
