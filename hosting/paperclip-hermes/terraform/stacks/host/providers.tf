provider "azurerm" {
  resource_provider_registrations = "none"
  features {}
  subscription_id     = split("/", var.subscription_resource_id)[2]
  storage_use_azuread = true
}
provider "azapi" {
  skip_provider_registration = true
  subscription_id            = split("/", var.subscription_resource_id)[2]
}
