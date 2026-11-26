#!/bin/sh
# Shared by service startup and the one-shot recovery configurator.
API_SERVER_KEY=$(cat /run/secrets/hermes_gateway_key)
export API_SERVER_KEY
for key in HERMES_DASHBOARD_BASIC_AUTH_PASSWORD HERMES_DASHBOARD_BASIC_AUTH_SECRET OPENAI_API_KEY; do
    file=$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')
    value=$(cat "/run/secrets/$file")
    export "$key=$value"
done
