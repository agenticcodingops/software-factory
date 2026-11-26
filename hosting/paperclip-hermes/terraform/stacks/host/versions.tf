terraform {
  required_version = "= 1.17.2"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "= 4.81.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "= 2.14.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "= 3.9.1"
    }
    time = {
      source  = "hashicorp/time"
      version = "= 0.14.2"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "= 4.4.2"
    }
  }
  backend "azurerm" {
    use_oidc         = true
    use_azuread_auth = true
  }
}
