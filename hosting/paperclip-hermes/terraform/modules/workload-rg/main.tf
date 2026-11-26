module "workload_rg" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  for_each         = { for k, v in var.targets : k => v if v.rg_managed_by == "trust" }
  source           = "Azure/avm-res-resources-resourcegroup/azurerm"
  version          = "0.5.0"
  name             = each.value.resource_group_name
  location         = each.value.location
  enable_telemetry = false
}

# Host-owned groups must first be created in the host state by the Owner.
# Reading them here orders and validates RG-scoped grants without creating a second owner.
data "azapi_resource" "host_owned" {
  for_each    = { for k, v in var.targets : k => v if v.rg_managed_by == "host" }
  type        = "Microsoft.Resources/resourceGroups@2024-03-01"
  resource_id = "${each.value.subscription_resource_id}/resourceGroups/${each.value.resource_group_name}"
}
