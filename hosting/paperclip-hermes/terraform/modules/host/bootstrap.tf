locals {
  forbidden_pattern = "(^|[^0-9a-fA-F])[0-9a-fA-F]{40}([^0-9a-fA-F]|$)|PRIVATE KEY|tskey-|sig=|kit_ref|kit_commit|(?i:age-secret-key-)"
  rendered_template = templatefile("${path.module}/bootstrap/cloud-init.yaml.tftpl", {
    agent_gz_b64 = base64gzip(file("${path.module}/bootstrap/sf-host-agent"))
  })
  rendered_custom_data  = base64encode(local.rendered_template)
  bootstrap_custom_data = var.bootstrap_custom_data != null ? var.bootstrap_custom_data : local.rendered_custom_data
  agent_sha256          = filesha256("${path.module}/bootstrap/sf-host-agent")
}
