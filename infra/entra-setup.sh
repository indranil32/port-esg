#!/usr/bin/env bash
# Create the Entra app registration for the ESG app and switch Microsoft sign-in on.
# Run in Azure Cloud Shell (or any shell logged in with `az login`) as a user who may register apps.
# Usage: infra/entra-setup.sh <resource-group> <container-app-name> [display-name]
# The client secret is kept in a shell variable and a Container Apps secret; it is never printed.
set -euo pipefail
RG=${1:?resource group}; APP=${2:?container app name}; NAME=${3:-ESG System of Record}
FQDN=$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)
REDIRECT="https://$FQDN/api/auth/entra/callback"
TENANT=$(az account show --query tenantId -o tsv)
role() { printf '{"id":"%s","displayName":"%s","description":"%s","value":"%s","allowedMemberTypes":["User"],"isEnabled":true}' "$(cat /proc/sys/kernel/random/uuid)" "$1" "$2" "$3"; }
ADMIN_ID=$(cat /proc/sys/kernel/random/uuid)
ROLES="[{\"id\":\"$ADMIN_ID\",\"displayName\":\"ESG Admin\",\"description\":\"Full access\",\"value\":\"ESG.Admin\",\"allowedMemberTypes\":[\"User\"],\"isEnabled\":true},$(role 'ESG Data Owner' 'Enters and reviews data' ESG.Owner),$(role 'ESG Auditor' 'Read-only audit access' ESG.Auditor),$(role 'ESG Field Operator' 'Captures field readings' ESG.Field)]"
echo "$ROLES" > /tmp/esg_roles.json
CLIENT=$(az ad app create --display-name "$NAME" --sign-in-audience AzureADMyOrg \
  --web-redirect-uris "$REDIRECT" --enable-id-token-issuance true --app-roles @/tmp/esg_roles.json --query appId -o tsv)
az ad sp create --id "$CLIENT" -o none
SECRET=$(az ad app credential reset --id "$CLIENT" --display-name esg-app --years 1 --query password -o tsv)
az containerapp secret set -g "$RG" -n "$APP" --secrets entra-secret="$SECRET" -o none
az containerapp update -g "$RG" -n "$APP" -o none --set-env-vars \
  ENTRA_TENANT_ID="$TENANT" ENTRA_CLIENT_ID="$CLIENT" ENTRA_CLIENT_SECRET=secretref:entra-secret \
  ENTRA_ROLE_MAP="ESG.Admin=esg_admin,ESG.Owner=data_owner,ESG.Auditor=auditor,ESG.Field=field_operator" \
  ENTRA_AUTO_PROVISION=true
# Give the signed-in user the Admin role so they can sign in straight away.
SP=$(az ad sp show --id "$CLIENT" --query id -o tsv); ME=$(az ad signed-in-user show --query id -o tsv)
az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$SP/appRoleAssignedTo" \
  --body "{\"principalId\":\"$ME\",\"resourceId\":\"$SP\",\"appRoleId\":\"$ADMIN_ID\"}" -o none
echo "Done. tenant=$TENANT client=$CLIENT redirect=$REDIRECT"
