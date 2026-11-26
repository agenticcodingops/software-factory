
mock_provider "tailscale" {}
variables {
  policy                 = "{\"grants\": []}"
  operator_login         = "operator@example.com"
  isolation_destinations = ["tag:other:22"]
  settings               = { acls_externally_managed_on = false, https_enabled = false }
  issue_join_key         = true
}
run "reject_baseline_join" {
  command = plan
  module { source = "../../modules/tailnet" }
  variables { mode = "baseline" }
  expect_failures = [tailscale_tailnet_key.join]
}
run "reject_unapplied_hardening_join" {
  command = plan
  module { source = "../../modules/tailnet" }
  expect_failures = [tailscale_tailnet_key.join]
}
