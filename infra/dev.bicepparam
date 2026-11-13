using './main.bicep'

// Trial / personal subscription. Synthetic data only.
param prefix = 'esgdev'
param image = 'ghcr.io/indranil32/port-esg:latest'
param location = 'centralindia'      // Container Apps and storage (trial allows one environment)
param dbLocation = 'uksouth'          // PostgreSQL is restricted in some trial regions
param demoPassword = readEnvironmentVariable('ESG_DEMO_PASSWORD', '')
param seedDemoData = true
param deployDocumentIntelligence = false   // built-in OCR; avoids a pay-per-use service on the trial
param storageSku = 'Standard_LRS'
param maxReplicas = 1
param entraTenantId = ''              // fill in once the app registration exists
param entraClientId = ''
param entraRoleMap = 'ESG.Admin=esg_admin,ESG.Owner=data_owner,ESG.Auditor=auditor,ESG.Field=field_operator'
param entraAllowedDomains = ''
// Secrets are passed on the command line, never stored here:
//   -p dbAdminPassword=$(openssl rand -base64 24) appSecret=$(openssl rand -hex 32)
param dbAdminPassword = readEnvironmentVariable('ESG_DB_PASSWORD', '')
param appSecret = readEnvironmentVariable('ESG_APP_SECRET', '')
