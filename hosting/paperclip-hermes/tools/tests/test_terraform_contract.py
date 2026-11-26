"""Pin, renderer and mock-plan checks that complement Terraform's runtime assertions."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest
import uuid

from test_terraform_guards import module
from trust_fixture import targets as trust_targets

BASE = Path(__file__).resolve().parents[2] / "terraform"
renderer = module("render-host")
trust_renderer = module("render-trust")
inspector = module("check-mock-plans")
budget_alerts = module("check-budget-alerts")


class PinTests(unittest.TestCase):
    def test_vnet_depends_on_workload_resource_group(self):
        text = (BASE / "modules/host/main.tf").read_text()
        network = text.split('module "network" {', 1)[1].split('\nmodule ', 1)[0]
        dependencies = re.search(r'depends_on\s*=\s*\[([^]]+)\]', network)
        self.assertIsNotNone(dependencies, "VNet creation must wait for its resource group, independently of subnet dependencies")
        self.assertIn("module.workload_rg", dependencies[1].split(", "))
        self.assertIn("data.azurerm_resource_group.workload", dependencies[1].split(", "))

    def test_every_adr_registry_module_and_root_provider_pin(self):
        required = {"resources-resourcegroup": "0.5.0", "managedidentity-userassignedidentity": "0.6.0", "storage-storageaccount": "0.11.0", "network-virtualnetwork": "0.23.0", "network-networksecuritygroup": "0.7.0", "network-publicipaddress": "0.3.0", "compute-virtualmachine": "0.22.1", "insights-actiongroup": "0.3.0", "logic-workflow": "0.2.0"}
        found = {}
        for path in (BASE / "modules").rglob("*.tf"):
            for source, version in re.findall(r'source\s*=\s*"Azure/avm-res-([^/]+)/azurerm"\s+version\s*=\s*"([^"]+)"', path.read_text()):
                self.assertEqual(version, required[source])
                found[source] = version
        self.assertEqual(found, required)
        pins = {"azurerm": "4.81.0", "azapi": "2.14.0", "random": "3.9.1", "time": "0.14.2", "tls": "4.4.2", "tailscale": "0.30.0"}
        for stack in (BASE / "stacks").iterdir():
            text = (stack / "versions.tf").read_text()
            self.assertIn('required_version = "= 1.17.2"', text)
            for name, version in re.findall(r'(\w+)\s*=\s*\{\s*source\s*=\s*"[^\"]+"\s*version\s*=\s*"= ([^\"]+)"', text):
                self.assertEqual(version, pins[name])
            self.assertIn("use_oidc         = true", text)
            self.assertIn("use_azuread_auth = true", text)
        for path in (BASE / "modules").glob("*/versions.tf"):
            for version in re.findall(r'version\s*=\s*"([^\"]+)"', path.read_text()):
                self.assertIn("<", version)

    def test_protection_and_join_generation_are_literal_and_independent(self):
        text = (BASE / "modules/host/main.tf").read_text()
        self.assertIn("prevent_destroy = true", text)
        self.assertIn("generate_admin_password_or_ssh_key = false", text)
        self.assertNotIn("prevent_destroy = var.", text)
        text = (BASE / "modules/tailnet/main.tf").read_text()
        self.assertIn("replace_triggered_by = [terraform_data.join_generation]", text)
        self.assertNotIn("virtual_machine", text)
        self.assertIn("reset_acl_on_destroy = false", text)

    def test_ops_federation_has_no_environment_only_fallback(self):
        text = (BASE / "modules/trust/main.tf").read_text()
        self.assertIn(":job_workflow_ref:${ref}", text)
        self.assertNotIn('subject  = "${var.sub_claim_prefix}:environment:${env}"', text)
        # The candidate fixture is exercised against actual generated credentials by the CI plan inspector.
        self.assertIn("ops-unapproved-release.json", (BASE.parent / "tools/check-mock-plans.py").read_text())



class RendererTests(unittest.TestCase):
    def test_trust_composes_static_subscription_providers_and_moves(self):
        targets = trust_targets()
        for third in (False, True):
            if third:
                targets["gamma"] = {**targets["alpha"], "subscription_resource_id": f"/subscriptions/{uuid.uuid5(uuid.NAMESPACE_DNS, 'gamma.example.test')}", "rg_managed_by": "host"}
            with tempfile.TemporaryDirectory() as tmp:
                root = trust_renderer.render(BASE, Path(tmp) / "rendered", targets)
                config = json.loads((root / "workload-subscriptions.tf.json").read_text())
                self.assertEqual(len(config["provider"]["azapi"]), 3 if third else 2)
                for provider in config["provider"]["azapi"]:
                    name = "workload_rg_" + provider["alias"]
                    self.assertEqual(config["module"][name]["providers"]["azapi"], "azapi." + provider["alias"])
                    self.assertIn(provider["subscription_id"], config["module"][name]["targets"])
                self.assertEqual(len(config["moved"]), 2)
                self.assertIn('module.trust.module.workload_rg["alpha"]', [move["from"] for move in config["moved"]])
                overrides = json.loads((root / "composition_override.tf.json").read_text())
                self.assertIn("merge(module.workload_rg_subscription_", overrides["module"]["trust"]["workload_resource_group_ids"])
                self.assertFalse((root / ".terraform").exists())

    def test_trust_composition_rejects_empty_or_invalid_subscriptions(self):
        for targets in ({}, {"alpha": {"subscription_resource_id": "not-a-subscription"}}):
            with self.assertRaises(ValueError):
                trust_renderer.composition(targets)

    def receipt(self):
        return {"target": "alpha", "run_id": "7", "environment": "production", "approved": True, "confirm_destroy": "DESTROY alpha", "backup": {"target": "alpha", "vm_id": "/mock/vm", "blob": "backups/final", "version": "fixture", "ciphertext_sha256": "0" * 64, "recipient_fp": "fixture", "size": 42, "verified": True, "successful": True, "reason": "final", "writers_stopped": True, "completed_at": datetime.now(timezone.utc).isoformat()}}

    def test_gated_render_keeps_address_and_changes_only_two_literals(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "rendered"
            renderer.render(BASE, destination, True, self.receipt(), "alpha", "7")
            original = (BASE / "modules/host/main.tf").read_text()
            result = (destination / "modules/host/main.tf").read_text()
            self.assertEqual(result, original.replace("prevent_destroy = true", "prevent_destroy = false").replace("disk_destroy_enabled = false", "disk_destroy_enabled = true"))
            self.assertIn('resource "azurerm_managed_disk" "data"', result)
            self.assertFalse((destination / "stacks/host/.terraform").exists())
            self.assertIn("prevent_destroy = true", (BASE / "modules/host/main.tf").read_text())

    def test_reject_missing_wrong_target_wrong_run_stale_or_unverified_evidence(self):
        for key, value in (("approved", False), ("environment", "ops"), ("confirm_destroy", "DESTROY beta"), ("target", "beta"), ("run_id", "8")):
            receipt = self.receipt()
            receipt[key] = value
            with self.assertRaises(ValueError):
                renderer.authorize(receipt, "alpha", "7")
        for key, value in (("completed_at", "2000-01-01T00:00:00Z"), ("completed_at", "9999-01-01T00:00:00Z"), ("target", "beta"), ("verified", False), ("reason", "canary"), ("writers_stopped", False), ("ciphertext_sha256", "")):
            receipt = self.receipt()
            receipt["backup"][key] = value
            with self.assertRaises(ValueError):
                renderer.authorize(receipt, "alpha", "7")
        with self.assertRaises(ValueError):
            renderer.authorize({}, "alpha", "7")


class MockPlanTests(unittest.TestCase):
    def test_budget_payload_thresholds_respect_maintenance(self):
        text = (BASE / "modules/guardrails/main.tf").read_text()
        ratio = re.search(r'threshold_ratio\s*=\s*"(.*)"', text)[1]
        expression = re.search(r'expression\s*=\s*"(@.*)"', text)[1]
        expression = expression.replace('${local.threshold_ratio}', ratio).replace('${var.name}', 'fixture').replace('${split("/", var.subscription_resource_id)[2]}', '00000000-0000-0000-0000-000000000000')
        rendered = expression.replace('${var.billing_month_verified}', 'true')
        for kind, budget, amount, expected in (("rg", 5.03, 4.024, True), ("rg", 5.03, 4.02, True), ("rg", 35, 27.99, False), ("subscription", 100.17, 85.1445, True), ("subscription", 101.7, 86.44, True), ("subscription", 100, 84.99, False)):
            payload = {"schemaId": "AIP Budget Notification", "data": {"SubscriptionId": "00000000-0000-0000-0000-000000000000", "BudgetName": f"fixture-{kind}", "Budget": budget, "NotificationThresholdAmount": amount}}
            self.assertEqual(budget_alerts.evaluate(rendered, payload, {}), expected)

    def test_secret_resource_and_pem_rejections(self):
        for typ, after in (("tls_private_key", {}), ("random_password", {}), ("azurerm_storage_account", {}), ("terraform_data", {"private_key_pem": "planted"})):
            self.assertTrue(inspector.inspect_plan({"resource_changes": [{"type": typ, "change": {"after": after}}]}))
        self.assertFalse(inspector.inspect_plan({"resource_changes": []}))

    def test_network_intermediaries_and_inbound_are_rejected(self):
        for typ in ("azurerm_lb", "azurerm_lb_nat_rule", "azurerm_bastion_host", "azurerm_nat_gateway"):
            self.assertTrue(inspector.inspect_plan({"resource_changes": [{"type": typ, "change": {"after": {}}}]}))
        for kind, props in (("Microsoft.Network/loadBalancers", {}), ("Microsoft.Network/bastionHosts", {}), ("Microsoft.Network/networkSecurityGroups/securityRules", {"direction": "Inbound", "access": "Allow"}), ("Microsoft.Network/publicIPAddresses", {"publicIPAllocationMethod": "Dynamic"})):
            self.assertTrue(inspector.inspect_plan({"resource_changes": [{"type": "azapi_resource", "change": {"after": {"type": kind + "@2024-01-01", "body": {"properties": props}}}}]}))


if __name__ == "__main__":
    unittest.main()
