provider "azurerm" {
  resource_provider_registrations = "none"
  features {}
  subscription_id     = var.vault_subscription_id
  tenant_id           = var.vault_tenant_id
  storage_use_azuread = true
}
provider "azurerm" {
  resource_provider_registrations = "none"
  alias                           = "target"
  features {}
  subscription_id     = split("/", var.targets[var.role_home_target].subscription_resource_id)[2]
  tenant_id           = var.target_tenant_id
  client_id           = var.target_client_id
  storage_use_azuread = true
}
provider "azapi" {
  skip_provider_registration = true
  subscription_id            = var.vault_subscription_id
  tenant_id                  = var.vault_tenant_id
}
provider "azapi" {
  skip_provider_registration = true
  alias                      = "target"
  subscription_id            = split("/", var.targets[var.role_home_target].subscription_resource_id)[2]
  tenant_id                  = var.target_tenant_id
  client_id                  = var.target_client_id
}
