variable "prefix" {
  type        = string
  description = "Instance prefix; storage names must be globally unique."
  validation {
    condition     = can(regex("^[a-z0-9]{1,17}$", var.prefix))
    error_message = "Prefix must contain 1-17 lowercase alphanumeric characters."
  }
}

variable "location" {
  type        = string
  description = "Vault region."
}

variable "vault_subscription_id" {
  type        = string
  description = "Vault subscription; supplied only from private configuration."
}

variable "vault_tenant_id" {
  type        = string
  description = "Vault tenant; supplied only from private configuration."
}

variable "target_tenant_id" {
  type        = string
  description = "Tenant used by the target provider aliases."
}

variable "targets" {
  type        = map(object({ subscription_resource_id = string, resource_group_name = string, location = string, rg_managed_by = optional(string, "trust") }))
  description = "Named targets in the target tenant, including newly registered targets."
}

variable "operator_object_id" {
  type        = string
  description = "Operator object ID for restore-assist data access."
}

variable "state_account_name" {
  type        = string
  description = "Existing bootstrap-created state account to import."
}

variable "sub_claim_prefix" {
  type        = string
  description = "Immutable repo prefix returned by the GitHub API, never synthesized from names."
}

variable "workflow_refs" {
  type        = map(map(string))
  description = "Owner-authorized exact reusable workflow refs per identity, at full commit SHAs; empty means no federation."
  default     = {}
  validation {
    condition     = alltrue(flatten([for identity, refs in var.workflow_refs : [for ref in values(refs) : can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[.]github/workflows/[A-Za-z0-9_-]+[.]ya?ml@[0-9a-f]{40}$", ref))]])) && alltrue([for refs in values(var.workflow_refs) : length(refs) <= 20]) && length(setsubtract(toset(keys(var.workflow_refs)), toset(["plan", "deploy", "ops", "restore"]))) == 0
    error_message = "Federation requires exact reusable workflow paths at full SHAs, known identities and at most 20 credentials per identity."
  }

}

variable "custody" {
  type        = string
  description = "Operator custody by default; vault permits gated secret reads."
  default     = "operator"
}

variable "pilot_end_date" {
  type        = string
  description = "RFC3339 pilot end; retention is Locked after this date."
}

variable "container_retention_fallback" {
  type        = bool
  description = "Enable only if self-test 3 shows account retention is not inherited."
  default     = false
}

variable "role_home_target" {
  type        = string
  description = "Immutable registered target that anchors target roles and tenant twins."
  validation {
    condition     = contains(keys(var.targets), var.role_home_target)
    error_message = "The trust home must remain a registered target."
  }
}

variable "target_client_id" {
  type        = string
  default     = null
  description = "Target-tenant OIDC client ID; use the plan twin for cross-tenant drift."
}
