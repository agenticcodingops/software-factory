locals {
  # HuJSON is consumed unchanged by the provider. Render tests as a reviewed patch output;
  # the operator merges it into the imported policy before enforcing apply.
  hardened = var.mode == "hardening"
  rendered_tests = {
    tagOwners = { "tag:pc-deployer" = [var.operator_login], "tag:pcpilot" = ["tag:pc-deployer"] }
    grants    = [{ src = [var.operator_login], dst = ["tag:pcpilot"], ip = ["tcp:22", "tcp:443", "tcp:9443"] }]
    ssh       = [{ action = "check", src = [var.operator_login], dst = ["tag:pcpilot"], users = ["admin"] }]
    tests = concat([
      { src = var.operator_login, accept = ["tag:pcpilot:22", "tag:pcpilot:443", "tag:pcpilot:9443"], deny = [] },
      { src = "autogroup:member", accept = [], deny = ["tag:pcpilot:22", "tag:pcpilot:443", "tag:pcpilot:9443"] }
    ], [{ src = "tag:pcpilot", accept = [], deny = var.isolation_destinations }])
    sshTests = [{ src = var.operator_login, dst = ["tag:pcpilot"], check = ["admin"] }]
  }
}
resource "tailscale_acl" "this" {
  acl                  = var.policy
  reset_acl_on_destroy = false
}
resource "tailscale_tailnet_settings" "this" {
  acls_externally_managed_on                  = local.hardened ? true : var.settings.acls_externally_managed_on
  acls_external_link                          = var.settings.acls_external_link
  devices_approval_on                         = var.settings.devices_approval_on
  devices_auto_updates_on                     = var.settings.devices_auto_updates_on
  devices_key_duration_days                   = var.settings.devices_key_duration_days
  users_approval_on                           = var.settings.users_approval_on
  users_role_allowed_to_join_external_tailnet = var.settings.users_role_allowed_to_join_external_tailnet
  posture_identity_collection_on              = var.settings.posture_identity_collection_on
  https_enabled                               = local.hardened ? true : var.settings.https_enabled
  network_flow_logging_on                     = var.settings.network_flow_logging_on
  route_selection                             = var.settings.route_selection
}
resource "tailscale_dns_preferences" "this" {
  magic_dns = var.magic_dns
}
resource "terraform_data" "join_generation" {
  triggers_replace = var.join_generation
}
resource "tailscale_tailnet_key" "join" {
  count               = var.issue_join_key ? 1 : 0
  reusable            = false
  ephemeral           = false
  preauthorized       = true
  expiry              = 3600
  tags                = ["tag:pcpilot"]
  recreate_if_invalid = "never"
  lifecycle {
    replace_triggered_by = [terraform_data.join_generation]
    precondition {
      condition     = local.hardened && var.hardening_applied
      error_message = "Baseline/report-only cannot authorize a host join; applied enforcing hardening is required."
    }
  }
}
