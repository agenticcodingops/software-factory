#!/usr/bin/env python3
"""Compose subscription-specific workload RG providers in an isolated Terraform tree."""

import argparse
import json
from pathlib import Path
import re
import shutil
import sys


def composition(targets):
    if not isinstance(targets, dict) or not targets:
        raise ValueError("at least one target is required")
    subscriptions = set()
    for target in targets.values():
        subscription = target["subscription_resource_id"]
        if not re.fullmatch(r"/subscriptions/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", subscription):
            raise ValueError("target subscription must be a full subscription resource ID")
        subscriptions.add(subscription)
    providers, modules, moves = [], {}, []
    ids, resources = [], []
    for subscription in sorted(subscriptions):
        alias = "subscription_" + subscription.rsplit("/", 1)[1].replace("-", "_")
        name = "workload_rg_" + alias
        providers.append({"alias": alias, "subscription_id": subscription.rsplit("/", 1)[1], "tenant_id": "${var.target_tenant_id}"})
        modules[name] = {
            "source": "../../modules/workload-rg",
            "providers": {"azapi": "azapi." + alias},
            "targets": '${{ for key, target in var.targets : key => target if target.subscription_resource_id == "' + subscription + '" }}',
        }
        ids.append(f"module.{name}.resource_ids")
        resources.append(f"module.{name}.resources")
    # Preserve existing RG addresses when upgrading a deployed trust stack.
    for key, target in sorted(targets.items()):
        if target.get("rg_managed_by", "trust") != "trust":
            continue
        subscription = target["subscription_resource_id"]
        name = "workload_rg_subscription_" + subscription.rsplit("/", 1)[1].replace("-", "_")
        moves.append({"from": f"module.trust.module.workload_rg[{json.dumps(key)}]", "to": f"module.{name}.module.workload_rg[{json.dumps(key)}]"})
    modules["trust"] = {"workload_resource_group_ids": "${merge(" + ", ".join(ids) + ")}"}
    return {
        "provider": {"azapi": providers},
        "module": modules,
        "moved": moves,
        "output": {"workload_resource_groups": {"value": "${merge(" + ", ".join(resources) + ")}"}},
    }


def render(source, destination, targets):
    config = composition(targets)
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or source == destination or source in destination.parents:
        raise ValueError("destination must be a new isolated directory outside the source tree")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".terraform", "*.tfstate*", "*.tfplan", "*.tfvars*"), symlinks=True)
    root = destination / "stacks/trust"
    # Override only the composition inputs/output; definitions remain in the normal root.
    overrides = {"module": {"trust": config["module"].pop("trust")}, "output": config.pop("output")}
    (root / "composition_override.tf.json").write_text(json.dumps(overrides, indent=2) + "\n")
    (root / "workload-subscriptions.tf.json").write_text(json.dumps(config, indent=2) + "\n")
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--config", type=Path, required=True, help="private tfvars JSON containing targets")
    args = parser.parse_args()
    try:
        print(render(args.source, args.destination, json.loads(args.config.read_text())["targets"]))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"render-trust: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
