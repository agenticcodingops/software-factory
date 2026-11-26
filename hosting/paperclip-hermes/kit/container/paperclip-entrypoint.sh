#!/bin/sh
set -eu
/bin/sh /kit/fixture-credentials.sh 1000
for key in BETTER_AUTH_SECRET PAPERCLIP_AGENT_JWT_SECRET PAPERCLIP_TOOL_ACTION_SIGNING_SECRET PAPERCLIP_SECRETS_MASTER_KEY; do
    file=$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')
    value=$(cat "/run/secrets/$file")
    export "$key=$value"
done
password=$(cat /run/secrets/postgres_password)
export DATABASE_URL="postgres://paperclip:$password@postgres:5432/paperclip"
test -s /caddy-ca/root.crt
# Keep the upstream ownership repair and privilege drop, and the image CMD.
exec docker-entrypoint.sh "$@"
