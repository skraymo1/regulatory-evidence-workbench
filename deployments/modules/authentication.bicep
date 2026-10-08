extension microsoftGraphV1

param environmentName string
param operatorObjectId string
param applicationNames array
param environmentDefaultDomain string

resource application 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: 'regulatory-workbench-${environmentName}-${uniqueString(subscription().id, resourceGroup().name)}'
  displayName: 'Regulatory Evidence Workbench (${environmentName})'
  signInAudience: 'AzureADMyOrg'
  owners: {
    relationships: [operatorObjectId]
    relationshipSemantics: 'append'
  }
  web: {
    redirectUris: [for name in applicationNames: 'https://${name}.${environmentDefaultDomain}/.auth/login/aad/callback']
    implicitGrantSettings: {
      enableIdTokenIssuance: true
      enableAccessTokenIssuance: false
    }
  }
}

resource servicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: application.appId
  accountEnabled: true
  owners: {
    relationships: [operatorObjectId]
    relationshipSemantics: 'append'
  }
}

output clientId string = application.appId
output applicationObjectId string = application.id
output servicePrincipalObjectId string = servicePrincipal.id
