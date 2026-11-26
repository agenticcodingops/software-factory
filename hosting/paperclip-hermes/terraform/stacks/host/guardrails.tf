module "guardrails" {
  source                   = "../../modules/guardrails"
  name                     = var.name
  location                 = var.location
  resource_group_id        = "${var.subscription_resource_id}/resourceGroups/${var.resource_group_name}"
  resource_group_name      = var.resource_group_name
  vm_resource_id           = "${var.subscription_resource_id}/resourceGroups/${var.resource_group_name}/providers/Microsoft.Compute/virtualMachines/${var.name}"
  subscription_resource_id = var.subscription_resource_id
  deallocator_role_id      = var.deallocator_role_id
  operator_email           = var.operator_email
  tenant_id                = var.tenant_id
  alert_application        = var.alert_application
  pilot_monthly_cap        = var.pilot_monthly_cap
  headroom                 = var.headroom
  override_cost            = var.override_cost
  monthly_credit           = var.monthly_credit
  budget_start             = var.budget_start
  billing_month_verified   = var.billing_month_verified
  depends_on               = [module.host]
}
output "guardrails" {
  description = "Budget and deallocator assertions."
  value       = module.guardrails.invariants
}
