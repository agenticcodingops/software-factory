output "resource_ids" {
  description = "Created workload resource groups, including the subscription selected by the provider."
  value       = { for key, rg in module.workload_rg : key => rg.resource_id }
}
output "resources" {
  description = "Resolved resource inputs for subscription-boundary regression checks."
  value       = { for key, rg in module.workload_rg : key => rg.resource }
}
