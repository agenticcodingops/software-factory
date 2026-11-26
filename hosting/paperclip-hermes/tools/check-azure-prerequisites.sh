#!/usr/bin/env bash
# Read-only deployment gate; Owner bootstrap performs registrations.
set -euo pipefail
subscription=${1:?usage: check-azure-prerequisites.sh SUBSCRIPTION_ID}
for namespace in Microsoft.Compute Microsoft.Network Microsoft.Logic Microsoft.Insights Microsoft.Consumption Microsoft.DevTestLab Microsoft.ManagedIdentity; do
  state=$(az provider show --subscription "$subscription" --namespace "$namespace" --query registrationState -o tsv)
  if [[ $state != Registered ]]; then
    echo "Owner bootstrap required: register $namespace in the target subscription (state: $state)." >&2
    exit 1
  fi
done
feature=$(az feature show --subscription "$subscription" --namespace Microsoft.Compute --name EncryptionAtHost --query properties.state -o tsv)
if [[ $feature != Registered ]]; then
  echo 'Owner bootstrap required: register Microsoft.Compute/EncryptionAtHost, wait for Registered, then re-register Microsoft.Compute.' >&2
  exit 1
fi
