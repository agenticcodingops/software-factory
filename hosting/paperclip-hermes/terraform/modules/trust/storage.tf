module "backups" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  source                          = "Azure/avm-res-storage-storageaccount/azurerm"
  version                         = "0.11.0"
  name                            = "${var.prefix}backups"
  parent_id                       = azurerm_resource_group.vault.id
  location                        = var.location
  account_replication_type        = "GRS"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  blob_properties = {
    versioning_enabled                = true
    delete_retention_policy           = { days = 7, enabled = true }
    container_delete_retention_policy = { days = 7, enabled = true }
  }
  immutability_policy = {
    allow_protected_append_writes = false
    period_since_creation_in_days = 30
    state                         = timecmp(var.pilot_end_date, plantimestamp()) > 0 ? "Unlocked" : "Locked"
  }
  containers = {
    backups = { name = "backups", immutable_storage_with_versioning = { enabled = true } }
    # Retain the old container so upgrading does not try to delete locked staging blobs.
    # All restore/operator grants now point to module.staging; no new uploads go here.
    staging = { name = "restore-staging" }
  }
  storage_management_policy_rule = {
    backups = {
      name    = "expire-backups"
      enabled = true
      filters = { blob_types = ["blockBlob"], prefix_match = ["backups/backups/"] }
      actions = {
        base_blob = { delete_after_days_since_modification_greater_than = 45 }
        version   = { delete_after_days_since_creation = 45 }
      }
    }
    staging = {
      name    = "expire-staging"
      enabled = true
      filters = { blob_types = ["blockBlob"], prefix_match = ["restore-staging/"] }
      actions = {
        base_blob = { delete_after_days_since_modification_greater_than = 1 }
        version   = { delete_after_days_since_creation = 1 }
      }
    }
  }
  lock             = { kind = "CanNotDelete" }
  enable_telemetry = false
}
# Restore ciphertext must age out after one day, outside the backup account's WORM default.
module "staging" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  source                          = "Azure/avm-res-storage-storageaccount/azurerm"
  version                         = "0.11.0"
  name                            = "${var.prefix}staging"
  parent_id                       = azurerm_resource_group.vault.id
  location                        = var.location
  account_replication_type        = "GRS"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  blob_properties = {
    versioning_enabled = true
  }
  containers = { staging = { name = "restore-staging" } }
  storage_management_policy_rule = {
    staging = {
      name    = "expire-staging"
      enabled = true
      filters = { blob_types = ["blockBlob"], prefix_match = ["restore-staging/"] }
      actions = {
        base_blob = { delete_after_days_since_modification_greater_than = 1 }
        version   = { delete_after_days_since_creation = 1 }
      }
    }
  }
  lock             = { kind = "CanNotDelete" }
  enable_telemetry = false
}
# Only enabled by the result of retention inheritance self-test 3.
resource "azapi_resource" "container_retention" {
  count     = var.container_retention_fallback ? 1 : 0
  type      = "Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2023-05-01"
  name      = "default"
  parent_id = module.backups.containers["backups"].id
  body      = { properties = { immutabilityPeriodSinceCreationInDays = 30, allowProtectedAppendWrites = false } }
}
# Bootstrap creates this account/container/lock; root imports adopt them before operator apply.
# AzAPI avoids persisting AzureRM's long-lived account-key and connection-string attributes.
module "state" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  source                          = "Azure/avm-res-storage-storageaccount/azurerm"
  version                         = "0.11.0"
  name                            = var.state_account_name
  location                        = var.location
  parent_id                       = azurerm_resource_group.vault.id
  account_replication_type        = "GRS"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  min_tls_version                 = "TLS1_2"
  blob_properties = {
    versioning_enabled                = true
    delete_retention_policy           = { days = 7, enabled = true }
    container_delete_retention_policy = { days = 7, enabled = true }
  }
  containers       = { tfstate = { name = "tfstate" } }
  lock             = { kind = "CanNotDelete", name = "state-cannot-delete" }
  enable_telemetry = false
}
resource "azurerm_key_vault" "this" {
  #checkov:skip=CKV_AZURE_109:GitHub-hosted gated restores and operator custody operations require the public Entra endpoint.
  #checkov:skip=CKV_AZURE_189:Public Entra endpoint required by ADR hosted workflows; RBAC and purge protection are enabled.
  #checkov:skip=CKV2_AZURE_32:Private endpoints would require a network-connected runner, outside the ADR architecture.

  name                       = "kv-${var.prefix}"
  resource_group_name        = azurerm_resource_group.vault.name
  location                   = var.location
  tenant_id                  = var.vault_tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  soft_delete_retention_days = 90
}
