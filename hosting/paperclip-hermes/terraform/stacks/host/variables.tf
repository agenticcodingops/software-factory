variable "name" {
  type        = string
  description = "Target host name."
}

variable "resource_group_name" {
  type        = string
  description = "Workload resource group."
}

variable "subscription_resource_id" {
  type        = string
  description = "Target subscription resource ID."
}

variable "location" {
  type        = string
  description = "Target region."
}

variable "rg_managed_by" {
  type        = string
  description = "trust references the pre-created RG; host creates it in subscription scope."
  default     = "trust"
}

variable "ssh_public_key" {
  type        = string
  description = "Operator public SSH key; a private key is never generated."
}

variable "bootstrap_custom_data" {
  type        = string
  description = "Receipt base64 cloud-init, reused byte for byte. Null renders the template and is allowed only for a new VM."
  default     = null
  nullable    = true
}

variable "bootstrap_render_for_new_vm" {
  type        = bool
  description = "True only after ARM confirms no VM exists; existing VMs must reuse receipt bytes."
  default     = false
}

variable "protocol_tags" {
  type        = map(string)
  description = "Current protocol tags and sf-maintenance-until read from ARM; absent on a new VM."
  default     = {}
}

variable "apps" {
  type        = set(string)
  description = "Installed app profiles."
  default     = ["paperclip", "hermes"]
}

variable "vm_size" {
  type        = string
  description = "Optional per-target size override."
  default     = null
}

variable "shutdown_time" {
  type        = string
  description = "Local shutdown HHMM."
  default     = "2200"
}

variable "backup_time" {
  type        = string
  description = "Local backup HHMM, at least 45 minutes before shutdown."
  default     = "2115"
}

variable "timezone" {
  type        = string
  description = "Azure shutdown schedule time zone."
  default     = "UTC"
}

variable "allow_disk_destroy" {
  type        = bool
  description = "Only a reviewed temporary rendered destroy configuration may enable this."
  default     = false
}

variable "parked" {
  type        = bool
  description = "Retain only the protected data disk when parked."
  default     = false
}

variable "tags" {
  type        = map(string)
  description = "Non-protocol resource tags."
  default     = {}
}
variable "deallocator_role_id" {
  type        = string
  description = "SF VM Deallocator role created by operator trust."
}

variable "operator_email" {
  type        = string
  description = "Private instance notification destination."
}

variable "tenant_id" {
  type        = string
  description = "Target tenant from private config."
}

variable "alert_application" {
  type        = object({ object_id = string, identifier_uri = string, monitor_application_id = string })
  description = "Operator-provisioned secure webhook Entra app and Azure Monitor sender; no URL credential."
}

variable "pilot_monthly_cap" {
  type        = number
  description = "Maximum pilot RG budget."
  default     = 35
}

variable "headroom" {
  type        = number
  description = "Credit minus other resources forecast."
}

variable "override_cost" {
  type        = bool
  description = "Approval-gated operator cost override."
  default     = false
}

variable "monthly_credit" {
  type        = number
  description = "Subscription credit."
}


variable "billing_month_verified" {
  type        = bool
  description = "Enable BillingMonth and subscription deallocation only after self-test 7."
  default     = false
}
