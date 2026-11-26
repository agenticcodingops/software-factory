provider "azurerm" {
  features {}
  subscription_id     = split("/", var.subscription_resource_id)[2]
  storage_use_azuread = true
}
provider "azapi" {
  subscription_id = split("/", var.subscription_resource_id)[2]
}
