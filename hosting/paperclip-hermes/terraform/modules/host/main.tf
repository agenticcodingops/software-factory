locals {
  rg_id                = "${var.subscription_resource_id}/resourceGroups/${var.resource_group_name}"
  vm_tags              = merge(var.tags, { expires = time_offset.expires.rfc3339 }, var.protocol_tags)
  disk_destroy_enabled = false # render-host.py changes this and the literal lifecycle together.
}
data "azurerm_resource_group" "workload" {
  count = var.rg_managed_by == "trust" ? 1 : 0
  name  = var.resource_group_name
}
module "workload_rg" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  count            = var.rg_managed_by == "host" ? 1 : 0
  source           = "Azure/avm-res-resources-resourcegroup/azurerm"
  version          = "0.5.0"
  name             = var.resource_group_name
  location         = var.location
  enable_telemetry = false
}
resource "time_static" "created" {}
resource "time_offset" "expires" {
  base_rfc3339 = time_static.created.rfc3339
  offset_days  = 30
}
# AVM disk 0.5.0 is Available but has no prevent_destroy interface. Use the ADR fallback.
resource "azurerm_managed_disk" "data" {
  #checkov:skip=CKV_AZURE_93:ADR uses platform encryption plus VM encryption-at-host; no customer-managed disk key is required.
  name                          = "${var.name}-data"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  storage_account_type          = "StandardSSD_LRS"
  create_option                 = "Empty"
  disk_size_gb                  = 64
  public_network_access_enabled = false
  network_access_policy         = "DenyAll"
  tags                          = var.tags
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = var.allow_disk_destroy == local.disk_destroy_enabled
      error_message = "Use the reviewed host renderer; a variable cannot disable literal disk protection."
    }
  }
  depends_on = [module.workload_rg, data.azurerm_resource_group.workload]
}
module "nsg" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  count     = var.parked ? 0 : 1
  source    = "Azure/avm-res-network-networksecuritygroup/azurerm"
  version   = "0.7.0"
  parent_id = local.rg_id
  location  = var.location
  name      = "${var.name}-nsg"
  security_rules = {
    deny_inbound = {
      name                       = "DenyAllInbound"
      access                     = "Deny"
      direction                  = "Inbound"
      priority                   = 100
      protocol                   = "*"
      source_address_prefix      = "*"
      destination_address_prefix = "*"
      source_port_range          = "*"
      destination_port_range     = "*"
    }
  }
  enable_telemetry = false
  depends_on       = [module.workload_rg, data.azurerm_resource_group.workload]
}
module "network" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  count         = var.parked ? 0 : 1
  source        = "Azure/avm-res-network-virtualnetwork/azurerm"
  version       = "0.23.0"
  name          = "${var.name}-vnet"
  parent_id     = local.rg_id
  location      = var.location
  address_space = ["10.42.0.0/24"]
  subnets = {
    host = {
      name                            = "host"
      address_prefixes                = ["10.42.0.0/27"]
      default_outbound_access_enabled = false
      network_security_group          = { id = module.nsg[0].resource_id }
    }
  }
  enable_telemetry = false
  depends_on       = [module.workload_rg, data.azurerm_resource_group.workload]
}
module "public_ip" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  count             = var.parked ? 0 : 1
  source            = "Azure/avm-res-network-publicipaddress/azurerm"
  version           = "0.3.0"
  parent_id         = local.rg_id
  location          = var.location
  name              = "${var.name}-egress"
  allocation_method = "Static"
  ip_version        = "IPv4"
  sku               = "Standard"
  enable_telemetry  = false
  depends_on        = [module.workload_rg, data.azurerm_resource_group.workload]
}
module "vm" {
  #checkov:skip=CKV_TF_1:ADR 0001 requires exact registry releases; test_terraform_contract.py verifies every source/version.
  count                  = var.parked ? 0 : 1
  source                 = "Azure/avm-res-compute-virtualmachine/azurerm"
  version                = "0.22.1"
  name                   = var.name
  parent_id              = local.rg_id
  resource_group_name    = var.resource_group_name
  location               = var.location
  zone                   = null
  os_type                = "Linux"
  sku_size               = coalesce(var.vm_size, length(var.apps) == 2 ? "Standard_B4as_v2" : "Standard_B2as_v2")
  source_image_reference = { publisher = "Canonical", offer = "ubuntu-24_04-lts", sku = "server", version = "latest" }
  account_credentials = {
    admin_credentials                = { username = "sfadmin", ssh_keys = [var.ssh_public_key], generate_admin_password_or_ssh_key = false }
    password_authentication_disabled = true
  }
  managed_identities         = { system_assigned = false, user_assigned_resource_ids = [] }
  custom_data                = var.bootstrap_custom_data
  boot_diagnostics           = true
  secure_boot_enabled        = true
  vtpm_enabled               = true
  encryption_at_host_enabled = true
  os_disk                    = { caching = "ReadWrite", storage_account_type = "StandardSSD_LRS", delete_option = "Delete", network_access_policy = "DenyAll", public_network_access_enabled = false }
  data_disk_existing_disks = {
    data = { caching = "None", managed_disk_resource_id = azurerm_managed_disk.data.id, lun = 0 }
  }
  network_interfaces = {
    host = {
      name                    = "${var.name}-nic"
      is_primary              = true
      network_security_groups = { host = { network_security_group_resource_id = module.nsg[0].resource_id } }
      ip_configurations = {
        primary = {
          name                          = "primary"
          private_ip_subnet_resource_id = "${local.rg_id}/providers/Microsoft.Network/virtualNetworks/${var.name}-vnet/subnets/host"
          public_ip_address_resource_id = module.public_ip[0].resource_id
        }
      }
    }
  }
  shutdown_schedules = { daily = { daily_recurrence_time = var.shutdown_time, timezone = var.timezone } }
  tags               = local.vm_tags
  enable_telemetry   = false
  depends_on         = [module.network]
}
