#!/usr/bin/env bash
# Install Linux amd64 check binaries with hashes verified from upstream release checksums.
set -euo pipefail
destination=${1:?usage: ci/install-tools.sh DESTINATION}
mkdir -p "$destination"
destination=$(cd "$destination" && pwd)
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
cd "$temporary"
curl --fail --silent --show-error --location --retry 3 \
  https://github.com/rhysd/actionlint/releases/download/v1.7.7/actionlint_1.7.7_linux_amd64.tar.gz -o actionlint.tar.gz
echo '023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757  actionlint.tar.gz' | sha256sum -c -
tar -xzf actionlint.tar.gz actionlint
curl --fail --silent --show-error --location --retry 3 \
  https://github.com/terraform-linters/tflint/releases/download/v0.66.0/tflint_linux_amd64.zip -o tflint.zip
echo '22be11cadc25cd02401e94c0ccc91aa63467debb8485c2a755d98f4c0a9d1e2e  tflint.zip' | sha256sum -c -
unzip -q tflint.zip tflint
curl --fail --silent --show-error --location --retry 3 \
  https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz -o gitleaks.tar.gz
echo '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  gitleaks.tar.gz' | sha256sum -c -
tar -xzf gitleaks.tar.gz gitleaks
# Current stable release, the line the kit host gets from docker-compose-plugin: https://github.com/docker/compose/releases/tag/v5.8.1
# Standalone invocation: https://docs.docker.com/compose/install/standalone/
curl --fail --silent --show-error --location --retry 3 \
  https://github.com/docker/compose/releases/download/v5.8.1/docker-compose-linux-x86_64 -o docker-compose
echo 'cf2713394ca63900da5d00e0be12045634cea93a504e1ab2f2ae24eed7f2d359  docker-compose' | sha256sum -c -
install -m 0755 actionlint tflint gitleaks docker-compose "$destination/"
