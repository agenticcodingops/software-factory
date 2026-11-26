#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077
kit=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$kit/backup-restore.py" restore "$@"
