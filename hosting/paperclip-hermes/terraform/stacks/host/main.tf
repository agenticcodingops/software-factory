module "host" {
  source                   = "../../modules/host"
  name                     = var.name
  resource_group_name      = var.resource_group_name
  subscription_resource_id = var.subscription_resource_id
  location                 = var.location
  rg_managed_by            = var.rg_managed_by
  ssh_public_key           = var.ssh_public_key
  bootstrap_custom_data    = var.bootstrap_custom_data
  protocol_tags            = var.protocol_tags
  apps                     = var.apps
  vm_size                  = var.vm_size
  shutdown_time            = var.shutdown_time
  backup_time              = var.backup_time
  timezone                 = var.timezone
  allow_disk_destroy       = var.allow_disk_destroy
  parked                   = var.parked
  tags                     = var.tags
}
output "invariants" {
  description = "Invariants."
  value       = module.host.invariants
  sensitive   = true
}
output "disk" {
  description = "Disk."
  value       = module.host.disk
}
