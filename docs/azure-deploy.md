# Deploying to Azure (your own subscription first, the company's later)

Nothing in the app is tied to one Azure account. Tenant, client ids, storage account, database and OCR
endpoint are all environment settings, filled in by `infra/main.bicep`. Moving to the company subscription
means running the same template with `infra/prod.bicepparam`, then restoring a backup of the data if any is worth keeping.

**Rule for the personal subscription: synthetic data only** (`seedDemoData = true`). Real AD Ports data goes only
into the company subscription, after IT security has reviewed it.

## What gets created

| Resource | Purpose | Notes |
|---|---|---|
| Container Apps environment + app | runs the API and web UI | 1 vCPU / 2 GiB, 1-2 replicas, HTTPS ingress |
| PostgreSQL Flexible Server 16 | system of record | schema is created on first start; 14-day backups |
| Storage account + `evidence` container | meter photos, uploaded documents | no public access, no shared keys, versioning and 30-day soft delete; app uses its managed identity |
| Document Intelligence (optional) | OCR for meter photos | otherwise the built-in OCR is used |
| Log Analytics workspace | container logs, 90 days | |

## Steps

1. **Sign in and make a resource group**
   ```
   az login
   az group create -n rg-esg-dev -l uaenorth
   ```
2. **Build the image** (a registry is needed once: `az acr create -n <name> -g rg-esg-dev --sku Basic`)
   ```
   az acr build -r <name> -t port-esg:1 .
   ```
3. **Register the app in Entra ID** (portal: Entra ID > App registrations > New registration)
   - Platform: Web. Redirect URI: `https://<app-url>/api/auth/entra/callback` (the deployment prints it; first deploy, add it afterwards).
   - Certificates & secrets: create a client secret.
   - App roles: create `ESG.Admin`, `ESG.Owner`, `ESG.Auditor`, `ESG.Field` (value = name), then assign users or groups under Enterprise applications > Users and groups.
   - Note the tenant id and client id.
4. **Deploy**
   ```
   export ESG_DB_PASSWORD=$(openssl rand -base64 24)
   export ESG_APP_SECRET=$(openssl rand -hex 32)
   az deployment group create -g rg-esg-dev -f infra/main.bicep -p infra/dev.bicepparam \
       -p image=<name>.azurecr.io/port-esg:1 entraTenantId=<tenant> entraClientId=<client> entraClientSecret=<secret>
   ```
   Give the app pull rights and point it at your image:
   ```
   az containerapp registry set -g rg-esg-dev -n <app> --server <name>.azurecr.io --identity system
   az role assignment create --assignee <app principal id> --role AcrPull --scope $(az acr show -n <name> --query id -o tsv)
   az containerapp update -g rg-esg-dev -n <app> --image <name>.azurecr.io/port-esg:1
   ```
5. **Add the redirect URI** printed as `entraRedirectUri` to the app registration, then open `appUrl` and choose *Sign in with Microsoft*.

## Settings reference

| Setting | Meaning |
|---|---|
| `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET` | enable Microsoft sign-in; leave tenant blank for password-only |
| `ENTRA_ROLE_MAP` | `AppRoleOrGroupId=esg_role,...`. When set, Entra decides each user's role at every sign-in |
| `ENTRA_AUTO_PROVISION` | `true` creates users on first sign-in (needs a mapped role); default `false` = an admin adds the user first |
| `ENTRA_DEFAULT_ROLE` | role for signed-in users with no mapped role (blank = refuse) |
| `ENTRA_ALLOWED_DOMAINS` | comma list of accepted email domains |
| `LOCAL_LOGIN_ENABLED` | `false` hides the password form and refuses `/api/auth/login`. Keep one break-glass admin until SSO is proven |
| `STORAGE_BACKEND` | `local` or `azure` |
| `AZURE_STORAGE_ACCOUNT_URL` / `AZURE_STORAGE_CONNECTION_STRING` / `AZURE_STORAGE_CONTAINER` | blob target. Connection string works for Azurite and trials; the account URL uses managed identity |
| `AZURE_DI_ENDPOINT`, `AZURE_DI_KEY` | OCR with Document Intelligence |

MFA, conditional access, device compliance and sign-in logs are Entra features: they apply to the app once its
enterprise application is covered by the company's policies. Tenant-portal users keep local accounts by design.

## Moving to the company subscription

1. IT creates the resource group, the Entra app registration and the app roles.
2. Fill `infra/prod.bicepparam` (tenant, client, role map, domains, admin email) and set the four secrets as environment variables.
3. Run the same `az deployment group create` against the company subscription.
4. Optional data move: `pg_dump` the esg schema from the old server and `psql` it into the new one; copy the `evidence` container with `azcopy`.
   Evidence rows store `az://evidence/<file>`, so they work unchanged against the new account.
5. Turn `LOCAL_LOGIN_ENABLED` off once SSO sign-in is confirmed for admins.

## What this does not yet include

Private endpoints and VNet integration, Key Vault references for secrets, custom domain and certificate, geo-redundant
database backups and high availability. These are IT-security decisions to make with the company's landing-zone standards.
The template uses Container Apps secrets, which is acceptable for a trial and should be reviewed for production.
