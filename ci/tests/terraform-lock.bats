#!/usr/bin/env bats

setup() {
  script="$BATS_TEST_DIRNAME/../check-terraform.sh"
  cd "$BATS_TEST_TMPDIR" || return
  git init -q
  mkdir -p bin hosting/paperclip-hermes/tools
  cp "$BATS_TEST_DIRNAME/../../hosting/paperclip-hermes/tools/check-mock-plans.py" hosting/paperclip-hermes/tools/
  cp "$BATS_TEST_DIRNAME/../../hosting/paperclip-hermes/tools/check-budget-alerts.py" hosting/paperclip-hermes/tools/
  export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
  export CALLS="$BATS_TEST_TMPDIR/calls"
  cat > bin/terraform <<'SH'
#!/usr/bin/env bash
echo "$*" >> "$CALLS"
if [[ "$*" == *'test -json -verbose'* ]]; then
  echo '{"type":"test_plan","test_plan":{"resource_changes":[]}}'
  echo '{"type":"test_summary","@message":"Success! Mock test fixture."}'
fi
if [[ "$*" == *'providers lock'* && "${DRIFT:-}" == yes ]]; then
  echo 'new platform hash' >> hosting/paperclip-hermes/terraform/stacks/host/.terraform.lock.hcl
fi
SH
  printf '#!/bin/sh\nexit 0\n' > bin/tflint
  cp bin/tflint bin/checkov
  chmod +x bin/*
}

stack() {
  mkdir -p hosting/paperclip-hermes/terraform/stacks/host
  touch hosting/paperclip-hermes/terraform/stacks/host/main.tf
}

lock() {
  echo '# platform hashes fixture' > hosting/paperclip-hermes/terraform/stacks/host/.terraform.lock.hcl
  git add hosting/paperclip-hermes/terraform/stacks/host/.terraform.lock.hcl
}

@test 'empty scaffold passes without invoking provider commands' {
  run bash "$script"
  [ "$status" -eq 0 ]
  [[ "$output" == *'none yet'* ]]
  run grep -q -e 'providers lock' "$CALLS"
  [ "$status" -eq 1 ]
}

@test 'a stack without a committed lock file fails' {
  stack
  run bash "$script"
  [ "$status" -eq 1 ]
  [[ "$output" == *'Missing committed provider lock file'* ]]
}

@test 'locks both required platforms after readonly init, validation and tests' {
  stack
  lock
  run bash "$script"
  [ "$status" -eq 0 ]
  grep -q -e 'init -backend=false -input=false -lockfile=readonly' "$CALLS"
  grep -q -e 'validate' "$CALLS"
  grep -q -e 'test' "$CALLS"
  grep -q -e 'providers lock -platform=linux_amd64 -platform=windows_amd64' "$CALLS"
}

@test 'regenerated platform hash drift fails' {
  stack
  lock
  export DRIFT=yes
  run bash "$script"
  [ "$status" -eq 1 ]
  [[ "$output" == *'Provider lock drift'* ]]
}
