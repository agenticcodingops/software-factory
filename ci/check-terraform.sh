#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
terraform fmt -check -recursive .
declare -A terraform_dirs=()
while IFS= read -r -d '' file; do
  terraform_dirs["$(dirname "$file")"]=1
done < <(git ls-files -z --cached --others --exclude-standard -- '*.tf' '*.tf.json')
if (( ${#terraform_dirs[@]} )); then
  for directory in "${!terraform_dirs[@]}"; do
    tflint --chdir="$directory" --config "$PWD/.tflint.hcl"
  done
else
  tflint --config "$PWD/.tflint.hcl"
fi
# Checkov already excludes node_modules and hidden generated directories.
checkov --directory . --framework terraform --compact --quiet

stacks=hosting/paperclip-hermes/terraform/stacks
if [[ ! -d "$stacks" ]] || ! find "$stacks" -type f \( -name '*.tf' -o -name '*.tf.json' \) -print -quit | read -r; then
  echo 'Terraform stacks: none yet (WP1); fmt, TFLint and Checkov completed on the tree.'
  exit 0
fi

snapshot=$(mktemp)
trap 'rm -f "$snapshot"' EXIT
while IFS= read -r -d '' stack; do
  if ! find "$stack" -maxdepth 1 -type f \( -name '*.tf' -o -name '*.tf.json' \) -print -quit | read -r; then
    continue
  fi
  lock="$stack/.terraform.lock.hcl"
  if [[ ! -f "$lock" ]]; then
    echo "Missing committed provider lock file: $lock" >&2
    exit 1
  fi
  git ls-files --error-unmatch "$lock" >/dev/null
  cp "$lock" "$snapshot"
  terraform -chdir="$stack" init -backend=false -input=false -lockfile=readonly
  terraform -chdir="$stack" validate
  # Verbose JSON exposes actual mocked resource changes, including nested AVM resources.
  mock_log=$(mktemp)
  if ! terraform -chdir="$stack" test -json -verbose > "$mock_log"; then
    cat "$mock_log"
    rm -f "$mock_log"
    exit 1
  fi
  python3 hosting/paperclip-hermes/tools/check-mock-plans.py "$mock_log"
  python3 - "$mock_log" <<'PYCODE'
import json
import sys
for line in open(sys.argv[1]):
    event = json.loads(line)
    if event.get("type") == "test_summary":
        print(event["@message"])
PYCODE
  rm -f "$mock_log"
  terraform -chdir="$stack" providers lock -platform=linux_amd64 -platform=windows_amd64
  if ! cmp -s "$snapshot" "$lock"; then
    diff -u "$snapshot" "$lock" || true
    echo "Provider lock drift: regenerate $lock for linux_amd64 and windows_amd64 and commit it." >&2
    exit 1
  fi
done < <(find "$stacks" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
