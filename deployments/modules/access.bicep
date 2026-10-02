param acrName string
param searchName string
param storageAccountName string
param principals array
resource acr 'Microsoft.ContainerRegistry/registries@2025-04-01' existing = { name: acrName }
resource search 'Microsoft.Search/searchServices@2025-05-01' existing = { name: searchName }
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in principals: {
  name: guid(acr.id, principal, 'acrpull')
  scope: acr
  properties: {
    principalId: principal
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}]
resource searchData 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in principals: {
  name: guid(search.id, principal, 'search-data')
  scope: search
  properties: {
    principalId: principal
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8ebe5a00-799e-43f5-93ac-243d3dce84a7')
  }
}]
module blobRoles './blob-access.bicep' = [for (principal, i) in principals: {
  name: 'blob-access-${i}'
  params: { storageAccountName: storageAccountName, principal: principal }
}]
