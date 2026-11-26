#!/usr/bin/env python3
"""Inspect verbose Terraform test JSON for secret resources and network/storage invariants."""

import argparse
import importlib.util
import json
import re
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("budget_alerts", Path(__file__).with_name("check-budget-alerts.py"))
budget_alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget_alerts)
spec = importlib.util.spec_from_file_location("policy_lint", Path(__file__).with_name("policy-lint.py"))
policy_lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy_lint)


def state_resources(module):
    yield from module.get("resources", [])
    for child in module.get("child_modules", []):
        yield from state_resources(child)


def credential_findings(resource, values):
    errors = []
    typ = resource.get("type")
    if typ in {"tls_private_key", "random_password", "azurerm_storage_account", "azurerm_storage_account_sas", "azurerm_storage_account_blob_container_sas"}:
        errors.append("forbidden secret resource/data source in mocked plan/state")
    if typ in {"azapi_resource_action", "azapi_data_plane_resource"} and str(values.get("action", "")).lower() in {"listkeys", "listaccountsas"}:
        errors.append("storage credential action in mocked plan/state")
    if any(key in json.dumps(values) for key in ("private_key_pem", "primary_access_key", "secondary_access_key", "primary_connection_string", "secondary_connection_string")):
        errors.append("private key or long-lived storage credential in mocked plan/state")
    return errors


def inspect_plan(plan):
    errors = []
    ops_credentials = []
    kinds = []
    for resource in state_resources(plan.get("prior_state", {}).get("values", {}).get("root_module", {})):
        errors.extend(credential_findings(resource, resource.get("values") or {}))
    for resource in plan.get("resource_changes", []):
        typ = resource["type"]
        after = resource.get("change", {}).get("after") or {}
        errors.extend(credential_findings(resource, after))
        if resource.get("mode") == "data":
            continue
        if typ in {"azurerm_lb", "azurerm_lb_nat_rule", "azurerm_lb_nat_pool", "azurerm_bastion_host", "azurerm_nat_gateway"}:
            errors.append("unexpected inbound/network intermediary")
        if any(key in json.dumps(after) for key in ("private_key_pem", "primary_access_key", "secondary_access_key", "primary_connection_string")):
            errors.append("private key or long-lived storage credential in mocked plan")
        if typ == "azurerm_federated_identity_credential":
            subject = after.get("subject", "")
            if not re.search(r":job_workflow_ref:[^@]+@[0-9a-f]{40}$", subject):
                errors.append("federation is not bound to an exact workflow identity")
            if ':environment:ops' in subject:
                ops_credentials.append(after)
        if typ == "azurerm_role_assignment":
            address, scope = resource.get("address", ""), after.get("scope", "")
            if '["delegation"]' in address and ("/blobServices/" in scope or "/providers/Microsoft.Storage/storageAccounts/" not in scope):
                errors.append("delegation grant must be at storage-account scope")
            if any(f'["{name}"]' in address for name in ("backup_write", "backup_read", "staging_write", "state_write", "state_read")) and "/blobServices/default/containers/" not in scope:
                errors.append("data role must be container scoped")
            if '["ops"]' in address and "Cost Management" in str(after.get("role_definition_name")) and after.get("role_definition_name") != "Cost Management Reader":
                errors.append("ops received cost writes")
        if typ != "azapi_resource":
            continue
        kind = after.get("type", "").split("@", 1)[0].lower()
        kinds.append(kind)
        body = after.get("body") or {}
        props = body.get("properties") or {}
        if kind == "microsoft.network/publicipaddresses":
            if props.get("publicIPAllocationMethod") != "Static" or props.get("publicIPAddressVersion") != "IPv4":
                errors.append("public IP must be static IPv4")
        if any(part in kind for part in ("loadbalancers", "bastionhosts", "inboundnatrules", "natgateways")):
            errors.append("unexpected inbound/network intermediary")
        if kind == "microsoft.network/networksecuritygroups/securityrules":
            if props.get("direction") == "Inbound" and props.get("access") == "Allow":
                errors.append("inbound allow rule")
        if kind == "microsoft.compute/virtualmachines":
            identity = body.get("identity") or (after.get("identity") or [None])[0]
            if identity and identity.get("type") not in (None, "None"):
                errors.append("VM managed identity")
        if kind == "microsoft.logic/workflows":
            definition = props["definition"]
            expression = definition["actions"]["Threshold_and_maintenance"]["expression"]
            # Mock host runs cover both the verified BillingMonth and email-only fallback.
            verified = "and(true," in expression
            errors.extend(budget_alerts.findings(definition, verified))
            def http_actions(actions):
                for action in actions.values():
                    if action.get("type") == "Http":
                        yield action
                    yield from http_actions(action.get("actions", {}))
                    yield from http_actions(action.get("else", {}).get("actions", {}))
            for action in http_actions(definition["actions"]):
                if action["inputs"].get("authentication") != {"type": "ManagedServiceIdentity", "audience": "https://management.azure.com/"}:
                    errors.append("Logic App HTTP action must authenticate to the ARM audience")
        if kind == "microsoft.storage/storageaccounts/managementpolicies":
            rules = {rule["name"]: rule for rule in props["policy"]["rules"]}
            required = (("expire-staging", 1, "restore-staging/"),) if after["parent_id"].endswith("staging") else (("expire-backups", 45, "backups/backups/"), ("expire-staging", 1, "restore-staging/"))
            for name, days, prefix in required:
                rule = rules.get(name, {})
                definition = rule.get("definition", {})
                actions = definition.get("actions", {})
                if not rule.get("enabled") or prefix not in definition.get("filters", {}).get("prefixMatch", []) or actions.get("baseBlob", {}).get("delete", {}).get("daysAfterModificationGreaterThan") != days or actions.get("version", {}).get("delete", {}).get("daysAfterCreationGreaterThan") != days:
                    errors.append(f"missing {name} base/version delete and prefix")
    if "microsoft.compute/virtualmachines" in kinds:
        if kinds.count("microsoft.network/publicipaddresses") != 1 or kinds.count("microsoft.network/networkinterfaces") != 1:
            errors.append("host must have exactly one NIC and one public IP")
    if ops_credentials:
        fixture = json.loads((Path(__file__).parent / "tests/fixtures/ops-unapproved-release.json").read_text())
        for candidate in fixture["subjects"]:
            if any(c.get("issuer") == fixture["issuer"] and fixture["audience"] in c.get("audience", []) and c.get("subject") == candidate for c in ops_credentials):
                errors.append("unapproved release can exchange an ops credential")
    return errors


def trust_read_findings(plan):
    """Check every ARM scope in the resolved trust plan against plan's read grants."""
    resources = plan.get("resource_changes", [])
    readers, authorization = [], []
    for resource in resources:
        address = resource.get("address", "")
        if resource.get("type") != "azurerm_role_assignment" or '["plan"]' not in address:
            continue
        values = resource.get("change", {}).get("after") or {}
        scope = values.get("scope", "").lower()
        if 'authorization_read' in address or 'SF Authorization Reader' in address:
            authorization.append(scope)
        elif any(key in address for key in ('vault_read', 'drill_read', 'trust_read', '-Reader"')):
            readers.append(scope)
    errors = []
    for resource in resources:
        if resource.get("mode") == "data":
            continue
        values = resource.get("change", {}).get("after") or {}
        typ, address = resource.get("type", ""), resource.get("address", "")
        if not typ.startswith(("azurerm_", "azapi_")):
            continue
        # The role API refreshes at scope; other ARM resources refresh at their ID.
        scope = values.get("scope") if typ in {"azurerm_role_assignment", "azurerm_role_definition"} else values.get("resource_id") or values.get("parent_id") or values.get("id")
        if typ == "azapi_resource" and str(values.get("type", "")).lower().startswith("microsoft.resources/resourcegroups@"):
            scope = f"{values['parent_id']}/resourceGroups/{values['name']}"
        if not scope:
            errors.append(f"{address}: trust resource scope unresolved for plan-reader audit")
            continue
        scope = scope.lower()
        grants = authorization if typ in {"azurerm_role_assignment", "azurerm_role_definition"} else readers
        if not any(scope == grant or scope.startswith(grant + "/") for grant in grants if grant):
            errors.append(f"{address}: plan identity has no read grant covering {scope}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    args = parser.parse_args()
    plans, errors = 0, []
    try:
        for line in args.log.read_text().splitlines():
            event = json.loads(line)
            if event.get("type") == "test_plan":
                plans += 1
                errors.extend(inspect_plan(event["test_plan"]))
                if event.get("@testrun") in {"trust_storage_and_authorization", "target_tenant_twins"}:
                    errors.extend(trust_read_findings(event["test_plan"]))
                if event.get("@testrun") in {"park_guard_accepts_vm_and_state_only_patch_delete", "adding_lexically_first_target_preserves_trust_home"}:
                    spec = importlib.util.spec_from_file_location("plan_guard", Path(__file__).with_name("plan-guard.py"))
                    plan_guard = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(plan_guard)
                    errors.extend(plan_guard.guard(event["test_plan"], override_replace=True))
                    changes = event["test_plan"]["resource_changes"]
                    if event.get("@testrun") == "park_guard_accepts_vm_and_state_only_patch_delete" and not any(r["type"] == "azapi_update_resource" and "this_os_disk_network_access" in r["address"] and r["change"]["actions"] == ["delete"] for r in changes):
                        errors.append("park regression did not exercise AVM OS disk patch deletion")
                if event.get("@testrun") == "baseline_no_diff":
                    # Use the full generated plan, including local Terraform resources.
                    baseline = event["test_plan"]
                    acl = next(r["change"] for r in baseline["resource_changes"] if r["type"] == "tailscale_acl")
                    errors.extend(policy_lint.baseline_findings(baseline, acl["after"]["acl"], acl["before"]["acl"]))
        if not plans:
            errors.append("no verbose mock plans found")
        for error in sorted(set(errors)):
            print(error)
        print(f"mock plan inspection: {plans} plan(s), {len(errors)} finding(s)")
        return int(bool(errors))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"mock plan inspection: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
