terraform {
  required_version = "= 1.17.2"
  required_providers {
    tailscale = {
      source  = "tailscale/tailscale"
      version = "= 0.30.0"
    }
  }
  backend "azurerm" {
    use_oidc         = true
    use_azuread_auth = true
  }
}
