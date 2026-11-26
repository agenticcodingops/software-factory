provider "azurerm" {
  features {}
  subscription_id     = var.vault_subscription_id
  tenant_id           = var.vault_tenant_id
  storage_use_azuread = true
}
provider "azurerm" {
  alias = "target"
  features {}
  subscription_id     = split("/", values(var.targets)[0].subscription_resource_id)[2]
  tenant_id           = var.target_tenant_id
  storage_use_azuread = true
}
provider "azapi" {
  subscription_id = var.vault_subscription_id
  tenant_id       = var.vault_tenant_id
}
provider "azapi" {
  alias           = "target"
  subscription_id = split("/", values(var.targets)[0].subscription_resource_id)[2]
  tenant_id       = var.target_tenant_id
}
