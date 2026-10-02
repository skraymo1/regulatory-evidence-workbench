using './foundation.bicep'

param environmentName = 'regwork-dev'
param location = 'swedencentral'
param profile = 'dev'
param searchSku = 'basic'
param searchReplicas = 1
param searchPartitions = 1
param operatorObjectId = readEnvironmentVariable('REGULATORY_WORKBENCH_OPERATOR_OBJECT_ID')
param foundryResourceGroup = readEnvironmentVariable('REGULATORY_WORKBENCH_FOUNDRY_RESOURCE_GROUP')
param foundryAccountName = readEnvironmentVariable('REGULATORY_WORKBENCH_FOUNDRY_ACCOUNT_NAME')
