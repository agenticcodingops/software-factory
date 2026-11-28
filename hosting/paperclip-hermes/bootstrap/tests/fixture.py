#!/usr/bin/env python3
"""Fail-closed fake CLIs: unknown commands fail; secret bodies never reach logs/disk."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
import zipfile
import uuid

# Test harness control data is outside the production child environment.
fixture_env = Path(sys.argv[0]).parent / 'fixture-environment.json'
if fixture_env.exists():
    os.environ.update(json.loads(fixture_env.read_text()))

HOME = Path(os.environ['SF_INSTANCE_DIR'])
DB = Path(os.environ['FAKE_DB'])
LOG = Path(os.environ['CALLS'])
SECRET = 'fixture-' + 'private-material'
RECIPIENT = 'age1fixturepublicrecipient'
FP = hashlib.sha256(RECIPIENT.encode()).hexdigest()
SHA = 'a' * 40
REF = 'sample/source/.github/workflows/sf-oidc-probe.yml@' + SHA
ENVS = {'plan': 'plan', 'deploy': 'production', 'ops': 'ops', 'restore': 'restore', 'mint': 'production'}
PREFIX = 'repo:sample@7/instance@8'
VAULT_SUB = str(uuid.uuid5(uuid.NAMESPACE_DNS, 'vault.example.test'))
TARGET_SUB = str(uuid.uuid5(uuid.NAMESPACE_DNS, 'target.example.test'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(value))
    os.replace(temporary, path)


def initialize():
    files = {'plan': 'ph-plan', 'deploy': 'ph-deploy', 'restore': 'ph-restore', 'mint': 'ph-deploy'}
    refs = {i: {'bootstrap': 'example-org/example-instance/.github/workflows/' + f + '.yml@' + SHA} for i, f in files.items()}
    refs['ops'] = {}
    claims = {i: {'bootstrap': {'sub': PREFIX + ':environment:' + ENVS[i] + ':job_workflow_ref:' + entries['bootstrap'],
                              'job_workflow_ref': entries['bootstrap']}} for i, entries in refs.items() if entries}
    claims['ops'] = {}
    variables = {'prefix': 'example', 'state_account_name': 'examplestate', 'location': 'example-region', 'workflow_refs': refs,
                 'custody': 'vault', 'pilot_end_date': '2030-01-01T00:00:00Z', 'monthly_credit': 100, 'role_home_target': 'primary',
                 'targets': {'primary': {'subscription_resource_id': '/subscriptions/' + TARGET_SUB, 'resource_group_name': 'rg-example', 'location': 'example-region', 'rg_managed_by': 'trust'}}}
    write(HOME / 'bootstrap.json', {'org': 'sample', 'operator_login': 'operator', 'instance_repo': 'instance', 'subscription': VAULT_SUB, 'staging_account': 'examplestaging',
          'backends': {'trust': {'storage_account_name': 'examplestate', 'resource_group_name': 'rg-example-vault', 'subscription_id': VAULT_SUB, 'tenant_id': 'tenant-placeholder', 'key': 'trust.tfstate'}},
          'key_vault_id': f'/subscriptions/{VAULT_SUB}/resourceGroups/rg-example-vault/providers/Microsoft.KeyVault/vaults/examplevault',
          'key_vault_name': 'examplevault'})
    write(HOME / 'config/instance.tfvars.json', variables)
    write(HOME / 'config/receipts/bootstrap.json', {'gates_verified': True, 'reviewers': [1]})
    write(HOME / 'config/receipts/oidc.json', {'run_id': 1, 'sub_claim_prefix': PREFIX, 'claims': claims})
    write(HOME / 'probe-request.json', {'nonce': 'fixture-nonce', 'refs': refs, 'caller_sha': SHA})
    write(HOME / 'config/age-keys.json', {FP: {'recipient': RECIPIENT, 'version_uri': f'https://sourcevault.vault.azure.net/secrets/age-{FP}/version', 'enabled': True}})
    sides = {}
    for side in ('source', 'destination'):
        subscription = side + '-placeholder'
        base = f'/subscriptions/{subscription}/resourceGroups/rg-example-vault/providers/Microsoft.Storage/storageAccounts/{side}backups'
        sides[side] = {'prefix': 'example', 'subscription': subscription, 'state_account': side + 'state', 'backup_account': side + 'backups',
                       'state_scope': base.replace(side + 'backups', side + 'state') + '/blobServices/default/containers/tfstate', 'backups_scope': base + '/blobServices/default/containers/backups',
                       'key_vault_name': side + 'vault', 'key_vault_id': f'/subscriptions/{subscription}/resourceGroups/rg-example-vault/providers/Microsoft.KeyVault/vaults/{side}vault',
                       'backends': {s: {'storage_account_name': side + 'state', 'resource_group_name': 'rg-example-vault',
                                        'subscription_id': subscription, 'tenant_id': side + '-tenant-placeholder', 'key': s + '.tfstate'} for s in ('trust', 'host', 'tailnet')}}
    write(HOME / 'migration.json', dict(sides, paused=True, lock_id='fixture-lease'))
    envs = {}
    for env in ENVS.values():
        envs[env] = {'can_admins_bypass': False, 'deployment_branch_policy': None if env == 'plan' else {'protected_branches': False, 'custom_branch_policies': True},
                     'protection_rules': []}
        if env in ('production', 'restore'):
            envs[env]['protection_rules'] = [{'type': 'required_reviewers', 'prevent_self_review': False, 'reviewers': [{'reviewer': {'id': 1}}]}]
    write(DB, {'assignments': [], 'envs': envs, 'files': {}, 'variables': {}, 'rulesets': {}, 'federations': {}, 'blob_copies': {}, 'keys': [],
               'template': {'use_default': False, 'include_claim_keys': ['repo', 'context', 'job_workflow_ref'], 'use_immutable_subject': True}})


if sys.argv[1:] == ['init']:
    initialize()
    sys.exit(0)

state = json.loads(DB.read_text())
exe = Path(sys.argv[0]).name
args = sys.argv[1:]
command = exe + ' ' + ' '.join(args)
with LOG.open('a') as log:
    log.write(command + '\n')


def out(value=None, raw=False):
    write(DB, state)
    if value is not None:
        if raw:
            sys.stdout.buffer.write(value if isinstance(value, bytes) else value.encode())
        else:
            print(json.dumps(value))
    sys.exit(0)


def option(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def body():
    return json.load(sys.stdin)


def failure(label):
    if os.environ.get('FAIL') and os.environ['FAIL'] in label:
        print(SECRET, file=sys.stderr)  # An SDK can echo a secret on errors.
        sys.exit(42)


failure(command)
if exe in ('escrow', 'key-reader', 'age-keygen'):
    if exe == 'key-reader' or (exe == 'age-keygen' and '-y' not in args):
        out(SECRET + '\n', raw=True)
    else:
        require_key = sys.stdin.read()
        if require_key.strip() != SECRET:
            sys.exit(43)
        out(RECIPIENT + '\n', raw=True)
if exe == 'age':
    if '-d' in args:
        with open(option('-i')) as identity:
            if identity.read().strip() != SECRET:
                sys.exit(43)
        encrypted = sys.stdin.buffer.read()
        out(b'archive-plaintext', raw=True)
    else:
        plaintext = sys.stdin.buffer.read()
        if plaintext != b'archive-plaintext':
            sys.exit(44)
        out(b'reencrypted-ciphertext', raw=True)
if exe == 'tailscale':
    sys.exit(91)  # Bootstrap must never join the operator/CI device.
if exe == 'gh':
    method = option('--method', 'GET')
    path = args[args.index('--method') + 2] if '--method' in args else args[-1]
    value = body() if '--input' in args else None
    if value is not None:
        with LOG.open('a') as log:
            log.write('GH_BODY ' + json.dumps(value) + '\n')
    if path == 'user': out({'login': os.environ.get('GH_LOGIN', 'operator'), 'type': 'User'})
    if path == 'orgs/sample': out({'plan': {'name': os.environ.get('PLAN', 'enterprise')}})
    if path == 'orgs/sample/actions/permissions': out({'enabled_repositories': 'all', 'allowed_actions': 'all' if not os.environ.get('BAD_ACTIONS') else 'selected'})
    if path.startswith('users/'): out({'id': 2 if path.endswith('second') else 1})
    if path.startswith('orgs/sample/repos'):
        out([{'name': 'instance'}])
    if path == 'repos/sample/source': out({'id': 9})
    if path == 'repos/sample/instance': out({'private': True, 'default_branch': 'main', 'id': 8, 'name': 'instance', 'owner': {'login': 'sample', 'id': 7}})
    if 'oidc/customization/sub' in path:
        if method == 'PUT': state['template'] = value
        result = dict(state['template'])
        if os.environ.get('BAD_TEMPLATE'): result['include_claim_keys'] = ['repo']
        out(result)
    if '/git/matching-refs/' in path:
        branch = path.split('/heads/')[1]
        out([{'ref': 'refs/heads/' + branch}] if branch in state.get('branches', {}) else [])
    if path.endswith('/git/refs'):
        state.setdefault('branches', {})[value['ref'].removeprefix('refs/heads/')] = dict(state['files']); out({})
    if '/git/trees/' in path:
        branch = path.split('/git/trees/')[1].split('?')[0]
        if branch != 'main' and branch not in state.get('branches', {}): sys.exit(4)  # GitHub 404
        files = state['files'] if branch == 'main' else state['branches'][branch]
        out({'tree': [{'path': k, 'sha': 'file-sha'} for k in files], 'truncated': False})
    if '/pulls' in path:
        if method == 'POST':
            state.setdefault('pulls', []).append(value)
            out({'number': len(state['pulls']), 'html_url': 'https://example.test/pull/1'})
        out([dict(p, number=n) for n, p in enumerate(state.get('pulls', []), 1) if p.get('head') in path])
    if '/git/ref/heads/main' in path: out({'object': {'sha': SHA}})
    if '/contents/' in path:
        name = path.split('/contents/')[1].split('?')[0]
        if 'sf-merge-gate.yml' in name: out({'content': base64.b64encode(b'implemented gate').decode()})
        branch = value['branch'] if method == 'PUT' else path.split('?ref=')[1]
        if method == 'PUT' and branch == 'main': sys.exit(97)
        if branch != 'main' and branch not in state.get('branches', {}): sys.exit(4)  # GitHub 404
        files = state['files'] if branch == 'main' else state['branches'][branch]
        if method == 'PUT': files[name] = value['content']
        out({'sha': 'file-sha', 'content': files[name]})
    if '/environments/' in path:
        env = path.split('/environments/')[1].split('/')[0]
        if path.endswith('/deployment-branch-policies'):
            out({'branch_policies': [{'name': 'main', 'type': 'branch'}]})
        if method == 'PUT':
            if not set(value) <= {'deployment_branch_policy', 'reviewers', 'prevent_self_review', 'wait_timer', 'can_admins_bypass'}: sys.exit(98)
            state['envs'][env] = {'can_admins_bypass': value.get('can_admins_bypass', True), 'deployment_branch_policy': value['deployment_branch_policy'], 'protection_rules': []}
            if 'reviewers' in value:
                state['envs'][env]['protection_rules'] = [{'type': 'required_reviewers', 'prevent_self_review': value['prevent_self_review'],
                    'reviewers': [{'reviewer': {'id': r['id']}} for r in value['reviewers']]}]
        result = dict(state['envs'][env])
        if os.environ.get('ADMIN_BYPASS'): result['can_admins_bypass'] = True
        if os.environ.get('ADMIN_BYPASS') == 'missing': result.pop('can_admins_bypass', None)
        if os.environ.get('BAD_GATE') and env == 'production': result['protection_rules'] = []
        out(result)
    if '/actions/variables' in path:
        name = path.rsplit('/', 1)[1]
        if method == 'GET' and '?' in path: out({'variables': list(state['variables'].values()), 'total_count': len(state['variables'])})
        if method == 'DELETE': state['variables'].pop(name, None); out({})
        if method != 'GET': state['variables'][value['name']] = value; out({})
        out(state['variables'][name])
    if '/pending_deployments' in path:
        out([] if os.environ.get('NO_WAIT') else [{'environment': {'name': e}, 'reviewers': [{'id': 1}]} for e in ('production', 'restore')])
    if '/actions/runs/' in path:
        if path.endswith('/logs'):
            request = json.loads((HOME / 'probe-request.json').read_text())
            records, seen = [], set()
            for i, entries in request['refs'].items():
                for key, ref in entries.items():
                    file = ref.rsplit('/', 1)[1].split('@')[0]
                    if file == 'sf-merge-gate.yml' and not state.get('gate_producer'): continue
                    if state.get('gate_producer') and (file != 'sf-merge-gate.yml' or state.get('missing_gate_claim')): continue
                    envs = ['production', 'restore'] if i == 'deploy' and file in ('ph-move.yml', 'ph-restore.yml', 'ph-vault-move.yml') else [ENVS[i]]
                    for env in envs:
                        if (env, ref) in seen: continue
                        seen.add((env, ref))
                        c = {'environment': env, 'sub': PREFIX + ':environment:' + env + ':job_workflow_ref:' + ref,
                             'job_workflow_ref': ref, 'iss': 'https://token.actions.githubusercontent.com', 'aud': 'api://AzureADTokenExchange', 'nonce': request['nonce']}
                        if os.environ.get('BAD_CLAIM'): c[os.environ['BAD_CLAIM']] = 'wrong'
                        records.append(c)
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as z:
                z.writestr('probe.txt', '\n'.join('SF_OIDC_CLAIMS=' + json.dumps(c) for c in records))
            out(data.getvalue(), raw=True)
        request = json.loads((HOME / 'probe-request.json').read_text())
        out({'id': 1, 'event': 'pull_request' if state.get('gate_producer') else 'workflow_dispatch', 'head_branch': 'main', 'head_sha': SHA,
             'referenced_workflows': [{'path': ref} for ref in sorted({r for entries in request['refs'].values() for r in entries.values() if state.get('gate_producer') or 'sf-merge-gate.yml@' not in r})], 'path': '.github/workflows/sf-bootstrap-probe.yml', 'conclusion': 'success'})
    if path.endswith('/dispatches'): out({})
    if '/tags?' in path: out([{'name': 'v1.0.' + str(n), 'commit': {'sha': sha}} for n, sha in enumerate(state.get('release_shas', [SHA]))])
    if '/git/ref/tags/' in path:
        n = int(path.rsplit('.', 1)[1]); sha = state.get('release_shas', [SHA])[n]
        out({'object': {'type': 'tag' if state.get('annotated') else 'commit', 'sha': sha}})
    if '/git/tags/' in path: out({'object': {'type': 'commit', 'sha': path.rsplit('/', 1)[1]}})
    if '/releases/tags/' in path:
        out(dict({'tag_name': path.rsplit('/', 1)[1], 'draft': False, 'prerelease': False, 'published_at': '2026-11-28T00:00:00Z'}, **state.get('release_overrides', {})))
    if '/compare/' in path: out({'status': state.get('compare_status', 'ahead')})
    if '/rulesets' in path:
        endpoint = path.split('?')[0]
        parts = endpoint.rsplit('/', 1)
        if parts[1].isdigit():
            store, key = parts[0], parts[1]
            if method == 'PUT': state['rulesets'][store][key] = dict(value, id=int(key))
            result = dict(state['rulesets'][store][key])
            if os.environ.get('BAD_RULESET'): result['enforcement'] = 'disabled'
            out(result)
        rules = state['rulesets'].setdefault(endpoint, {})
        if method == 'POST':
            key = str(len(rules) + 1); rules[key] = dict(value, id=int(key)); out(rules[key])
        out(list(rules.values()))
if exe == 'az':
    if args[:2] == ['vm', 'list']:
        out([{'id': '/mock/virtualMachines/target'}] if not state.get('no_vm') else [])
    if args[:2] == ['vm', 'show']:
        out({'tags': state.get('target_tags', {'sf-approved-sha': SHA})})
    if args[:2] == ['account', 'show']:
        out({'user': {'type': 'servicePrincipal' if os.environ.get('APP_LOGIN') else 'user'}, 'tenantId': 'target-tenant-placeholder' if os.environ.get('CROSS_TENANT') and option('--subscription') != VAULT_SUB else 'tenant-placeholder',
             'subscriptionPolicies': {'spendingLimit': os.environ.get('SPENDING', 'Off')}})
    if args[:3] == ['ad', 'signed-in-user', 'show']: out({'id': 'operator-placeholder'})
    if args[:2] == ['account', 'get-access-token']: out({'accessToken': 'fixture-' + 'access-material'})
    if args[:2] == ['identity', 'show']: out({'clientId': 'client-' + option('--resource-group') + '-' + option('--name')})
    if args[:2] == ['role', 'assignment']:
        action = args[2]
        if action == 'create':
            assignment = {'id': option('--scope') + '/providers/Microsoft.Authorization/roleAssignments/' + option('--name'),
                          'principalId': option('--assignee-object-id'), 'roleDefinitionName': option('--role'), 'scope': option('--scope')}
            state['assignments'].append(assignment)
            write(DB, state)
            if os.environ.get('SIGNAL_GRANT'):
                os.kill(os.getppid(), signal.SIGHUP if os.environ['SIGNAL_GRANT'] == 'HUP' else signal.SIGTERM)
                print(json.dumps(assignment))
                sys.exit(0)
            if os.environ.get('LOST_GRANT_RESPONSE'): sys.exit(42)
            out(assignment)
        if action == 'delete':
            if os.environ.get('SIGNAL_GRANT'):
                os.kill(os.getppid(), signal.SIGINT)
                os.kill(os.getppid(), signal.SIGHUP)
            if not os.environ.get('STICKY_ROLE'):
                state['assignments'] = [r for r in state['assignments'] if r['id'] != option('--ids')]
            out({})
        if option('--include-inherited', '') is not None and '--include-inherited' in args:
            out([] if os.environ.get('NO_OWNER') else [{'roleDefinitionName': 'Owner'}])
        out([dict(r, scope='wrong') for r in state['assignments']] if os.environ.get('BAD_ROLE') else state['assignments'])
    if args[:2] == ['feature', 'show']:
        out({'properties': {'state': 'NotRegistered' if os.environ.get('FEATURE_STUCK') or (os.environ.get('FEATURE_MISSING') and not state.get('feature_registered')) else 'Registered'}})
    if args[:2] == ['feature', 'register']: state['feature_registered'] = True; out({})
    if args[:2] == ['provider', 'register']: out({})
    if args[:2] == ['provider', 'show']: out({'registrationState': 'NotRegistered' if os.environ.get('PROVIDER_STUCK') else 'Registered'})
    if args[:2] == ['group', 'list']: out([{'name': 'rg-example-vault'}] + ([] if os.environ.get('HOST_RG_MISSING') and not state.get('host_rg') else [{'name': 'rg-example'}]))
    if args[:2] == ['group', 'show']: out({'name': option('--name')})
    if args[:2] == ['group', 'create']: out({})
    if args[:3] == ['storage', 'account', 'list']: out([] if os.environ.get('STATE_MISSING') else [{'name': 'examplestate'}])
    if args[:3] == ['storage', 'account', 'create']: out({})
    if args[:3] == ['storage', 'account', 'show']:
        out({'allowSharedKeyAccess': bool(os.environ.get('BAD_STORAGE')), 'allowBlobPublicAccess': False, 'enableHttpsTrafficOnly': True, 'minimumTlsVersion': 'TLS1_2', 'sku': {'name': 'Standard_GRS'}, 'publicNetworkAccess': 'Enabled', 'networkRuleSet': {'defaultAction': 'Allow'}, 'defaultToOAuthAuthentication': True})
    if args[:3] == ['storage', 'account', 'blob-service-properties']:
        out({'isVersioningEnabled': True, 'deleteRetentionPolicy': {'enabled': True, 'days': 7}, 'containerDeleteRetentionPolicy': {'enabled': True, 'days': 7}})
    if args[:2] == ['resource', 'create']: out({'properties': {'publicAccess': 'None'}})
    if args[:2] == ['resource', 'show']: out({'properties': {'publicAccess': 'None'}})
    if args[:2] == ['lock', 'create']: out({})
    if args[:2] == ['lock', 'list']:
        rg = f"/subscriptions/{option('--subscription')}/resourceGroups/rg-example-vault"
        locks = state.setdefault('storage_locks', {})
        if option('--subscription') not in locks:
            locks[option('--subscription')] = [
                {'name': name + '-cannot-delete', 'level': 'CanNotDelete', 'notes': 'retain ' + name,
                 'id': rg + '/providers/Microsoft.Storage/storageAccounts/example' + name + '/providers/Microsoft.Authorization/locks/' + name + '-cannot-delete'}
                for name in ('state', 'backups', 'staging')]
        out(locks[option('--subscription')] +
            ([{'id': rg + '/providers/Microsoft.Authorization/locks/parent-lock', 'level': 'CanNotDelete'}] if os.environ.get('PARENT_LOCK') else []))
    if args[:2] == ['lock', 'delete']:
        # Crash recovery must already be durable before the delete request.
        ledger = json.loads((HOME / 'locks.json').read_text())
        assert any(entry['id'] == option('--ids') for entry in ledger)
        locks = state['storage_locks'][option('--subscription')]
        locks[:] = [lock for lock in locks if lock['id'] != option('--ids')]
        out({})
    if args[:3] == ['rest', '--method', 'put']:
        lock_id = option('--url').removeprefix('https://management.azure.com').split('?')[0]
        subscription = lock_id.split('/')[2]
        lock = dict(json.loads(option('--body'))['properties'], id=lock_id, name=lock_id.rsplit('/', 1)[1])
        locks = state['storage_locks'][subscription]
        locks[:] = [old for old in locks if old['id'] != lock_id]
        locks.append(lock)
        if os.environ.get('BAD_LOCK_RESTORE'): locks[-1]['notes'] = 'wrong'
        out({})
    if args[:3] == ['keyvault', 'secret', 'list']: out([])
    if args[:3] == ['keyvault', 'secret', 'set']:
        value = sys.stdin.read()
        if value.strip() != SECRET: sys.exit(43)
        uri = f"https://{option('--vault-name')}.vault.azure.net/secrets/{option('--name')}/version"
        state['keys'].append(uri)
        out({'id': uri, 'enabled': True})
    if args[:3] == ['keyvault', 'secret', 'show']:
        out({'value': SECRET, 'attributes': {'enabled': not bool(os.environ.get('DISABLED_KEY'))}})
    if args[:3] == ['storage', 'blob', 'list']:
        if option('--account-name') == 'destinationbackups':
            from urllib.parse import urlsplit, unquote
            out([{'name': unquote(urlsplit(url).path.split('/backups/',1)[1])} for url in state['blob_copies'] if 'destinationbackups' in url])
        if '--include' in args:
            blob = {'name': 'backups/retained.age', 'versionId': 'historical-version', 'properties': {}}
            if 'm' in option('--include') and not os.environ.get('MISSING_METADATA'): blob['metadata'] = {'recipient_fp': FP}
            if 'i' in option('--include'): blob['properties']['immutabilityPolicy'] = {'policyMode': 'Locked', 'expiryTime': '2099-01-01T00:00:00Z'}
            out([blob])
        out([])
    if args[:4] == ['storage', 'blob', 'lease', 'renew']: out({'leaseId': option('--lease-id')})
    if args[:3] == ['storage', 'blob', 'show']:
        if option('--name') == 'migration.lock': out({'properties': {'lease': {'status': 'unlocked' if os.environ.get('NO_LEASE') else 'locked'}}})
        out({'metadata': {'recipient_fp': 'unknown' if os.environ.get('UNKNOWN_FP') else FP}, 'versionId': 'copied-version',
             'properties': {'immutabilityPolicy': {} if os.environ.get('NO_WORM') else {'policyMode': 'Locked', 'expiryTime': '2099-01-01T00:00:00Z'}}})
    if args[:2] == ['vm', 'list-usage']: out([{'name': {'value': 'Total Regional vCPUs'}, 'limit': 20, 'currentValue': 4}, {'name': {'value': 'standardBASv2Family'}, 'limit': 12, 'currentValue': 0}])
    if args[:1] == ['rest']: sys.stdin.read(); out({'properties': {'rows': [[10]], 'columns': [{'name': 'Cost', 'type': 'Number'}]}})
if exe == 'terraform':
    command_args = args[1:]
    directory = args[0].split('=', 1)[1]
    root = Path(directory)
    rendered = {'directory': directory, 'host_prevent_destroy': (root.parents[1] / 'modules/host/main.tf').read_text().count('prevent_destroy = true')}
    for name in ('workload-subscriptions.tf.json', 'composition_override.tf.json'):
        if (root / name).exists(): rendered[name] = json.loads((root / name).read_text())
    with LOG.open('a') as log: log.write('TF_RENDER ' + json.dumps(rendered) + '\n')
    if command_args[0] == 'init':
        config = sys.stdin.read()
        backend = dict(re.findall(r'(\w+) = "([^"]+)"', config))
        if not any(r['roleDefinitionName'] == 'Storage Blob Data Contributor' and r['scope'].startswith('/subscriptions/' + backend['subscription_id'] + '/') for r in state['assignments']): sys.exit(92)
        backend_key = backend['storage_account_name'] + '/' + backend['key']
        previous = state.setdefault('initialized', {}).get(directory)
        states = state.setdefault('backend_states', {})
        if '-migrate-state' in command_args:
            state['migrated'] = True
            if not previous: sys.exit(95)
            states[backend_key] = states.get(previous, {'lineage': 'fixture-lineage', 'serial': 9, 'resources': []})
        elif backend_key not in states:
            states[backend_key] = {'lineage': 'initialized-empty-lineage', 'serial': 0, 'resources': [], 'outputs': {}} if backend['storage_account_name'] == 'destinationstate' else {'lineage': 'fixture-lineage', 'serial': 9, 'resources': []}
        state['initialized'][directory] = backend_key
        with LOG.open('a') as log: log.write('TF_BACKEND ' + json.dumps(config) + '\n')
        out(b'', raw=True)
    if command_args[0] in ('apply', 'plan', 'destroy'):
        varfile = Path(next(a.split('=', 1)[1] for a in command_args if a.startswith('-var-file=')))
        if varfile.suffix != '.json' or varfile.stat().st_mode & 0o777 != 0o600: sys.exit(94)
        values = json.loads(varfile.read_bytes())
        with LOG.open('a') as log: log.write('TF_VARS ' + json.dumps(values) + '\n')
        state['tfvars'] = values
        state.setdefault('tfvars_by_stack', {})[Path(args[0].split('=', 1)[1]).name] = values
        if '-target=module.host.module.workload_rg[0]' in command_args: state['host_rg'] = True
        if command_args[0] == 'plan' and os.environ.get('TF_DRIFT'): sys.exit(2)
        if command_args[0] == 'destroy':
            # Azure rejects RBAC deletion beneath an account deletion lock.
            if any(state.get('storage_locks', {}).values()): sys.exit(96)
            state['revoked'] = True
        out(b'', raw=True)
    if command_args[0] == 'output':
        if command_args[-1] == 'deallocator_role_id': out('/subscriptions/' + TARGET_SUB + '/providers/Microsoft.Authorization/roleDefinitions/deallocator-role')
        vals = state['tfvars']
        claims = json.loads((HOME / 'config/receipts/oidc.json').read_text())['claims']
        out({'federations': {i: {k: {'name': 'github-' + k, 'subject': 'wrong' if os.environ.get('BAD_FEDERATION') else c['sub'], 'issuer': 'https://token.actions.githubusercontent.com', 'audience': ['api://AzureADTokenExchange']} for k, c in entries.items()} for i, entries in claims.items()},
             'target': {}, 'vault': {}, 'twins': []})
    if command_args[:1] == ['state'] and directory not in state.get('initialized', {}): sys.exit(93)
    if command_args[:2] == ['state', 'pull']:
        changed = os.environ.get('BAD_STATE') and state.get('migrated')
        state['pulled'] = True
        result = dict(state['backend_states'][state['initialized'][directory]])
        if changed: result['resources'] = [{'type': 'unexpected'}]
        out(result)
    if command_args[:2] == ['show', '-json']:
        roles = [{'address': f'module.trust.azurerm_role_assignment.operator["{name}"]', 'type': 'azurerm_role_assignment',
                  'values': {'scope': f'/subscriptions/{VAULT_SUB}/resourceGroups/rg-example-vault/providers/Microsoft.Storage/storageAccounts/example{name}/blobServices/default/containers/{name}'}}
                 for name in ('state', 'backups', 'staging')]
        out({'values': {'root_module': {'child_modules': [{'resources': roles}]}}})
    if command_args[:2] == ['state', 'list']:
        out('' if state.get('revoked') else 'module.trust.module.identities["ops"].azurerm_federated_identity_credential.this["github"]\nmodule.trust.azurerm_role_assignment.operator["state"]\nmodule.trust.azurerm_role_assignment.operator["backups"]\nmodule.trust.azurerm_role_assignment.operator["staging"]\nmodule.trust.azurerm_resource_group.vault\n', raw=True)
if exe == 'curl':
    config = sys.stdin.read()
    url = re.search(r'url = "(.*?)"', config)[1]
    method = re.search(r'request = "(.*?)"', config)[1]
    data_match = re.search(r'data-binary = "@(.*?)"', config)
    value = None
    if data_match:
        value = Path(data_match[1]).read_bytes()
    with LOG.open('a') as log:
        log.write('HTTP ' + method + ' ' + url + '\n')
    failure('HTTP ' + method + ' ' + url)
    if '--include' in args:
        if method == 'PUT':
            if 'x-ms-immutability-policy-until-date: Thu, 01 Jan 2099 00:00:00 GMT' not in config: sys.exit(98)
            state['pilot_locked'] = True
            out(b'HTTP/1.1 200 OK\r\nx-ms-immutability-policy-mode: Locked\r\nx-ms-immutability-policy-until-date: Thu, 01 Jan 2099 00:00:00 GMT\r\n\r\n', raw=True)
        mode = 'Locked' if state.get('pilot_locked') and not os.environ.get('PILOT_STUCK') else 'Unlocked'
        xml = f'<EnumerationResults><Blobs><Blob><Name>backups/pilot.age</Name><VersionId>pilot-version</VersionId><Properties><ImmutabilityPolicyMode>{mode}</ImmutabilityPolicyMode><ImmutabilityPolicyUntilDate>2099-01-01T00:00:00Z</ImmutabilityPolicyUntilDate></Properties></Blob></Blobs><NextMarker/></EnumerationResults>'
        out(b'HTTP/1.1 200 OK\r\n\r\n' + xml.encode(), raw=True)
    if 'api.tailscale.com' in url:
        if '--write-out' in args: out('200' if os.environ.get('BAD_REVOKE') else '401', raw=True)
        if url.endswith('/settings'):
            if os.environ.get('SIGNAL_TS'):
                os.kill(os.getppid(), signal.SIGHUP)
            out({'httpsEnabled': not bool(os.environ.get('NO_HTTPS'))})
        if url.endswith('/dns/preferences'): out({'magicDNS': not bool(os.environ.get('NO_DNS'))})
        if method == 'POST':
            request = json.loads(value)
            with LOG.open('a') as log: log.write('TS_BODY ' + json.dumps(request) + '\n')
            key = 'federation' + uuid.uuid4().hex
            state['federations'][key] = dict(request, id=key, audience='fixture-audience')
            out(state['federations'][key])
        if method == 'DELETE':
            key = url.rsplit('/', 1)[1]
            if key in state['federations']: del state['federations'][key]
            else: state['token_revoked'] = True
            out({})
        if url.endswith('/keys'): out({'keys': [{'id': key} for key in state['federations']]})
        key = url.rsplit('/', 1)[1]
        out(state['federations'][key])
    if method == 'PUT':
        # Ciphertext only, never private material or decrypted archives.
        if value in (SECRET.encode(), b'archive-plaintext'): sys.exit(90)
        state['blob_copies'][url] = base64.b64encode(value).decode(); out(b'', raw=True)
    result = base64.b64decode(state['blob_copies'][url]) if url in state['blob_copies'] else b'fixture-ciphertext'
    if os.environ.get('BAD_HASH') and 'destinationbackups' in url: result = b'corrupted'
    out(result, raw=True)
print('UNIMPLEMENTED FAKE: ' + command, file=sys.stderr)
sys.exit(99)
