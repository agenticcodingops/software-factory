#!/usr/bin/env python3
"""Check immutable action references and explicit job permissions across the tree."""

import re
from pathlib import Path

import yaml

from repo_files import files

PIN = re.compile(r"[\w.-]+/[\w./-]+@[a-f0-9]{40}$")
VERSION = re.compile(r"#\s+v?\d+\.\d+(?:\.\d+)?\b")


def nodes(node):
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            yield key, value
            yield from nodes(value)
    elif isinstance(node, yaml.SequenceNode):
        for value in node.value:
            yield from nodes(value)


def main() -> int:
    count = 0
    for path in files(Path.cwd()):
        workflow = ".github/workflows" in path.as_posix()
        if path.suffix not in (".yml", ".yaml") or not (workflow or path.name in ("action.yml", "action.yaml")):
            continue
        text = path.read_text()
        data = yaml.load(text, Loader=yaml.BaseLoader)
        if workflow:
            for name, job in data.get("jobs", {}).items():
                permissions = job.get("permissions")
                if not isinstance(permissions, dict):
                    print(f"{path.relative_to(Path.cwd())}: job {name} needs explicit permissions mapping")
                    count += 1
        for key, value in nodes(yaml.compose(text, Loader=yaml.BaseLoader)):
            if key.value != "uses":
                continue
            ref = value.value
            if ref.startswith("./"):
                continue
            line = value.start_mark.line
            if not PIN.fullmatch(ref) or not VERSION.search(text.splitlines()[line]):
                print(f"{path.relative_to(Path.cwd())}:{line + 1}: uses must pin a full commit SHA with a version comment")
                count += 1
    print(f"workflow policy: {count} finding(s)")
    return int(count != 0)


if __name__ == "__main__":
    raise SystemExit(main())
