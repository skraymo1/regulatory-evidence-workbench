param name string
@allowed(['api', 'ui'])
param service string
param location string
@allowed(['dev', 'prod'])
param profile string
param environmentId string
param registryServer string
param identityId string
param identityClientId string
param image string
param port int
param variables object
param tenantId string
param authClientId string
param allowedObjectId string
@secure()
param authClientSecret string

resource app 'Microsoft.App/containerApps@2025-07-01' = {
  name: name
  location: location
  tags: { purpose: 'regulatory-evidence-workbench', profile: profile }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: [{ name: 'entra-client-secret', value: authClientSecret }]
      registries: [{ server: registryServer, identity: identityId }]
      ingress: {
        external: true
        targetPort: port
        transport: 'auto'
        allowInsecure: false
        stickySessions: { affinity: 'sticky' }
      }
    }
    template: {
      containers: [{
        name: service
        image: image
        env: [for item in items(union(variables, { AZURE_CLIENT_ID: identityClientId })): {
          name: item.key
          value: item.value
        }]
        resources: { cpu: json('0.5'), memory: '1Gi' }
        probes: [
          { type: 'Startup', tcpSocket: { port: port }, initialDelaySeconds: 5, periodSeconds: 5, failureThreshold: 60 }
          { type: 'Readiness', tcpSocket: { port: port }, periodSeconds: 10, failureThreshold: 3 }
          { type: 'Liveness', tcpSocket: { port: port }, initialDelaySeconds: 30, periodSeconds: 30, failureThreshold: 3 }
        ]
      }]
      scale: {
        minReplicas: profile == 'prod' ? 2 : 0
        maxReplicas: profile == 'prod' ? 2 : 1
        rules: [{ name: 'http', http: { metadata: { concurrentRequests: '10' } } }]
      }
    }
  }
}
resource auth 'Microsoft.App/containerApps/authConfigs@2025-07-01' = {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: service == 'ui' ? 'RedirectToLoginPage' : 'Return401'
      redirectToProvider: 'azureActiveDirectory'
    }
    httpSettings: { requireHttps: true }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: authClientId
          clientSecretSettingName: 'entra-client-secret'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [authClientId, 'api://${authClientId}']
          defaultAuthorizationPolicy: { allowedPrincipals: { identities: [allowedObjectId] } }
        }
      }
    }
    login: {
      cookieExpiration: { convention: 'FixedTime', timeToExpiration: '01:00:00' }
      tokenStore: { enabled: false }
    }
  }
}
output url string = 'https://${app.properties.configuration.ingress.fqdn}'
