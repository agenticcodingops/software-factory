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
  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+) [A-Za-z0-9+/=]+", var.ssh_public_key))
    error_message = "Supply the operator public SSH key; private keys and generated credentials are forbidden."
  }

}

variable "bootstrap_custom_data" {
  type        = string
  description = "Original base64 creation-time cloud-init from the VM receipt; reuse byte-for-byte."
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

resource "terraform_data" "invariants" {
  input = var.bootstrap_custom_data
  lifecycle {
    precondition {
      condition     = contains(["trust", "host"], var.rg_managed_by)
      error_message = "rg_managed_by must be trust or host."
    }
    precondition {
      condition     = alltrue([for key in keys(var.protocol_tags) : contains(["sf-approved-sha", "sf-recovery-sha", "sf-recovery-until", "sf-delivery-recipient", "sf-maintenance-until"], key)]) && length(setintersection(toset(keys(var.tags)), toset(["sf-approved-sha", "sf-recovery-sha", "sf-recovery-until", "sf-delivery-recipient", "sf-maintenance-until", "expires"]))) == 0
      error_message = "Protocol tags must come only from the current ARM pass-through, never ordinary tags."
    }
    precondition {
      condition     = can(regex("^([01][0-9]|2[0-3])[0-5][0-9]$", var.shutdown_time)) && can(regex("^([01][0-9]|2[0-3])[0-5][0-9]$", var.backup_time)) && ((tonumber(substr(var.shutdown_time, 0, 2)) * 60 + tonumber(substr(var.shutdown_time, 2, 2)) - tonumber(substr(var.backup_time, 0, 2)) * 60 - tonumber(substr(var.backup_time, 2, 2)) + 1440) % 1440 >= 45)
      error_message = "Shutdown must be at least 45 minutes after backup."
    }
    precondition {
      condition     = length(var.apps) > 0 && length(setsubtract(var.apps, toset(["paperclip", "hermes"]))) == 0
      error_message = "Select paperclip, hermes or both."
    }
    precondition {
      condition     = can(base64decode(var.bootstrap_custom_data)) && length(regexall("[0-9a-f]{40}|PRIVATE KEY|tskey-|sig=|kit_ref|kit_commit", try(base64decode(var.bootstrap_custom_data), ""))) == 0
      error_message = "Creation-time cloud-init must contain neither secrets nor a release SHA."
    }
  }
}
