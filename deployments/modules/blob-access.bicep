param storageAccountName string
param principal string
resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' existing = { name: storageAccountName }
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' existing = {
  parent: storage
  name: 'default'
}
var names = ['approved-documents', 'comparison-reports', 'poc-state']
resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' existing = [for name in names: {
  parent: blobs
  name: name
}]
resource roles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (name, i) in names: {
  scope: containers[i]
  name: guid(containers[i].id, principal, 'blob-data')
  properties: {
    principalId: principal
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}]
