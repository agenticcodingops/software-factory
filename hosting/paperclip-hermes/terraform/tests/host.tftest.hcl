
mock_provider "azurerm" {
  override_during = plan
  mock_resource "azurerm_managed_disk" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Compute/disks/fixture-data" } }
  mock_resource "azurerm_user_assigned_identity" { defaults = { principal_id = "00000000-0000-0000-0000-000000000000", client_id = "00000000-0000-0000-0000-000000000000", tenant_id = "00000000-0000-0000-0000-000000000000" } }
  mock_resource "azurerm_role_definition" { defaults = { role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000" } }
  mock_data "azurerm_role_definition" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000", role_definition_id = "00000000-0000-0000-0000-000000000000" } }
}
mock_provider "azapi" {
  override_during = plan
  mock_data "azapi_resource" { defaults = { output = { properties = { accessEndpoint = "https://example.test/workflows/fixture" } } } }
  mock_resource "azapi_resource" {
    defaults = {
      id     = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture"
      output = { properties = { vmId = "00000000-0000-0000-0000-000000000000", storageProfile = { osDisk = { managedDisk = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Compute/disks/os" } } } }, identity = { principalId = "00000000-0000-0000-0000-000000000000", tenantId = "00000000-0000-0000-0000-000000000000" } }
    }
  }
}
mock_provider "time" {
  override_during = plan
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
run "new_host_invariants" {
  command = plan
  assert {
    condition     = alltrue([for rule in values(output.invariants.nsg) : rule.direction != "Inbound" || rule.access != "Allow"])
    error_message = "No inbound allow rules. Azure defaults remain implicit."
  }
  assert {
    condition     = length(output.invariants.nic) == 1 && length(values(output.invariants.nic)[0].body.properties.ipConfigurations) == 1 && values(output.invariants.nic)[0].body.properties.ipConfigurations[0].properties.publicIPAddress.id == output.invariants.public_ip
    error_message = "Exactly one NIC and one public IP configuration."
  }
  assert {
    condition     = length(output.invariants.vm.identity) == 0 && output.invariants.generated_private_key == "no_key" && output.invariants.vm.body.properties.osProfile.linuxConfiguration.disablePasswordAuthentication
    error_message = "No managed identities or generated private credentials."
  }
  assert {
    condition     = !contains(keys(output.invariants.tags), "sf-maintenance-until") && !contains(keys(output.invariants.tags), "sf-approved-sha") && !contains(keys(output.invariants.tags), "sf-recovery-sha") && !contains(keys(output.invariants.tags), "sf-recovery-until") && !contains(keys(output.invariants.tags), "sf-delivery-recipient")
    error_message = "Protocol tags must be absent on new hosts."
  }
  assert {
    condition     = output.invariants.custom_data == var.bootstrap_custom_data && !strcontains(base64decode(output.invariants.custom_data), "d87a252bc1f8b4dc7622b53b5f98816e24eb6cba")
    error_message = "Creation-time cloud-init is independent of every release SHA."
  }
  assert {
    condition     = output.invariants.expires_offset_days == 30 && output.invariants.expires_base == "2026-11-01T00:00:00Z" && output.invariants.tags.expires == "2026-12-01T00:00:00Z" && output.invariants.shutdown == "2200" && output.invariants.backup == "2115"
    error_message = "Stable expires is creation plus 30 days; shutdown follows backup by 45 minutes."
  }
  assert {
    condition     = output.disk.protection_enabled && output.guardrails.rg_budget.amount == 5
    error_message = "Disk is protected and RG budget never falls below five pounds."
  }
  assert {
    condition     = output.guardrails.assignment.scope == "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture" && output.guardrails.logic_app.parent_id == "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture" && output.guardrails.subscription_budget.time_grain == "Monthly"
    error_message = "Deallocator and its role belong to target RG, with subscription email-only fallback."
  }
}
run "upgrade_preserves_bootstrap_and_protocol" {
  command = plan
  variables { protocol_tags = { sf-approved-sha = "d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", sf-recovery-sha = "d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", sf-recovery-until = "2026-11-27T00:00:00Z", sf-delivery-recipient = "age1fixture", sf-maintenance-until = "2026-11-26T14:00:00Z" } }
  assert {
    condition     = alltrue([for key, val in var.protocol_tags : output.invariants.tags[key] == val]) && output.invariants.custom_data == var.bootstrap_custom_data && output.disk.protection_enabled
    error_message = "All current protocol tags and original bootstrap pass unchanged through upgrades/moves."
  }
}
run "park_preserves_disk" {
  command = plan
  variables { parked = true }
  assert {
    condition     = output.invariants.vm == null && output.disk.protection_enabled
    error_message = "Parking retains protected disk without VM, OS disk or IPv4."
  }
}
run "subscription_scope_and_single_app" {
  command = plan
  variables {
    rg_managed_by = "host"
    apps          = ["hermes"]
    headroom      = 50
  }
  assert {
    condition     = output.invariants.vm.body.properties.hardwareProfile.vmSize == "Standard_B2as_v2" && output.guardrails.rg_budget.amount == 35
    error_message = "One app uses B2as_v2 and RG budget respects cap."
  }
}

override_resource {
  target          = module.host.module.network[0].azapi_resource.vnet
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Network/virtualNetworks/fixture-vnet" }
  override_during = plan
}

override_resource {
  target          = module.host.module.public_ip[0].azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Network/publicIPAddresses/fixture-egress" }
  override_during = plan
}

override_resource {
  target          = module.host.module.nsg[0].azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Network/networkSecurityGroups/fixture-nsg" }
  override_during = plan
}

override_resource {
  target          = module.guardrails.module.deallocator.azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Logic/workflows/fixture-deallocator", identity = { type = "SystemAssigned", principal_id = "00000000-0000-0000-0000-000000000000", tenant_id = "00000000-0000-0000-0000-000000000000", identity_ids = [] } }
  override_during = plan
}

run "billing_month_and_override_budget" {
  command = plan
  assert {
    condition     = alltrue([for budget in [output.guardrails.rg_budget, output.guardrails.subscription_budget] : toset(one([for n in budget.notification : n if n.threshold_type == "Actual" && n.threshold == 100]).contact_groups) == toset([output.guardrails.action_groups.deallocate])])
    error_message = "Actual 100 percent must reach the deallocator in both verified budgets."
  }
  variables {
    billing_month_verified = true
    override_cost          = true
    pilot_monthly_cap      = 40
  }
  assert {
    condition     = output.guardrails.subscription_budget.time_grain == "BillingMonth" && output.guardrails.rg_budget.amount == 40
    error_message = "Verified BillingMonth and explicitly overridden pilot cap are preserved."
  }
  assert {
    condition     = length(output.guardrails.rg_budget.notification) == 4 && length(output.guardrails.subscription_budget.notification) == 4 && strcontains(output.guardrails.logic_app.body.properties.definition.actions.Threshold_and_maintenance.expression, "NotificationThresholdAmount") && strcontains(output.guardrails.logic_app.body.properties.definition.actions.Threshold_and_maintenance.actions.Hard_threshold.else.actions.Parse_maintenance.actions.Maintenance_active.inputs, "sf-maintenance-until")
    error_message = "Both budgets have the exact thresholds and deallocator uses documented budget payload and maintenance guard."
  }
}

override_resource {
  target          = module.guardrails.module.action_group["email"].azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Insights/actionGroups/fixture-email" }
  override_during = plan
}
override_resource {
  target          = module.guardrails.module.action_group["deallocate"].azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Insights/actionGroups/fixture-deallocate" }
  override_during = plan
}
run "actual100_subscription_email_only_fallback" {
  command = plan
  assert {
    condition     = toset(one([for n in output.guardrails.subscription_budget.notification : n if n.threshold_type == "Actual" && n.threshold == 100]).contact_groups) == toset([output.guardrails.action_groups.email]) && toset(one([for n in output.guardrails.rg_budget.notification : n if n.threshold_type == "Actual" && n.threshold == 100]).contact_groups) == toset([output.guardrails.action_groups.deallocate])
    error_message = "Unverified billing months must keep actual100 email-only for the subscription; RG actual100 deallocates."
  }
}

run "rendered_bootstrap_for_new_vm" {
  command = plan
  module { source = "../../modules/host" }
  variables {
    bootstrap_custom_data       = null
    bootstrap_render_for_new_vm = true
  }
  assert {
    condition     = startswith(base64decode(output.invariants.custom_data), "#cloud-config\n") && strcontains(base64decode(output.invariants.custom_data), base64gzip(file("../../modules/host/bootstrap/sf-host-agent")))
    error_message = "New VM creation data installs the fixed compressed agent."
  }
  assert {
    condition     = base64decode(output.invariants.custom_data) == file("../../../tools/tests/fixtures/host-cloud-init.yaml")
    error_message = "Review and record the Terraform-rendered creation payload fixture when bootstrap changes."
  }
  assert {
    condition     = output.invariants.agent_sha256 == filesha256("../../modules/host/bootstrap/sf-host-agent")
    error_message = "The receipt must record the shipped agent digest."
  }
}
