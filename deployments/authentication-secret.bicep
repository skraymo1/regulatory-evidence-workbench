param vaultName string
param secretName string
param clientId string
param expires int

@secure()
@minLength(1)
param authClientSecret string

resource vault 'Microsoft.KeyVault/vaults@2025-05-01' existing = {
  name: vaultName
}

resource secret 'Microsoft.KeyVault/vaults/secrets@2025-05-01' = {
  parent: vault
  name: secretName
  tags: { appId: clientId }
  properties: {
    value: authClientSecret
    attributes: {
      enabled: true
      exp: expires
    }
  }
}
