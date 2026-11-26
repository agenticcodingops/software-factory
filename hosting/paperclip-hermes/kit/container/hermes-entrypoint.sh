#!/bin/sh
set -eu
/bin/sh /kit/fixture-credentials.sh 10000
API_SERVER_KEY=$(cat /run/secrets/hermes_gateway_key)
export API_SERVER_KEY
for key in HERMES_DASHBOARD_BASIC_AUTH_PASSWORD HERMES_DASHBOARD_BASIC_AUTH_SECRET OPENAI_API_KEY; do
    file=$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')
    value=$(cat "/run/secrets/$file")
    export "$key=$value"
done
install -m 0400 -o 10000 -g 10000 /run/secrets/sandbox_ssh_key /run/kit/sandbox_ed25519
/opt/hermes/.venv/bin/python /kit/configure-hermes.py
# exec preserves PID 1 for the upstream s6 dispatcher and supervised dashboard.
exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
