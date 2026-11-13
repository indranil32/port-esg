# Moving from the personal trial to the company Azure subscription

Everything is infrastructure-as-code or a script, so this is a re-run in a new subscription, not a rebuild.

## 1. Decisions and approvals first
- Subscription, resource group name, and region. Trial limits no longer apply, so use the company's approved region (for example UAE North) for the app, database and storage together. This also removes the slow cross-region database link.
- Who may create app registrations in the company Entra tenant (needed for SSO and for the GitHub deploy identity). If you cannot, ask the identity team to run the two scripts below.
- Data residency, network rules (private endpoints / VNet), and a naming standard.

## 2. Deploy the infrastructure
1. Edit `infra/prod.bicepparam` (or copy `dev.bicepparam`): set `location` and `dbLocation` to the same company region, `prefix`, and the image.
2. Raise sizes: `dbSku`/`dbTier` (General Purpose instead of B1ms), `maxReplicas` (2+), `storageSku` (for example `Standard_ZRS`). Enable DB high availability and backups retention as per policy.
3. Provide secrets as environment variables, never in files: `ESG_DB_PASSWORD`, `ESG_APP_SECRET`, `ESG_DEMO_PASSWORD` (or set `seedDemoData=false` and `LOCAL_LOGIN_ENABLED=false` once SSO works).
4. `az group create ...` then `az deployment group create -g <rg> -f infra/main.bicep -p infra/prod.bicepparam`.
5. Optional: set `deployDocumentIntelligence=true` for better OCR on meter photos.

## 3. Image registry
The image is currently `ghcr.io/indranil32/port-esg` (a personal GitHub account). Choose one:
- Move the repository into the company GitHub organisation (recommended; ghcr then follows the org), or
- Use Azure Container Registry: create an ACR, change the push step in `.github/workflows/ci-cd.yml` and the `image` parameter, and grant the Container App `AcrPull` through its managed identity.
Make sure the Container App can pull (public ghcr image needs nothing; a private one needs a registry credential or ACR).

## 4. Microsoft Entra sign-in (SSO)
Run `infra/entra-setup.sh <rg> <containerapp>` in the company tenant. It creates the app registration, roles (ESG.Admin etc.), secret and env vars. Then:
- Add the company app URL as the redirect URI (the script derives it from the app FQDN; if you add a custom domain, set `ENTRA_REDIRECT_URI`).
- Assign users or groups to the app roles; set `ENTRA_ALLOWED_DOMAINS` to the company domain and `ENTRA_ROLE_MAP` as needed.
- Prefer folding these values into the Bicep parameters (`entraTenantId`, `entraClientId`, secret in Key Vault) so redeploys do not drop them. Today they exist only on the running trial app.
- Consider a client certificate or Key Vault reference instead of a client secret; secrets expire.

## 5. GitHub to Azure deploy login
1. Move or fork the repo to the company GitHub organisation; recreate the `azure-dev` environment (add a `azure-prod` environment with required reviewers for production).
2. Run `infra/github-oidc-setup.sh <rg> <org/repo> <environment>` with the company subscription selected. It creates the identity, a federated credential and Contributor on that one resource group.
3. If login fails with a subject mismatch, copy the subject printed in the error (GitHub orgs can use immutable ID-based subjects) and add a federated credential with it.
4. Set repository variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`.
5. Delete the old `esg-github-deploy` identity in the personal tenant.

## 3b. Data
- Database: `pg_dump` from the trial server and `pg_restore` into the new one (or start clean). Trial data is synthetic test data, so a clean start is normal.
- Evidence and original files: copy the `evidence` blob container with `azcopy copy`. File URIs are stored as `az://container/blob`, so keep the container name `evidence`.
- Run the smoke suite afterwards (`BASE_URL=... DEMO_PASSWORD=... pytest`). Use `SMOKE_MODE=readonly` against anything holding real data.

## 6. Security hardening for production
- Private networking: PostgreSQL private endpoint or VNet integration, and remove the `AllowAzureServices` firewall rule in `infra/main.bicep`; storage with private endpoint and public access disabled.
- Secrets in Key Vault with Container App references; rotate the app secret and DB password.
- Blob immutability (WORM) policy on the evidence container for audit retention; soft delete enabled.
- Custom domain and managed certificate; WAF/Front Door if required; log retention and alerts on Log Analytics.
- Turn off demo login and demo data seeding; review `ALERT_WEBHOOK_URL`.
- Branch protection on `main` and required reviews on production deploys.

## 7. Cost and cleanup
Trial credits expire; after cutover delete `rg-esg-dev-in` and the personal Entra app registrations (`esg-github-deploy`, the ESG SSO app).
