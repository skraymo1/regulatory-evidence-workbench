targetScope = 'resourceGroup'

param environmentName string
param location string
param profile string
param operatorObjectId string
param foundryLocation string
param modelName string
param modelVersion string
param modelCapacity int
param embeddingCapacity int
param authEnabled bool = true

module foundation './foundation.bicep' = {
  name: 'foundation'
  params: {
    environmentName: environmentName
    location: location
    profile: profile
    operatorObjectId: operatorObjectId
    foundryLocation: foundryLocation
    modelName: modelName
    modelVersion: modelVersion
    modelCapacity: modelCapacity
    embeddingCapacity: embeddingCapacity
  }
}
module authentication './modules/authentication.bicep' = if (authEnabled) {
  name: 'authentication'
  params: {
    environmentName: environmentName
    operatorObjectId: operatorObjectId
    applicationNames: foundation.outputs.applicationNames
    environmentDefaultDomain: foundation.outputs.environmentDefaultDomain
  }
}
output foundationName string = foundation.outputs.foundationName
output registryName string = foundation.outputs.registryName
output registryEndpoint string = foundation.outputs.registryEndpoint
output environmentId string = foundation.outputs.environmentId
output environmentName string = foundation.outputs.environmentName
output vaultName string = foundation.outputs.vaultName
output vaultId string = foundation.outputs.vaultId
output vaultUrl string = foundation.outputs.vaultUrl
output projectEndpoint string = foundation.outputs.projectEndpoint
output embeddingEndpoint string = foundation.outputs.embeddingEndpoint
output modelDeployment string = foundation.outputs.modelDeployment
output modelVersion string = foundation.outputs.modelVersion
output embeddingDeployment string = foundation.outputs.embeddingDeployment
output searchEndpoint string = foundation.outputs.searchEndpoint
output apiName string = foundation.outputs.applicationNames[0]
output uiName string = foundation.outputs.applicationNames[1]
output authClientId string = authEnabled ? authentication!.outputs.clientId : ''
output authApplicationObjectId string = authEnabled ? authentication!.outputs.applicationObjectId : ''
output authServicePrincipalObjectId string = authEnabled ? authentication!.outputs.servicePrincipalObjectId : ''
