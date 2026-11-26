
mock_provider "azurerm" {
  override_during = apply
  mock_resource "azurerm_managed_disk" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Compute/disks/fixture-data" } }
  mock_resource "azurerm_user_assigned_identity" { defaults = { principal_id = "00000000-0000-0000-0000-000000000000", client_id = "00000000-0000-0000-0000-000000000000", tenant_id = "00000000-0000-0000-0000-000000000000" } }
  mock_resource "azurerm_role_definition" { defaults = { role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000" } }
  mock_data "azurerm_role_definition" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000", role_definition_id = "00000000-0000-0000-0000-000000000000" } }
}
mock_provider "azapi" {
  override_during = apply
  mock_data "azapi_resource" {
    override_during = plan
    defaults        = { output = { properties = { accessEndpoint = "https://example.test/workflows/fixture" } } }
  }
  mock_resource "azapi_resource" {
    defaults = {
      id     = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture"
      output = { properties = { vmId = "00000000-0000-0000-0000-000000000000", storageProfile = { osDisk = { managedDisk = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Compute/disks/os" } } } }, identity = { principalId = "00000000-0000-0000-0000-000000000000", tenantId = "00000000-0000-0000-0000-000000000000" } }
    }
  }
}
mock_provider "time" {
  override_during = apply
  mock_resource "time_static" { defaults = { rfc3339 = "2026-11-01T00:00:00Z" } }
  mock_resource "time_offset" { defaults = { rfc3339 = "2026-12-01T00:00:00Z" } }
}
mock_provider "random" {}

mock_provider "tls" {}
variables {
  name                     = "fixture"
  resource_group_name      = "rg-fixture"
  subscription_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000"
  location                 = "uksouth"
  ssh_public_key           = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBERERERERERERERERERERERERERERERERERERERERER"
  bootstrap_custom_data    = "I2Nsb3VkLWNvbmZpZ1xucGFja2FnZXM6IFtdCg=="
  deallocator_role_id      = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000"
  operator_email           = "operator@example.com"
  tenant_id                = "00000000-0000-0000-0000-000000000000"
  alert_application        = { object_id = "00000000-0000-0000-0000-000000000000", identifier_uri = "api://sf-fixture-alerts", monitor_application_id = "00000000-0000-0000-0000-000000000000" }
  headroom                 = 0
  monthly_credit           = 100
}

# Persist the AVM resources, then feed the actual park destruction plan to plan-guard.
run "initialize_host_for_park" {
  command = apply
}
run "park_guard_accepts_vm_and_state_only_patch_delete" {
  command = plan
  variables { parked = true }
}
run "budget_replacement_uses_current_month" {
  command = apply
  variables { billing_month_verified = true }
  assert {
    condition     = output.guardrails.subscription_budget.time_period[0].start_date == formatdate("YYYY-MM-01'T'00:00:00Z", plantimestamp())
    error_message = "A grain replacement must use the current plan month, never a static configured date."
  }
}
run "fractional_headroom_rounds_down" {
  command = plan
  variables { headroom = 5.99 }
  assert {
    condition     = output.guardrails.rg_budget.amount == 5
    error_message = "Round RG budget down to whole pounds."
  }
}

override_resource {
  target          = module.host.module.network[0].azapi_resource.vnet
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Network/virtualNetworks/fixture-vnet" }
  override_during = apply
}

override_resource {
  target          = module.host.module.public_ip[0].azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Network/publicIPAddresses/fixture-egress" }
  override_during = apply
}

override_resource {
  target          = module.host.module.nsg[0].azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Network/networkSecurityGroups/fixture-nsg" }
  override_during = apply
}

override_resource {
  target          = module.guardrails.module.deallocator.azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Logic/workflows/fixture-deallocator", identity = { type = "SystemAssigned", principal_id = "00000000-0000-0000-0000-000000000000", tenant_id = "00000000-0000-0000-0000-000000000000", identity_ids = [] } }
  override_during = apply
}

override_resource {
  target          = module.guardrails.module.action_group["email"].azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Insights/actionGroups/fixture-email" }
  override_during = apply
}

override_resource {
  target          = module.guardrails.module.action_group["deallocate"].azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Insights/actionGroups/fixture-deallocate" }
  override_during = apply
}
