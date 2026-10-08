param accountName string
param appPrincipals array
param searchPrincipal string
param projectPrincipal string

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = { name: accountName }
resource appRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in appPrincipals: {
  name: guid(account.id, principal, 'regulatory-workbench-foundry-user')
  scope: account
  properties: {
    principalId: principal
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  }
}]
resource modelRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in concat(appPrincipals, [searchPrincipal, projectPrincipal]): {
  name: guid(account.id, principal, 'regulatory-workbench-openai-user')
  scope: account
  properties: {
    principalId: principal
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}]
