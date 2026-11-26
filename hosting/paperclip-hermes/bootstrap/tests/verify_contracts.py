#!/usr/bin/env python3
"""Compare executed bootstrap calls/receipts with the real WP4 HCL interfaces.

Top-level HCL blocks end at column zero in these fmt-checked sources. We parse
those boundaries, rather than keeping a second list of variables/import IDs.
"""
import json
import os
from pathlib import Path
import re
import shlex

BASE = Path(__file__).resolve().parents[2] / 'terraform'
HOME = Path(os.environ['SF_INSTANCE_DIR'])
LINES = Path(os.environ['CALLS']).read_text().splitlines()


def blocks(text, kind):
    return re.findall(r'^' + kind + r'(?: "([^"]+)")? \{\n(.*?)^}', text, re.M | re.S)


def variables(stack):
    return {name: body for path in (BASE / 'stacks' / stack).glob('*.tf')
            for name, body in blocks(path.read_text(), 'variable')}


def value(text, name):
    return json.loads(re.search(r'\b' + name + r'\s*=\s*("[^"\n]*")', text)[1])


def expand(text, values):
    return re.sub(r'\$\{var\.(\w+)\}', lambda m: str(values[m[1]]), text)


rendered = {v['directory']: v for line in LINES if line.startswith('TF_RENDER ') for v in [json.loads(line[len('TF_RENDER '):])]}

calls, applied = [], {}
for line in LINES:
    if line.startswith('terraform '):
        argv = shlex.split(line)
        directory = Path(argv[1].split('=', 1)[1])
        calls.append((directory, argv[2:]))
    if line.startswith('TF_VARS '):
        directory, args = calls[-1]
        stack = directory.name
        supplied = json.loads(line[len('TF_VARS '):])
        declared = variables(stack)
        required = {k for k, v in declared.items() if not re.search(r'^\s*default\s*=', v, re.M)}
        assert not supplied.keys() - declared.keys(), (stack, 'undeclared inputs', supplied.keys() - declared.keys())
        assert required <= supplied.keys(), (stack, 'missing inputs', required - supplied.keys())
        applied[stack] = supplied
        assert directory.resolve().is_relative_to(HOME)
        # The fixture snapshots non-secret source while Terraform is running.
        # The actual working tree must be gone when bootstrap returns.
        assert not directory.exists()
        assert rendered[str(directory)]['host_prevent_destroy'] == 1
        if stack == 'trust':
            generated = rendered[str(directory)]['workload-subscriptions.tf.json']
            subscriptions = {t['subscription_resource_id'].split('/')[2] for t in supplied['targets'].values()}
            assert subscriptions <= {p['subscription_id'] for p in generated['provider']['azapi']}
            assert all(p['skip_provider_registration'] is True and p['client_id'] == '${var.target_client_id}' for p in generated['provider']['azapi'])
            assert 'merge(' in rendered[str(directory)]['composition_override.tf.json']['module']['trust']['workload_resource_group_ids']

if 'trust' in applied:
    supplied = applied['trust']
    imported = {}
    for _, body in blocks((BASE / 'stacks/trust/main.tf').read_text(), 'import'):
        address = re.search(r'^\s*to\s*=\s*(.+)', body, re.M)[1].strip()
        imported[address] = expand(value(body, 'id'), supplied)
    resource = next(shlex.split(line) for line in LINES if line.startswith('az resource create '))
    container_id = resource[resource.index('--id') + 1]
    lock = next(shlex.split(line) for line in LINES if line.startswith('az lock create '))
    account_id = lock[lock.index('--resource') + 1]
    lock_id = account_id + '/providers/Microsoft.Authorization/locks/' + lock[lock.index('--name') + 1]
    assert imported == {
        'module.trust.azurerm_resource_group.vault': account_id.split('/providers/')[0],
        'module.trust.module.state.azapi_resource.this': account_id,
        'module.trust.module.state.module.containers["tfstate"].azapi_resource.this': container_id,
        'module.trust.module.state.azapi_resource.lock[0]': lock_id,
    }, imported
    state = dict(blocks((BASE / 'modules/trust/storage.tf').read_text(), 'module'))['state']
    updates = next(shlex.split(line) for line in LINES if line.startswith('az storage account blob-service-properties update '))
    blob_days, container_days = re.findall(r'(?:container_)?delete_retention_policy\s*=\s*\{ days = (\d+), enabled = true \}', state)
    assert updates[updates.index('--delete-retention-days') + 1] == blob_days
    assert updates[updates.index('--container-delete-retention-days') + 1] == container_days
    for line in LINES:
        if line.startswith('az storage account create '):
            argv = shlex.split(line)
            assert argv[argv.index('--sku') + 1] == value(state, 'account_sku_name')
            assert argv[argv.index('--default-action') + 1] == 'Allow'
            assert argv[argv.index('--public-network-access') + 1] == 'Enabled'
            assert argv[argv.index('--default-to-oauth-authentication') + 1] == 'true'
    target = json.loads((HOME / 'config/targets/primary.json').read_text())
    assert target['role_home_target'] == supplied['role_home_target']
    assert supplied['target_client_id'] is None  # Owner CLI; workflow selects twins from receipt.
    assert target['plan_inputs']['target']['target_client_id'] == target['client_ids']['target']['plan']
    assert target['plan_inputs']['vault']['client_id'] == target['client_ids']['vault']['plan']
    assert 'target_client_id' in variables('trust')
    config = json.loads((HOME / 'bootstrap.json').read_text())
    assert config['backup_account'] == supplied['prefix'] + 'backups'
    assert config['staging_account'] == supplied['prefix'] + 'staging'
    assert config['key_vault_name'] == 'kv-' + supplied['prefix']
    for identity in ('plan', 'deploy', 'ops', 'restore'):
        assert any('--name id-sf-' + identity in line for line in LINES if line.startswith('az identity show'))
    # Subscription authorization reads and RG Reader grants are verified on the
    # actual same/cross-tenant Terraform mock plans by check-mock-plans.py.
    trust_source = (BASE / 'modules/trust/main.tf').read_text()
    assert 'Microsoft.Authorization/*/read' in trust_source
    assert 'drill_read' in trust_source and 'trust_read' in trust_source
    federation = trust_source.split('federations = ', 1)[1].split('backup_account_id', 1)[0]
    fields = set(re.findall(r'^\s*(\w+)\s*=', federation, re.M))
    for entries in target['authorization']['federations'].values():
        for credential in entries.values():
            assert set(credential) == fields, (set(credential), fields)
            assert credential['issuer'] == 'https://token.actions.githubusercontent.com'
            assert credential['audience'] == ['api://AzureADTokenExchange']

for line in LINES:
    if line.startswith('az role assignment create ') and 'Storage Blob Data' in line:
        argv = shlex.split(line)
        assert re.fullmatch(r'/subscriptions/[^/]+/resourceGroups/[^/]+', argv[argv.index('--scope') + 1])

print('Bootstrap calls, four import IDs, required/declared inputs, storage settings and receipt contracts match WP4.')
