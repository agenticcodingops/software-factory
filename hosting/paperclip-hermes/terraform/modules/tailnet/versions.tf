terraform {
  required_version = ">= 1.16.0, < 2.0.0"
  required_providers {
    tailscale = {
      source  = "tailscale/tailscale"
      version = ">= 0.30.0, < 1.0.0"
    }
  }
}
