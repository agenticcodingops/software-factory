#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
[[ $(id -u) == 0 ]] || { echo 'kit-e2e requires root for volume and private-path checks.' >&2; exit 1; }
docker info >/dev/null
docker compose version
# Fail before pulling the entire application stack when nested-container
# kernels forbid mount/network namespaces. This is also the pinned DB image.
docker run --rm --network none --entrypoint /bin/true \
  postgres:17@sha256:87c8b42f423b45d5247e6aa8fdb2e7762fec164c68a50fe3eb07e7bfcaf4dbcf
python3 -m pytest -q -x -p no:cacheprovider hosting/paperclip-hermes/kit/e2e
