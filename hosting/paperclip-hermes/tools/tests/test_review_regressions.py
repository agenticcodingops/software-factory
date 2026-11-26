"""Adversarial regressions for PR 18 review and the full Terraform self-audit."""

import copy
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

from test_terraform_guards import module, plan, policy
from test_terraform_contract import BASE, budget_alerts, renderer, trust_renderer
import test_terraform_contract as contract
from trust_fixture import targets

worm = module("lock-backup-versions")


class ReviewTests(unittest.TestCase):
    def test_scoped_workflows_cannot_implicitly_register_resource_providers(self):
        for path in (BASE / "stacks").glob("*/providers.tf"):
            for provider, body in re.findall(r'provider "(azurerm|azapi)" \{(.*?)(?=\n\})', path.read_text(), re.S):
                setting = r'resource_provider_registrations\s*=\s*"none"' if provider == "azurerm" else r'skip_provider_registration\s*=\s*true'
                self.assertRegex(body, setting)
        for provider in trust_renderer.composition(targets())["provider"]["azapi"]:
            self.assertIs(provider["skip_provider_registration"], True)

    def test_bootstrap_parent_is_imported_with_children(self):
        text = (BASE / "stacks/trust/main.tf").read_text()
        imports = re.findall(r'import\s*\{\s*to\s*=\s*([^\n]+)\s+id\s*=\s*"([^\n]+)"', text)
        adopted = dict(imports)
        self.assertEqual(adopted["module.trust.azurerm_resource_group.vault"], "/subscriptions/${var.vault_subscription_id}/resourceGroups/rg-${var.prefix}-vault")
        self.assertEqual(len(adopted), 4)
        self.assertTrue(all(adopted["module.trust.azurerm_resource_group.vault"] in resource_id for resource_id in adopted.values()))

    def test_role_names_are_namespaced_but_assignments_use_ids(self):
        text = (BASE / "modules/trust/main.tf").read_text()
        for name in ("target", "vault"):
            resource = text.split(f'resource "azurerm_role_definition" "{name}" {{')[1].split('\n}')[0]
            self.assertRegex(resource, r'name\s*=\s*"\$\{var.prefix\} \$\{each.value.name\}"')
        self.assertIn('azurerm_role_definition.target["host_operator"].role_definition_resource_id', text)
        self.assertIn('azurerm_role_definition.vault["backup_writer"].role_definition_resource_id', text)

    def test_rg_cascade_rejected_even_with_noop_disk_and_override(self):
        for typ, kind in (("azurerm_resource_group", None), ("azapi_resource", "Microsoft.Resources/resourceGroups@2024-03-01")):
            for actions in (["delete"], ["delete", "create"], ["create", "delete"], ["forget"]):
                resource = {"type": typ, "change": {"actions": actions, "before": {"name": "rg-fixture", "type": kind}, "after": None}}
                disk = {"type": "azurerm_managed_disk", "change": {"actions": ["no-op"], "before": {"resource_group_name": "rg-fixture"}, "after": {"resource_group_name": "rg-fixture"}}}
                with self.subTest(typ=typ, actions=actions):
                    self.assertTrue(plan.guard({"resource_changes": [resource, disk]}, True))

    def test_vm_cannot_delete_data_disk_via_arm_attachment(self):
        for unknown in (False, True):
            after = {"type": "Microsoft.Compute/virtualMachines@2024-07-01", "body": {"properties": {"storageProfile": {"dataDisks": [{"deleteOption": "Delete"}]}}}}
            change = {"actions": ["update"], "before": copy.deepcopy(after), "after": after}
            if unknown:
                after["body"]["properties"]["storageProfile"]["dataDisks"] = []
                change["after_unknown"] = {"body": {"properties": {"storageProfile": {"dataDisks": True}}}}
            self.assertTrue(plan.guard({"resource_changes": [{"type": "azapi_resource", "change": change}]}, True))

    def test_nested_deployments_cannot_hide_child_deletes(self):
        for typ in ("azurerm_resource_group_template_deployment", "azurerm_subscription_template_deployment", "azapi_resource"):
            for actions in (["create"], ["update"], ["delete"], ["delete", "create"], ["forget"]):
                body = {"type": "Microsoft.Resources/deployments@2025-04-01", "body": {"properties": {"mode": "Complete"}}}
                resource = {"type": typ, "change": {"actions": actions, "before": body, "after": body}}
                self.assertTrue(plan.guard({"resource_changes": [resource]}, True))

    def test_vm_delete_override_still_checks_existing_disk_delete_option(self):
        for typ in ("azapi_resource", "azurerm_virtual_machine"):
            before = {"type": "Microsoft.Compute/virtualMachines@2024-07-01", "delete_data_disks_on_termination": True, "body": {"properties": {"storageProfile": {"dataDisks": [{"deleteOption": "Delete"}]}}}}
            resource = {"type": typ, "change": {"actions": ["delete"], "before": before, "after": None}}
            self.assertTrue(plan.guard({"resource_changes": [resource]}, True))
            before["delete_data_disks_on_termination"] = False
            before["body"]["properties"]["storageProfile"]["dataDisks"][0]["deleteOption"] = "Detach"
            self.assertFalse(plan.guard({"resource_changes": [resource]}, True))

    def test_first_apply_unknown_disk_id_does_not_hide_known_detach(self):
        after = {"type": "Microsoft.Compute/virtualMachines@2024-07-01", "body": {"properties": {"storageProfile": {"dataDisks": [{"deleteOption": "Detach"}]}}}}
        unknown_disk = {"managedDisk": {"id": True}}
        resource = {"type": "azapi_resource", "change": {"actions": ["create"], "before": None, "after": after, "after_unknown": {"body": {"properties": {"storageProfile": {"dataDisks": [unknown_disk]}}}}}}
        self.assertFalse(plan.guard({"resource_changes": [resource]}))
        unknown_disk["deleteOption"] = True
        self.assertTrue(plan.guard({"resource_changes": [resource]}))

    def test_arm_type_change_cannot_hide_protected_before(self):
        resource = {"type": "azapi_resource", "change": {"actions": ["delete", "create"], "before": {"type": "Microsoft.Resources/resourceGroups@2024-03-01"}, "after": {"type": "Microsoft.Network/networkSecurityGroups@2024-01-01"}}}
        self.assertTrue(plan.guard({"resource_changes": [resource]}, True))

    def test_first_account_policy_must_be_unlocked(self):
        policy_body = {"enabled": True, "immutabilityPolicy": {"state": "Locked", "immutabilityPeriodSinceCreationInDays": 30}}
        after = {"type": "Microsoft.Storage/storageAccounts@2025-08-01", "body": {"properties": {"immutableStorageWithVersioning": policy_body}}}
        resource = {"type": "azapi_resource", "change": {"actions": ["create"], "before": None, "after": after}}
        self.assertTrue(plan.guard({"resource_changes": [resource]}))
        policy_body["immutabilityPolicy"]["state"] = "Unlocked"
        self.assertFalse(plan.guard({"resource_changes": [resource]}))

    def test_new_lifecycle_policy_cannot_delete_existing_versions_indirectly(self):
        resource = {"type": "azurerm_storage_management_policy", "change": {"actions": ["create"], "before": None, "after": {"storage_account_id": "/existing/account", "rule": [{"delete_after_days_since_creation": 1}]}}}
        self.assertTrue(plan.guard({"resource_changes": [resource]}, True))

    def test_host_owned_rg_read_orders_grants_without_another_owner(self):
        text = (BASE / "modules/workload-rg/main.tf").read_text()
        self.assertIn('data "azapi_resource" "host_owned"', text)
        self.assertIn('if v.rg_managed_by == "host"', text)
        self.assertIn('data.azapi_resource.host_owned', (BASE / "modules/workload-rg/outputs.tf").read_text())
        trust = (BASE / "modules/trust/main.tf").read_text()
        self.assertNotIn('target.rg_managed_by != "trust" ||', trust)
        self.assertIn('depends_on                     = [terraform_data.workload_rg_ready', trust)

    def test_live_maintenance_tag_is_preserved_and_cannot_be_injected(self):
        text = (BASE / "modules/host/variables.tf").read_text()
        self.assertIn('"sf-maintenance-until"', text)
        for key in plan.LIVE_TAGS:
            resource = {"type": "azurerm_linux_virtual_machine", "change": {"actions": ["update"], "before": {"tags": {key: "live"}}, "after": {"tags": {key: "live"}}}}
            self.assertFalse(plan.guard({"resource_changes": [resource]}))
            resource["change"]["after"]["tags"] = {}
            self.assertTrue(plan.guard({"resource_changes": [resource]}, True))

    def test_renderer_rejects_parent_and_file_symlinks_without_writing_source(self):
        for render in (renderer.render, lambda source, destination, *_: trust_renderer.render(source, destination, targets())):
            for parent in (True, False):
                with tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp)
                    source, outside, destination = [directory / name for name in ("source", "outside", "rendered")]
                    is_host = render is renderer.render
                    relative = Path("modules/host/main.tf" if is_host else "stacks/trust/composition_override.tf.json")
                    target = outside / relative
                    target.parent.mkdir(parents=True)
                    original = "prevent_destroy = true\ndisk_destroy_enabled = false\n"
                    target.write_text(original)
                    if parent:
                        (source / relative.parent.parent).mkdir(parents=True)
                        (source / relative.parent).symlink_to(target.parent, target_is_directory=True)
                    else:
                        (source / relative.parent).mkdir(parents=True)
                        (source / relative).symlink_to(target)
                    with self.assertRaises(ValueError):
                        render(source, destination, True, contract.RendererTests().receipt(), "alpha", "7")
                    self.assertEqual(target.read_text(), original)
                    self.assertFalse(destination.exists())

    def test_actual100_routes_and_recheck_observe_new_maintenance(self):
        text = (BASE / "modules/guardrails/main.tf").read_text()
        notifications = re.findall(r'actual100 = \{ threshold = 100, type = "Actual", action = ([^}]+)\}', text)
        self.assertEqual([item.strip() for item in notifications], ['"deallocate"', 'var.billing_month_verified ? "deallocate" : "email"'])
        expressions = re.findall(r'expression\s*=\s*"(@.*)"', text)
        ratio = re.search(r'threshold_ratio\s*=\s*"(.*)"', text)[1]
        expression = expressions[1].replace('${local.threshold_ratio}', ratio)
        payload = {"data": {"Budget": 35, "NotificationThresholdAmount": 28}}
        self.assertTrue(budget_alerts.evaluate(expression, payload, {}))
        self.assertFalse(budget_alerts.evaluate(expression, payload, {"sf-maintenance-until": "2026-11-26T14:00:00Z"}))
        payload["data"]["NotificationThresholdAmount"] = 35
        self.assertTrue(budget_alerts.evaluate(expression, payload, {"sf-maintenance-until": "2026-11-26T14:00:00Z"}))
        self.assertIn('runAfter = { Recheck_VM = ["Succeeded"] }', text)
        self.assertIn('retryPolicy = { type = "none" }', text)
        self.assertEqual(text.count('depends_on'), 2)


class SelectorAuditTests(unittest.TestCase):
    def test_ipsets_rejected_on_each_side_and_through_aliases(self):
        for section in ("grants", "acls", "ssh"):
            for side in ("src", "dst"):
                for alias in ("ipset:pilot", "alias", "group:alias"):
                    dst = "tag:pcpilot:22" if section == "acls" else "tag:pcpilot"
                    rule = {"src": ["operator@example.com"], "dst": [dst], "action": "check", "users": ["admin"]}
                    rule[side] = [alias + ":22" if side == "dst" and section == "acls" else alias]
                    p = {"grants": [], "ipsets": {"ipset:pilot": ["add 100.64.0.0/10"]}, "hosts": {"alias": "ipset:pilot"}, "groups": {"group:alias": ["alias"]}, section: [rule]}
                    with self.subTest(section=section, side=side, alias=alias), self.assertRaises(ValueError):
                        policy.findings(p, "operator@example.com")

    def test_broad_and_case_insensitive_selectors(self):
        for section in ("grants", "acls", "ssh"):
            for selector in ("autogroup:danger-all", "tag:PCPilot", "AUTOGROUP:TAGGED"):
                for side in ("src", "dst"):
                    rule = {"src": ["another@" + "example.com"], "dst": ["tag:other:22" if section == "acls" else "tag:other"], "action": "accept", "users": ["admin"]}
                    rule[side] = [selector + ":22" if side == "dst" and section == "acls" else selector]
                    self.assertTrue(policy.findings({"grants": [], section: [rule]}, "operator@example.com"))
        self.assertFalse(policy.findings({"grants": [{"src": ["operator@example.com"], "dst": ["tag:PCPilot"]}]}, "operator@example.com"))

    def test_unknown_selectors_and_alias_shadowing_fail_closed(self):
        for selector in ("ipset:unknown", "autogroup:future", "svc:unknown", "unknown-host"):
            with self.assertRaises(ValueError):
                policy.findings({"grants": [{"src": [selector], "dst": ["tag:other"]}]}, "operator@example.com")
        for key in ("tag:pcpilot", "autogroup:tagged", "100.64.0.1", "operator@example.com", "*"):
            with self.assertRaises(ValueError):
                policy.findings({"hosts": {key: "10.0.0.1"}, "grants": []}, "operator@example.com")
        self.assertTrue(policy.findings({}, "operator@example.com"))
        self.assertFalse(policy.findings({"grants": []}, "operator@example.com"))

    def test_incomplete_baseline_rejected(self):
        baseline = {"complete": False, "resource_changes": [
            {"type": "tailscale_acl", "change": {"actions": ["no-op"], "after": {"acl": "{}"}}},
            {"type": "tailscale_tailnet_settings", "change": {"actions": ["no-op"], "after": {}}},
        ]}
        self.assertTrue(policy.baseline_findings(baseline, "{}", "{}"))
        baseline["complete"] = True
        self.assertFalse(policy.baseline_findings(baseline, "{}", "{}"))


class WormClosureTests(unittest.TestCase):
    now = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
    expiry = datetime(2026, 12, 20, 12, tzinfo=timezone.utc)

    def service(self, fail=False, persist=True):
        rows = [
            {"name": "backups/a ?#.age", "version": "2026-11-25T12:00:00.0000000Z", "mode": "unlocked", "expiry": self.expiry},
            {"name": "backups/a ?#.age", "version": "2026-10-01T12:00:00.0000000Z", "mode": "unlocked", "expiry": self.now},
            {"name": "receipts/retained.json", "version": "v-locked", "mode": "locked", "expiry": self.expiry},
        ]
        calls = []

        def request(method, path, headers):
            calls.append((method, path, headers))
            query = parse_qs(urlsplit(path).query)
            if method == "GET":
                self.assertEqual(query["include"], ["versions,immutabilitypolicy"])
                page = ET.Element("EnumerationResults")
                blobs = ET.SubElement(page, "Blobs")
                selected = rows[:1] if "marker" not in query else rows[1:]
                for row in selected:
                    blob = ET.SubElement(blobs, "Blob")
                    ET.SubElement(blob, "Name").text = row["name"]
                    ET.SubElement(blob, "VersionId").text = row["version"]
                    props = ET.SubElement(blob, "Properties")
                    ET.SubElement(props, "ImmutabilityPolicyMode").text = row["mode"]
                    ET.SubElement(props, "ImmutabilityPolicyUntilDate").text = format_datetime(row["expiry"], usegmt=True)
                ET.SubElement(page, "NextMarker").text = "next+/=" if "marker" not in query else ""
                return {}, ET.tostring(page)
            self.assertEqual(method, "PUT")
            self.assertIn("a%20%3F%23.age", path)
            self.assertEqual(query["versionid"], [rows[0]["version"]])
            self.assertEqual(headers["x-ms-immutability-policy-until-date"], format_datetime(self.expiry, usegmt=True))
            self.assertEqual(headers["x-ms-immutability-policy-mode"], "Locked")
            if fail:
                raise OSError("injected authorization failure")
            if persist:
                rows[0]["mode"] = "locked"
            return {key: value.lower() if key.endswith("mode") else value for key, value in headers.items()}, b""

        return request, calls

    def test_paginated_version_lock_preserves_expiry_and_is_idempotent(self):
        request, calls = self.service()
        self.assertEqual(worm.lock_versions(request, "2026-11-26T12:00:00Z", now=self.now), 1)
        self.assertFalse(any(method == "PUT" for method, *_ in calls))
        self.assertEqual(worm.lock_versions(request, "2026-11-26T12:00:00Z", apply=True, now=self.now), 1)
        self.assertEqual(worm.lock_versions(request, "2026-11-26T12:00:00Z", apply=True, now=self.now), 0)
        self.assertEqual(sum(method == "PUT" for method, *_ in calls), 1)

    def test_early_failure_and_failed_lock_never_succeed(self):
        request, calls = self.service()
        with self.assertRaises(ValueError):
            worm.lock_versions(request, "2027-01-01T00:00:00Z", apply=True, now=self.now)
        self.assertEqual(calls, [])
        request, _ = self.service(fail=True)
        with self.assertRaises(OSError):
            worm.lock_versions(request, "2026-11-26T12:00:00Z", apply=True, now=self.now)
        request, _ = self.service(persist=False)
        with self.assertRaises(ValueError):
            worm.lock_versions(request, "2026-11-26T12:00:00Z", apply=True, now=self.now)

    def test_missing_retention_inventory_fails_closed(self):
        with self.assertRaises(ValueError):
            worm.lock_versions(lambda *_: ({}, b'<EnumerationResults><Blobs><Blob><Name>a</Name></Blob></Blobs></EnumerationResults>'), "2026-11-26T12:00:00Z", apply=True, now=self.now)


if __name__ == "__main__":
    unittest.main()
