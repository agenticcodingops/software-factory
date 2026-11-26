#!/usr/bin/env python3
"""Check LF, JSON syntax and portable script commands in the public tree."""

import ast
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hosting/paperclip-hermes/tools"))
from repo_files import files

errors = 0
for path in files(Path.cwd()):
    if path.is_symlink():
        continue
    data = path.read_bytes()
    if b"\0" in data:
        continue
    if b"\r" in data:
        print(f"{path.relative_to(Path.cwd())}: CR found; use LF line endings")
        errors += 1
    # Shell commands (including quoted/absolute executables) and Python argv
    # calls must work on runners without the optional ripgrep executable.
    invocation = re.compile(rb"(?<![\w./-])(?:/[\w./-]+/)?[\"']?rg[\"']?(?=[\s,;\]|&)]|$)(?!\s*=)")
    lines = []
    if path.suffix == ".py":
        for node in ast.walk(ast.parse(data, filename=str(path))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name not in {"run", "Popen", "call", "check_call", "check_output", "system", "getoutput", "getstatusoutput", "execvp", "execvpe"}:
                continue
            argument = node.args[0]
            if isinstance(argument, (ast.List, ast.Tuple)) and argument.elts:
                argument = argument.elts[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str) and invocation.search(argument.value.encode()):
                lines.append(node.lineno)
    elif path.suffix in {".sh", ".bats"} or data.startswith(b"#!"):
        lines = [number for number, line in enumerate(data.splitlines(), 1)
                 if not line.lstrip().startswith(b"#") and invocation.search(line)]
    for number in lines:
        print(f"{path.relative_to(Path.cwd())}:{number}: ripgrep invocation forbidden; use grep -E or grep -F")
        errors += 1
    if path.suffix == ".json":
        try:
            json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            print(f"{path.relative_to(Path.cwd())}: invalid JSON: {exc}")
            errors += 1
print(f"tree hygiene: {errors} finding(s)")
raise SystemExit(int(errors != 0))
