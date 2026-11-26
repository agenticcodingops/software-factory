#!/usr/bin/env bash
# Never trace commands: the host-only destination file contains a SAS.
set +x
set -euo pipefail
umask 077
kit=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$kit/backup-restore.py" backup "$@"
