terraform {
  required_version = ">= 1.16.0, < 2.0.0"
  required_providers {
    azurerm = {
      source                = "hashicorp/azurerm"
      version               = ">= 4.81.0, < 5.0.0"
      configuration_aliases = [azurerm.target]
    }
    azapi = {
      source                = "Azure/azapi"
      version               = ">= 2.12.0, < 3.0.0"
      configuration_aliases = [azapi.target]
    }
  }
}
