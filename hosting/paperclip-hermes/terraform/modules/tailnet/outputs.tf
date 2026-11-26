output "reviewed_fragment" {
  description = "Merge into the imported policy in the separate operator-reviewed hardening PR."
  value       = local.rendered_tests
}
output "invariants" {
  description = "Actual policy, settings and join-key configuration."
  sensitive   = true
  value       = { acl = tailscale_acl.this, settings = tailscale_tailnet_settings.this, dns = tailscale_dns_preferences.this, keys = tailscale_tailnet_key.join }
}
output "join_key" {
  description = "Single-use one-hour key; deliver only as envelope ciphertext, never custom_data."
  value       = var.issue_join_key ? tailscale_tailnet_key.join[0].key : null
  sensitive   = true
}
