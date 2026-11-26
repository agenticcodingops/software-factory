
mock_provider "azurerm" {
  override_during = plan
  mock_resource "azurerm_resource_group" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault" } }
  mock_resource "azurerm_user_assigned_identity" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-sf-fixture", principal_id = "00000000-0000-0000-0000-000000000000", client_id = "00000000-0000-0000-0000-000000000000", tenant_id = "00000000-0000-0000-0000-000000000000" } }
  mock_resource "azurerm_role_definition" { defaults = { role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000" } }
  mock_data "azurerm_role_definition" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000", role_definition_id = "00000000-0000-0000-0000-000000000000" } }
}
mock_provider "azapi" {
  override_during = plan
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

mock_provider "azurerm" {
  override_during = plan
  alias           = "target"
  mock_resource "azurerm_role_definition" { defaults = { role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000" } }
  mock_resource "azurerm_user_assigned_identity" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-sf-fixture", principal_id = "00000000-0000-0000-0000-000000000000" } }
}
mock_provider "azapi" {
  override_during = plan
  alias           = "target"
  mock_data "azapi_client_config" { defaults = { subscription_id = "00000000-0000-0000-0000-000000000000" } }
}
variables {
  prefix                = "sffixture"
  location              = "uksouth"
  vault_subscription_id = "00000000-0000-0000-0000-000000000000"
  vault_tenant_id       = "00000000-0000-0000-0000-000000000000"
  target_tenant_id      = "00000000-0000-0000-0000-000000000000"
  operator_object_id    = "00000000-0000-0000-0000-000000000000"
  state_account_name    = "sffixturestate"
  sub_claim_prefix      = "repo:example@1/instance@2"
  workflow_refs         = { ops = { health = "agenticcodingops/software-factory/.github/workflows/ph-ops.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" } }
  pilot_end_date        = "2000-01-01T00:00:00Z"
  targets               = { alpha = { subscription_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000", resource_group_name = "rg-fixture", location = "uksouth" }, beta = { subscription_resource_id = "/subscriptions/${uuidv5("dns", "beta.example.test")}", resource_group_name = "rg-second", location = "uksouth" } }
}
run "trust_storage_and_authorization" {
  command = plan
  assert {
    condition     = output.storage.state.body.properties.allowSharedKeyAccess == false && output.storage.backups.body.properties.allowSharedKeyAccess == false
    error_message = "Shared Key must be disabled on state and backup accounts."
  }
  assert {
    condition     = output.storage.backups.body.properties.immutableStorageWithVersioning.immutabilityPolicy.immutabilityPeriodSinceCreationInDays == 30 && output.storage.backups.body.properties.immutableStorageWithVersioning.immutabilityPolicy.state == "Locked"
    error_message = "Backups need 30-day version WORM locked after pilot."
  }
  assert {
    condition     = output.storage.key_vault.rbac_authorization_enabled && output.storage.key_vault.purge_protection_enabled
    error_message = "Key Vault must use RBAC and purge protection."
  }
  assert {
    condition     = output.authorization.vault.ops.delegation.scope == "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.Storage/storageAccounts/sffixturebackups" && output.authorization.vault.restore.delegation.scope == "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.Storage/storageAccounts/sffixturestaging"
    error_message = "Delegation keys require account scope, not container scope."
  }
  assert {
    condition     = endswith(output.authorization.vault.ops.backup_write.scope, "/containers/backups") && endswith(output.authorization.vault.restore.staging_write.scope, "/containers/restore-staging") && !contains(keys(output.authorization.vault.restore), "key_read")
    error_message = "Data rights are container scoped and operator custody grants no key access."
  }
  assert {
    condition     = length([for grant in values(output.authorization.target.ops) : grant if grant.role_definition_id_or_name == "Cost Management Reader"]) == 2 && alltrue([for grant in values(output.authorization.target.ops) : !strcontains(grant.role_definition_id_or_name, "Contributor")])
    error_message = "Ops must have cost read access to every target, including beta, and no cost writes."
  }
  assert {
    condition     = length(output.authorization.federations.ops) == 1 && endswith(output.authorization.federations.ops.health.subject, ":job_workflow_ref:agenticcodingops/software-factory/.github/workflows/ph-ops.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba") && !contains([for grant in values(output.authorization.vault.deploy) : grant.scope], output.authorization.vault.ops.backup_write.scope)
    error_message = "An added candidate ops job cannot match the exact owner-authorized workflow subject; deploy cannot write backups."
  }
  assert {
    condition     = !contains(output.authorization.roles.host_operator.actions, "Microsoft.Compute/virtualMachines/write") && !contains(output.authorization.roles.host_operator.actions, "Microsoft.Resources/tags/write") && strcontains(output.authorization.condition, "@Request[") && strcontains(output.authorization.condition, "@Resource[")
    error_message = "Ops cannot write tags/VMs; both role assignment write and delete need ABAC."
  }
}
run "vault_custody" {
  command = plan
  variables { custody = "vault" }
  assert {
    condition     = output.authorization.vault.restore.key_read.role_definition_id_or_name == "Key Vault Secrets User"
    error_message = "Only gated restore may read enabled historical age keys in vault custody."
  }
}
run "target_tenant_twins" {
  command = plan
  # Only equality matters in this mocked layout; no real tenant identifiers are stored.
  variables { target_tenant_id = "different-mocked-tenant" }
  assert {
    condition     = toset(output.authorization.twins) == toset(["plan", "deploy", "ops", "restore"])
    error_message = "Cross-tenant targets require all target-facing identity twins."
  }
}

override_resource {
  target          = module.trust.module.state.azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.Storage/storageAccounts/sffixturestate" }
  override_during = plan
}

override_resource {
  target          = module.trust.module.backups.azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.Storage/storageAccounts/sffixturebackups" }
  override_during = plan
}

override_resource {
  target          = module.trust.module.state.module.containers["tfstate"].azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.Storage/storageAccounts/sffixturestate/blobServices/default/containers/tfstate" }
  override_during = plan
}

override_resource {
  target          = module.trust.module.state.azapi_resource.lock[0]
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.Storage/storageAccounts/sffixturestate/providers/Microsoft.Authorization/locks/state-cannot-delete" }
  override_during = plan
}

run "reject_unpinned_ops_workflow" {
  command = plan
  variables { workflow_refs = { ops = { candidate = "example/release/.github/workflows/ph-ops.yml@refs/heads/main" } } }
  expect_failures = [var.workflow_refs]
}

run "pilot_unlock_and_retention_fallback" {
  command = plan
  variables {
    pilot_end_date               = "9999-01-01T00:00:00Z"
    container_retention_fallback = true
  }
  assert {
    condition     = output.storage.backups.body.properties.immutableStorageWithVersioning.immutabilityPolicy.state == "Unlocked"
    error_message = "Only the explicit future pilot window permits unlocked WORM."
  }
}
override_resource {
  target          = module.trust.module.backups.module.containers["backups"].azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.Storage/storageAccounts/sffixturebackups/blobServices/default/containers/backups" }
  override_during = plan
}

run "shared_subscription_cost_grants_are_unique" {
  command = plan
  variables {
    targets = {
      alpha = { subscription_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000", resource_group_name = "rg-fixture", location = "uksouth" }
      beta  = { subscription_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000", resource_group_name = "rg-second", location = "uksouth" }
    }
  }
  assert {
    condition     = length([for grant in values(output.authorization.target.ops) : grant if grant.role_definition_id_or_name == "Cost Management Reader"]) == 1
    error_message = "Targets in one subscription must share their subscription-scoped cost reader grant."
  }
}

run "workload_rgs_use_target_subscriptions" {
  command = plan
  assert {
    condition = alltrue([for key, target in var.targets :
      output.workload_resource_groups[key].parent_id == target.subscription_resource_id &&
      output.workload_resource_groups[key].name == target.resource_group_name &&
      output.authorization.target.deploy["${key}-Contributor"].scope == "${output.workload_resource_groups[key].parent_id}/resourceGroups/${target.resource_group_name}"
    ])
    error_message = "Each workload RG and its Contributor grant must use the target's own subscription."
  }
}
run "staging_excluded_from_locked_backup_retention" {
  command = plan
  assert {
    condition = (output.storage.staging.body.properties.immutableStorageWithVersioning == null &&
      output.storage.staging.body.properties.allowSharedKeyAccess == false &&
      output.storage.staging.name != output.storage.backups.name &&
      strcontains(output.authorization.vault.restore.staging_write.scope, "/storageAccounts/${output.storage.staging.name}/") &&
    !strcontains(output.authorization.vault.restore.staging_write.scope, "/storageAccounts/${output.storage.backups.name}/"))
    error_message = "Restore staging must be outside the locked backup account and retain scoped restore access."
  }
}

mock_provider "azapi" {
  override_during = plan
  alias           = "subscription_fc0c4069_12db_58b7_b273_ff3056b17b14"
  mock_data "azapi_client_config" { defaults = { subscription_id = uuidv5("dns", "beta.example.test") } }
}

override_resource {
  target          = module.workload_rg_subscription_00000000_0000_0000_0000_000000000000.module.workload_rg["alpha"].azapi_resource.this[0]
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture" }
  override_during = plan
}

override_resource {
  target          = module.workload_rg_subscription_fc0c4069_12db_58b7_b273_ff3056b17b14.module.workload_rg["beta"].azapi_resource.this[0]
  values          = { id = "/subscriptions/${uuidv5("dns", "beta.example.test")}/resourceGroups/rg-second" }
  override_during = plan
}

override_resource {
  target          = module.workload_rg_subscription_00000000_0000_0000_0000_000000000000.module.workload_rg["beta"].azapi_resource.this[0]
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-second" }
  override_during = plan
}

mock_provider "azapi" {
  override_during = plan
  alias           = "subscription_00000000_0000_0000_0000_000000000000"
  mock_data "azapi_client_config" { defaults = { subscription_id = "00000000-0000-0000-0000-000000000000" } }
}
override_resource {
  target          = module.trust.module.staging.azapi_resource.this
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.Storage/storageAccounts/sffixturestaging" }
  override_during = plan
}
