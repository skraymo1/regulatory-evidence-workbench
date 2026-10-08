using './foundation.bicep'

param environmentName = 'regwork-dev'
param location = 'northeurope'
param profile = 'dev'
param searchSku = 'basic'
param searchReplicas = 1
param searchPartitions = 1
param operatorObjectId = readEnvironmentVariable('REGULATORY_WORKBENCH_OPERATOR_OBJECT_ID')
