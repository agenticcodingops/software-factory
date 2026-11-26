#!/usr/bin/env bash
# The Python standard-library transport keeps request bodies and secrets in memory.
set +x
set -euo pipefail
ulimit -c 0
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$(dirname -- "${BASH_SOURCE[0]}")/bootstrap.py" "$@"
