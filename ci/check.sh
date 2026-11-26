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

mapfile -d '' scripts < <(git ls-files -z --cached --others --exclude-standard -- '*.sh' '*.bats' | sort -zu)
shellcheck "${scripts[@]}"
shellcheck hosting/paperclip-hermes/bootstrap/sf
bats ci/tests
bats hosting/paperclip-hermes/bootstrap/tests
bash ci/check-terraform.sh

mapfile -d '' configs < <(git ls-files -z --cached --others --exclude-standard -- 'renovate.json' 'renovate.json5' '.renovaterc*' 'renovate/*.json' 'renovate/*.json5' | sort -zu)
if (( ${#configs[@]} )); then
  npm run validate:renovate -- "${configs[@]}"
else
  # Exercise the installed validator even before WP12 adds the real presets.
  fixture=$(mktemp --suffix=.json)
  trap 'rm -f "$fixture"' EXIT
  echo '{}' > "$fixture"
  npm run validate:renovate -- "$fixture"
  echo 'Renovate presets: none yet (WP1); validator smoke check passed.'
fi
echo 'All repository checks passed (WP1 + WP4 + WP5).'
