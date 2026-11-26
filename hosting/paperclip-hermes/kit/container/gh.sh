#!/bin/sh
set -eu
# Preserve Paperclip broker credentials. Paperclip's GitHub launcher sets
# PAPERCLIP_GITHUB_OPERATION_ACTIVE when it runs gh; if its broker withheld a
# token, gh must run without one rather than fall back to the kit's copy.
# Otherwise (Hermes, the sandbox, runs without a GitHub connection) use the
# token placed here by root before the service drops privileges.
if [ -z "${GH_TOKEN:-}" ] && [ -z "${GITHUB_TOKEN:-}" ] \
    && [ -z "${PAPERCLIP_GITHUB_OPERATION_ACTIVE:-}" ]; then
    GH_TOKEN=$(cat /run/kit/github-token)
fi
if [ -n "${GH_TOKEN:-}" ]; then
    export GH_TOKEN
fi
exec /usr/bin/gh "$@"
