"""Exercise the isolation boundary and destructive-plan guards without credentials."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("fixtures")


def module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


policy = module("policy-lint")
plan = module("plan-guard")


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class PolicyTests(unittest.TestCase):
    def test_accept_operator_only(self):
        self.assertEqual(policy.findings(fixture("policy-good.json"), "operator@example.com"), [])

    def test_release_blocking_fixtures(self):
        for name in ("allow-all", "tagged", "pilot-source"):
            with self.subTest(name=name):
                run = subprocess.run([sys.executable, TOOLS / "policy-lint.py", FIXTURES / f"policy-{name}.json", "--operator", "operator@example.com"], capture_output=True, text=True)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assertIn("finding(s)", run.stdout)

    def test_expand_aliases_and_groups_and_acl_ports(self):
        for section in ("grants", "acls"):
            for source in ("tag:pcpilot", "*", "autogroup:tagged", "autogroup:member", "0.0.0.0/0", "::/0"):
                p = {"hosts": {"pilot-alias": source}, "groups": {"group:alias": ["pilot-alias"]}, section: [{"src": ["group:alias"], "dst": ["tag:other:443" if section == "acls" else "tag:other"]}]}
                self.assertTrue(policy.findings(p, "operator@example.com"))
            p = {"groups": {"group:operator": ["operator@example.com"]}, "hosts": {"pilot-alias": "tag:pcpilot"}, section: [{"src": ["group:operator"], "dst": ["pilot-alias:443" if section == "acls" else "pilot-alias"]}]}
            self.assertFalse(policy.findings(p, "operator@example.com"))
            p[section][0]["src"] = ["autogroup:member"]
            self.assertTrue(policy.findings(p, "operator@example.com"))

    def test_group_mixing_and_cycles(self):
        p = {"groups": {"group:both": ["operator@example.com", "autogroup:member"]}, "grants": [{"src": ["group:both"], "dst": ["tag:pcpilot"]}]}
        self.assertTrue(policy.findings(p, "operator@example.com"))
        p["groups"]["group:both"] = ["group:both"]
        with self.assertRaises(ValueError):
            policy.findings(p, "operator@example.com")

    def test_ssh_requires_check_and_admin(self):
        p = fixture("policy-good.json")
        p["ssh"][0]["action"] = "accept"
        self.assertTrue(policy.findings(p, "operator@example.com"))

    def test_ssh_test_schema_matches_tailscale(self):
        p = fixture("policy-good.json")
        self.assertFalse(policy.findings(p, "operator@example.com"))
        p["sshTests"][0]["check"] = True
        with self.assertRaises(ValueError):
            policy.findings(p, "operator@example.com")
        p["sshTests"][0] = {"src": "operator@example.com", "dst": "tag:pcpilot", "user": "admin", "check": True}
        with self.assertRaises(ValueError):
            policy.findings(p, "operator@example.com")

    def test_hujson_comments_strings_and_trailing_commas(self):
        text = '{/* comment */"hosts":{"url":"https://example.test/a,}",},// comment\n"grants":[],}'
        self.assertEqual(policy.load_policy(text)["hosts"]["url"], "https://example.test/a,}")
        for text in ('{"grants":[],"grants":[]}', '[]', '{broken'):
            with self.assertRaises(ValueError):
                policy.load_policy(text)

    def test_baseline_exception_is_read_only_and_no_diff(self):
        text = (FIXTURES / "policy-allow-all.json").read_text()
        baseline = {"resource_changes": [
            {"type": "tailscale_acl", "change": {"actions": ["no-op"], "after": {"acl": text}}},
            {"type": "tailscale_tailnet_settings", "change": {"actions": ["no-op"], "after": {"acls_externally_managed_on": False}}}]}
        self.assertFalse(policy.baseline_findings(baseline, text, text))
        self.assertTrue(policy.baseline_findings(baseline, text + " ", text))
        changed = copy.deepcopy(baseline)
        changed["resource_changes"][1]["change"]["actions"] = ["update"]
        self.assertTrue(policy.baseline_findings(changed, text, text))
        changed = copy.deepcopy(baseline)
        changed["resource_changes"].append({"type": "azurerm_linux_virtual_machine", "change": {"actions": ["create"]}})
        self.assertTrue(policy.baseline_findings(changed, text, text))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps(baseline))
            args = [sys.executable, TOOLS / "policy-lint.py", FIXTURES / "policy-allow-all.json", "--operator", "operator@example.com", "--mode", "report-only", "--baseline-plan", path, "--live-policy", FIXTURES / "policy-allow-all.json"]
            run = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("source covers", run.stdout)
            for operation in ("apply", "join"):
                self.assertEqual(subprocess.run([*args, "--operation", operation], capture_output=True).returncode, 2)
            self.assertEqual(subprocess.run(args[:args.index("--baseline-plan")], capture_output=True).returncode, 2)


class PlanTests(unittest.TestCase):
    def test_release_blocking_fixtures(self):
        for name in ("vm-replace", "lifecycle-change"):
            run = subprocess.run([sys.executable, TOOLS / "plan-guard.py", FIXTURES / f"plan-{name}.json"], capture_output=True, text=True)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)

    def test_safe_and_destructive_override(self):
        self.assertFalse(plan.guard(fixture("plan-safe.json")))
        self.assertFalse(plan.guard(fixture("plan-vm-replace.json"), True))
        self.assertTrue(plan.guard(fixture("plan-lifecycle-change.json"), True))

    def test_custom_data_never_overridden(self):
        for name in ("change", "unknown"):
            self.assertTrue(plan.guard(fixture(f"plan-custom-data-{name}.json"), True))

    def test_all_protocol_tags_changes_removals_and_unknown(self):
        for tag in plan.PROTOCOL:
            for new, unknown in (("candidate", {}), (None, {}), ("previous", {"tags": {tag: True}}), ("previous", {"tags": True})):
                p = fixture("plan-safe.json")
                change = p["resource_changes"][0]["change"]
                change["before"]["tags"] = {tag: "previous"}
                change["after"]["tags"] = {} if new is None else {tag: new}
                change["after_unknown"] = unknown
                self.assertTrue(plan.guard(p, True), (tag, new, unknown))

    def test_azapi_vm_fingerprint_and_new_host(self):
        for triggers in (["changed"], None):
            p = fixture("plan-safe.json")
            r = p["resource_changes"][0]
            r["type"] = "azapi_resource"
            for v in ("before", "after"):
                r["change"][v]["type"] = "Microsoft.Compute/virtualMachines@2024-07-01"
            r["change"]["before"]["replace_triggers_external_values"] = ["original"]
            r["change"]["after"]["replace_triggers_external_values"] = triggers
            self.assertTrue(plan.guard(p, True))
            r["change"]["before"] = None
            r["change"]["after"]["tags"] = {}
            r["change"]["actions"] = ["create"]
            self.assertFalse(plan.guard(p))

    def test_azapi_storage_retention_and_lifecycle(self):
        p = fixture("plan-lifecycle-change.json")
        r = p["resource_changes"][0]
        r["type"] = "azapi_resource"
        for kind in ("managementPolicies", "immutabilityPolicies"):
            for v in ("before", "after"):
                r["change"][v]["type"] = f"Microsoft.Storage/storageAccounts/{kind}@2023-05-01"
            self.assertTrue(plan.guard(p, True))

    def test_new_vm_cannot_publish_approval_tags(self):
        p = fixture("plan-safe.json")
        p["resource_changes"][0]["change"]["before"] = None
        p["resource_changes"][0]["change"]["actions"] = ["create"]
        self.assertTrue(plan.guard(p, True))
        p["resource_changes"][0]["change"]["after"]["tags"] = {}
        self.assertFalse(plan.guard(p))

    def test_forgetting_protected_resources_is_never_an_override(self):
        p = fixture("plan-vm-replace.json")
        p["resource_changes"][0]["change"]["actions"] = ["forget"]
        self.assertTrue(plan.guard(p, True))
        with self.assertRaises(ValueError):
            plan.guard([])

    def test_protected_deletes_and_incomplete_plans(self):
        for typ in plan.PROTECTED:
            p = fixture("plan-vm-replace.json")
            p["resource_changes"][0]["type"] = typ
            self.assertTrue(plan.guard(p))
        for p in ({}, {"resource_changes": [], "complete": False}, {"resource_changes": [], "deferred_changes": [{}]}):
            self.assertTrue(plan.guard(p))


if __name__ == "__main__":
    unittest.main()
