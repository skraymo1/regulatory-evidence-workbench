using './foundation.bicep'

param environmentName = 'regwork-prod'
param location = 'northeurope'
param profile = 'prod'
param searchSku = 'basic'
// Query availability for a static corpus; use 3 when query + indexing SLA is required.
param searchReplicas = 2
param searchPartitions = 1
param operatorObjectId = readEnvironmentVariable('REGULATORY_WORKBENCH_OPERATOR_OBJECT_ID')
