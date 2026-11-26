#!/bin/sh
set -eu
# Git calls this with get/store/erase and the credential protocol on stdin.
# Never store credentials, answer another host, or print a token on its own.
[ "${1:-}" = get ] || exit 0
protocol='' host='' path=''
while IFS='=' read -r name value && [ -n "$name" ]; do
    case "$name" in
        protocol) protocol=$value ;;
        host) host=$value ;;
        path) path=$value ;;
    esac
done
[ "$protocol" = https ] && [ "$host" = github.com ] || exit 0
fixture=$(cat /run/kit/fixture-repository)
[ "${path%.git}" = "$fixture" ] || exit 0
[ -s /run/kit/github-token ] || exit 1
printf 'username=x-access-token\npassword=%s\n\n' "$(cat /run/kit/github-token)"
