output "invariants" {
  description = "Actual budgets, deallocator and RG-scoped assignment."
  value       = { rg_budget = azurerm_consumption_budget_resource_group.pilot, subscription_budget = azurerm_consumption_budget_subscription.credit, logic_app = module.deallocator.resource, assignment = azurerm_role_assignment.deallocator, action_groups = { for k, v in module.action_group : k => v.resource_id } }
}
