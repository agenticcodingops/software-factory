#!/usr/bin/env python3
"""Reject instance identifiers in the public working tree without printing them."""

import argparse
import hashlib
import os
import re
from pathlib import Path

from repo_files import files

GUID = re.compile(r"(?<![a-f0-9])[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}(?![a-f0-9])", re.I)
EMAIL = re.compile(r"[a-z0-9][a-z0-9.!#$%&'*+=?^_`{|}~-]*@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}", re.I)
TAILNET = re.compile(r"(?<![a-z0-9_.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+ts\.net\b", re.I)
TOKEN = re.compile(r"[A-Za-z0-9_@.+-]+")


def entries(path: Path) -> set[str]:
    return {line.strip().casefold() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def findings(text: str, allowed: set[str], private: set[str]):
    for kind, pattern in (("guid", GUID), ("email", EMAIL), ("tailnet", TAILNET)):
        for match in pattern.finditer(text):
            value = match.group().casefold()
            if value in allowed:
                continue
            if kind == "email" and value.rsplit("@", 1)[1] == "users.noreply.github.com":
                continue
            if kind == "tailnet" and (value in ("example.ts.net", "test.ts.net")
                                       or value.endswith((".example.ts.net", ".test.ts.net"))):
                continue
            yield text.count("\n", 0, match.start()) + 1, kind
    for match in TOKEN.finditer(text):
        digest = hashlib.sha256(match.group().casefold().encode()).hexdigest()
        if digest in private:
            yield text.count("\n", 0, match.start()) + 1, "private-name"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--allow", type=Path)
    parser.add_argument("--private-hashes", type=Path, default=os.getenv("SF_PRIVATE_NAMES_HASH_FILE"))
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        allowed = entries(args.allow or root / "deny-list/allow.txt")
        if any("*" in value for value in allowed):
            raise ValueError("allow entries must be exact strings")
        private = entries(args.private_hashes) if args.private_hashes else set()
        if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in private):
            raise ValueError("private hash entries must be SHA-256 digests")
        count = 0
        for path in files(root):
            content = os.readlink(path) if path.is_symlink() else path.read_bytes().decode("utf-8", errors="replace")
            for line, kind in sorted(set(findings(content, allowed, private))):
                print(f"{path.relative_to(root)}:{line}: denied {kind}")
                count += 1
    except (OSError, ValueError) as exc:
        parser.exit(2, f"deny-list configuration/read error: {exc}\n")
    print(f"deny-list: {count} finding(s)")
    return int(count != 0)


if __name__ == "__main__":
    raise SystemExit(main())
