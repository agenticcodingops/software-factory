locals {
  rg_budget = max(5, var.override_cost ? var.pilot_monthly_cap : min(var.pilot_monthly_cap, var.headroom))
  # Unsigned URL: the secure webhook authenticates with Entra, never a SAS in state.
  callback_url = "${data.azapi_resource.endpoint.output.properties.accessEndpoint}/triggers/manual/paths/invoke?api-version=2016-10-01"
}
module "deallocator" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  source              = "Azure/avm-res-logic-workflow/azurerm"
  version             = "0.2.0"
  name                = "${var.name}-deallocator"
  location            = var.location
  resource_group_id   = var.resource_group_id
  resource_group_name = var.resource_group_name
  managed_identities  = { system_assigned = true }
  access_control = {
    triggers = { openAuthenticationPolicies = { policies = {
      monitor = { type = "AAD", claims = [
        { name = "iss", value = "https://sts.windows.net/${var.tenant_id}/" },
        { name = "aud", value = var.alert_application.identifier_uri },
        { name = "appid", value = var.alert_application.monitor_application_id }
      ] }
    } } }
  }
  logic_app_definition = {
    "$schema"      = "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#"
    contentVersion = "1.0.0.0"
    parameters     = {}
    triggers       = { manual = { type = "Request", kind = "Http", inputs = { schema = { type = "object" } } } }
    actions = {
      Read_VM = {
        type     = "Http"
        inputs   = { method = "GET", uri = "https://management.azure.com${var.vm_resource_id}?api-version=2024-07-01", authentication = { type = "ManagedServiceIdentity" } }
        runAfter = {}
      }
      Threshold_and_maintenance = {
        type       = "If"
        expression = "@and(equals(triggerBody()?['schemaId'], 'AIP Budget Notification'), equals(triggerBody()?['data']?['SubscriptionId'], '${split("/", var.subscription_resource_id)[2]}'), or(and(equals(triggerBody()?['data']?['BudgetName'], '${var.name}-rg'), greaterOrEquals(float(triggerBody()?['data']?['NotificationThresholdAmount']), 0.8)), and(${var.billing_month_verified}, equals(triggerBody()?['data']?['BudgetName'], '${var.name}-subscription'), greaterOrEquals(float(triggerBody()?['data']?['NotificationThresholdAmount']), 0.85))), or(empty(body('Read_VM')?['tags']?['sf-maintenance-until']), lessOrEquals(ticks(coalesce(body('Read_VM')?['tags']?['sf-maintenance-until'], '1970-01-01T00:00:00Z')), ticks(utcNow())), greaterOrEquals(float(triggerBody()?['data']?['NotificationThresholdAmount']), 1)))"
        actions = {
          Deallocate = { type = "Http", inputs = { method = "POST", uri = "https://management.azure.com${var.vm_resource_id}/deallocate?api-version=2024-07-01", authentication = { type = "ManagedServiceIdentity" } }, runAfter = {} }
        }
        else     = { actions = {} }
        runAfter = { Read_VM = ["Succeeded"] }
      }
    }
    outputs = {}
  }
  enable_telemetry = false
}
resource "azurerm_role_assignment" "deallocator" {
  scope              = var.resource_group_id
  role_definition_id = var.deallocator_role_id
  principal_id       = module.deallocator.resource.identity[0].principal_id
  principal_type     = "ServicePrincipal"
}
module "action_group" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  for_each        = toset(["email", "deallocate"])
  source          = "Azure/avm-res-insights-actiongroup/azurerm"
  version         = "0.3.0"
  parent_id       = var.resource_group_id
  location        = "global"
  name            = "${var.name}-${each.key}"
  short_name      = each.key
  email_receivers = each.key == "email" ? { operator = { name = "operator", email_address = var.operator_email } } : {}
  webhook_receivers = each.key == "deallocate" ? { deallocator = {
    name           = "deallocator"
    service_uri    = local.callback_url
    use_aad_auth   = true
    object_id      = var.alert_application.object_id
    identifier_uri = var.alert_application.identifier_uri
    tenant_id      = var.tenant_id
  } } : {}
  enable_telemetry = false
}
resource "azurerm_consumption_budget_resource_group" "pilot" {
  name              = "${var.name}-rg"
  resource_group_id = var.resource_group_id
  amount            = local.rg_budget
  time_grain        = "Monthly"
  time_period { start_date = var.budget_start }
  dynamic "notification" {
    for_each = { actual50 = { threshold = 50, type = "Actual", action = "email" }, actual80 = { threshold = 80, type = "Actual", action = "deallocate" }, forecast100 = { threshold = 100, type = "Forecasted", action = "email" } }
    content {
      enabled        = true
      threshold      = notification.value.threshold
      threshold_type = notification.value.type
      operator       = "GreaterThanOrEqualTo"
      contact_groups = [module.action_group[notification.value.action].resource_id]
    }
  }
}
resource "azurerm_consumption_budget_subscription" "credit" {
  name            = "${var.name}-subscription"
  subscription_id = var.subscription_resource_id
  amount          = var.monthly_credit
  time_grain      = var.billing_month_verified ? "BillingMonth" : "Monthly"
  time_period { start_date = var.budget_start }
  dynamic "notification" {
    for_each = { actual70 = { threshold = 70, type = "Actual", action = "email" }, actual85 = { threshold = 85, type = "Actual", action = var.billing_month_verified ? "deallocate" : "email" }, forecast100 = { threshold = 100, type = "Forecasted", action = "email" } }
    content {
      enabled        = true
      threshold      = notification.value.threshold
      threshold_type = notification.value.type
      operator       = "GreaterThanOrEqualTo"
      contact_groups = [module.action_group[notification.value.action].resource_id]
    }
  }
}

# Read the unsigned runtime endpoint, never listCallbackUrl (which returns a SAS).
data "azapi_resource" "endpoint" {
  type                   = "Microsoft.Logic/workflows@2019-05-01"
  resource_id            = module.deallocator.resource_id
  response_export_values = ["properties.accessEndpoint"]
}
