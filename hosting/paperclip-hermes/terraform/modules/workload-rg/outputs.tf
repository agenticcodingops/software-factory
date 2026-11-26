output "resource_ids" {
  description = "Created or read workload resource groups, including the subscription selected by the provider."
  value       = merge({ for key, rg in module.workload_rg : key => rg.resource_id }, { for key, rg in data.azapi_resource.host_owned : key => rg.id })
}
output "resources" {
  description = "Resolved resource inputs for subscription-boundary regression checks."
  value       = { for key, rg in module.workload_rg : key => rg.resource }
}
