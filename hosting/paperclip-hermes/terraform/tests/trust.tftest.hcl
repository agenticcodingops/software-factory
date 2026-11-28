
mock_provider "azurerm" {
  override_during = plan
  mock_resource "azurerm_key_vault" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.KeyVault/vaults/kv-sffixture", vault_uri = "https://kv-sffixture.vault.azure.net/" } }
  mock_resource "azurerm_resource_group" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault" } }
  mock_resource "azurerm_user_assigned_identity" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-sf-fixture", principal_id = "00000000-0000-0000-0000-000000000000", client_id = "00000000-0000-0000-0000-000000000000", tenant_id = "00000000-0000-0000-0000-000000000000" } }
  mock_resource "azurerm_role_definition" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000", role_definition_id = "00000000-0000-0000-0000-000000000000", role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000" } }
  mock_data "azurerm_role_definition" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000", role_definition_id = "00000000-0000-0000-0000-000000000000" } }
}
mock_provider "azapi" {
  override_during = plan
  mock_resource "azapi_resource" {
    defaults = {
      location = "uksouth"
      id       = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture"
      output   = { properties = { vmId = "00000000-0000-0000-0000-000000000000", storageProfile = { osDisk = { managedDisk = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture/providers/Microsoft.Compute/disks/os" } } } }, identity = { principalId = "00000000-0000-0000-0000-000000000000", tenantId = "00000000-0000-0000-0000-000000000000" } }
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
  mock_resource "azurerm_resource_group" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-trust" } }
  mock_resource "azurerm_role_definition" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000", role_definition_id = "00000000-0000-0000-0000-000000000000", role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000" } }
  mock_resource "azurerm_user_assigned_identity" { defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-trust/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-sf-fixture", principal_id = "00000000-0000-0000-0000-000000000000", client_id = "00000000-0000-0000-0000-000000000000", tenant_id = "00000000-0000-0000-0000-000000000000" } }
}
mock_provider "azapi" {
  override_during = plan
  alias           = "target"
  mock_data "azapi_client_config" { defaults = { subscription_id = "00000000-0000-0000-0000-000000000000" } }
}
variables {
  role_home_target      = "alpha"
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
override_resource {
  target          = module.trust.azurerm_resource_group.vault
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault" }
  override_during = plan
}
override_resource {
  target          = module.trust.azurerm_resource_group.drill
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-drill" }
  override_during = plan
}
run "trust_storage_and_authorization" {
  command = plan
  assert {
    condition = (output.authorization.roles.prerequisite_reader.actions == ["Microsoft.Features/providers/features/read", "Microsoft.Resources/subscriptions/providers/read"] &&
    alltrue([for key, target in var.targets : output.authorization.target.deploy["${target.subscription_resource_id}-SF Prerequisite Reader"].scope == target.subscription_resource_id]))
    error_message = "Deploy preflight needs only subscription feature/provider reads, never registration writes."
  }
  assert {
    condition     = output.authorization.roles.authorization_reader.actions == ["Microsoft.Authorization/*/read"] && output.authorization.roles.vault_authorization_reader.actions == ["Microsoft.Authorization/*/read"]
    error_message = "Subscription authorization roles must grant exactly read-only authorization actions."
  }
  assert {
    condition = alltrue([for account in [output.storage.backups, output.storage.staging, output.storage.state] :
      account.body.sku.name == "Standard_GRS" && account.body.properties.publicNetworkAccess == "Enabled" &&
      account.body.properties.networkAcls.defaultAction == "Allow" && !account.body.properties.allowSharedKeyAccess &&
      account.body.properties.defaultToOAuthAuthentication && !account.body.properties.allowBlobPublicAccess
    ])
    error_message = "Every storage account must be geo-redundant and publicly reachable with Entra-only authorization."
  }
  assert {
    condition     = length(output.storage.container_retention) == 0 && length(output.storage.container_retention_locks) == 0
    error_message = "A successful inheritance self-test needs no fallback policy or lock action."
  }
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
  assert {
    condition     = output.authorization.migration_writer_name == "${var.prefix} SF Backup Migration Writer" && length(output.authorization.migration_writer[0].actions) == 0 && toset(output.authorization.migration_writer[0].data_actions) == toset(["Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read", "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action", "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write"])
    error_message = "The temporary migration writer can only read/add/write blob data; it cannot delete or change WORM/Shared Key."
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
  assert {
    condition     = length(output.storage.container_retention) == 1 && output.storage.container_retention[0].body.properties.immutabilityPeriodSinceCreationInDays == 30 && length(output.storage.container_retention_locks) == 0
    error_message = "Fallback must retain 30 days without locking during the pilot window."
  }
}
run "post_pilot_locks_retention_fallback" {
  command = plan
  variables { container_retention_fallback = true }
  assert {
    condition = (output.storage.backups.body.properties.immutableStorageWithVersioning.immutabilityPolicy.state == "Locked" &&
      length(output.storage.container_retention_locks) == 1 &&
      output.storage.container_retention_locks[0].resource_id == output.storage.container_retention[0].id &&
      output.storage.container_retention_locks[0].type == output.storage.container_retention[0].type &&
      output.storage.container_retention_locks[0].action == "lock" &&
      output.storage.container_retention_locks[0].method == "POST" &&
      output.storage.container_retention_locks[0].when == "apply" &&
      output.storage.container_retention_locks[0].headers["If-Match"] == "\"policy-etag\"" &&
      output.storage.container_retention[0].body.properties.immutabilityPeriodSinceCreationInDays == 30 &&
    !output.storage.container_retention[0].body.properties.allowProtectedAppendWrites)
    error_message = "Post-pilot fallback must POST lock for the 30-day policy with its quoted ETag."
  }
}
override_resource {
  target = module.trust.azapi_resource.container_retention[0]
  values = {
    id     = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.Storage/storageAccounts/sffixturebackups/blobServices/default/containers/backups/immutabilityPolicies/default"
    output = { etag = "\"policy-etag\"" }
  }
  override_during = plan
}
override_resource {
  target          = module.trust.module.backups.module.containers["backups"].azapi_resource.this
  values          = { location = "uksouth", id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-sffixture-vault/providers/Microsoft.Storage/storageAccounts/sffixturebackups/blobServices/default/containers/backups" }
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

run "role_names_include_instance_prefix" {
  command = plan
  variables { prefix = "sfsecond" }
  assert {
    condition     = length(output.authorization.role_names) == 7 && alltrue([for name in values(output.authorization.role_names) : startswith(name, "sfsecond SF ")])
    error_message = "Custom role display names must differ across instance prefixes in the same tenant."
  }
}
run "host_owned_rg_is_read_before_grants" {
  command = plan
  variables {
    targets = { alpha = { subscription_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000", resource_group_name = "rg-fixture", location = "uksouth", rg_managed_by = "host" } }
  }
  override_data {
    target = module.workload_rg_subscription_00000000_0000_0000_0000_000000000000.data.azapi_resource.host_owned["alpha"]
    values = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture" }
  }
  assert {
    condition     = length(output.workload_resource_groups) == 0 && output.authorization.target.deploy["alpha-Contributor"].scope == "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-fixture"
    error_message = "A host-owned RG is read, never created in trust, before establishing RG-only permissions."
  }
}

# Exercise the documented new-account-after-pilot sequence with uploads held paused.
run "initialize_account_policy_unlocked" {
  command = apply
  variables { pilot_end_date = "9999-01-01T00:00:00Z" }
  assert {
    condition     = output.storage.backups.body.properties.immutableStorageWithVersioning.immutabilityPolicy.state == "Unlocked"
    error_message = "A new account must first create its default policy as Unlocked."
  }
}
run "initialized_account_can_transition_to_locked" {
  command = plan
  assert {
    condition     = output.storage.backups.body.properties.immutableStorageWithVersioning.immutabilityPolicy.state == "Locked"
    error_message = "Restoring the intended pilot end must lock the initialized account default."
  }
}

run "reject_invalid_prefix" {
  command = plan
  variables { prefix = "Invalid-prefix" }
  expect_failures = [var.prefix]
}
run "reject_missing_home" {
  command = plan
  variables { role_home_target = "missing" }
  expect_failures = [var.role_home_target]
}

run "initialize_twins_for_target_map_change" {
  command = apply
  variables { target_tenant_id = "different-mocked-tenant" }
}
run "adding_lexically_first_target_preserves_trust_home" {
  command = plan
  variables {
    target_tenant_id = "different-mocked-tenant"
    targets = {
      a0    = { subscription_resource_id = "/subscriptions/${uuidv5("dns", "beta.example.test")}", resource_group_name = "rg-new", location = "northeurope" }
      alpha = { subscription_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000", resource_group_name = "rg-fixture", location = "uksouth" }
      beta  = { subscription_resource_id = "/subscriptions/${uuidv5("dns", "beta.example.test")}", resource_group_name = "rg-second", location = "uksouth" }
    }
  }
}

run "mint_has_only_vault_data_grants" {
  command = plan
  variables {
    workflow_refs = { "plan" = { "ph_plan" = "example-org/example-instance/.github/workflows/ph-plan.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_tailnet_import" = "example-org/example-instance/.github/workflows/ph-tailnet-import.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "sf_merge_gate" = "example-org/example-instance/.github/workflows/sf-merge-gate.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_selftest" = "example-org/example-instance/.github/workflows/ph-selftest.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "ops" = { "ph_ops" = "example-org/example-instance/.github/workflows/ph-ops.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_power" = "example-org/example-instance/.github/workflows/ph-power.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_cost_guard" = "example-org/example-instance/.github/workflows/ph-cost-guard.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_backup_verify" = "example-org/example-instance/.github/workflows/ph-backup-verify.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "mint" = { "ph_deploy" = "example-org/example-instance/.github/workflows/ph-deploy.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_vault_move" = "example-org/example-instance/.github/workflows/ph-vault-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_move" = "example-org/example-instance/.github/workflows/ph-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_teardown" = "example-org/example-instance/.github/workflows/ph-teardown.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_rotate_age" = "example-org/example-instance/.github/workflows/ph-rotate-age.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_selftest" = "example-org/example-instance/.github/workflows/ph-selftest.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "deploy" = { "ph_deploy" = "example-org/example-instance/.github/workflows/ph-deploy.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_teardown" = "example-org/example-instance/.github/workflows/ph-teardown.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_rotate_age" = "example-org/example-instance/.github/workflows/ph-rotate-age.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_selftest" = "example-org/example-instance/.github/workflows/ph-selftest.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_move" = "example-org/example-instance/.github/workflows/ph-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_restore" = "example-org/example-instance/.github/workflows/ph-restore.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_vault_move" = "example-org/example-instance/.github/workflows/ph-vault-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "restore" = { "ph_restore" = "example-org/example-instance/.github/workflows/ph-restore.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_move" = "example-org/example-instance/.github/workflows/ph-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_vault_move" = "example-org/example-instance/.github/workflows/ph-vault-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" } }
  }
  assert {
    condition = (length(output.authorization.target.mint) == 0 &&
      toset(keys(output.authorization.vault.mint)) == toset(["backup_write", "backup_read", "delegation"]) &&
      output.authorization.vault.mint == output.authorization.vault.ops &&
      !contains(output.authorization.twins, "mint") &&
      length(output.authorization.federations.mint) == 6 &&
      length(output.authorization.federations.deploy) == 10 &&
      length(output.authorization.federations.plan) == 4 &&
      length(output.authorization.federations.ops) == 4 &&
      length(output.authorization.federations.restore) == 3 &&
      output.authorization.roles.backup_writer.actions == [] &&
      output.authorization.roles.delegation.actions == ["Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey/action"] &&
    output.mint_client_id != "")
    error_message = "Mint must have exactly the backup data/delegation grants, no target rights or twin; every allowlisted environment needs one credential."
  }
  assert {
    condition = alltrue([for file in ["ph_move", "ph_restore", "ph_vault_move"] :
      strcontains(output.authorization.federations.deploy["${file}-restore"].subject, ":environment:restore:") &&
      strcontains(output.authorization.federations.deploy[file].subject, ":environment:production:")
    ])
    error_message = "Move and restore require separate approved restore deploy credentials."
  }
}
run "mint_has_only_vault_data_grants_cross_tenant" {
  command = plan
  variables {
    workflow_refs    = { "plan" = { "ph_plan" = "example-org/example-instance/.github/workflows/ph-plan.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_tailnet_import" = "example-org/example-instance/.github/workflows/ph-tailnet-import.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "sf_merge_gate" = "example-org/example-instance/.github/workflows/sf-merge-gate.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_selftest" = "example-org/example-instance/.github/workflows/ph-selftest.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "ops" = { "ph_ops" = "example-org/example-instance/.github/workflows/ph-ops.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_power" = "example-org/example-instance/.github/workflows/ph-power.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_cost_guard" = "example-org/example-instance/.github/workflows/ph-cost-guard.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_backup_verify" = "example-org/example-instance/.github/workflows/ph-backup-verify.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "mint" = { "ph_deploy" = "example-org/example-instance/.github/workflows/ph-deploy.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_vault_move" = "example-org/example-instance/.github/workflows/ph-vault-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_move" = "example-org/example-instance/.github/workflows/ph-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_teardown" = "example-org/example-instance/.github/workflows/ph-teardown.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_rotate_age" = "example-org/example-instance/.github/workflows/ph-rotate-age.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_selftest" = "example-org/example-instance/.github/workflows/ph-selftest.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "deploy" = { "ph_deploy" = "example-org/example-instance/.github/workflows/ph-deploy.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_teardown" = "example-org/example-instance/.github/workflows/ph-teardown.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_rotate_age" = "example-org/example-instance/.github/workflows/ph-rotate-age.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_selftest" = "example-org/example-instance/.github/workflows/ph-selftest.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_move" = "example-org/example-instance/.github/workflows/ph-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_restore" = "example-org/example-instance/.github/workflows/ph-restore.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_vault_move" = "example-org/example-instance/.github/workflows/ph-vault-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "restore" = { "ph_restore" = "example-org/example-instance/.github/workflows/ph-restore.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_move" = "example-org/example-instance/.github/workflows/ph-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_vault_move" = "example-org/example-instance/.github/workflows/ph-vault-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" } }
    target_tenant_id = "different-mocked-tenant"
  }
  assert {
    condition = (length(output.authorization.target.mint) == 0 &&
      toset(keys(output.authorization.vault.mint)) == toset(["backup_write", "backup_read", "delegation"]) &&
      output.authorization.vault.mint == output.authorization.vault.ops &&
      !contains(output.authorization.twins, "mint") &&
      length(output.authorization.federations.mint) == 6 &&
      length(output.authorization.federations.deploy) == 10 &&
      length(output.authorization.federations.plan) == 4 &&
      length(output.authorization.federations.ops) == 4 &&
      length(output.authorization.federations.restore) == 3 &&
      output.authorization.roles.backup_writer.actions == [] &&
      output.authorization.roles.delegation.actions == ["Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey/action"] &&
    output.mint_client_id != "")
    error_message = "Mint must have exactly the backup data/delegation grants, no target rights or twin; every allowlisted environment needs one credential."
  }
  assert {
    condition = alltrue([for file in ["ph_move", "ph_restore", "ph_vault_move"] :
      strcontains(output.authorization.federations.deploy["${file}-restore"].subject, ":environment:restore:") &&
      strcontains(output.authorization.federations.deploy[file].subject, ":environment:production:")
    ])
    error_message = "Move and restore require separate approved restore deploy credentials."
  }
}
run "existing_credential_addresses_and_names" {
  command = plan
  variables { workflow_refs = { "plan" = { "ph_plan" = "example-org/example-instance/.github/workflows/ph-plan.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_tailnet_import" = "example-org/example-instance/.github/workflows/ph-tailnet-import.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "sf_merge_gate" = "example-org/example-instance/.github/workflows/sf-merge-gate.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_selftest" = "example-org/example-instance/.github/workflows/ph-selftest.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "ops" = { "ph_ops" = "example-org/example-instance/.github/workflows/ph-ops.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_power" = "example-org/example-instance/.github/workflows/ph-power.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_cost_guard" = "example-org/example-instance/.github/workflows/ph-cost-guard.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_backup_verify" = "example-org/example-instance/.github/workflows/ph-backup-verify.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "mint" = { "ph_deploy" = "example-org/example-instance/.github/workflows/ph-deploy.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_vault_move" = "example-org/example-instance/.github/workflows/ph-vault-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_move" = "example-org/example-instance/.github/workflows/ph-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_teardown" = "example-org/example-instance/.github/workflows/ph-teardown.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_rotate_age" = "example-org/example-instance/.github/workflows/ph-rotate-age.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_selftest" = "example-org/example-instance/.github/workflows/ph-selftest.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "deploy" = { "ph_deploy" = "example-org/example-instance/.github/workflows/ph-deploy.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_teardown" = "example-org/example-instance/.github/workflows/ph-teardown.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_rotate_age" = "example-org/example-instance/.github/workflows/ph-rotate-age.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_selftest" = "example-org/example-instance/.github/workflows/ph-selftest.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_move" = "example-org/example-instance/.github/workflows/ph-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_restore" = "example-org/example-instance/.github/workflows/ph-restore.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_vault_move" = "example-org/example-instance/.github/workflows/ph-vault-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" }, "restore" = { "ph_restore" = "example-org/example-instance/.github/workflows/ph-restore.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_move" = "example-org/example-instance/.github/workflows/ph-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba", "ph_vault_move" = "example-org/example-instance/.github/workflows/ph-vault-move.yml@d87a252bc1f8b4dc7622b53b5f98816e24eb6cba" } } }
  assert {
    condition = length(output.authorization.federations.deploy) == 10 && alltrue(flatten([for identity in ["plan", "deploy", "ops", "restore"] : [
      for key, ref in var.workflow_refs[identity] :
      output.authorization.federations[identity][key].name == "github-${key}" &&
      endswith(output.authorization.federations[identity][key].subject, ":job_workflow_ref:${ref}")
    ]]))
    error_message = "Primary credential keys (AVM resource addresses) and github-key names must remain stable."
  }
}
run "reject_production_file_in_ops" {
  command = plan
  variables { workflow_refs = { ops = { candidate = "example-org/example-instance/.github/workflows/ph-deploy.yml@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" } } }
  expect_failures = [var.workflow_refs]
}
run "reject_probe_federation" {
  command = plan
  variables { workflow_refs = { plan = { candidate = "example-org/example-instance/.github/workflows/sf-oidc-probe.yml@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" } } }
  expect_failures = [var.workflow_refs]
}
run "reject_expanded_credential_budget" {
  command = plan
  variables {
    workflow_refs = { deploy = { for n in range(11) : "release${n}" => "example-org/example-instance/.github/workflows/ph-restore.yml@${format("%040x", n + 1)}" } }
  }
  expect_failures = [var.workflow_refs]
}

override_resource {
  target          = module.trust.azurerm_role_definition.target["host_operator"]
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "host-operator.example.test")}", role_definition_id = uuidv5("dns", "host-operator.example.test"), role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "host-operator.example.test")}" }
  override_during = plan
}

override_resource {
  target          = module.trust.azurerm_role_definition.target["deallocator"]
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "deallocator.example.test")}", role_definition_id = uuidv5("dns", "deallocator.example.test"), role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "deallocator.example.test")}" }
  override_during = plan
}

override_resource {
  target          = module.trust.azurerm_role_definition.target["authorization_reader"]
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "authorization-reader.example.test")}", role_definition_id = uuidv5("dns", "authorization-reader.example.test"), role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "authorization-reader.example.test")}" }
  override_during = plan
}

override_resource {
  target          = module.trust.azurerm_role_definition.target["prerequisite_reader"]
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "prerequisite-reader.example.test")}", role_definition_id = uuidv5("dns", "prerequisite-reader.example.test"), role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "prerequisite-reader.example.test")}" }
  override_during = plan
}

override_resource {
  target          = module.trust.azurerm_role_definition.vault["backup_writer"]
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "backup-writer.example.test")}", role_definition_id = uuidv5("dns", "backup-writer.example.test"), role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "backup-writer.example.test")}" }
  override_during = plan
}

override_resource {
  target          = module.trust.azurerm_role_definition.vault["delegation"]
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "delegation.example.test")}", role_definition_id = uuidv5("dns", "delegation.example.test"), role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "delegation.example.test")}" }
  override_during = plan
}

override_resource {
  target          = module.trust.azurerm_role_definition.vault["vault_authorization_reader"]
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "vault-authorization-reader.example.test")}", role_definition_id = uuidv5("dns", "vault-authorization-reader.example.test"), role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/${uuidv5("dns", "vault-authorization-reader.example.test")}" }
  override_during = plan
}
