#!/usr/bin/env python3
"""Remove optional registry deprecation messages, which may contain personal e-mails."""

import json
from pathlib import Path

path = Path("package-lock.json")
lock = json.loads(path.read_text())
for package in lock["packages"].values():
    package.pop("deprecated", None)
path.write_text(json.dumps(lock, indent=2) + "\n")
