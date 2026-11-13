// ESG System of Record - Azure deployment.
// Same template for a personal trial subscription and the company subscription: only the parameters differ.
//   az group create -n rg-esg-dev -l uaenorth
//   az deployment group create -g rg-esg-dev -f infra/main.bicep -p infra/dev.bicepparam
targetScope = 'resourceGroup'

@description('Short name used to build resource names (letters/numbers).')
@minLength(3)
@maxLength(12)
param prefix string = 'esg'

param location string = resourceGroup().location

@description('Region for PostgreSQL. Trial subscriptions often cannot create it in every region, so it can differ from the app region.')
param dbLocation string = location

@description('Container image to run, e.g. myregistry.azurecr.io/port-esg:1.0. Leave the default for a first deploy, then update.')
param image string = 'mcr.microsoft.com/k8se/quickstart:latest'

@secure()
@description('PostgreSQL administrator password.')
param dbAdminPassword string

@secure()
@description('Long random string used to sign session tokens.')
param appSecret string

@description('Entra ID tenant of the users who sign in. Blank turns SSO off (password sign-in only).')
param entraTenantId string = ''
param entraClientId string = ''
@secure()
param entraClientSecret string = ''
@description('Entra app roles to ESG roles, e.g. AdPorts.Admin=esg_admin,AdPorts.Owner=data_owner')
param entraRoleMap string = ''
param entraAllowedDomains string = ''
@description('Exact redirect URI registered on the Entra app, e.g. https://<app-fqdn>/api/auth/entra/callback. Needed because the app sits behind a proxy.')
param entraRedirectUri string = ''
param entraDefaultRole string = ''
param entraAutoProvision bool = false
@description('Keep password sign-in available next to SSO (demo and break-glass). Set false for production.')
param localLoginEnabled bool = true

@description('true loads synthetic demo data on first start. Must be false for real data.')
param seedDemoData bool = false
@secure()
@description('Password for the seeded demo users. Set this when seedDemoData is true, otherwise the published default applies.')
param demoPassword string = ''
param adminEmail string = ''
@secure()
param adminPassword string = ''

@description('Deploy Azure AI Document Intelligence for meter-photo OCR.')
param deployDocumentIntelligence bool = true

@description('PostgreSQL SKU. Burstable B1ms is enough for a trial.')
param dbSku string = 'Standard_B1ms'

@description('Storage redundancy. Standard_LRS is cheapest; use Standard_GRS for production.')
param storageSku string = 'Standard_GRS'

@description('Maximum container replicas. 1 keeps a trial cheap and predictable.')
param maxReplicas int = 2
param dbTier string = 'Burstable'

var uniq = uniqueString(resourceGroup().id)
var base = toLower('${prefix}${uniq}')

// ---------------------------------------------------------------- monitoring
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}-${uniq}'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 90 }
}

// ---------------------------------------------------------------- storage for evidence files
resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take('st${base}', 24)
  location: location
  sku: { name: storageSku }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false      // managed identity only
    supportsHttpsTrafficOnly: true
  }
}
resource blobSvc 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: sa
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 30 }
    containerDeleteRetentionPolicy: { enabled: true, days: 30 }
    isVersioningEnabled: true         // evidence is never silently overwritten
  }
}
resource evidence 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobSvc
  name: 'evidence'
  properties: { publicAccess: 'None' }
}

// ---------------------------------------------------------------- database
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: 'psql-${prefix}-${uniq}-${dbLocation}'
  location: dbLocation
  sku: { name: dbSku, tier: dbTier }
  properties: {
    version: '16'
    administratorLogin: 'esgadmin'
    administratorLoginPassword: dbAdminPassword
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 14, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
  }
}
resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: pg
  name: 'esg'
}
// Lets Azure-hosted services (the container app) connect. Tighten with a private endpoint for production.
resource pgAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: pg
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

// ---------------------------------------------------------------- OCR
resource di 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (deployDocumentIntelligence) {
  name: 'di-${prefix}-${uniq}'
  location: location
  kind: 'FormRecognizer'
  sku: { name: 'S0' }
  properties: { customSubDomainName: 'di-${prefix}-${uniq}', publicNetworkAccess: 'Enabled' }
}

// ---------------------------------------------------------------- app
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${prefix}-${uniq}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

var dbUrl = 'postgresql://esgadmin:${uriComponent(dbAdminPassword)}@${pg.properties.fullyQualifiedDomainName}:5432/esg?sslmode=require'

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-${prefix}-${uniq}'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: true, targetPort: 8000, transport: 'auto' }
      secrets: concat([
        { name: 'database-url', value: dbUrl }
        { name: 'app-secret', value: appSecret }
      ], deployDocumentIntelligence ? [
        { name: 'di-key', value: di!.listKeys().key1 }
      ] : [], entraClientSecret != '' ? [
        { name: 'entra-secret', value: entraClientSecret }
      ] : [], adminPassword != '' ? [
        { name: 'admin-password', value: adminPassword }
      ] : [], demoPassword != '' ? [
        { name: 'demo-password', value: demoPassword }
      ] : [])
    }
    template: {
      containers: [
        {
          name: 'esg'
          image: image
          resources: { cpu: json('1.0'), memory: '2Gi' }       // OCR model needs the memory
          env: concat([
            { name: 'DATABASE_URL', secretRef: 'database-url' }
            { name: 'APP_SECRET', secretRef: 'app-secret' }
            { name: 'SEED_DEMO_DATA', value: string(seedDemoData) }
            { name: 'ADMIN_EMAIL', value: adminEmail }
            { name: 'STORAGE_BACKEND', value: 'azure' }
            { name: 'AZURE_STORAGE_ACCOUNT_URL', value: sa.properties.primaryEndpoints.blob }
            { name: 'AZURE_STORAGE_CONTAINER', value: evidence.name }
            { name: 'ENTRA_TENANT_ID', value: entraTenantId }
            { name: 'ENTRA_CLIENT_ID', value: entraClientId }
            { name: 'ENTRA_ROLE_MAP', value: entraRoleMap }
            { name: 'ENTRA_ALLOWED_DOMAINS', value: entraAllowedDomains }
            { name: 'ENTRA_REDIRECT_URI', value: entraRedirectUri }
            { name: 'ENTRA_DEFAULT_ROLE', value: entraDefaultRole }
            { name: 'ENTRA_AUTO_PROVISION', value: string(entraAutoProvision) }
            { name: 'LOCAL_LOGIN_ENABLED', value: string(entraTenantId == '' || localLoginEnabled) }
          ], deployDocumentIntelligence ? [
            { name: 'AZURE_DI_ENDPOINT', value: di!.properties.endpoint }
            { name: 'AZURE_DI_KEY', secretRef: 'di-key' }
          ] : [], entraClientSecret != '' ? [
            { name: 'ENTRA_CLIENT_SECRET', secretRef: 'entra-secret' }
          ] : [], adminPassword != '' ? [
            { name: 'ADMIN_PASSWORD', secretRef: 'admin-password' }
          ] : [], demoPassword != '' ? [
            { name: 'DEMO_PASSWORD', secretRef: 'demo-password' }
          ] : [])
          probes: [
            { type: 'Readiness', httpGet: { path: '/api/health', port: 8000 }, initialDelaySeconds: 10 }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: maxReplicas }
    }
  }
}

// App's managed identity -> Storage Blob Data Contributor on the account
resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: sa
  name: guid(sa.id, app.id, 'blob-contributor')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output entraRedirectUri string = 'https://${app.properties.configuration.ingress.fqdn}/api/auth/entra/callback'
output storageAccount string = sa.name
output postgresServer string = pg.properties.fullyQualifiedDomainName
