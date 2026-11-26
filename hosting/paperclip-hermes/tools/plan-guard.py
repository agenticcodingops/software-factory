#!/usr/bin/env python3
"""Reject destructive Terraform plans and immutable host/protocol changes."""

import argparse
import json
from pathlib import Path
import sys

PROTOCOL = ("sf-approved-sha", "sf-recovery-sha", "sf-recovery-until", "sf-delivery-recipient")
LIVE_TAGS = (*PROTOCOL, "sf-maintenance-until")
PROTECTED = {"azurerm_subscription", "azurerm_resource_group", "azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine", "azurerm_virtual_machine", "azurerm_managed_disk", "azurerm_storage_account", "azurerm_storage_container", "azurerm_storage_blob", "azurerm_key_vault", "azurerm_key_vault_secret", "tailscale_acl"}


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
    return str(before.get("type") or after.get("type", "")).split("@", 1)[0].lower()


def unknown_disk_deletion(value):
    for key in ("body", "properties", "storageProfile", "dataDisks"):
        if value is True:
            return True
        if not isinstance(value, dict):
            return False
        value = value.get(key, False)
    if value is True:
        return True
    # Disk IDs may legitimately be unknown on first apply; only deletion semantics matter.
    return isinstance(value, list) and any(item is True or isinstance(item, dict) and item.get("deleteOption") is True for item in value)


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
        kinds = {kind, azapi_kind(resource, {}, after)}
        typ = resource.get("type", "")
        addr = resource.get("address", typ)
        vm = typ in {"azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine", "azurerm_virtual_machine"} or "microsoft.compute/virtualmachines" in kinds
        protected = typ in PROTECTED or any(k.startswith(("microsoft.compute/disks", "microsoft.storage/storageaccounts", "microsoft.keyvault/vaults")) or k in {"microsoft.resources/resourcegroups", "microsoft.subscription/aliases", "microsoft.resources/subscriptions"} for k in kinds) or vm
        if typ.startswith("azapi_") and (not kind or unknown_at(unknown, "type")):
            issues.append(f"{addr}: missing/unknown ARM resource type")
        # Nested ARM templates can delete children without individual resource changes
        # (Complete mode, or AzureRM deployment teardown). These stacks do not use them.
        deployment = typ in {"azurerm_template_deployment", "azurerm_resource_group_template_deployment", "azurerm_subscription_template_deployment", "azurerm_management_group_template_deployment", "azurerm_tenant_template_deployment"} or "microsoft.resources/deployments" in kinds
        if deployment and actions != ["no-op"]:
            issues.append(f"{addr}: nested deployment may change protected resources indirectly")
        if "forget" in actions and protected:
            issues.append(f"{addr}: protected resource removed from Terraform ownership")
        # Preservation applies to VMs left behind. An approved delete-only plan has no successor.
        authorized_delete = override_replace and vm and actions == ["delete"] and change.get("after") is None
        if "delete" in actions and protected and not authorized_delete:
            issues.append(f"{addr}: protected resource delete/replace")
        if vm and (after or authorized_delete):
            # A later VM-only delete must never cascade to the separately protected data disk.
            configuration = before if authorized_delete else after
            disks = configuration.get("body", {}).get("properties", {}).get("storageProfile", {}).get("dataDisks", [])
            if any(disk.get("deleteOption", "Detach") != "Detach" for disk in disks) or configuration.get("delete_data_disks_on_termination") is True or unknown_at(unknown, "delete_data_disks_on_termination") or unknown_disk_deletion(unknown):
                issues.append(f"{addr}: VM may delete protected data disks indirectly")
        if vm and not before:
            new_tags = after.get("tags") or {}
            if any(tag in new_tags or unknown_at(unknown, "tags", tag) for tag in LIVE_TAGS):
                issues.append(f"{addr}: new VM must not receive protocol tags before health approval")
        if vm and before and not authorized_delete:
            for tag in LIVE_TAGS:
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
        if retention and (before != after or unknown):
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
        if not before and kind == "microsoft.storage/storageaccounts":
            policy = after.get("body", {}).get("properties", {}).get("immutableStorageWithVersioning") or {}
            if policy.get("immutabilityPolicy", {}).get("state") == "Locked":
                issues.append(f"{addr}: initialize new account policy as Unlocked before transitioning to Locked")
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
