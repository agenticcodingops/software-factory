variable "name" {
  type        = string
  description = "Target name."
}

variable "location" {
  type        = string
  description = "Target region."
}

variable "resource_group_id" {
  type        = string
  description = "Target RG resource ID."
}

variable "resource_group_name" {
  type        = string
  description = "Target RG name."
}

variable "vm_resource_id" {
  type        = string
  description = "Only VM the Logic App may deallocate."
}

variable "subscription_resource_id" {
  type        = string
  description = "Target subscription resource ID."
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

variable "budget_start" {
  type        = string
  description = "Budget start date in RFC3339."
}

variable "billing_month_verified" {
  type        = bool
  description = "Enable BillingMonth and subscription deallocation only after self-test 7."
  default     = false
}
