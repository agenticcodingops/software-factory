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
        hard_ratio = re.search(r'hard_ratio\s*=\s*"(.*)"', text)[1]
        expression = expressions[1].replace('${local.hard_ratio}', hard_ratio)
        payload = {"data": {"Budget": 35, "NotificationThresholdAmount": 28}}
        self.assertFalse(budget_alerts.evaluate(expression, payload, {}))
        payload["data"]["NotificationThresholdAmount"] = 35
        self.assertTrue(budget_alerts.evaluate(expression, payload, {"sf-maintenance-until": "bad"}))
        self.assertIn('Parse_maintenance = ["Failed", "TimedOut"]', text)
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
                    if section != "ssh":
                        del rule["users"]
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


class PrePushReviewTests(unittest.TestCase):
    def resource(self, typ, kind, before=None, after=None, actions=None, **extra):
        return {"address": extra.pop("address", f"{typ}.fixture"), "type": typ, "change": {
            "actions": actions or ["create"], "before": before,
            "after": {"type": kind + "@2024-03-02", **(after or {})} if after is not None else None,
            **extra}}

    def test_park_patch_delete_is_state_only_but_disk_delete_is_blocked(self):
        vm = self.resource("azapi_resource", "Microsoft.Compute/virtualMachines", before={"type": "Microsoft.Compute/virtualMachines@2024-07-01"}, actions=["delete"])
        patch = self.resource("azapi_update_resource", "Microsoft.Compute/disks", before={"type": "Microsoft.Compute/disks@2024-03-02"}, actions=["delete"])
        self.assertFalse(plan.guard({"resource_changes": [vm, patch]}, True))
        patch["type"] = "azapi_resource"
        self.assertTrue(plan.guard({"resource_changes": [vm, patch]}, True))

    def test_arm_actions_cannot_destroy_disks_storage_or_vms(self):
        for kind in ("Microsoft.Compute/disks", "Microsoft.Storage/storageAccounts", "Microsoft.Compute/virtualMachines", "Microsoft.Network/networkSecurityGroups"):
            for method in ("DELETE", "POST", "PATCH"):
                r = self.resource("azapi_resource_action", kind, after={"resource_id": "/fixture/resource", "method": method})
                if method == "DELETE" or not kind.startswith("Microsoft.Network"):
                    self.assertTrue(plan.guard({"resource_changes": [r]}, True))
        r = self.resource("azapi_resource_action", "Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies", after={"method": "POST", "action": "lock"}, address="module.trust.azapi_resource_action.lock_container_retention[0]")
        self.assertTrue(any("immutability/lifecycle" in x for x in plan.guard({"resource_changes": [r]})))

    def test_destroy_actions_are_checked_but_ordinary_action_state_removal_is_safe(self):
        before = {"type": "Microsoft.Compute/disks@2024-03-02", "method": "DELETE", "when": "destroy"}
        r = self.resource("azapi_resource_action", "Microsoft.Compute/disks", before=before, actions=["delete"])
        self.assertTrue(plan.guard({"resource_changes": [r]}, True))
        before["when"] = "apply"
        self.assertFalse(plan.guard({"resource_changes": [r]}, True))

    def test_indirect_vm_tags_cannot_bypass_protocol_guard(self):
        vm_id = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/pc"
        for typ, kind, after in (
            ("azapi_resource", "Microsoft.Resources/tags", {"parent_id": vm_id, "body": {"properties": {"tags": {}}}}),
            ("azapi_resource_action", "Microsoft.Compute/virtualMachines", {"resource_id": vm_id, "method": "PATCH", "body": {"tags": {}}}),
            ("azapi_update_resource", "Microsoft.Compute/virtualMachines", {"resource_id": vm_id, "body": {"properties": {"tags": {}}}}),
        ):
            self.assertTrue(plan.guard({"resource_changes": [self.resource(typ, kind, after=after)]}, True))
        r = self.resource("azapi_update_resource", "Microsoft.Compute/virtualMachines", after={"resource_id": vm_id}, after_unknown={"body": True})
        self.assertTrue(plan.guard({"resource_changes": [r]}, True))

    def test_tag_extension_delete_and_destroy_action_also_change_protocol_tags(self):
        for typ, when in (("azapi_resource", None), ("azapi_resource_action", "destroy")):
            before = {"type": "Microsoft.Resources/tags@2021-04-01", "method": "PATCH", "when": when, "body": {"properties": {"tags": {}}}}
            r = self.resource(typ, "Microsoft.Resources/tags", before=before, actions=["delete"])
            self.assertTrue(plan.guard({"resource_changes": [r]}, True))

    def test_blob_service_retention_fields_are_guarded_in_avm_shape(self):
        for key, old, new in (("isVersioningEnabled", True, False), ("deleteRetentionPolicy", {"enabled": True, "days": 7}, {"enabled": False}), ("containerDeleteRetentionPolicy", {"enabled": True}, None), ("restorePolicy", {"enabled": True}, None), ("changeFeed", {"enabled": True}, None)):
            kind = "Microsoft.Storage/storageAccounts/blobServices"
            r = self.resource("azapi_update_resource", kind, before={"type": kind + "@2025-08-01", "body": {"properties": {key: old}}}, after={"body": {"properties": {key: new}}}, actions=["update"])
            self.assertTrue(plan.guard({"resource_changes": [r]}, True))
            r["change"]["after"] = copy.deepcopy(r["change"]["before"])
            self.assertFalse(plan.guard({"resource_changes": [r]}))
            r["change"]["after_unknown"] = {"body": {"properties": {key: True}}}
            self.assertTrue(plan.guard({"resource_changes": [r]}))
        kind = "Microsoft.Storage/storageAccounts/blobServices/containers"
        r = self.resource("azapi_resource", kind, before={"type": kind + "@2025-08-01", "body": {"properties": {"immutableStorageWithVersioning": {"enabled": True}}}}, after={"body": {"properties": {}}}, actions=["update"])
        self.assertTrue(plan.guard({"resource_changes": [r]}, True))

    def test_trust_anchor_replacements_are_never_overridden(self):
        for address in ('module.trust.azurerm_role_definition.target["deallocator"]', 'module.trust.azurerm_resource_group.target_identities[0]', 'module.trust.module.target_identities["plan"].azurerm_user_assigned_identity.this'):
            r = {"address": address, "type": "azurerm_user_assigned_identity", "change": {"actions": ["delete", "create"], "before": {}, "after": {}}}
            self.assertTrue(plan.guard({"resource_changes": [r]}, True))

        # Removing a target's grant must not be mistaken for rotating a twin principal.
        r = {"address": 'module.trust.module.target_identities["plan"].azurerm_role_assignment.this["retired-Reader"]', "type": "azurerm_role_assignment", "change": {"actions": ["delete"], "before": {}, "after": None}}
        self.assertFalse(plan.guard({"resource_changes": [r]}))

    def test_policy_case_normalization_and_legacy_fields_fail_closed(self):
        import json
        p = {"grants": [{"src": ["operator@example.com"], "dst": ["tag:pcpilot"], "ip": ["tcp:22"]}], "ACLs": [{"Action": "accept", "Src": ["*"], "Dst": ["*:*"]}]}
        self.assertTrue(policy.findings(policy.load_policy(json.dumps(p)), "operator@example.com"))
        p = {"Hosts": {"pilot": "100.64.0.1"}, "Grants": [{"Src": ["*"], "Dst": ["pilot"]}]}
        self.assertTrue(policy.findings(p, "operator@example.com"))
        for p in ({"acls": [], "ACLs": []}, {"Hosts": {}, "hosts": {}}, {"grants": [{"src": ["*"], "Src": ["operator@example.com"], "dst": ["tag:pcpilot"]}]}, {"grants": [], "typo": []}):
            with self.assertRaises(ValueError):
                policy.findings(p, "operator@example.com")
        for key in ("users", "ports", "Users", "Ports", "unrecognized"):
            p = {"acls": [{"src": ["operator@example.com"], "dst": ["tag:pcpilot:*"], key: ["*"]}]}
            with self.assertRaises(ValueError):
                policy.findings(p, "operator@example.com")

    def test_legacy_policy_remains_importable_only_as_exact_read_only_baseline(self):
        import json
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = directory / "live.json"
            live.write_text(json.dumps({"ACLs": [{"Action": "accept", "Users": ["*"], "Ports": ["*:*"]}]}))
            baseline = directory / "plan.json"
            baseline.write_text(json.dumps({"resource_changes": [
                {"type": "tailscale_acl", "change": {"actions": ["no-op"], "before": {"acl": live.read_text()}, "after": {"acl": live.read_text()}}},
                {"type": "tailscale_tailnet_settings", "change": {"actions": ["no-op"], "after": {}}},
            ]}))
            args = [sys.executable, BASE.parent / "tools/policy-lint.py", live, "--operator", "operator@example.com"]
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)
            report = [*args, "--mode", "report-only", "--baseline-plan", baseline, "--live-policy", live]
            self.assertEqual(subprocess.run(report, capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run([*report, "--operation", "apply"], capture_output=True).returncode, 2)
            self.assertEqual(subprocess.run([*report, "--operation", "join"], capture_output=True).returncode, 2)

    def test_policy_cannot_publish_route_through_or_delegate_pilot(self):
        cases = [
            {"nodeAttrs": [{"target": ["tag:pcpilot"], "attr": ["funnel"]}]},
            {"nodeAttrs": [{"target": ["100.64.0.0/10"], "attr": ["future-capability"]}]},
            {"autoApprovers": {"exitNode": ["tag:pcpilot"]}},
            {"autoApprovers": {"routes": {"10.0.0.0/8": ["tag:pcpilot"]}}},
            {"grants": [{"src": ["autogroup:member"], "dst": ["autogroup:internet"], "ip": ["*"], "via": ["tag:pcpilot"]}]},
            {"tagOwners": {"tag:pcpilot": ["autogroup:member"]}},
            {"tagOwners": {"tag:pc-deployer": ["autogroup:member"]}},
        ]
        for additions in cases:
            p = {"grants": [], **additions}
            self.assertTrue(policy.findings(p, "operator@example.com"), additions)
        self.assertFalse(policy.findings({"grants": [], "tagOwners": {"tag:pcpilot": ["tag:pc-deployer"], "tag:pc-deployer": ["operator@example.com"]}}, "operator@example.com"))

    def test_storage_data_reads_and_prior_state_cannot_hide_credentials(self):
        inspector = module("check-mock-plans")
        for typ, values in (("azurerm_storage_account", {}), ("azurerm_storage_account_sas", {}), ("azurerm_storage_account_blob_container_sas", {}), ("azapi_resource_action", {"action": "listKeys"}), ("azapi_resource_action", {"action": "listAccountSas"}), ("terraform_data", {"primary_access_key": "fixture"})):
            for mode in ("data", "managed"):
                resource = {"type": typ, "mode": mode, "change": {"after": values}}
                self.assertTrue(inspector.inspect_plan({"resource_changes": [resource]}))
                resource = {"type": typ, "mode": mode, "values": values}
                self.assertTrue(inspector.inspect_plan({"prior_state": {"values": {"root_module": {"child_modules": [{"resources": [resource]}]}}}}))

    def test_encryption_prerequisite_is_read_only_and_fails_clearly(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "az"
            stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$AZ_CALLS"\nif [ "$1" = feature ]; then echo "$FEATURE_STATE"; else echo Registered; fi\n')
            stub.chmod(0o755)
            for state in ("Registered", "NotRegistered"):
                log = Path(tmp) / "calls"
                log.write_text("")
                env = {**os.environ, "PATH": tmp + ":" + os.environ["PATH"], "FEATURE_STATE": state, "AZ_CALLS": str(log)}
                result = subprocess.run([BASE.parent / "tools/check-azure-prerequisites.sh", "fixture"], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, int(state != "Registered"))
                self.assertIn("Microsoft.DevTestLab", log.read_text())
                self.assertIn("EncryptionAtHost", log.read_text())
                self.assertNotIn(" register ", log.read_text())
                if result.returncode:
                    self.assertIn("Owner bootstrap required", result.stderr)

    def test_trust_home_and_target_client_contracts(self):
        module_text = (BASE / "modules/trust/main.tf").read_text()
        providers = (BASE / "stacks/trust/providers.tf").read_text()
        self.assertNotIn("values(var.targets)[0]", module_text + providers)
        home_guard = module_text.split('resource "terraform_data" "trust_home"')[1]
        self.assertIn("prevent_destroy = true", home_guard)
        for field in ("key", "subscription", "location"):
            self.assertRegex(home_guard, field + r"\s*=")
        self.assertEqual(providers.count("client_id"), 4)  # LHS and variable reference on both aliases.
        for provider in trust_renderer.composition(targets())["provider"]["azapi"]:
            self.assertEqual(provider["client_id"], "${var.target_client_id}")
        for name in ("modules/trust/variables.tf", "stacks/trust/variables.tf"):
            text = (BASE / name).read_text()
            self.assertIn('contains(keys(var.targets), var.role_home_target)', text)
            self.assertIn('^[a-z0-9]{1,17}$', text)

    def test_plan_reader_audit_rejects_missing_authorization_and_rg_reads(self):
        inspector = module("check-mock-plans")
        resources = []
        for name, scope in (("vault_read", "/subscriptions/v/resourceGroups/vault"), ("drill_read", "/subscriptions/v/resourceGroups/drill"), ("authorization_read", "/subscriptions/v")):
            resources.append({"address": f'module.trust.module.identities["plan"].azurerm_role_assignment.this["{name}"]', "type": "azurerm_role_assignment", "change": {"after": {"scope": scope}}})
        resources += [
            {"address": "module.trust.azurerm_role_definition.vault", "type": "azurerm_role_definition", "change": {"after": {"scope": "/subscriptions/v"}}},
            {"address": "module.trust.azurerm_resource_group.drill", "type": "azurerm_resource_group", "change": {"after": {"id": "/subscriptions/v/resourceGroups/drill"}}},
        ]
        self.assertFalse(inspector.trust_read_findings({"resource_changes": resources}))
        for index in (1, 2):
            changed = copy.deepcopy(resources)
            changed.pop(index)
            self.assertTrue(inspector.trust_read_findings({"resource_changes": changed}))

    def test_bootstrap_lock_name_is_explicit_in_both_contracts(self):
        root = BASE.parents[2]
        for p in (BASE / "README.md", root / "docs/adr/0001-azure-deployment.md"):
            self.assertIn('`state-cannot-delete`', p.read_text())


if __name__ == "__main__":
    unittest.main()
