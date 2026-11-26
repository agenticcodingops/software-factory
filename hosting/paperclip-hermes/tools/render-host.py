#!/usr/bin/env python3
"""Render literal disk protection into an isolated Terraform tree for a gated destroy."""

import argparse
from datetime import datetime, timezone, timedelta
import json
import re
from pathlib import Path
import shutil
import sys


def authorize(receipt, target, run_id, now=None):
    """Validate the non-secret receipt from the trusted production workflow.

    Authenticating the approval and blob hash is the workflow's responsibility;
    this local renderer cannot turn caller-supplied JSON into GitHub approval.
    """
    now = now or datetime.now(timezone.utc)
    if not target or not run_id or receipt.get("target") != target or str(receipt.get("run_id")) != run_id:
        raise ValueError("authorization must match the selected target and run")
    if receipt.get("environment") != "production" or receipt.get("approved") is not True or receipt.get("confirm_destroy") != f"DESTROY {target}":
        raise ValueError("approved production and exact typed target confirmation required")
    backup = receipt.get("backup", {})
    if not all(backup.get(key) for key in ("vm_id", "blob", "version", "ciphertext_sha256", "recipient_fp", "size")):
        raise ValueError("complete off-site backup identity and verification required")
    if backup.get("target") != target or backup.get("verified") is not True or backup.get("successful") is not True or backup.get("reason") != "final" or backup.get("writers_stopped") is not True:
        raise ValueError("fresh successful final backup with stopped writers required")
    if not isinstance(backup["size"], int) or isinstance(backup["size"], bool) or backup["size"] <= 0 or not re.fullmatch(r"[0-9a-f]{64}", backup["ciphertext_sha256"]):
        raise ValueError("positive backup size and exact SHA-256 hash required")
    if "completed_at" not in backup:
        raise ValueError("backup completion time required")
    completed = datetime.fromisoformat(backup["completed_at"].replace("Z", "+00:00"))
    if completed.tzinfo is None or not timedelta(0) <= now - completed <= timedelta(hours=24):
        raise ValueError("verified backup must be at most 24 hours old")


def render(source, destination, allow_disk_destroy=False, receipt=None, target=None, run_id=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or source in destination.parents or destination.exists():
        raise ValueError("destination must be a new isolated directory outside the source tree")
    if allow_disk_destroy:
        authorize(receipt or {}, target, run_id)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".terraform", "*.tfstate*", "*.tfplan", "*.tfvars*"), symlinks=True)
    path = destination / "modules/host/main.tf"
    if path.is_symlink():
        shutil.rmtree(destination)
        raise ValueError("protection template must be a regular file")
    text = path.read_text()
    if text.count("prevent_destroy = true") != 1 or text.count("disk_destroy_enabled = false") != 1:
        shutil.rmtree(destination)
        raise ValueError("unexpected protection template; refuse to render")
    if allow_disk_destroy:
        text = text.replace("prevent_destroy = true", "prevent_destroy = false").replace("disk_destroy_enabled = false", "disk_destroy_enabled = true")
        path.write_text(text)
    return destination / "stacks/host"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--allow-disk-destroy", action="store_true")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--target")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        receipt = json.loads(args.authorization.read_text()) if args.authorization else None
        print(render(args.source, args.destination, args.allow_disk_destroy, receipt, args.target, args.run_id))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"render-host: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
