output "resource_id" {
  description = "Vault resource group ID."
  value       = azurerm_resource_group.vault.id
}
output "identities" {
  description = "Vault identity resource outputs, no credentials."
  value       = { for k, v in module.identities : k => v.resource_id }
}
output "deallocator_role_id" {
  description = "Only role the deploy identity may assign."
  value       = azurerm_role_definition.target["deallocator"].role_definition_resource_id
}
output "authorization" {
  description = "Resolved least-privilege grants and exact subjects, used by acceptance tests."
  value       = { target = local.target_grants, vault = local.vault_grants, federations = local.federations, roles = local.roles, condition = local.deallocator_condition, twins = keys(module.target_identities) }
}
output "storage" {
  description = "Actual storage inputs and state settings for mocked invariant checks."
  value       = { backups = module.backups.resource, staging = module.staging.resource, state = module.state.resource, key_vault = azurerm_key_vault.this }
  sensitive   = true
}
