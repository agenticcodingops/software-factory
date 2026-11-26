module "tailnet" {
  source                 = "../../modules/tailnet"
  policy                 = var.policy
  operator_login         = var.operator_login
  mode                   = var.mode
  settings               = var.settings
  magic_dns              = var.magic_dns
  join_generation        = var.join_generation
  issue_join_key         = var.issue_join_key
  hardening_applied      = var.hardening_applied
  isolation_destinations = var.isolation_destinations
}
output "invariants" {
  description = "Invariants."
  value       = module.tailnet.invariants
  sensitive   = true
}
output "reviewed_fragment" {
  description = "Reviewed fragment."
  value       = module.tailnet.reviewed_fragment
}
output "join_key" {
  description = "Join key."
  value       = module.tailnet.join_key
  sensitive   = true
}
