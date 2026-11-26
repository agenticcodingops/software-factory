#!/usr/bin/env python3
"""Reject destructive Terraform plans and immutable host/protocol changes."""

import argparse
import json
from pathlib import Path
import sys

PROTOCOL = ("sf-approved-sha", "sf-recovery-sha", "sf-recovery-until", "sf-delivery-recipient")
PROTECTED = {"azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine", "azurerm_virtual_machine", "azurerm_managed_disk", "azurerm_storage_account", "azurerm_storage_container", "tailscale_acl"}


def unknown_at(value, *path):
    for key in path:
        if value is True:
            return True
        if not isinstance(value, dict):
            return False
        value = value.get(key, False)
    return bool(value)


def azapi_kind(resource, before, after):
    if not resource.get("type", "").startswith("azapi_"):
        return ""
    return str(after.get("type", before.get("type", ""))).split("@", 1)[0].lower()


def guard(plan, override_replace=False):
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    issues = []
    if plan.get("errored") or plan.get("deferred_changes") or plan.get("complete") is False:
        issues.append("plan is incomplete or errored")
    if "resource_changes" not in plan or not isinstance(plan["resource_changes"], list):
        return ["plan has no resource_changes array"]
    for resource in plan["resource_changes"]:
        if not isinstance(resource, dict) or not isinstance(resource.get("change"), dict):
            raise ValueError("malformed resource change")
        change = resource["change"]
        actions = change.get("actions", [])
        before = change.get("before") or {}
        after = change.get("after") or {}
        unknown = change.get("after_unknown") or {}
        kind = azapi_kind(resource, before, after)
        typ = resource.get("type", "")
        addr = resource.get("address", typ)
        vm = typ in {"azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine", "azurerm_virtual_machine"} or kind == "microsoft.compute/virtualmachines"
        protected = typ in PROTECTED or kind.startswith(("microsoft.compute/disks", "microsoft.storage/storageaccounts")) or vm
        if "forget" in actions and protected:
            issues.append(f"{addr}: protected resource removed from Terraform ownership")
        # Preservation applies to VMs left behind. An approved delete-only plan has no successor.
        authorized_delete = override_replace and vm and actions == ["delete"] and change.get("after") is None
        if "delete" in actions and protected and not authorized_delete:
            issues.append(f"{addr}: protected resource delete/replace")
        if vm and not before:
            new_tags = after.get("tags") or {}
            if any(tag in new_tags or unknown_at(unknown, "tags", tag) for tag in PROTOCOL):
                issues.append(f"{addr}: new VM must not receive protocol tags before health approval")
        if vm and before and not authorized_delete:
            for tag in PROTOCOL:
                old_tags, new_tags = before.get("tags") or {}, after.get("tags") or {}
                if old_tags.get(tag) != new_tags.get(tag) or unknown_at(unknown, "tags", tag):
                    issues.append(f"{addr}: protocol tag changed/unknown: {tag}")
            for path in (("custom_data",), ("body", "properties", "osProfile", "customData")):
                old, new = before, after
                for key in path:
                    old = old.get(key) if isinstance(old, dict) else None
                    new = new.get(key) if isinstance(new, dict) else None
                if old != new or unknown_at(unknown, *path):
                    issues.append(f"{addr}: custom_data changed/unknown")
            # AVM 0.22.1 puts customData in a write-only sensitive_body. Its first replacement
            # trigger is the password/customData fingerprint; passwords are forbidden by the module.
            if kind == "microsoft.compute/virtualmachines":
                old = before.get("replace_triggers_external_values")
                new = after.get("replace_triggers_external_values")
                def fingerprint(value):
                    if isinstance(value, str):
                        value = json.loads(value)
                    return value[0] if isinstance(value, list) and value else None
                if fingerprint(old) != fingerprint(new) or unknown_at(unknown, "replace_triggers_external_values") or unknown_at(unknown, "sensitive_body"):
                    issues.append(f"{addr}: AVM custom_data fingerprint changed/unknown")
        retention = typ in {"azurerm_storage_management_policy", "azurerm_storage_container_immutability_policy"} or "immutabilitypolicies" in kind or "managementpolicies" in kind
        if before and retention and (before != after or unknown):
            issues.append(f"{addr}: immutability/lifecycle changed")
        if before and (typ == "azurerm_storage_account" or kind == "microsoft.storage/storageaccounts"):
            for key in ("immutability_policy", "blob_properties", "shared_access_key_enabled"):
                if before.get(key) != after.get(key) or unknown_at(unknown, key):
                    issues.append(f"{addr}: storage protection changed: {key}")
            old_props = before.get("body", {}).get("properties", {})
            new_props = after.get("body", {}).get("properties", {})
            for key in ("immutableStorageWithVersioning", "allowSharedKeyAccess"):
                if old_props.get(key) != new_props.get(key) or unknown_at(unknown, "body", "properties", key):
                    issues.append(f"{addr}: storage protection changed: {key}")
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--override-replace", action="store_true")
    args = parser.parse_args()
    try:
        issues = guard(json.loads(args.plan.read_text()), args.override_replace)
        for issue in issues:
            print(issue)
        print(f"plan-guard: {len(issues)} finding(s)")
        return int(bool(issues))
    except (OSError, ValueError, TypeError) as exc:
        print(f"plan-guard: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
