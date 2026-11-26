variable "policy" {
  type        = string
  description = "Complete imported HuJSON policy, never a replacement fragment."
}

variable "operator_login" {
  type        = string
  description = "Operator login from private configuration."
}

variable "mode" {
  type        = string
  description = "baseline preserves imported policy and settings, for read-only no-diff planning only."
  default     = "hardening"
}

variable "settings" {
  type        = object({ acls_externally_managed_on = optional(bool), acls_external_link = optional(string), devices_approval_on = optional(bool), devices_auto_updates_on = optional(bool), devices_key_duration_days = optional(number), users_approval_on = optional(bool), users_role_allowed_to_join_external_tailnet = optional(string), posture_identity_collection_on = optional(bool), https_enabled = optional(bool), network_flow_logging_on = optional(bool), route_selection = optional(string) })
  description = "All live imported settings; hardening changes only external policy management and HTTPS."
}

variable "magic_dns" {
  type        = bool
  description = "MagicDNS preference copied from live state for baseline."
  default     = true
}

variable "join_generation" {
  type        = number
  description = "Explicit replacement generation independent of the VM."
  default     = 0
}

variable "issue_join_key" {
  type        = bool
  description = "Enable only after reviewed enforcing hardening was applied."
  default     = false
}

variable "hardening_applied" {
  type        = bool
  description = "Verified by approved workflow from applied policy hash receipt, never report-only."
  default     = false
}

variable "isolation_destinations" {
  type        = list(string)
  description = "Sample every other tag, user and host:port in the imported policy for rendered deny tests."
  validation {
    condition     = length(var.isolation_destinations) > 0
    error_message = "Isolation tests must cover the imported policy's other destinations."
  }
}
