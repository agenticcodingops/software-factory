#!/usr/bin/env bash
set -euo pipefail
kit=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ ${1:-} == recover ]]; then
    exec python3 "$kit/kit-control.py" "$@"
fi
exec python3 "$kit/kit-control.py" upgrade "$@"
