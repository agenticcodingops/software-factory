#!/usr/bin/env python3
"""Check immutable action references and explicit job permissions across the tree."""

import re
from pathlib import Path

import yaml

from repo_files import files

PIN = re.compile(r"[\w.-]+/[\w./-]+@[a-f0-9]{40}$")
VERSION = re.compile(r"#\s+v?\d+\.\d+(?:\.\d+)?\b")


def field(node, name):
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if key.value == name:
                return value
    return None


def step_references(node):
    steps = field(node, "steps")
    if isinstance(steps, yaml.SequenceNode):
        for step in steps.value:
            reference = field(step, "uses")
            if reference is not None:
                yield reference


def references(node, workflow):
    if workflow:
        jobs = field(node, "jobs")
        if isinstance(jobs, yaml.MappingNode):
            for _, job in jobs.value:
                reference = field(job, "uses")
                if reference is not None:
                    yield reference
                yield from step_references(job)
    else:
        runs = field(node, "runs")
        using = field(runs, "using")
        if isinstance(using, yaml.ScalarNode) and using.value == "composite":
            yield from step_references(runs)


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
        for value in references(yaml.compose(text, Loader=yaml.BaseLoader), workflow):
            ref = value.value
            if isinstance(ref, str) and ref.startswith("./"):
                continue
            line = value.start_mark.line
            if not isinstance(ref, str) or not PIN.fullmatch(ref) or not VERSION.search(text.splitlines()[line]):
                print(f"{path.relative_to(Path.cwd())}:{line + 1}: uses must pin a full commit SHA with a version comment")
                count += 1
    print(f"workflow policy: {count} finding(s)")
    return int(count != 0)


if __name__ == "__main__":
    raise SystemExit(main())
