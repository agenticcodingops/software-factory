#!/usr/bin/env python3
"""Enforce the pilot's network isolation against a complete JSON/HuJSON policy."""

import argparse
import ipaddress
import json
from pathlib import Path
import re
import sys

PILOT = "tag:pcpilot"
# The pilot is a tagged identity; member-owned devices and internet routes do not include it.
BROAD = {"*", "autogroup:tagged"}
TAILNET_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def load_policy(text):
    """Remove HuJSON comments/trailing commas without touching quoted strings."""
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/|[^"/]+|/', text)
    clean = "".join(t for t in tokens if not t.startswith(("//", "/*")))
    # A second token pass prevents trailing-comma removal inside string literals.
    parts = re.findall(r'"(?:\\.|[^"\\])*"|\s+|.', clean)
    for i, part in enumerate(parts):
        if part == ",":
            following = next((p for p in parts[i + 1:] if p.strip()), "")
            if following in ("}", "]"):
                parts[i] = ""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate policy key")
            result[key] = value
        return result

    value = json.loads("".join(parts), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("policy must be an object")
    return value


def selectors(policy, value, trail=()):
    if not isinstance(value, str):
        raise ValueError("selectors must be strings")
    if value in trail:
        raise ValueError("cyclic group/host alias")
    if value.startswith("group:"):
        members = policy.get("groups", {}).get(value)
        if not isinstance(members, list) or not members:
            raise ValueError("unknown or empty group")
        return {item for member in members for item in selectors(policy, member, (*trail, value))}
    if value in policy.get("hosts", {}):
        return selectors(policy, policy["hosts"][value], (*trail, value))
    return {value}


def destination(value, acl=False):
    if not isinstance(value, str):
        raise ValueError("destination must be a string")
    if acl:
        # ACL destinations carry ports; tag names already contain one colon.
        value, separator, _ = value.rpartition(":")
        if not separator or not value:
            raise ValueError("ACL destination needs ports")
        value = value.strip("[]")
    return value


def covers_pilot(value):
    if value == PILOT or value in BROAD:
        return True
    try:
        network = ipaddress.ip_network(value, strict=False)
        return any(network.version == tailnet.version and network.overlaps(tailnet) for tailnet in TAILNET_NETWORKS)
    except ValueError:
        return False


def findings(policy, operator):
    result = []
    for section in ("grants", "acls"):
        entries = policy.get(section, [])
        if not isinstance(entries, list):
            raise ValueError(f"{section} must be a list")
        for index, rule in enumerate(entries):
            if not isinstance(rule, dict) or not isinstance(rule.get("src"), list) or not isinstance(rule.get("dst"), list) or not rule["src"] or not rule["dst"]:
                raise ValueError("every rule requires nonempty src/dst arrays")
            src = {s for item in rule["src"] for s in selectors(policy, item)}
            dst = {s for item in rule["dst"] for s in selectors(policy, destination(item, section == "acls"))}
            if any(covers_pilot(s) for s in src):
                result.append(f"{section}[{index}]: source covers {PILOT}")
            if any(covers_pilot(d) for d in dst) and src != {operator}:
                result.append(f"{section}[{index}]: only the operator may reach {PILOT}")
    for index, rule in enumerate(policy.get("ssh", [])):
        dst = {s for item in rule.get("dst", []) for s in selectors(policy, item)}
        if any(covers_pilot(d) for d in dst):
            src = {s for item in rule.get("src", []) for s in selectors(policy, item)}
            if src != {operator} or rule.get("action") != "check" or rule.get("users") != ["admin"]:
                result.append(f"ssh[{index}]: pilot SSH requires operator check mode as admin")
    for test in policy.get("sshTests", []):
        if not isinstance(test.get("src"), str) or not isinstance(test.get("dst"), list) or "user" in test:
            raise ValueError("sshTests requires src and a dst array, with accept/check/deny user arrays")
        if any(not isinstance(test[key], list) or not all(isinstance(user, str) for user in test[key]) for key in ("accept", "check", "deny") if key in test):
            raise ValueError("sshTests accept/check/deny must be arrays of SSH users")
    return result


def baseline_findings(plan, policy_text, live_policy):
    """The report-only exception requires an externally read live baseline and no-diff plan."""
    errors = []
    if policy_text != live_policy:
        errors.append("baseline policy differs from the live policy")
    allowed = {"tailscale_acl", "tailscale_tailnet_settings", "tailscale_dns_preferences"}
    seen = set()
    for resource in plan.get("resource_changes", []):
        if resource.get("mode", "managed") != "managed":
            errors.append("baseline has unrelated data reads")
        if resource.get("type") not in allowed or resource.get("change", {}).get("actions") != ["no-op"]:
            errors.append("baseline must have no resource diff or unrelated deployment changes")
        seen.add(resource.get("type"))
    if not {"tailscale_acl", "tailscale_tailnet_settings"} <= seen:
        errors.append("baseline requires policy and settings in its no-diff plan")
    for resource in plan.get("resource_changes", []):
        if resource.get("type") == "tailscale_acl" and resource.get("change", {}).get("after", {}).get("acl") != live_policy:
            errors.append("baseline plan does not preserve live policy")
    if plan.get("errored") or plan.get("deferred_changes"):
        errors.append("baseline plan is incomplete")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", type=Path)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--mode", choices=("enforcing", "report-only"), default="enforcing")
    parser.add_argument("--baseline-plan", type=Path)
    parser.add_argument("--live-policy", type=Path)
    parser.add_argument("--operation", choices=("plan", "apply", "join"), default="plan")
    args = parser.parse_args()
    try:
        text = args.policy.read_text()
        issues = findings(load_policy(text), args.operator)
        if args.mode == "report-only":
            if args.operation != "plan" or not args.baseline_plan or not args.live_policy:
                raise ValueError("report-only requires a read-only no-diff baseline plan and live policy; apply/join forbidden")
            errors = baseline_findings(json.loads(args.baseline_plan.read_text()), text, args.live_policy.read_text())
            if errors:
                raise ValueError("; ".join(errors))
        for issue in issues:
            print(issue)
        print(f"policy-lint ({args.mode}): {len(issues)} finding(s)")
        return int(bool(issues) and args.mode == "enforcing")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(f"policy-lint: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
