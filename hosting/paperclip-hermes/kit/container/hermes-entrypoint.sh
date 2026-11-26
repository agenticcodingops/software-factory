#!/bin/sh
set -eu
/bin/sh /kit/fixture-credentials.sh 10000
install -m 0400 -o 10000 -g 10000 /run/secrets/sandbox_ssh_key /run/kit/sandbox_ed25519
/opt/hermes/.venv/bin/python /kit/configure-hermes.py
# exec preserves PID 1 for the upstream s6 dispatcher and supervised dashboard.
exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
