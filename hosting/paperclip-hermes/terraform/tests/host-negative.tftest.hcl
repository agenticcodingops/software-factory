
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
  parked                   = true
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

run "reject_short_backup_gap" {
  command = plan
  module { source = "../../modules/host" }
  variables { backup_time = "2130" }
  expect_failures = [terraform_data.invariants]
}
run "reject_ungated_disk_destroy" {
  command = plan
  module { source = "../../modules/host" }
  variables { allow_disk_destroy = true }
  expect_failures = [azurerm_managed_disk.data]
}
run "reject_release_in_cloud_init" {
  command = plan
  module { source = "../../modules/host" }
  variables { bootstrap_custom_data = base64encode("#cloud-config\nkit_commit: candidate") }
  expect_failures = [terraform_data.invariants]
}
run "reject_protocol_tag_injection" {
  command = plan
  module { source = "../../modules/host" }
  variables { tags = { sf-approved-sha = "candidate" } }
  expect_failures = [terraform_data.invariants]
}

run "protected_parked_disk" {
  command = plan
  module { source = "../../modules/host" }
  assert {
    condition     = output.disk.protection_enabled
    error_message = "A parked disk retains protection."
  }
}

run "reject_maintenance_from_ordinary_tags" {
  command = plan
  module { source = "../../modules/host" }
  variables { tags = { sf-maintenance-until = "2026-11-26T14:00:00Z" } }
  expect_failures = [terraform_data.invariants]
}

run "sha256_checksum_in_cloud_init" {
  command = plan
  module { source = "../../modules/host" }
  variables { bootstrap_custom_data = base64encode("#cloud-config\n# sha256:${join("", [for i in range(64) : "a"])}\n") }
  assert {
    condition     = output.disk.protection_enabled
    error_message = "A standalone SHA-256 checksum is valid creation data."
  }
}
run "reject_standalone_release_sha" {
  command = plan
  module { source = "../../modules/host" }
  variables { bootstrap_custom_data = base64encode("#cloud-config\n# commit ${join("", [for i in range(40) : "A"])}\n") }
  expect_failures = [terraform_data.invariants]
}

run "reject_null_bootstrap_without_new_vm_flag" {
  command = plan
  module { source = "../../modules/host" }
  variables { bootstrap_custom_data = null }
  expect_failures = [terraform_data.invariants]
  assert {
    condition     = var.bootstrap_render_for_new_vm == false
    error_message = "Rendering is an explicit opt-in, including for a VM without protocol tags."
  }
}
run "reject_new_vm_flag_with_protocol_tags" {
  command = plan
  module { source = "../../modules/host" }
  variables {
    bootstrap_custom_data       = null
    bootstrap_render_for_new_vm = true
    protocol_tags               = { sf-approved-sha = "candidate" }
  }
  expect_failures = [terraform_data.invariants]
  assert {
    condition     = startswith(base64decode(output.invariants.custom_data), "#cloud-config\n")
    error_message = "Null resolves to the template; rejection must come from the explicit new-VM guard."
  }
}
run "reject_new_vm_flag_with_receipt_bytes" {
  command = plan
  module { source = "../../modules/host" }
  variables { bootstrap_render_for_new_vm = true }
  expect_failures = [terraform_data.invariants]
}

run "reject_upper_age_identity_in_receipt" {
  command = plan
  module { source = "../../modules/host" }
  variables { bootstrap_custom_data = base64encode("#cloud-config\n${join("-", ["AGE", "SECRET", "KEY"])}-1${join("", [for i in range(58) : "Q"])}\n") }
  expect_failures = [terraform_data.invariants]
}

run "reject_lower_age_identity_in_receipt" {
  command = plan
  module { source = "../../modules/host" }
  variables { bootstrap_custom_data = base64encode("#cloud-config\n${join("-", ["age", "secret", "key"])}-1${join("", [for i in range(58) : "Q"])}\n") }
  expect_failures = [terraform_data.invariants]
}
