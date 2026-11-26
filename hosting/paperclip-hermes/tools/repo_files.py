"""Enumerate the public working tree consistently for repository checks."""

import subprocess
from pathlib import Path


def files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, check=True, stdout=subprocess.PIPE,
    )
    return [root / name for name in sorted(set(result.stdout.decode().split("\0")))
            if name and ((root / name).exists() or (root / name).is_symlink())]
