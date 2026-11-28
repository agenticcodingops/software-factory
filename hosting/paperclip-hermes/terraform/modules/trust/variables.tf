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
  validation {
    condition     = length(var.targets) > 0 && alltrue([for t in values(var.targets) : contains(["trust", "host"], t.rg_managed_by)])
    error_message = "Register at least one target with trust or host RG ownership."
  }

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
    condition = alltrue(flatten([for identity, refs in var.workflow_refs : [for ref in values(refs) :
      can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[.]github/workflows/[A-Za-z0-9_-]+[.]ya?ml@[0-9a-f]{40}$", ref)) && contains(["plan", "deploy", "ops", "restore", "mint"], identity)
    ]])) && length(setsubtract(toset(keys(var.workflow_refs)), toset(["plan", "deploy", "ops", "restore", "mint"]))) == 0
    error_message = format("Rejected identity/file: %s.", join(", ", concat(
      flatten([for identity, refs in var.workflow_refs : [for ref in values(refs) : "${identity}/${basename(split("@", ref)[0])}"
        if !can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[.]github/workflows/[A-Za-z0-9_-]+[.]ya?ml@[0-9a-f]{40}$", ref)) || !contains(["plan", "deploy", "ops", "restore", "mint"], identity)
      ]]),
      [for identity, refs in var.workflow_refs : "${identity}/<no refs>" if length(refs) == 0 && !contains(["plan", "deploy", "ops", "restore", "mint"], identity)]
    )))
  }
  validation {
    condition     = alltrue([for refs in values(var.workflow_refs) : length(refs) <= 20])
    error_message = format("Over 20 requested refs for identities: %s.", join(", ", [for identity, refs in var.workflow_refs : identity if length(refs) > 20]))
  }
  validation {
    condition = alltrue(flatten([for identity, refs in var.workflow_refs : [
      for ref in values(refs) : contains(keys(lookup(jsondecode(file("${path.module}/federation-allowlist.json")).identities, identity, {})), basename(split("@", ref)[0]))
    ]]))
    error_message = format("Rejected identity/file: %s.", join(", ", flatten([for identity, refs in var.workflow_refs : [for ref in values(refs) : "${identity}/${basename(split("@", ref)[0])}" if !contains(keys(lookup(jsondecode(file("${path.module}/federation-allowlist.json")).identities, identity, {})), basename(split("@", ref)[0]))]])))
  }
  validation {
    condition = alltrue([for identity, refs in var.workflow_refs :
      sum(concat([0], [for ref in values(refs) : length(lookup(lookup(jsondecode(file("${path.module}/federation-allowlist.json")).identities, identity, {}), basename(split("@", ref)[0]), []))])) <= 20
    ])
    error_message = format("Expanded credentials exceed 20 for identities: %s; prune obsolete SHAs after handoff.", join(", ", [for identity, refs in var.workflow_refs : identity
      if sum(concat([0], [for ref in values(refs) : length(lookup(lookup(jsondecode(file("${path.module}/federation-allowlist.json")).identities, identity, {}), basename(split("@", ref)[0]), []))])) > 20
    ]))
  }
  validation {
    condition = alltrue([for refs in values(var.workflow_refs) : length(values(refs)) == length(distinct(values(refs)))])
    error_message = format("Duplicate identity/file refs: %s.", join(", ", distinct(flatten([for identity, refs in var.workflow_refs : [for ref in distinct(values(refs)) : "${identity}/${basename(split("@", ref)[0])}"
      if length([for other in values(refs) : other if other == ref]) > 1
    ]]))))
  }
  validation {
    condition = alltrue([for refs in values(var.workflow_refs) :
      length(distinct([for ref in values(refs) : split("/.github/", ref)[0]])) <= 1
      ]) && length(distinct(flatten([for identity, refs in var.workflow_refs :
        [for ref in values(refs) : split("/.github/", ref)[0]] if contains(["ops", "mint", "deploy"], identity)
    ]))) <= 1
    error_message = "Each identity's refs must share one owner/repo; ops, mint and deploy must share the same source repository."
  }



}

variable "custody" {
  type        = string
  description = "Operator custody by default; vault permits gated secret reads."
  default     = "operator"
  validation {
    condition     = contains(["operator", "vault"], var.custody)
    error_message = "Custody must be operator or vault."
  }

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

variable "workload_resource_group_ids" {
  type        = map(string)
  description = "Created or read workload RG IDs from subscription-specific root modules."
}

variable "role_home_target" {
  type        = string
  description = "Immutable registered target that anchors target roles and tenant twins."
  validation {
    condition     = contains(keys(var.targets), var.role_home_target)
    error_message = "The trust home must remain a registered target."
  }
}
