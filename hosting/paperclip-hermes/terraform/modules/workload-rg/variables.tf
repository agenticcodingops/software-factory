variable "targets" {
  type        = map(object({ subscription_resource_id = string, resource_group_name = string, location = string, rg_managed_by = optional(string, "trust") }))
  description = "Targets in this provider's subscription."
}
