#!/usr/bin/env python3
"""Check LF line endings and JSON syntax in every public-tree file."""

import json
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
    if path.suffix == ".json":
        try:
            json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            print(f"{path.relative_to(Path.cwd())}: invalid JSON: {exc}")
            errors += 1
print(f"tree hygiene: {errors} finding(s)")
raise SystemExit(int(errors != 0))
