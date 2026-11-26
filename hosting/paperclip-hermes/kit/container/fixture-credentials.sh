#!/bin/sh
set -eu
# Called as root by the three service entrypoints. Host source stays 0600.
runtime_uid=$1
install -d -m 0755 /run/kit
install -m 0400 -o "$runtime_uid" -g "$runtime_uid" \
    /run/secrets/fixture_github_token /run/kit/github-token
printf '%s\n' "$FIXTURE_REPOSITORY" > /run/kit/fixture-repository
chmod 0444 /run/kit/fixture-repository
