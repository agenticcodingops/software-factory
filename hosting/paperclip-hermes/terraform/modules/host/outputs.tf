output "resource_id" {
  description = "Host ID, null when parked."
  value       = var.parked ? null : module.vm[0].resource_id
}
output "disk" {
  description = "Retained data disk. Its address never changes when rendering protection."
  value       = { resource_id = azurerm_managed_disk.data.id, protection_enabled = !local.disk_destroy_enabled }
}
output "invariants" {
  description = "Actual AVM resources used by mock-provider assertions."
  sensitive   = true
  value = {
    vm                    = var.parked ? null : module.vm[0].resource
    nic                   = var.parked ? {} : module.vm[0].network_interfaces_azapi
    nsg                   = var.parked ? null : module.nsg[0].security_rules
    public_ip             = var.parked ? null : module.public_ip[0].resource_id
    custom_data           = local.bootstrap_custom_data
    agent_sha256          = local.agent_sha256
    tags                  = local.vm_tags
    expires_offset_days   = time_offset.expires.offset_days
    expires_base          = time_static.created.rfc3339
    shutdown              = var.shutdown_time
    backup                = var.backup_time
    generated_private_key = var.parked ? null : module.vm[0].admin_generated_ssh_private_key
  }
}
