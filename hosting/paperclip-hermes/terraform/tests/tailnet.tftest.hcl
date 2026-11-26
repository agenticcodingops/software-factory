
mock_provider "tailscale" {}
variables {
  policy                 = "{\"grants\": []}"
  operator_login         = "operator@example.com"
  non_operator_login     = "user@example.com"
  isolation_destinations = ["tag:other:22", "operator@example.com:22", "sample-host:443"]
  settings               = { acls_externally_managed_on = false, https_enabled = false }
}
run "hardened_tailnet" {
  command = plan
  variables {
    issue_join_key    = true
    hardening_applied = true
  }
  assert {
    condition     = output.invariants.settings.acls_externally_managed_on && output.invariants.settings.https_enabled && output.invariants.dns.magic_dns && !output.invariants.acl.reset_acl_on_destroy
    error_message = "Hardening locks policy editor, enables HTTPS/MagicDNS and never resets policy on destroy."
  }
  assert {
    condition     = !output.invariants.keys[0].reusable && !output.invariants.keys[0].ephemeral && output.invariants.keys[0].preauthorized && output.invariants.keys[0].expiry == 3600 && output.invariants.keys[0].recreate_if_invalid == "never" && output.invariants.keys[0].tags == toset(["tag:pcpilot"])
    error_message = "Join key must be single-use, preauthorized and one-hour, never automatically reminted."
  }
  assert {
    condition     = output.reviewed_fragment.tests[1].src == var.non_operator_login && output.reviewed_fragment.tests[0].src == var.operator_login && length(output.reviewed_fragment.tests[2].deny) == 3 && output.reviewed_fragment.ssh[0].action == "check" && output.reviewed_fragment.sshTests[0].check[0] == "admin" && output.reviewed_fragment.sshTests[0].dst[0] == "tag:pcpilot"
    error_message = "Render operator accept, other-user deny, outbound deny and SSH check tests."
  }
}
run "baseline_preserves_live_values" {
  command = plan
  variables { mode = "baseline" }
  assert {
    condition     = output.invariants.acl.acl == var.policy && !output.invariants.settings.acls_externally_managed_on && !output.invariants.settings.https_enabled && length(output.invariants.keys) == 0
    error_message = "Baseline copies policy/settings unchanged and creates no key."
  }
}

# Seed only the mock provider's state to represent the three imported live resources.
# The following complete plan is also passed through report-only lint by check-mock-plans.py.
run "baseline_mock_imports" {
  command = apply
  variables {
    mode   = "baseline"
    policy = "{\"grants\": [{\"src\": [\"*\"], \"dst\": [\"*\"], \"ip\": [\"*\"]}]}"
  }
}
run "baseline_no_diff" {
  command = plan
  variables {
    mode   = "baseline"
    policy = "{\"grants\": [{\"src\": [\"*\"], \"dst\": [\"*\"], \"ip\": [\"*\"]}]}"
  }
  assert {
    condition     = length(output.invariants.keys) == 0
    error_message = "The imported baseline must not create a join key."
  }
}

run "no_second_user_omits_deny_source" {
  command = plan
  variables { non_operator_login = null }
  assert {
    condition     = length(output.reviewed_fragment.tests) == 2 && output.reviewed_fragment.tests[0].src == var.operator_login && output.reviewed_fragment.tests[1].src == "tag:pcpilot"
    error_message = "Single-user tailnets must omit the non-operator test, never use an autogroup."
  }
}
run "reject_operator_as_deny_source" {
  command = plan
  variables { non_operator_login = "operator@example.com" }
  expect_failures = [var.non_operator_login]
}
run "reject_autogroup_as_deny_source" {
  command = plan
  variables { non_operator_login = "autogroup:member" }
  expect_failures = [var.non_operator_login]
}
run "reject_wildcard_as_deny_source" {
  command = plan
  variables { non_operator_login = join("@", ["*", "example.com"]) }
  expect_failures = [var.non_operator_login]
}
