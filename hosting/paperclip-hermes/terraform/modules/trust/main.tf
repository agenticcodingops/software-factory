locals {
  vault_rg_id  = "/subscriptions/${var.vault_subscription_id}/resourceGroups/rg-${var.prefix}-vault"
  allowlist    = jsondecode(file("${path.module}/federation-allowlist.json"))
  environments = { for identity, files in local.allowlist.identities : identity => files }
  federations = { for identity, files in local.environments : identity => merge({}, [
    for key, ref in lookup(var.workflow_refs, identity, {}) : {
      for index, env in lookup(files, basename(split("@", ref)[0]), []) :
      (index == 0 ? key : "${key}-${env}") => {
        name     = index == 0 ? "github-${key}" : "github-${key}-${env}"
        issuer   = "https://token.actions.githubusercontent.com"
        audience = ["api://AzureADTokenExchange"]
        subject  = "${var.sub_claim_prefix}:environment:${env}:job_workflow_ref:${ref}"
      }
    }
  ]...) }
  backup_account_id  = "${local.vault_rg_id}/providers/Microsoft.Storage/storageAccounts/${var.prefix}backups"
  backups_scope      = "${local.backup_account_id}/blobServices/default/containers/backups"
  staging_account_id = "${local.vault_rg_id}/providers/Microsoft.Storage/storageAccounts/${var.prefix}staging"
  staging_scope      = "${local.staging_account_id}/blobServices/default/containers/restore-staging"
  state_scope        = "${local.vault_rg_id}/providers/Microsoft.Storage/storageAccounts/${var.state_account_name}/blobServices/default/containers/tfstate"
  roles = {
    prerequisite_reader        = { name = "SF Prerequisite Reader", actions = ["Microsoft.Features/providers/features/read", "Microsoft.Resources/subscriptions/providers/read"], data_actions = [] }
    vault_authorization_reader = { name = "SF Vault Authorization Reader", actions = ["Microsoft.Authorization/*/read"], data_actions = [] }
    authorization_reader       = { name = "SF Authorization Reader", actions = ["Microsoft.Authorization/*/read"], data_actions = [] }
    host_operator              = { name = "SF Host Operator", actions = ["Microsoft.Compute/virtualMachines/read", "Microsoft.Compute/virtualMachines/start/action", "Microsoft.Compute/virtualMachines/deallocate/action", "Microsoft.Compute/virtualMachines/runCommands/*"], data_actions = [] }
    deallocator                = { name = "SF VM Deallocator", actions = ["Microsoft.Compute/virtualMachines/read", "Microsoft.Compute/virtualMachines/deallocate/action"], data_actions = [] }
    backup_writer              = { name = "SF Backup Writer", actions = [], data_actions = ["Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action", "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write"] }
    delegation                 = { name = "SF Delegation Key", actions = ["Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey/action"], data_actions = [] }
  }
  target_roles = {
    plan    = ["Reader", "Cost Management Reader", "SF Authorization Reader"]
    deploy  = ["Contributor", "Cost Management Contributor", "Role Based Access Control Administrator", "SF Prerequisite Reader"]
    ops     = ["SF Host Operator", "Cost Management Reader"]
    restore = ["SF Host Operator"]
    mint    = []
  }
  # Both assign and delete must be constrained, otherwise RBAC Administrator could remove arbitrary grants.
  deallocator_condition = <<-CONDITION
    ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR
      (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${basename(azurerm_role_definition.target["deallocator"].role_definition_resource_id)}} AND
       @Request[Microsoft.Authorization/roleAssignments:PrincipalType] ForAnyOfAnyValues:StringEqualsIgnoreCase {'ServicePrincipal'})) AND
    ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR
      (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${basename(azurerm_role_definition.target["deallocator"].role_definition_resource_id)}} AND
       @Resource[Microsoft.Authorization/roleAssignments:PrincipalType] ForAnyOfAnyValues:StringEqualsIgnoreCase {'ServicePrincipal'}))
  CONDITION
  target_grants = { for identity in keys(local.environments) : identity => merge([
    for key, target in var.targets : {
      for role in local.target_roles[identity] :
      ((startswith(role, "Cost Management") || contains(["SF Authorization Reader", "SF Prerequisite Reader"], role)) ? "${target.subscription_resource_id}-${role}" : "${key}-${role}") => {
        role_definition_id_or_name = role == "SF Prerequisite Reader" ? azurerm_role_definition.target["prerequisite_reader"].role_definition_resource_id : role == "SF Authorization Reader" ? azurerm_role_definition.target["authorization_reader"].role_definition_resource_id : role == "SF Host Operator" ? azurerm_role_definition.target["host_operator"].role_definition_resource_id : role
        scope                      = (startswith(role, "Cost Management") || contains(["SF Authorization Reader", "SF Prerequisite Reader"], role)) ? target.subscription_resource_id : "${target.subscription_resource_id}/resourceGroups/${target.resource_group_name}"
        condition                  = role == "Role Based Access Control Administrator" ? local.deallocator_condition : null
        condition_version          = role == "Role Based Access Control Administrator" ? "2.0" : null
      }
    }
  ]...) }
  vault_grants = {
    plan = {
      authorization_read = { role_definition_id_or_name = azurerm_role_definition.vault["vault_authorization_reader"].role_definition_resource_id, scope = "/subscriptions/${var.vault_subscription_id}" }
      drill_read         = { role_definition_id_or_name = "Reader", scope = azurerm_resource_group.drill.id }
      vault_read         = { role_definition_id_or_name = "Reader", scope = local.vault_rg_id }
      state_read         = { role_definition_id_or_name = "Storage Blob Data Reader", scope = local.state_scope }
    }
    deploy = {
      state_write = { role_definition_id_or_name = "Storage Blob Data Contributor", scope = local.state_scope }
      drill       = { role_definition_id_or_name = "Contributor", scope = azurerm_resource_group.drill.id }
    }
    ops = {
      backup_write = { role_definition_id_or_name = azurerm_role_definition.vault["backup_writer"].role_definition_resource_id, scope = local.backups_scope }
      backup_read  = { role_definition_id_or_name = "Storage Blob Data Reader", scope = local.backups_scope }
      delegation   = { role_definition_id_or_name = azurerm_role_definition.vault["delegation"].role_definition_resource_id, scope = local.backup_account_id }
    }
    mint = {
      backup_write = { role_definition_id_or_name = azurerm_role_definition.vault["backup_writer"].role_definition_resource_id, scope = local.backups_scope }
      backup_read  = { role_definition_id_or_name = "Storage Blob Data Reader", scope = local.backups_scope }
      delegation   = { role_definition_id_or_name = azurerm_role_definition.vault["delegation"].role_definition_resource_id, scope = local.backup_account_id }
    }
    restore = merge({
      backup_read   = { role_definition_id_or_name = "Storage Blob Data Reader", scope = local.backups_scope }
      staging_write = { role_definition_id_or_name = "Storage Blob Data Contributor", scope = local.staging_scope }
      delegation    = { role_definition_id_or_name = azurerm_role_definition.vault["delegation"].role_definition_resource_id, scope = local.staging_account_id }
    }, var.custody == "vault" ? { key_read = { role_definition_id_or_name = "Key Vault Secrets User", scope = azurerm_key_vault.this.id } } : {})
  }
}

resource "azurerm_resource_group" "vault" {
  name     = "rg-${var.prefix}-vault"
  location = var.location
}
resource "azurerm_resource_group" "drill" {
  name     = "rg-${var.prefix}-drill"
  location = var.location
}
resource "azurerm_role_definition" "target" {
  provider          = azurerm.target
  for_each          = { for k, v in local.roles : k => v if contains(["host_operator", "deallocator", "authorization_reader", "prerequisite_reader"], k) }
  name              = "${var.prefix} ${each.value.name}"
  scope             = var.targets[var.role_home_target].subscription_resource_id
  assignable_scopes = distinct([for t in var.targets : t.subscription_resource_id])
  lifecycle { prevent_destroy = true }
  permissions {
    actions      = each.value.actions
    data_actions = each.value.data_actions
  }
}
resource "azurerm_role_definition" "vault" {
  for_each = { for k, v in local.roles : k => v if contains(["backup_writer", "delegation", "vault_authorization_reader"], k) }
  name     = "${var.prefix} ${each.value.name}"
  scope    = "/subscriptions/${var.vault_subscription_id}"
  permissions {
    actions      = each.value.actions
    data_actions = each.value.data_actions
  }
}
module "identities" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  for_each                       = local.environments
  source                         = "Azure/avm-res-managedidentity-userassignedidentity/azurerm"
  version                        = "0.6.0"
  name                           = "id-sf-${each.key}"
  location                       = var.location
  resource_group_name            = azurerm_resource_group.vault.name
  federated_identity_credentials = local.federations[each.key]
  outbound_role_assignments      = merge(local.vault_grants[each.key], var.vault_tenant_id == var.target_tenant_id ? local.target_grants[each.key] : {})
  enable_telemetry               = false
  depends_on                     = [terraform_data.workload_rg_ready, module.backups, module.staging, module.state, terraform_data.federation_contract]
}
module "target_identities" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  for_each                       = { for identity, files in local.environments : identity => files if var.vault_tenant_id != var.target_tenant_id && identity != "mint" }
  source                         = "Azure/avm-res-managedidentity-userassignedidentity/azurerm"
  version                        = "0.6.0"
  providers                      = { azurerm = azurerm.target, azapi = azapi.target }
  name                           = "id-sf-${each.key}"
  location                       = var.targets[var.role_home_target].location
  resource_group_name            = azurerm_resource_group.target_identities[0].name
  federated_identity_credentials = local.federations[each.key]
  outbound_role_assignments = merge(local.target_grants[each.key], each.key == "plan" ? {
    trust_read = { role_definition_id_or_name = "Reader", scope = azurerm_resource_group.target_identities[0].id }
  } : {})
  enable_telemetry = false
  depends_on       = [terraform_data.workload_rg_ready, terraform_data.federation_contract]
}
resource "azurerm_role_assignment" "operator" {
  for_each = {
    backups = { scope = local.backups_scope, role = "Storage Blob Data Reader" }
    staging = { scope = local.staging_scope, role = "Storage Blob Data Contributor" }
  }
  principal_id         = var.operator_object_id
  scope                = each.value.scope
  role_definition_name = each.value.role
  depends_on           = [module.backups, module.staging]
}

# Twins must be outside the Contributor-controlled workload RG.
resource "azurerm_resource_group" "target_identities" {
  provider = azurerm.target
  count    = var.vault_tenant_id != var.target_tenant_id ? 1 : 0
  name     = "rg-${var.prefix}-trust"
  location = var.targets[var.role_home_target].location
  lifecycle { prevent_destroy = true }
}

# Referencing the resolved module outputs also orders RG creation before scoped grants.
resource "terraform_data" "workload_rg_ready" {
  input = var.workload_resource_group_ids
  lifecycle {
    precondition {
      condition = alltrue([for key, target in var.targets :
        lookup(var.workload_resource_group_ids, key, "") == "${target.subscription_resource_id}/resourceGroups/${target.resource_group_name}"
      ])
      error_message = "Every workload RG must be created or read through its subscription provider; run render-trust.py and initialize host-owned RGs as Owner before scoped grants."
    }
  }
}

# Replacement would rotate role GUIDs and twin principal IDs. Refuse home edits in state.
resource "terraform_data" "trust_home" {
  triggers_replace = {
    key          = var.role_home_target
    subscription = var.targets[var.role_home_target].subscription_resource_id
    location     = var.targets[var.role_home_target].location
  }
  lifecycle { prevent_destroy = true }
}

resource "azurerm_role_definition" "migration_writer" {
  name  = "${var.prefix} SF Backup Migration Writer"
  scope = "/subscriptions/${var.vault_subscription_id}"
  permissions {
    actions = []
    data_actions = [
      "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read",
      "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action",
      "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write",
    ]
  }
}

# https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations
resource "terraform_data" "federation_contract" {
  lifecycle {
    precondition {
      condition     = toset(keys(local.allowlist.identities)) == toset(keys(local.target_roles))
      error_message = "Identity allowlist and target role table differ: ${join(", ", setunion(setsubtract(toset(keys(local.allowlist.identities)), toset(keys(local.target_roles))), setsubtract(toset(keys(local.target_roles)), toset(keys(local.allowlist.identities)))))}. Every identity requires an explicit role entry."
    }
    precondition {
      condition = alltrue([for identity, refs in var.workflow_refs :
        length(local.federations[identity]) == sum(concat([0], [for ref in values(refs) : length(lookup(local.environments[identity], basename(split("@", ref)[0]), []))]))
      if contains(keys(local.environments), identity)])
      error_message = "Credential keys collide after environment expansion; use distinct workflow_refs keys."
    }
  }
}
