module "trust" {
  source                       = "../../modules/trust"
  providers                    = { azurerm = azurerm, azapi = azapi, azurerm.target = azurerm.target, azapi.target = azapi.target }
  workload_resource_group_ids  = {}
  prefix                       = var.prefix
  location                     = var.location
  vault_subscription_id        = var.vault_subscription_id
  vault_tenant_id              = var.vault_tenant_id
  target_tenant_id             = var.target_tenant_id
  targets                      = var.targets
  role_home_target             = var.role_home_target
  operator_object_id           = var.operator_object_id
  state_account_name           = var.state_account_name
  sub_claim_prefix             = var.sub_claim_prefix
  workflow_refs                = var.workflow_refs
  custody                      = var.custody
  pilot_end_date               = var.pilot_end_date
  container_retention_fallback = var.container_retention_fallback
}

# Adopt the bootstrap state account and its parent RG, container and lock.
import {
  to = module.trust.azurerm_resource_group.vault
  id = "/subscriptions/${var.vault_subscription_id}/resourceGroups/rg-${var.prefix}-vault"
}
import {
  to = module.trust.module.state.azapi_resource.this
  id = "/subscriptions/${var.vault_subscription_id}/resourceGroups/rg-${var.prefix}-vault/providers/Microsoft.Storage/storageAccounts/${var.state_account_name}"
}
output "authorization" {
  description = "Authorization."
  value       = module.trust.authorization
}
output "storage" {
  description = "Storage."
  value       = module.trust.storage
  sensitive   = true
}
output "deallocator_role_id" {
  description = "Deallocator role id."
  value       = module.trust.deallocator_role_id
}

import {
  to = module.trust.module.state.module.containers["tfstate"].azapi_resource.this
  id = "/subscriptions/${var.vault_subscription_id}/resourceGroups/rg-${var.prefix}-vault/providers/Microsoft.Storage/storageAccounts/${var.state_account_name}/blobServices/default/containers/tfstate"
}
import {
  to = module.trust.module.state.azapi_resource.lock[0]
  id = "/subscriptions/${var.vault_subscription_id}/resourceGroups/rg-${var.prefix}-vault/providers/Microsoft.Storage/storageAccounts/${var.state_account_name}/providers/Microsoft.Authorization/locks/state-cannot-delete"
}

output "workload_resource_groups" {
  description = "Resolved workload RG resources from every subscription."
  value       = {}
}
