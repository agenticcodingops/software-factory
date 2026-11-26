#!/bin/sh
set -eu
/bin/sh /kit/fixture-credentials.sh 1000
test -s /caddy-ca/root.crt
# Keep the upstream ownership repair and privilege drop, and the image CMD.
exec docker-entrypoint.sh "$@"
