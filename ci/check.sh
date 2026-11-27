#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
export PYTHONDONTWRITEBYTECODE=1

python3 ci/check-tree.py
python3 hosting/paperclip-hermes/tools/check-workflows.py
python3 hosting/paperclip-hermes/tools/deny-list.py
python3 -m unittest discover -s hosting/paperclip-hermes/tools/tests -v
# RootPathTests exercise root-only ownership and symlink protections.
if [[ $(id -u) == 0 ]]; then
  python3 -m pytest -q -p no:cacheprovider hosting/paperclip-hermes/kit/tests
else
  sudo "$(command -v python3)" -m pytest -q -p no:cacheprovider hosting/paperclip-hermes/kit/tests
fi
actionlint -color
zizmor --offline --strict-collection .
gitleaks dir . --redact --no-banner
npm run lint:markdown

mapfile -d '' scripts < <(git ls-files -z --cached --others --exclude-standard -- '*.sh' '*.bats' 'hosting/paperclip-hermes/kit/sfctl' | sort -zu)
shellcheck "${scripts[@]}"
shellcheck hosting/paperclip-hermes/bootstrap/sf
bats ci/tests
bats hosting/paperclip-hermes/bootstrap/tests
bash ci/check-terraform.sh

mapfile -d '' configs < <(git ls-files -z --cached --others --exclude-standard -- 'renovate.json' 'renovate.json5' '.renovaterc*' 'renovate/*.json' 'renovate/*.json5' | sort -zu)
(( ${#configs[@]} )) || { echo 'No Renovate presets found' >&2; exit 1; }
npm run validate:renovate -- "${configs[@]}"
npm run test:renovate
echo 'All repository checks passed.'
