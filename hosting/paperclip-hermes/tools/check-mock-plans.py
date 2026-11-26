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


def inspect_plan(plan):
    errors = []
    ops_credentials = []
    kinds = []
    for resource in plan.get("resource_changes", []):
        if resource.get("mode") == "data":
            continue
        typ = resource["type"]
        after = resource.get("change", {}).get("after") or {}
        if typ in {"tls_private_key", "random_password", "azurerm_storage_account"}:
            errors.append("forbidden secret resource in mocked plan")
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
            expression = props["definition"]["actions"]["Threshold_and_maintenance"]["expression"]
            # Mock host runs cover both the verified BillingMonth and email-only fallback.
            verified = "and(true," in expression
            errors.extend(budget_alerts.findings(expression, verified))
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
