using './main.bicep'

// Company subscription. Real data. Review every value with IT security before deploying.
param prefix = 'adpesg'
param seedDemoData = false
param deployDocumentIntelligence = true
param dbSku = 'Standard_D2ds_v5'
param dbTier = 'GeneralPurpose'
param entraTenantId = ''              // AD Ports Entra tenant id
param entraClientId = ''              // app registration created by the AD Ports IT team
param entraRoleMap = ''               // their app-role or group ids -> esg_admin / data_owner / auditor / field_operator
param entraAllowedDomains = ''        // e.g. adports.ae
param adminEmail = ''                 // break-glass local admin; set LOCAL_LOGIN_ENABLED later once SSO is proven
param dbAdminPassword = readEnvironmentVariable('ESG_DB_PASSWORD', '')
param appSecret = readEnvironmentVariable('ESG_APP_SECRET', '')
param adminPassword = readEnvironmentVariable('ESG_ADMIN_PASSWORD', '')
param entraClientSecret = readEnvironmentVariable('ESG_ENTRA_SECRET', '')
