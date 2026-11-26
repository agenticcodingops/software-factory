#!/usr/bin/env bats
# Bats runs each test in its own subshell; exports are intentionally isolated.
# shellcheck disable=SC2030,SC2031

setup() {
  export SF_INSTANCE_DIR="$BATS_TEST_TMPDIR/private"
  export FAKE_DB="$BATS_TEST_TMPDIR/cloud.json" CALLS="$BATS_TEST_TMPDIR/calls"
  export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
  bootstrap="$BATS_TEST_DIRNAME/../sf-bootstrap.sh"
  helper="$BATS_TEST_DIRNAME/../sf"
  mkdir -p "$BATS_TEST_TMPDIR/bin"
  for tool in az gh terraform tailscale curl age age-keygen escrow key-reader; do
    ln -s "$BATS_TEST_DIRNAME/fixture.py" "$BATS_TEST_TMPDIR/bin/$tool"
  done
  export VAULT_SUB TARGET_SUB
  VAULT_SUB=$(python3 -c "import uuid; print(uuid.uuid5(uuid.NAMESPACE_DNS, 'vault.example.test'))")
  TARGET_SUB=$(python3 -c "import uuid; print(uuid.uuid5(uuid.NAMESPACE_DNS, 'target.example.test'))")
  python3 "$BATS_TEST_DIRNAME/fixture.py" init
  touch "$CALLS"
}

boot() { bash "$bootstrap" "$@" --attempts 1 --retry-delay 0; }
trust() { boot trust --target primary --vault-subscription "$VAULT_SUB" --subscription "$TARGET_SUB" --resource-group rg-example --location example-region "$@"; }
init() { boot init --org sample --instance-repo instance --reviewer first --probe-run 1 "$@"; }
assert_clean() {
  python3 - <<'PY'
import json, os
from pathlib import Path
assert json.loads(Path(os.environ['FAKE_DB']).read_text())['assignments'] == []
p = Path(os.environ['SF_INSTANCE_DIR']) / 'assignments.json'
assert not p.exists() or json.loads(p.read_text()) == []
PY
}
refute_match() {
  if rg "$@"; then return 1; else [ "$?" -eq 1 ]; fi
}
assert_no_secrets() {
  [[ "$output" != *'fixture-private-material'* ]]
  refute_match -l 'fixture-private-material|archive-plaintext|tskey-api-fixture-secret' "$BATS_TEST_TMPDIR" --glob '!bin/**'
}

@test 'dry-run is default: no writes, secret generation, grants or Terraform apply' {
  run trust
  [ "$status" -eq 0 ]
  [[ "$output" == *'APPLY terraform'* ]]
  refute_match 'assignment create|terraform .* apply' "$CALLS"
  assert_clean
}

@test 'init refuses Free Pro Team and unknown capability without changing settings' {
  for plan in free pro team unknown; do
    export PLAN="$plan"
    run init --apply
    [ "$status" -eq 1 ]
    [[ "$output" == *'Enterprise required'* ]]
  done
  refute_match 'GH_BODY' "$CALLS"
}

@test 'init reports only the wrong org Actions setting' {
  export BAD_ACTIONS=1
  run init --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'allowed_actions'* ]]
  refute_match 'GH_BODY' "$CALLS"
}

@test 'single reviewer disables self-review prevention and Enterprise waits at both gates' {
  run init --apply
  [ "$status" -eq 0 ]
  [[ "$output" == *'Reviewer gates verified'* ]]
  rg '"prevent_self_review": false' "$CALLS"
  assert_no_secrets
}

@test 'two distinct reviewers enable self-review prevention' {
  run boot init --org sample --instance-repo instance --reviewer first,second --probe-run 1 --apply
  [ "$status" -eq 0 ]
  rg '"prevent_self_review": true' "$CALLS"
}

@test 'init refuses failed protection readback and a probe that does not wait' {
  export BAD_GATE=1
  run init --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'readback mismatch'* ]]
  unset BAD_GATE
  export NO_WAIT=1
  run init --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'did not wait'* ]]
}

@test 'environment-scoped probe collects real sub and workflow claims' {
  run boot oidc-probe --probe-run 1 --apply
  [ "$status" -eq 0 ]
  run boot --print-subject
  [ "$status" -eq 0 ]
  [[ "$output" == *'repo:sample@7/instance@8:environment:production:job_workflow_ref:'* ]]
  [[ "$output" != *'repo:sample/instance:environment'* ]]
}

@test 'probe refuses wrong sub workflow nonce and issuer and never federates' {
  for claim in sub job_workflow_ref nonce iss aud environment; do
    export BAD_CLAIM="$claim"
    run boot oidc-probe --probe-run 1 --apply
    [ "$status" -eq 1 ]
  done
  refute_match 'terraform|federated-identity' "$CALLS"
}

@test 'trust refuses changed subject template and unprobed workflow SHA' {
  export BAD_TEMPLATE=1
  run trust --apply
  [ "$status" -eq 1 ]
  unset BAD_TEMPLATE
  python3 - <<'PY'
import json,os
from pathlib import Path
p=Path(os.environ['SF_INSTANCE_DIR'])/'config/instance.tfvars.json'
x=json.loads(p.read_text());x['workflow_refs']['ops']['bootstrap']+='bad';p.write_text(json.dumps(x))
PY
  run trust --apply
  [ "$status" -eq 1 ]
  refute_match 'assignment create' "$CALLS"
}

@test 'trust refuses non-Owner and service principal operator sessions' {
  export NO_OWNER=1
  run trust --apply
  [ "$status" -eq 1 ]
  unset NO_OWNER
  export APP_LOGIN=1
  run trust --apply
  [ "$status" -eq 1 ]
  refute_match 'assignment create' "$CALLS"
}

@test 'spending-limited same-subscription vault requires explicit risk acceptance' {
  export SPENDING=On
  python3 - <<'PYCODE'
import json,os
from pathlib import Path
p=Path(os.environ['SF_INSTANCE_DIR'])/'config/instance.tfvars.json'
v=json.loads(p.read_text()); v.pop('targets'); v.pop('role_home_target'); p.write_text(json.dumps(v))
PYCODE
  run boot trust --target primary --vault-subscription "$VAULT_SUB" --subscription "$VAULT_SUB" --resource-group rg-example --location example-region --vault-mode same-subscription
  [ "$status" -eq 1 ]
  [[ "$output" == *'--accept-vault-risk required'* ]]
  run boot trust --target primary --vault-subscription "$VAULT_SUB" --subscription "$VAULT_SUB" --resource-group rg-example --location example-region --vault-mode same-subscription --accept-vault-risk
  [ "$status" -eq 0 ]
}

@test 'trust applies WP4 variables, verifies no drift and removes temporary roles' {
  run trust --apply
  [ "$status" -eq 0 ]
  rg '"rg_managed_by": "trust"' "$CALLS"
  rg 'Storage Blob Data Contributor.*resourceGroups/rg-example-vault' "$CALLS"
  rg 'Key Vault Secrets Officer' "$CALLS"
  rg 'plan -input=false -detailed-exitcode' "$CALLS"
  assert_clean
  assert_no_secrets
  run trust --apply
  [ "$status" -eq 0 ]
  refute_match 'storage account create' "$CALLS"
  assert_clean
}

@test 'subscription scope sets host RG ownership' {
  run trust --scope subscription --apply
  [ "$status" -eq 0 ]
  rg '"rg_managed_by": "host"' "$CALLS"
}

@test 'trust refuses unsafe storage, apply failure and drift with grant cleanup' {
  export BAD_STORAGE=1
  run trust --apply
  [ "$status" -eq 1 ]
  assert_clean
  unset BAD_STORAGE
  export FAIL='apply -input=false'
  run trust --apply
  [ "$status" -eq 1 ]
  assert_clean
  unset FAIL
  export TF_DRIFT=1
  run trust --apply
  [ "$status" -eq 1 ]
  assert_clean
}

@test 'age-key vault custody grants verifies writes via stdin and cleans on success' {
  run boot age-key --custody vault --escrow-cmd escrow --apply
  [ "$status" -eq 0 ]
  rg 'assignment create.*Key Vault Secrets Officer.*Microsoft.KeyVault/vaults/examplevault' "$CALLS"
  rg 'secret set.*--file /dev/stdin' "$CALLS"
  rg 'secret show --id' "$CALLS"
  refute_match -- '--value' "$CALLS"
  assert_clean
  assert_no_secrets
}

@test 'operator custody never writes vault or prints keys; helper delegates age-key' {
  run bash "$helper" age-key --custody operator --escrow-cmd escrow --apply
  [ "$status" -eq 0 ]
  refute_match 'keyvault|assignment create' "$CALLS"
  assert_no_secrets
}

@test 'key rotation preserves enabled historical map and dispatches rotate workflow' {
  run boot age-key --custody vault --escrow-cmd escrow --rotate --apply
  [ "$status" -eq 0 ]
  rg 'ph-rotate-age.yml/dispatches' "$CALLS"
  refute_match 'set-attributes|secret delete|secret purge' "$CALLS"
  assert_clean
}

@test 'age-key refuses missing escrow, failed escrow, write and secret readback failures' {
  run boot age-key --apply
  [ "$status" -eq 1 ]
  for fail in escrow 'secret set' 'secret show'; do
    export FAIL="$fail"
    run boot age-key --custody vault --escrow-cmd escrow --apply
    [ "$status" -eq 1 ]
    assert_clean
    assert_no_secrets
  done
}

@test 'lost grant response and SIGTERM both clean assignments recorded before mutation' {
  export LOST_GRANT_RESPONSE=1
  run boot age-key --custody vault --escrow-cmd escrow --apply
  [ "$status" -eq 1 ]
  assert_clean
  unset LOST_GRANT_RESPONSE
  export SIGNAL_GRANT=1
  run boot age-key --custody vault --escrow-cmd escrow --apply
  [ "$status" -eq 1 ]
  assert_clean
}

@test 'cleanup failures retain crash ledger and block resumption until cleanup succeeds' {
  export STICKY_ROLE=1 FAIL='secret set'
  run boot age-key --custody vault --escrow-cmd escrow --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'CLEANUP FAILED'* ]]
  unset FAIL STICKY_ROLE
  run boot age-key --custody vault --escrow-cmd escrow --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'Unfinished assignment ledger'* ]]
  run boot cleanup --apply
  [ "$status" -eq 0 ]
  assert_clean
}

@test 'tailscale federates exact probe claims without policy writes or joining' {
  run bash -c 'printf "%s" tskey-api-fixture-secret | bash "$1" tailscale --token-stdin --apply' _ "$bootstrap"
  [ "$status" -eq 0 ]
  rg 'TS_BODY.*repo:sample@7/instance@8:environment:production:job_workflow_ref:' "$CALLS"
  rg 'HTTP DELETE.*/keys/fixture' "$CALLS"
  refute_match 'HTTP (PUT|PATCH).*policy|tagOwners|^tailscale ' "$CALLS"
  assert_no_secrets
}

@test 'tailscale refuses missing HTTPS and DNS and revokes token on both failures' {
  for condition in NO_HTTPS NO_DNS; do
    export "$condition=1"
    run bash -c 'printf "%s" tskey-api-fixture-secret | bash "$1" tailscale --token-stdin --apply' _ "$bootstrap"
    [ "$status" -eq 1 ]
    rg 'HTTP DELETE.*/keys/fixture' "$CALLS"
    unset "$condition"
  done
  assert_no_secrets
}

@test 'tailscale creation failure revokes token and offers claim-only console fallback' {
  export FAIL='HTTP POST'
  run bash -c 'printf "%s" tskey-api-fixture-secret | bash "$1" tailscale --token-stdin --apply' _ "$bootstrap"
  [ "$status" -eq 1 ]
  rg 'HTTP DELETE.*/keys/fixture' "$CALLS"
  run boot tailscale --print-subject
  [ "$status" -eq 0 ]
  [[ "$output" == *'policy_file:read'* ]]
  assert_no_secrets
}

@test 'rulesets resolve pinned gate, use all three endpoints, read full objects and are idempotent' {
  sha=$(printf 'a%.0s' {1..40})
  run boot rulesets --source-repo sample/source --release-sha "$sha" --apply
  [ "$status" -eq 0 ]
  rg 'POST repos/sample/source/rulesets' "$CALLS"
  rg 'POST repos/sample/instance/rulesets' "$CALLS"
  rg 'POST orgs/sample/rulesets' "$CALLS"
  before=$(rg -c -- '--method POST.*rulesets' "$CALLS")
  run boot rulesets --source-repo sample/source --release-sha "$sha" --apply
  [ "$status" -eq 0 ]
  [ "$(rg -c -- '--method POST.*rulesets' "$CALLS")" -eq "$before" ]
}

@test 'rulesets refuse unresolved SHA and full payload readback mismatch' {
  run boot rulesets --source-repo sample/source --release-sha 0000 --apply
  [ "$status" -eq 1 ]
  export BAD_RULESET=1
  sha=$(printf 'a%.0s' {1..40})
  run boot rulesets --source-repo sample/source --release-sha "$sha" --apply
  [ "$status" -eq 1 ]
}

@test 'restore-assist streams plaintext only between age processes and verifies staged hash' {
  run bash "$helper" restore-assist --backup-account sourcebackups --blob backups/retained.age --version historical-version --staging-blob restore/unique.age --ephemeral-recipient age1ephemeral --key-cmd key-reader --apply
  [ "$status" -eq 0 ]
  rg 'HTTP GET.*versionid=historical-version' "$CALLS"
  rg 'HTTP PUT.*/restore-staging/restore/unique.age' "$CALLS"
  assert_no_secrets
}

@test 'restore-assist refuses unknown fingerprint before retrieving any key' {
  export UNKNOWN_FP=1
  run boot restore-assist --backup-account sourcebackups --blob backups/retained.age --staging-blob restore/unique.age --ephemeral-recipient age1ephemeral --key-cmd key-reader --apply
  [ "$status" -eq 1 ]
  refute_match '^key-reader' "$CALLS"
}

@test 'vault state migration grants removable RG roles and records verified per-stack receipts' {
  run boot vault-move --phase state --apply
  [ "$status" -eq 0 ]
  rg 'init -migrate-state -force-copy' "$CALLS"
  [ -f "$SF_INSTANCE_DIR/config/receipts/vault-move.json" ]
  assert_clean
}

@test 'vault backups stream every version preserve metadata and verify hashes and retention' {
  run boot vault-move --phase backups --apply
  [ "$status" -eq 0 ]
  rg 'Storage Blob Data Reader.*resourceGroups/rg-example-vault' "$CALLS"
  rg 'example SF Backup Migration Writer.*resourceGroups/rg-example-vault' "$CALLS"
  rg 'versionid=historical-version' "$CALLS"
  assert_clean
  assert_no_secrets
}

@test 'vault keys copy enabled exact historical versions via stdin under temporary roles' {
  run boot vault-move --phase keys --apply
  [ "$status" -eq 0 ]
  rg 'Key Vault Secrets User.*secrets/age-' "$CALLS"
  rg 'Key Vault Secrets Officer.*destinationvault' "$CALLS"
  rg 'secret set.*destinationvault.*--file /dev/stdin' "$CALLS"
  assert_clean
  assert_no_secrets
}

@test 'every migration phase cleans temporary grants after injected copy failure' {
  for pair in 'state:state pull' 'backups:HTTP GET' 'keys:secret set'; do
    export FAIL="${pair#*:}"
    run boot vault-move --phase "${pair%%:*}" --apply
    [ "$status" -eq 1 ]
    assert_clean
    assert_no_secrets
  done
}

@test 'vault migration refuses lost lease, bad hash, missing retention and disabled historical keys' {
  export NO_LEASE=1
  run boot vault-move --phase backups --apply
  [ "$status" -eq 1 ]
  unset NO_LEASE
  export BAD_HASH=1
  run boot vault-move --phase backups --apply
  [ "$status" -eq 1 ]
  assert_clean
  unset BAD_HASH
  export NO_WORM=1
  run boot vault-move --phase backups --apply
  [ "$status" -eq 1 ]
  assert_clean
  unset NO_WORM
  export DISABLED_KEY=1
  run boot vault-move --phase keys --apply
  [ "$status" -eq 1 ]
  assert_clean
}

@test 'revoke targets only authorization leaves and verifies removal' {
  run boot --revoke --apply
  [ "$status" -eq 0 ]
  rg -- '-target=module.trust.*azurerm_federated_identity_credential' "$CALLS"
  refute_match -- '-target=module.trust.azurerm_resource_group' "$CALLS"
}

@test 'trust home cannot be removed, and public checkout is never an instance directory' {
  run trust --remove-target --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'trust home must remain registered'* ]]
  run boot cleanup --instance-dir "$BATS_TEST_DIRNAME/.." --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'outside the public checkout'* ]]
}

@test 'all remaining modes default to dry-run without key generation or remote mutations' {
  run boot age-key --custody vault --escrow-cmd escrow
  [ "$status" -eq 0 ]
  run boot tailscale --token-stdin
  [ "$status" -eq 0 ]
  for phase in state backups keys; do
    run boot vault-move --phase "$phase"
    [ "$status" -eq 0 ]
  done
  sha=$(printf 'a%.0s' {1..40})
  run boot rulesets --source-repo sample/source --release-sha "$sha"
  [ "$status" -eq 0 ]
  run boot restore-assist --backup-account sourcebackups --blob backups/retained.age --staging-blob restore/unique.age --ephemeral-recipient age1ephemeral --key-cmd key-reader
  [ "$status" -eq 0 ]
  refute_match 'assignment create|secret set|age-keygen|HTTP PUT|GH_BODY' "$CALLS"
  assert_clean
}

@test 'init dispatches pinned per-environment probe but leaves setup incomplete until reviewers wait' {
  run boot init --org sample --instance-repo instance --reviewer first --source-repo sample/source --release-sha "$(printf 'a%.0s' {1..40})" --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'Setup incomplete'* ]]
  rg 'sf-bootstrap-probe.yml/dispatches' "$CALLS"
  python3 - <<'PY'
import base64,json,os
from pathlib import Path
x=json.loads(Path(os.environ['FAKE_DB']).read_text())
y=base64.b64decode(x['files']['.github/workflows/sf-bootstrap-probe.yml']).decode()
for env in ('plan','production','ops','restore'):
 assert 'environment: '+env in y
assert y.count('id-token: write') == 4
assert not json.loads((Path(os.environ['SF_INSTANCE_DIR'])/'config/receipts/bootstrap.json').read_text())['gates_verified']
PY
}

@test 'age-key cleans after failed grant propagation and key generation' {
  export BAD_ROLE=1
  run boot age-key --custody vault --escrow-cmd escrow --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'has not propagated'* ]]
  assert_clean
  unset BAD_ROLE
  export FAIL=age-keygen
  run boot age-key --custody vault --escrow-cmd escrow --apply
  [ "$status" -eq 1 ]
  assert_clean
}

@test 'tailscale reports token cleanup failure without disclosing the token' {
  export BAD_REVOKE=1
  run bash -c 'printf "%s" tskey-api-fixture-secret | bash "$1" tailscale --token-stdin --apply' _ "$bootstrap"
  [ "$status" -eq 1 ]
  [[ "$output" == *'TOKEN CLEANUP FAILED'* ]]
  assert_no_secrets
}

@test 'state migration inventory failure stays pending and resumes only against destination' {
  export BAD_STATE=1
  run boot vault-move --phase state --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'lineage/serial/inventory mismatch'* ]]
  assert_clean
  unset BAD_STATE
  : > "$CALLS"
  run boot vault-move --phase state --apply
  [ "$status" -eq 0 ]
  # The pending trust backend is inspected at destination before other stacks.
  python3 - <<'PY'
import os
from pathlib import Path
lines=Path(os.environ['CALLS']).read_text().splitlines()
first=next(x for x in lines if x.startswith('TF_BACKEND '))
assert 'destinationstate' in first
PY
  assert_clean
}

@test 'backend migration init failure and destination key readback failure both clean all grants' {
  export FAIL='init -migrate-state'
  run boot vault-move --phase state --apply
  [ "$status" -eq 1 ]
  assert_clean
  unset FAIL
  export FAIL='secret show --id https://destinationvault'
  run boot vault-move --phase keys --apply
  [ "$status" -eq 1 ]
  assert_clean
  assert_no_secrets
}

@test 'static target caller additions preserve other targets workflow refs and await review' {
  python3 - <<'PY'
import json,os,base64
from pathlib import Path
home=Path(os.environ['SF_INSTANCE_DIR']);db=Path(os.environ['FAKE_DB'])
v=json.loads((home/'config/instance.tfvars.json').read_text());c=json.loads((home/'config/receipts/oidc.json').read_text())
x=json.loads(db.read_text())
for w in ('ph-ops','ph-power','ph-cost-guard','ph-backup-verify'):
 ref='sample/source/.github/workflows/'+w+'.yml@'+'a'*40
 v['workflow_refs']['ops'][w]=ref
 c['claims']['ops'][w]={'job_workflow_ref':ref,'sub':'repo:sample@7/instance@8:environment:ops:job_workflow_ref:'+ref}
 text='name: '+w+'\non: workflow_dispatch\npermissions: {}\njobs:\n  target-other:\n    uses: sample/source/.github/workflows/'+w+'.yml@'+'b'*40+' # v0.9.0\n    with:\n      target: other\n'
 x['files']['.github/workflows/'+w+'.yml']=base64.b64encode(text.encode()).decode()
(home/'config/instance.tfvars.json').write_text(json.dumps(v));(home/'config/receipts/oidc.json').write_text(json.dumps(c));db.write_text(json.dumps(x))
PY
  run trust --apply
  [ "$status" -eq 0 ]
  [[ "$output" == *'CODEOWNER review'* ]]
  for workflow in ph-ops ph-power ph-cost-guard ph-backup-verify; do
    rg 'target-primary:' "$SF_INSTANCE_DIR/.github/workflows/$workflow.yml"
    rg 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb # v0.9.0' "$SF_INSTANCE_DIR/.github/workflows/$workflow.yml"
  done
  refute_match 'PUT repos/sample/instance/contents/.github/workflows/ph-' "$CALLS"
  assert_clean
}

@test 'vault state migration isolates and verifies every named target backend' {
  python3 - <<'PY'
import json,os
from pathlib import Path
home=Path(os.environ['SF_INSTANCE_DIR']);p=home/'migration.json'
x=json.loads(p.read_text())
for side in ('source','destination'):
 base=x[side]['backends'].pop('host')
 for name in ('alpha','beta'):
  x[side]['backends']['host-'+name]=dict(base,key='host-'+name+'.tfstate')
p.write_text(json.dumps(x))
p=home/'config/instance.tfvars.json';v=json.loads(p.read_text());v['targets']={'alpha':dict(v['targets']['primary']), 'beta':dict(v['targets']['primary'], subscription_resource_id='/subscriptions/'+str(__import__('uuid').uuid5(__import__('uuid').NAMESPACE_DNS, 'second.example.test')))};p.write_text(json.dumps(v))
PY
  run boot vault-move --phase state --apply
  [ "$status" -eq 0 ]
  rg 'migration-host-alpha/stacks/host' "$CALLS"
  rg 'migration-host-beta/stacks/host' "$CALLS"
  python3 - <<'PY'
import json,os
from pathlib import Path
s=json.loads((Path(os.environ['SF_INSTANCE_DIR'])/'config/receipts/vault-move.json').read_text())['states']
assert set(s)=={'trust','host-alpha','host-beta','tailnet'}
assert all(v['status']=='verified' for v in s.values())
PY
  assert_clean
}

@test 'bootstrap calls and receipts match parsed WP4 imports variables and state settings' {
  export STATE_MISSING=1
  run trust --apply
  [ "$status" -eq 0 ]
  run python3 "$BATS_TEST_DIRNAME/verify_contracts.py"
  [ "$status" -eq 0 ]
  assert_clean
}

@test 'Owner registers all final namespaces and EncryptionAtHost before Terraform and fails closed while pending' {
  export FEATURE_MISSING=1
  run trust --apply
  [ "$status" -eq 0 ]
  python3 - <<'PY'
import os
from pathlib import Path
lines=Path(os.environ['CALLS']).read_text().splitlines()
feature=next(i for i,l in enumerate(lines) if 'feature register' in l)
registered=next(i for i,l in enumerate(lines) if i>feature and 'feature show' in l)
compute=next(i for i,l in enumerate(lines) if i>registered and 'provider register --namespace Microsoft.Compute' in l)
assert feature<registered<compute<next(i for i,l in enumerate(lines) if l.startswith('terraform '))
for namespace in ('Compute','Network','Logic','Insights','Consumption','DevTestLab','ManagedIdentity','Storage','KeyVault'):
 assert any('provider show --namespace Microsoft.'+namespace+' ' in l for l in lines)
PY
  : > "$CALLS"
  export FEATURE_STUCK=1
  run trust --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'EncryptionAtHost registration has not completed'* ]]
  refute_match '^terraform ' "$CALLS"
  assert_clean
}

@test 'trust home survives a lexically earlier subscription and cross-tenant receipt selects real plan twins' {
  export CROSS_TENANT=1
  run trust --apply
  [ "$status" -eq 0 ]
  run boot trust --target a0 --vault-subscription "$VAULT_SUB" --subscription "$TARGET_SUB" --resource-group rg-new --location other-region --apply
  [ "$status" -eq 0 ]
  python3 - <<'PY'
import json,os
from pathlib import Path
home=Path(os.environ['SF_INSTANCE_DIR']);v=json.loads((home/'config/instance.tfvars.json').read_text())
assert v['role_home_target']=='primary' and set(v['targets'])=={'primary','a0'}
r=json.loads((home/'config/targets/a0.json').read_text())
assert r['client_ids']['vault']['plan'] != r['client_ids']['target']['plan']
assert r['plan_inputs']['target']['target_client_id']==r['client_ids']['target']['plan']
assert r['plan_inputs']['vault']['client_id']==r['client_ids']['vault']['plan']
PY
  run trust --role-home-target a0 --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'immutable'* ]]
  run boot trust --target primary --vault-subscription "$VAULT_SUB" --subscription "$TARGET_SUB" --resource-group rg-example --location moved-region --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'immutable'* ]]
  assert_clean
}

@test 'host-owned RG initialization orders target roles then host RG then full trust preserving host receipt inputs' {
  export HOST_RG_MISSING=1
  python3 - <<'PY'
import base64,json,os
from pathlib import Path
home=Path(os.environ['SF_INSTANCE_DIR']);p=home/'bootstrap.json';v=json.loads(p.read_text())
v['backends']['host-primary']=dict(v['backends']['trust'],key='host-primary.tfstate');p.write_text(json.dumps(v))
# Supply every required host-root input, including values the RG-only apply
# must preserve from its receipt. No VM is planned/applied by this bootstrap.
root=Path.cwd()/'hosting/paperclip-hermes/terraform/stacks/host/variables.tf'
import re
v={k: 'fixture' for k,b in re.findall(r'^variable "([^"]+)" \{\n(.*?)^}',root.read_text(),re.M|re.S) if not re.search(r'^\s*default\s*=',b,re.M)}
v.update(budget_start='obsolete input', rg_managed_by='host',resource_group_name='rg-example',subscription_resource_id='/subscriptions/'+os.environ['TARGET_SUB'],protocol_tags={k:'original' for k in ('sf-approved-sha','sf-recovery-sha','sf-recovery-until','sf-delivery-recipient','sf-maintenance-until')},bootstrap_custom_data=base64.b64encode(b'original bootstrap').decode())
(home/'config/host-primary.tfvars.json').write_text(json.dumps(v))
PY
  run trust --scope subscription --apply
  [ "$status" -eq 0 ]
  python3 - <<'PY'
import json,os
from pathlib import Path
lines=Path(os.environ['CALLS']).read_text().splitlines()
roles=next(i for i,l in enumerate(lines) if '-target=module.trust.azurerm_role_definition.target' in l)
rg=next(i for i,l in enumerate(lines) if '-target=module.host.module.workload_rg[0]' in l)
full=next(i for i,l in enumerate(lines) if 'trust apply ' in l and '-target=' not in l)
assert roles<rg<full
v=json.loads((Path(os.environ['SF_INSTANCE_DIR'])/'config/host-primary.tfvars.json').read_text())
assert v['deallocator_role_id'].endswith('/roleDefinitions/deallocator-role')
assert 'budget_start' not in v
assert len(v['protocol_tags'])==5 and v['protocol_tags']['sf-maintenance-until']=='original'
assert __import__('base64').b64decode(v['bootstrap_custom_data'])==b'original bootstrap'
PY
  run python3 "$BATS_TEST_DIRNAME/verify_contracts.py"
  [ "$status" -eq 0 ]
  assert_clean
}

@test 'prefix boundaries agree with Terraform and invalid prefixes fail before mutations' {
  for prefix in a abcdefghijklmnopq Upper bad-dash abcdefghijklmnopqr; do
    python3 - "$prefix" <<'PY'
import json,os,sys
from pathlib import Path
p=Path(os.environ['SF_INSTANCE_DIR'])/'config/instance.tfvars.json';v=json.loads(p.read_text());v['prefix']=sys.argv[1];p.write_text(json.dumps(v))
PY
    : > "$CALLS"
    run trust
    if [[ $prefix == a || $prefix == abcdefghijklmnopq ]]; then
      [ "$status" -eq 0 ]
    else
      [ "$status" -eq 1 ]
      refute_match 'provider register|assignment create|group create|storage account create' "$CALLS"
    fi
  done
}

@test 'temporary storage roles refuse inherited RG locks without removing account locks' {
  export PARENT_LOCK=1
  run trust --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'unlocked vault RG'* ]]
  refute_match 'assignment create|lock delete' "$CALLS"
  run boot vault-move --phase backups --apply
  [ "$status" -eq 1 ]
  refute_match 'assignment create|lock delete' "$CALLS"
  assert_clean
}

@test 'restore staging account and receipt match Terraform separate WORM-free staging' {
  run boot restore-assist --backup-account sourcebackups --blob backups/retained.age --staging-blob restore/unique.age --ephemeral-recipient age1ephemeral --key-cmd key-reader --apply
  [ "$status" -eq 0 ]
  rg 'HTTP PUT https://examplestaging.blob.core.windows.net/restore-staging/' "$CALLS"
  refute_match 'HTTP PUT https://sourcebackups' "$CALLS"
  python3 - <<'PY'
import json,os
from pathlib import Path
r=json.loads((Path(os.environ['SF_INSTANCE_DIR'])/'config/receipts/restore-assist.json').read_text())
assert r['staging_account']=='examplestaging' and r['staging_container']=='restore-staging'
PY
  run boot restore-assist --backup-account sourcebackups --staging-account sourcebackups --blob backups/retained.age --staging-blob restore/other.age --ephemeral-recipient age1ephemeral --key-cmd key-reader --apply
  [ "$status" -eq 1 ]
  assert_no_secrets
}

pilot_config() {
  python3 - <<'PY'
import json,os
from pathlib import Path
home=Path(os.environ['SF_INSTANCE_DIR']);p=home/'config/instance.tfvars.json';v=json.loads(p.read_text());v['pilot_end_date']='2000-01-01T00:00:00Z';p.write_text(json.dumps(v))
(home/'pilot-closure.json').write_text(json.dumps({'paused':True,'lock_id':'fixture-lease'}))
PY
}

@test 'pilot closure locks versions with unchanged expiry only after full trust and verifies cleanup before receipt' {
  pilot_config
  run trust --close-pilot --apply
  [ "$status" -eq 0 ]
  rg 'Storage Blob Data Owner.*resourceGroups/rg-example-vault' "$CALLS"
  rg 'HTTP PUT.*versionid=pilot-version' "$CALLS"
  python3 - <<'PY'
import json,os
from pathlib import Path
home=Path(os.environ['SF_INSTANCE_DIR']);r=json.loads((home/'config/receipts/pilot-closure.json').read_text())
assert r['versions_locked']==1 and r['inventory_verified'] and r['temporary_roles_removed']
assert r['account']=='examplebackups' and r['lock_id']=='fixture-lease'
lines=Path(os.environ['CALLS']).read_text().splitlines()
assert next(i for i,l in enumerate(lines) if 'trust apply ' in l)<next(i for i,l in enumerate(lines) if 'HTTP PUT' in l)
PY
  assert_clean
  run trust --close-pilot --apply
  [ "$status" -eq 0 ]
  assert_clean
  assert_no_secrets
}

@test 'pilot closure refuses missing pause lease early dates and incomplete locking and cleans failures' {
  run trust --close-pilot --apply
  [ "$status" -eq 1 ]
  pilot_config
  export NO_LEASE=1
  run trust --close-pilot --apply
  [ "$status" -eq 1 ]
  assert_clean
  unset NO_LEASE
  for fail in 'HTTP PUT' none; do
    export FAIL="$fail" PILOT_STUCK=1
    run trust --close-pilot --apply
    [ "$status" -eq 1 ]
    python3 - <<'PYCODE'
import json,os
from pathlib import Path
assert not json.loads((Path(os.environ['SF_INSTANCE_DIR'])/'config/receipts/pilot-closure.json').read_text())['inventory_verified']
PYCODE
    assert_clean
    assert_no_secrets
  done
}

@test 'trust refuses Azure federation output differing from real probe claims' {
  export BAD_FEDERATION=1
  run trust --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'Azure federation differs from probe'* ]]
  assert_clean
}

@test 'target removal requires destruction receipt and retains removed subscription alias through the final apply' {
  export SECOND_SUB
  SECOND_SUB=$(python3 -c "import uuid; print(uuid.uuid5(uuid.NAMESPACE_DNS, 'removed.example.test'))")
  python3 - <<'PY'
import json,os
from pathlib import Path
p=Path(os.environ['SF_INSTANCE_DIR'])/'config/instance.tfvars.json';v=json.loads(p.read_text());v['targets']['beta']=dict(v['targets']['primary'], subscription_resource_id='/subscriptions/'+os.environ['SECOND_SUB']);p.write_text(json.dumps(v))
PY
  run boot trust --target beta --vault-subscription "$VAULT_SUB" --subscription "$SECOND_SUB" --resource-group rg-example --location example-region --remove-target --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'Verified destruction receipt'* ]]
  refute_match 'assignment create|^terraform ' "$CALLS"
  python3 - <<'PY'
import json,os
from pathlib import Path
(Path(os.environ['SF_INSTANCE_DIR'])/'config/receipts/destroy-beta.json').write_text(json.dumps({'target':'beta','destroyed':True}))
PY
  run boot trust --target beta --vault-subscription "$VAULT_SUB" --subscription "$SECOND_SUB" --resource-group rg-example --location example-region --remove-target --apply
  [ "$status" -eq 0 ]
  python3 - <<'PY'
import json,os,shlex
from pathlib import Path
home=Path(os.environ['SF_INSTANCE_DIR']);v=json.loads((home/'config/instance.tfvars.json').read_text())
assert set(v['targets'])=={'primary'} and v['role_home_target']=='primary'
r=json.loads((home/'config/targets/beta.json').read_text());assert r['status']=='removed'
line=next(l for l in Path(os.environ['CALLS']).read_text().splitlines() if l.startswith('terraform '))
root=Path(shlex.split(line)[1].split('=',1)[1]);providers=json.loads((root/'workload-subscriptions.tf.json').read_text())['provider']['azapi']
assert {p['subscription_id'] for p in providers}=={os.environ['SECOND_SUB'],os.environ['TARGET_SUB']}
PY
  assert_clean
}

@test 'adopting existing targets requires an explicit trust home before any mutation' {
  python3 - <<'PY'
import json,os
from pathlib import Path
p=Path(os.environ['SF_INSTANCE_DIR'])/'config/instance.tfvars.json';v=json.loads(p.read_text());v.pop('role_home_target');p.write_text(json.dumps(v))
PY
  run trust --apply
  [ "$status" -eq 1 ]
  [[ "$output" == *'--role-home-target'* ]]
  refute_match 'provider register|assignment create|^terraform ' "$CALLS"
  run trust --role-home-target primary --apply
  [ "$status" -eq 0 ]
  assert_clean
}
