"""WP3 host contracts and failure recovery; all credentials generated in memory."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest
import yaml

KIT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('control', KIT / 'kit-control.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
r = c.r


def delegation(**changes):
    now = datetime.now(timezone.utc)
    query = dict(sv='2023-11-03', sr='c', sp='cw', spr='https', sig=secrets.token_hex(32),
                 skoid=secrets.token_hex(16), sktid=secrets.token_hex(16), sks='b', skv='2023-11-03',
                 skt=(now - timedelta(minutes=1)).isoformat(),
                 ske=(now + timedelta(days=6)).isoformat(), se=(now + timedelta(days=5)).isoformat())
    for key, value in changes.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = value
    return 'https://storage.example.test/backups?' + urlencode(query)


def test_user_delegation_accept():
    assert r.sas_url(delegation(sp='cw')).scheme == 'https'


def test_create_only_sas_rejected_before_any_upload():
    with patch.object(r.urllib.request, 'build_opener') as opener, pytest.raises(r.RecoveryError):
        r.sas_url(delegation(sp='c'))
    opener.assert_not_called()


@pytest.mark.parametrize('change', [dict(si='policy'), dict(skoid=None), dict(sktid=None),
    dict(ske=None), dict(skt=None), dict(sks='f'), dict(se=None), dict(sp='wc'), dict(sp='rcw'),
    dict(sp='cwl'), dict(sp='cwd'), dict(sr='b'), dict(ss='b'), dict(srt='co'),
    dict(ske=(datetime.now(timezone.utc)+timedelta(days=8)).isoformat()),
    dict(ske=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()),
    dict(se=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()),
    dict(skt=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()),
    dict(ske='unparseable'), dict(ske='2026-01-01')])
def test_shared_key_disabled_rejects_service_account_policy_and_broad_sas(change):
    # Local acceptance gate rejects these before any request; disabling Shared
    # Key cannot be bypassed by substituting a service/account/stored-policy SAS.
    with pytest.raises(r.RecoveryError):
        r.sas_url(delegation(**change))


def test_upload_metadata_on_single_put_and_block_commit(tmp_path):
    archive = tmp_path / 'blob'
    archive.write_bytes(secrets.token_bytes(64))
    for limit in (100, 1):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 201
        response.headers = {'x-ms-version-id': 'test-version', 'ETag': 'test-etag'}
        with patch.object(r, 'SINGLE_PUT_LIMIT', limit), patch.object(r.urllib.request, 'build_opener', return_value=opener):
            result = r.upload(r.sas_url(delegation()), archive, 'unique.age', {'target': 'test', 'reason': 'nightly'})
        final = opener.open.call_args.args[0]
        assert final.get_header('X-ms-meta-target') == 'test'
        assert final.get_header('X-ms-meta-reason') == 'nightly'
        assert result['version'] == 'test-version'
        requests = [call.args[0] for call in opener.open.call_args_list]
        assert all(req.get_header('X-ms-version') == '2023-11-03' for req in requests)
        if limit == 1:
            assert '&comp=block&' in requests[0].full_url
            assert '&comp=blocklist' in requests[-1].full_url
        else:
            assert len(requests) == 1 and 'comp=block' not in final.full_url


@pytest.mark.parametrize('reason', ['nightly', 'pre-stop', 'pre-upgrade', 'canary', 'final'])
def test_metadata_and_durable_receipts(tmp_path, reason):
    archive = tmp_path / 'blob'
    archive.write_bytes(secrets.token_bytes(64))
    args = SimpleNamespace(reason=reason, canary=reason=='canary', target='fixture', vm_id='fixture-vm', stop_operation_id='stop-1')
    with patch.object(r, 'STATE_DIR', tmp_path / 'status'), patch.object(r, 'upload', return_value={'version': 'v1'}) as upload:
        receipt = r.publish_backup(args, 'age1'+secrets.token_hex(29), r.sas_url(delegation()), archive, 'unique.age', {'paperclip_schema': {'applied': [1]}})
    assert receipt == json.loads((tmp_path/('status/last-canary.json' if reason=='canary' else 'status/last-backup.json')).read_text())
    assert (tmp_path/('status/last-canary.json' if reason=='canary' else 'status/last-backup.json')).stat().st_mode & 0o777 == 0o600
    metadata = upload.call_args_list[0].args[3]
    assert set(metadata) >= {'kit_commit', 'image_digests', 'schema', 'recipient_fp', 'target', 'vm_id', 'backup_id', 'completed_at', 'reason'}
    assert receipt['sha256'] == r.digest(archive)
    assert receipt['data_backup'] == (reason != 'canary')
    assert upload.call_args_list[1].args[2].startswith('receipts/fixture/')
    assert ('stop_operation_id' in metadata) == (reason in ('pre-stop', 'final'))


def test_canary_does_not_touch_applications(tmp_path):
    args = SimpleNamespace(work_dir=tmp_path, target='test', vm_id='test', reason='canary')
    with patch.object(r, 'run') as run, patch.object(r, 'publish_backup', return_value={'blob':'canary'}) as publish, patch.object(r, 'Compose') as compose:
        r.canary(args, 'age1'+secrets.token_hex(29), r.sas_url(delegation()))
    compose.assert_not_called()
    assert run.call_args.args[0][0] == 'age'
    assert args.canary
    assert publish.call_count == 1


class Stack:
    def __init__(self, root, paperclip=True):
        self.kit, self.name = KIT, 'fixture'
        self.installed = ['postgres','paperclip','caddy'] if paperclip else ['hermes','sandbox','caddy']
        self.running = set(self.installed)
        self.calls = []
        self.volumes = {}
        for name in self.installed:
            path = root / name
            path.mkdir()
            (path/'state').write_text('old-data')
            self.volumes[name] = path
        self.fail_dump = self.fail_restart = self.fail_restore = False

    def fence(self, compose):
        assert compose is self
        self.calls.append(('fence-project',))
        self.running.clear()

    def images(self):
        return {service: {'image_id': 'fixture'} for service in self.installed}

    def services(self):
        return self.installed

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == 'start' and '--wait' in args:
            raise r.RecoveryError('unknown flag: --wait (Compose 2.38.2)')
        if args[0] == 'ps':
            return '\n'.join(s for s in self.installed if s in self.running)
        if args[0] == 'stop':
            self.running -= set(args[3:]) if len(args)>3 else self.running.copy()
        if args[0] == 'start' or args[0] == 'up':
            if self.fail_restart:
                raise r.RecoveryError('restart failed')
            self.running = {'postgres'} if '--no-deps' in args else set(self.installed)
        if 'pg_restore' in args:
            assert self.running == {'postgres'}
            assert kwargs['input_file'].read() == b'database dump'
            if self.fail_restore:
                raise r.RecoveryError('restore failed')
        if 'pg_dump' in args:
            assert 'postgres' in self.running
            assert not set(r.SERVICES) & self.running
            if self.fail_dump:
                raise r.RecoveryError('dump failed')
            kwargs['output'].write(b'database dump')
        if 'pg_database_size' in ' '.join(args):
            return '100'
        return ''


@pytest.mark.parametrize('paperclip', [True, False])
def test_rollback_point_sequence_and_restore_data(tmp_path, paperclip):
    stack = Stack(tmp_path, paperclip)
    point = tmp_path/'rollback'
    with patch.object(c, 'volume_map', return_value=stack.volumes), patch.object(c, 'fence_project', side_effect=stack.fence):
        record = c.rollback_point('create', point, stack)
        assert not stack.running
        assert point.stat().st_mode & 0o777 == 0o700
        for volume in stack.volumes.values():
            (volume/'state').write_text('migration floor: candidate data')
        c.rollback_point('restore', point, stack)
    assert stack.running == set(stack.installed)
    assert all((volume/'state').read_text() == 'old-data' for volume in stack.volumes.values())
    dumps = [index for index,args in enumerate(stack.calls) if 'pg_dump' in args]
    if paperclip:
        stop_db = stack.calls.index(('stop','--timeout','90','postgres'))
        assert dumps[0] < stop_db
        assert 'database.dump' in record['files']
    else:
        assert not dumps
        assert 'database.dump' not in record['files']


def test_dump_failure_restores_old_stack_and_prevents_archives(tmp_path):
    stack = Stack(tmp_path)
    stack.fail_dump = True
    with patch.object(c, 'volume_map') as volumes, pytest.raises(r.RecoveryError, match='dump failed'):
        c.rollback_point('create', tmp_path/'rollback', stack)
    volumes.assert_not_called()
    assert stack.running == set(stack.installed)
    assert ('stop','--timeout','90','postgres') not in stack.calls
    assert any(args[:7] == ('up','-d','--no-recreate','--no-build','--pull','never','--wait')
               for args in stack.calls)
    assert not (tmp_path/'rollback').exists()


def test_dump_restart_failure_alerts(tmp_path, capsys):
    stack = Stack(tmp_path)
    stack.fail_dump = stack.fail_restart = True
    with pytest.raises(r.RecoveryError, match='restart failed'):
        c.rollback_point('create', tmp_path/'rollback', stack)
    assert 'ops-alert: previous stack restart failed' in capsys.readouterr().err


def test_upgrade_dump_failure_never_installs_candidate(tmp_path):
    stack = Stack(tmp_path)
    stack.fail_dump = True
    with patch.object(c.r, 'Compose', return_value=stack), patch.object(c, 'STATE', tmp_path/'status'), \
         patch.object(c, 'required_space', return_value=0), patch.object(c.r, 'kit_commit', return_value='a'*40), \
         patch.object(c, 'checkout') as checkout, patch.object(c, 'ROLLBACK_ROOT', tmp_path/'points'), \
         pytest.raises(r.RecoveryError, match='dump failed'):
        c.upgrade('b'*40, 'run-1', 0)
    checkout.assert_not_called()
    assert stack.running == set(stack.installed)


def test_space_refusal_precedes_stop_or_checkout(tmp_path):
    with patch.object(c.r, 'Compose'), patch.object(c, 'required_space', return_value=100), \
         patch.object(c.shutil, 'disk_usage', return_value=SimpleNamespace(free=99)), \
         patch.object(c, 'rollback_point') as point, patch.object(c, 'checkout') as checkout, \
         patch.object(c, 'STATE', tmp_path/'status'), patch.object(c, 'ROLLBACK_ROOT', tmp_path/'points'), \
         pytest.raises(r.RecoveryError, match='Insufficient free space'):
        c.upgrade('b'*40, 'run', 0)
    point.assert_not_called()
    checkout.assert_not_called()


def test_space_requirement_volumes_plus_database_times_2_2(tmp_path):
    stack = Stack(tmp_path)
    size = sum((path/'state').stat().st_size for path in stack.volumes.values())
    with patch.object(c, 'volume_map', return_value=stack.volumes), patch.object(c, 'fence_project', side_effect=stack.fence):
        assert c.required_space(stack) == __import__('math').ceil((size+100)*2.2)


def test_op_lock_excludes_backup_restore_upgrade(tmp_path):
    with patch.object(r, 'LOCK_DIR', tmp_path/'run'):
        with r.locked():
            with pytest.raises(r.RecoveryError, match='Another host operation'):
                with r.locked():
                    pytest.fail('Overlapping operation was permitted')
    assert (tmp_path/'run/op.lock').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('profiles,expected', [('paperclip',{'postgres','paperclip','caddy'}),('hermes',{'hermes','sandbox','caddy'}),('paperclip,hermes',{'postgres','paperclip','hermes','sandbox','caddy'})])
def test_profile_matrix_compose_config(tmp_path, profiles, expected):
    root=tmp_path/'kit'; shutil.copytree(KIT, root)
    (root/'.secrets').mkdir()
    (root/'.secrets/compose.env').write_text('COMPOSE_PROFILES='+profiles+'\n')
    result=subprocess.run(['docker','compose','--env-file','.secrets/compose.env','config','--format','json'],cwd=root,capture_output=True,text=True,check=True)
    config=json.loads(result.stdout)
    assert set(config['services']) == expected


def test_secret_to_service_isolation_and_capabilities():
    config=yaml.safe_load((KIT/'compose.yaml').read_text())
    services=config['services']
    intended={
        'postgres_password': {'postgres','paperclip'},
        'better_auth_secret': {'paperclip'}, 'paperclip_agent_jwt_secret': {'paperclip'},
        'paperclip_tool_action_signing_secret': {'paperclip'}, 'paperclip_secrets_master_key': {'paperclip'},
        'hermes_gateway_key': {'hermes'}, 'hermes_dashboard_basic_auth_password': {'hermes'},
        'hermes_dashboard_basic_auth_secret': {'hermes'}, 'openai_api_key': {'hermes'},
        'fixture_github_token': {'paperclip','hermes','sandbox'}, 'sandbox_ssh_key': {'hermes'},
        'sandbox_ssh_public_key': {'sandbox'}}
    for secret, owners in intended.items():
        assert {name for name,service in services.items() if secret in service.get('secrets',[])} == owners
    for name,service in services.items():
        assert service['cap_drop'] == ['ALL']
        assert service['cap_add']
        if name in ('paperclip', 'hermes'):
            assert 'KILL' in service['cap_add']
        assert not any(name in service['environment'] for name in ('OPENAI_API_KEY','POSTGRES_PASSWORD','BETTER_AUTH_SECRET')) if 'environment' in service else True
        assert not any('backup.env' in mount or 'delivery.key' in mount for mount in service.get('volumes',[]))


def test_non_tty_refused_without_stdin_and_atomic_secret_with_stdin(tmp_path):
    inp=io.TextIOWrapper(io.BytesIO(secrets.token_bytes(16).hex().encode()+b'\n'))
    with patch.object(c, 'KIT', tmp_path), patch.object(sys, 'stdin', inp), pytest.raises(r.RecoveryError,match='--stdin'):
        c.secret_set('openai-api-key')
    with patch.object(c, 'KIT', tmp_path), patch.object(sys,'stdin',inp):
        c.secret_set('openai-api-key', True)
    saved=tmp_path/'.secrets/openai-api-key'
    assert saved.stat().st_mode & 0o777 == 0o600
    assert saved.read_bytes().strip()
    assert not (tmp_path/'.secrets/compose.env').exists()


def test_secret_cli_rejects_non_tty():
    result=subprocess.run([str(KIT/'sfctl'),'secret','set','openai-api-key'],input='generated-fixture',text=True,capture_output=True)
    assert result.returncode == 1
    assert '--stdin' in result.stderr
    assert 'generated-fixture' not in result.stderr


def test_github_expiry_recorded_from_real_header(tmp_path):
    token=secrets.token_hex(32)
    inp=io.TextIOWrapper(io.BytesIO(token.encode()))
    response=MagicMock(); response.__enter__.return_value.headers={'github-authentication-token-expiration':'2099-01-01 00:00:00 UTC'}
    with patch.object(c,'KIT',tmp_path),patch.object(c,'STATE',tmp_path/'state'),patch.object(sys,'stdin',inp),patch.object(c.urllib.request,'urlopen',return_value=response) as request:
        c.secret_set('github-token',True)
    assert request.call_args.args[0].full_url == 'https://api.github.com/'
    assert json.loads((tmp_path/'state/github-token.json').read_text())['expires_at'].startswith('2099-')
    assert (tmp_path/'.secrets/github-token').read_text().strip()==token


def test_health_is_one_json_line_under_4kb_even_unhealthy(tmp_path,capsys):
    with patch.object(c.r, 'Compose', side_effect=r.RecoveryError('Docker unavailable')),patch.object(c,'probe',return_value=None):
        assert not c.health()
    output=capsys.readouterr().out
    assert len(output.encode())<4096
    assert len(output.splitlines())==1
    assert json.loads(output)['healthy'] is False


def test_join_rejects_report_only_unreviewed_or_unapplied_before_decrypt(tmp_path):
    valid=dict(mode='enforcing', passed=True, applied=True, reviewed=True,policy_sha256='a'*64,applied_policy_sha256='a'*64)
    for change in [dict(mode='report-only'),dict(applied=False),dict(reviewed=False),dict(passed=False),dict(applied_policy_sha256='b'*64)]:
        receipt=tmp_path/'receipt';receipt.write_text(json.dumps({**valid,**change}));receipt.chmod(0o600)
        with patch.object(c,'decrypt_input') as decrypt,pytest.raises(r.RecoveryError,match='enforcing'):
            c.join_tailnet(receipt)
        decrypt.assert_not_called()


def test_bootstrap_key_pin_and_package_pins():
    bootstrap=(KIT/'bootstrap.sh').read_text()
    assert bootstrap.count('9DC858229FC7DD38854AE2D88D81803C0EBFCD88')==2
    dockerfile=(KIT/'Dockerfile.hermes').read_text()
    assert 'gh=2.46.0-3' in dockerfile
    assert 'openssh-server=1:10.0p1-7+deb13u4' in dockerfile
    assert '--non-interactive' in bootstrap and '--prepare-only' in bootstrap
    assert 'Persistent=true' not in (KIT/'systemd/paperclip-hermes-backup.timer').read_text()


@pytest.mark.parametrize('restore_fails', [False, True])
def test_upgrade_failed_health_restores_previous_code_and_data(tmp_path, restore_fails):
    stack=Stack(tmp_path)
    stack.fail_restore = restore_fails
    error = 'restore failed' if restore_fails else 'previous kit and data restored'
    previous='a'*40
    candidate_sha='b'*40
    candidate=tmp_path/'candidate';candidate.mkdir()
    old=tmp_path/'old';(old/'.secrets').mkdir(parents=True)
    install=tmp_path/'install';install.mkdir()
    events=[]
    original=r.run
    def compose_factory(kit, project):
        return stack
    def execute(args,**kwargs):
        if args[0]=='bash':
            for volume in stack.volumes.values():
                (volume/'state').write_text('candidate schema')
            raise r.RecoveryError('health failed')
        if 'recover-locked' in args:
            assert (install/'current').resolve()==old
            events.append('previous-activated-before-restore')
            c.recover_point(Path(args[args.index('--point')+1]), stack)
            return ''
        return original(args,**kwargs)
    with patch.object(c,'KIT',old),patch.object(c,'INSTALL',install),patch.object(c,'STATE',tmp_path/'status'), \
         patch.object(c,'ROLLBACK_ROOT',tmp_path/'points'),patch.object(c.r,'kit_commit',return_value=previous), \
         patch.object(c,'volume_map',return_value=stack.volumes),patch.object(c,'required_space',return_value=0), \
         patch.object(c,'fence_project',side_effect=stack.fence), \
         patch.object(c.r,'Compose',side_effect=compose_factory),patch.object(c,'checkout',side_effect=lambda sha: old if sha==previous else candidate), \
         patch.object(c.r,'run',side_effect=execute),pytest.raises(r.RecoveryError,match=error):
        c.upgrade(candidate_sha,'upgrade-1',0)
    assert events==['previous-activated-before-restore']
    assert all((v/'state').read_text()=='old-data' for v in stack.volumes.values())
    assert stack.running == (set() if restore_fails else set(stack.installed))
    assert json.loads((tmp_path/'status/last-op.json').read_text())['phase'] == ('rollback-failed' if restore_fails else 'rolled-back')


def test_upgrade_handoff_journal_blocks_another_candidate(tmp_path):
    status=tmp_path/'status';status.mkdir(mode=0o700)
    r.atomic_json(status/'last-op.json',dict(protocol=1,operation='upgrade',phase='healthy-awaiting-handoff'))
    with patch.object(c,'STATE',status),patch.object(c.r,'Compose') as compose,pytest.raises(r.RecoveryError,match='handoff'):
        c.upgrade('b'*40,'run',0)
    compose.assert_not_called()


@pytest.mark.parametrize('entry', ['directory', 'file', 'symlink', 'dangling-symlink'])
def test_upgrade_reused_run_id_preserves_journal(tmp_path, entry):
    state = tmp_path / 'state'
    points = tmp_path / 'points'
    points.mkdir()
    point = points / 'used-run'
    if entry == 'directory':
        point.mkdir()
        (point / 'rollback.json').write_text('{"old": "snapshot"}')
    elif entry == 'file':
        point.write_text('retained snapshot')
    else:
        target = tmp_path / 'target'
        if entry == 'symlink':
            target.mkdir()
        point.symlink_to(target)
    with patch.object(c, 'STATE', state), patch.object(c, 'ROLLBACK_ROOT', points), \
         patch.object(r, 'Compose') as compose, patch.object(c, 'required_space', return_value=0), \
         patch.object(r, 'kit_commit', return_value='a' * 40), \
         patch.object(c, 'rollback_point', wraps=c.rollback_point) as snapshot:
        compose.return_value.return_value = ''
        c.journal('rolled-back', run_id='used-run', previous_sha='a' * 40)
        before = (state / 'last-op.json').read_bytes()
        with pytest.raises(r.RecoveryError, match='Rollback path already exists'):
            c.upgrade('b' * 40, 'used-run', 0)
        assert (state / 'last-op.json').read_bytes() == before
        compose.assert_not_called()
        snapshot.assert_not_called()


@pytest.mark.parametrize('run_id', ['', '.', '..', '../escape', '/absolute', 'a/b', 'a b', 'a\n', 'x' * 256])
def test_upgrade_invalid_run_id_preserves_journal(tmp_path, run_id):
    with patch.object(c, 'STATE', tmp_path / 'state'), patch.object(r, 'Compose') as compose:
        c.journal('rolled-back', run_id='previous')
        before = (c.STATE / 'last-op.json').read_bytes()
        with pytest.raises(r.RecoveryError, match='Invalid operation ID'):
            c.upgrade('b' * 40, run_id, 0)
        assert (c.STATE / 'last-op.json').read_bytes() == before
        compose.assert_not_called()


def test_prepare_config_generates_once_and_removes_legacy_env_secrets(tmp_path):
    directory=tmp_path/'.secrets';directory.mkdir(mode=0o700)
    original=secrets.token_hex(32)
    c.atomic_secret(directory/'compose.env',('COMPOSE_PROFILES=paperclip\nBETTER_AUTH_SECRET='+original+'\n').encode())
    with patch.object(c,'KIT',tmp_path):
        c.prepare_config()
        first={p.name:p.read_bytes() for p in directory.iterdir()}
        c.prepare_config()
    assert first=={p.name:p.read_bytes() for p in directory.iterdir()}
    assert (directory/'better-auth-secret').read_text().strip()==original
    assert 'BETTER_AUTH_SECRET' not in (directory/'compose.env').read_text()
    assert (directory/'openai-api-key').read_bytes()==b''
    assert len({(directory/name).read_text() for name in r.SECRET_NAMES if name!='openai-api-key'})==len(r.SECRET_NAMES)-1


@pytest.mark.parametrize('has_credential,prepare_only', [(False,False),(True,False),(True,True)])
def test_timezone_timer_and_credential_gate(tmp_path,has_credential,prepare_only):
    host=tmp_path/'host';host.mkdir(mode=0o700)
    units=tmp_path/'units';units.mkdir()
    (host/'config.json').write_text(json.dumps(dict(timezone='Europe/London',shutdown_time='00:20')))
    if has_credential:
        (host/'backup.env').touch()
    with patch.object(c,'HOST',host),patch.object(c,'UNITS',units),patch.object(c.r,'run') as run,patch.object(c.r,'backup_settings') as validate:
        c.configure_host(prepare_only)
    text=(units/'paperclip-hermes-backup.timer').read_text()
    assert '23:35:00 Europe/London' in text
    assert 'Persistent=true' not in text
    assert run.call_args_list[0].args[0]==['timedatectl','set-timezone','Europe/London']
    assert run.call_args.args[0][1]==('enable' if has_credential and not prepare_only else 'disable')
    assert validate.called==(has_credential and not prepare_only)


def test_host_rejects_ipv6_and_requires_dashboard_pair():
    spec=importlib.util.spec_from_file_location('wp3_host',KIT/'host-config.py')
    host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)
    with patch.object(host,'docker_config',return_value={'ipv6':True}),pytest.raises(SystemExit,match='IPv6 must stay off'):
        host.desired('/data')
    # Reuse the original input fixture to test both missing halves.
    from test_host_config import InputTests
    case=InputTests()
    for key,value in [('DASHBOARD_GATEWAY','172.30.99.2'),('DASHBOARD_SUBNET','172.30.99.0/24')]:
        values=case.valid_values();values[key]=value
        with pytest.raises(SystemExit,match='together'):
            case.validate(values)


def test_join_valid_evidence_uses_file_key_and_serve(tmp_path):
    receipt=tmp_path/'receipt'
    receipt.write_text(json.dumps(dict(mode='enforcing',passed=True,applied=True,reviewed=True,policy_sha256='a'*64,applied_policy_sha256='a'*64)))
    receipt.chmod(0o600)
    stack=MagicMock();stack.services.return_value=['hermes','sandbox','caddy']
    key=secrets.token_hex(32)
    with patch.object(c,'decrypt_input',return_value=key),patch.object(c.r,'run') as run,patch.object(c.r,'Compose',return_value=stack):
        c.join_tailnet(receipt)
    command=run.call_args_list[0].args[0]
    assert '--ssh' in command and '--advertise-tags=tag:pcpilot' in command
    assert any(arg.startswith('--auth-key=file:/run/') for arg in command)
    assert key not in repr(run.call_args_list)
    assert '--https=9443' in run.call_args.args[0]


def test_restore_verify_only_checks_without_creating_project(tmp_path):
    args=SimpleNamespace(fenced=True,identity=tmp_path/'key',archive=tmp_path/'archive',work_dir=tmp_path,verify_only=True)
    with patch.object(r,'private_file'),patch.object(r,'run'),patch.object(r,'unpack_bundle') as unpack,patch.object(r,'restore_target') as target,patch.object(r,'Compose') as compose:
        r.restore(args)
    unpack.assert_called_once()
    target.assert_not_called()
    compose.assert_not_called()


def test_staged_restore_identity_removed_even_on_failure(tmp_path):
    key=Path('/run')/('wp3-test-'+secrets.token_hex(8))
    key.write_text('generated ephemeral fixture');key.chmod(0o600)
    argv=['backup-restore.py','restore','--fenced','--staged','--archive',str(tmp_path/'blob'),'--identity',str(key),'--target',str(tmp_path/'restore')]
    with patch.object(sys,'argv',argv),patch.object(r,'locked',__import__('contextlib').nullcontext),patch.object(r,'restore',side_effect=r.RecoveryError('restore failed')),patch.object(r.signal,'signal'),pytest.raises(r.RecoveryError,match='restore failed'):
        r.main()
    assert not key.exists()


def test_canary_preserves_last_successful_data_receipt(tmp_path):
    archive=tmp_path/'archive';archive.write_bytes(secrets.token_bytes(16))
    state=tmp_path/'state'
    args=SimpleNamespace(reason='nightly',canary=False,target='fixture',vm_id='fixture-vm')
    with patch.object(r,'STATE_DIR',state),patch.object(r,'upload',return_value={}):
        r.publish_backup(args,'age1'+secrets.token_hex(29),r.sas_url(delegation()),archive,'data.age')
        data=(state/'last-backup.json').read_bytes()
        args.canary=True
        r.publish_backup(args,'age1'+secrets.token_hex(29),r.sas_url(delegation()),archive,'canary.age')
    assert (state/'last-backup.json').read_bytes()==data
    assert json.loads((state/'last-canary.json').read_text())['data_backup'] is False


def test_health_checks_tls_authenticated_endpoint_and_drop_counters(tmp_path,capsys):
    daemon=tmp_path/'daemon.json';daemon.write_text('{"ipv6":false}')
    compose=MagicMock()
    compose.services.return_value=['postgres','paperclip','hermes','sandbox','caddy']
    def command(*args):
        if args[0]=='ps':
            return json.dumps([dict(Health='healthy') for _ in range(5)])
        if args[2]=='caddy':
            assert args[3] == 'curl'
            assert args[args.index('--cacert')+1] == '/public-ca/root.crt'
            assert 'https://caddy/health/detailed' in args[-1]
            return '401'
        return ''
    compose.side_effect=command
    def probe(args):
        if args[0]=='iptables':
            return '1 64 DROP all -- * * 0.0.0.0/0 169.254.169.254\n2 128 DROP tcp -- * * 0.0.0.0/0 168.63.129.16 tcp dpt:80\n3 192 DROP tcp -- * * 0.0.0.0/0 168.63.129.16 tcp dpt:32526'
        return '{"Web":{"fixture:443":{}}}'
    with patch.object(c.r,'Compose',return_value=compose),patch.object(c,'probe',side_effect=probe),patch.object(c,'DOCKER_DAEMON',daemon),patch.object(c.r,'schema_state',return_value={'applied':[1]}):
        assert c.health()
    output=capsys.readouterr().out
    assert len(output.encode())<4096
    assert json.loads(output)['checks']['metadata_drop_counters']==[['1','64'],['2','128'],['3','192']]


def test_pre_stop_validation_failure_never_starts_services():
    args=SimpleNamespace(reason='pre-stop')
    with patch.object(r,'data_backup',side_effect=r.RecoveryError('invalid credential')),patch.object(r,'Compose') as compose,pytest.raises(r.RecoveryError):
        r.backup(args)
    compose.assert_not_called()


def test_pre_stop_failure_after_shutdown_restarts_previous_writers():
    args=SimpleNamespace(reason='pre-stop')
    stack=MagicMock()
    def stopped(args):
        args._restart_services=(stack,['paperclip','caddy'])
        raise r.RecoveryError('dump failed')
    with patch.object(r,'data_backup',side_effect=stopped),pytest.raises(r.RecoveryError,match='dump failed'):
        r.backup(args)
    stack.assert_called_once_with('up','-d','--no-recreate','--no-build','--pull','never',
                                  '--wait','--wait-timeout','300','paperclip','caddy')


@pytest.mark.parametrize('command', ['backup','restore'])
@pytest.mark.parametrize('failure', [False,True])
def test_operation_status_durable_on_success_and_failure(tmp_path,command,failure):
    if command=='backup':
        argv=['backup-restore.py','backup']
    else:
        argv=['backup-restore.py','restore','--fenced','--archive','archive','--identity','key','--target','target']
    operation=MagicMock(side_effect=r.RecoveryError('injected') if failure else None)
    with patch.object(sys,'argv',argv),patch.object(r,'STATE_DIR',tmp_path/'state'),patch.object(r,'locked',__import__('contextlib').nullcontext),patch.object(r,command,operation),patch.object(r.signal,'signal'):
        if failure:
            with pytest.raises(r.RecoveryError):
                r.main()
        else:
            r.main()
    record=json.loads((tmp_path/'state/last-op.json').read_text())
    assert record['operation']==command
    assert record['phase']==('failed' if failure else 'complete')
    assert record['completed_at']>=record['started_at']


@pytest.mark.parametrize('mode', ['on','off'])
def test_sfctl_messaging_mutation_is_locked_and_recreates_services(tmp_path,mode):
    stack=MagicMock()
    with patch.object(sys,'argv',['sfctl','messaging',mode]),patch.object(c.r,'locked',__import__('contextlib').nullcontext),patch.object(c,'update_settings') as update,patch.object(c.r,'Compose',return_value=stack),patch.object(c.r.signal,'signal'):
        assert c.main()==0
    update.assert_called_once_with(KIT_MESSAGING_OFF='1' if mode=='off' else None)
    stack.assert_called_once_with('up','-d','--wait')


def test_sfctl_credential_validation_precedes_atomic_replacement(tmp_path):
    host=tmp_path/'host';host.mkdir(mode=0o700)
    original=secrets.token_hex(32).encode()
    c.atomic_secret(host/'backup.env',original)
    with patch.object(sys,'argv',['sfctl','set-backup-credential']),patch.object(c.r,'locked',__import__('contextlib').nullcontext),patch.object(c,'HOST',host),patch.object(c,'decrypt_input',return_value='invalid replacement'),patch.object(c.r,'backup_settings',side_effect=r.RecoveryError('invalid credential')),patch.object(c.r.signal,'signal'),pytest.raises(r.RecoveryError):
        c.main()
    assert (host/'backup.env').read_bytes()==original


def test_sfctl_public_delivery_recipient_does_not_print_identity(tmp_path,capsys):
    host=tmp_path/'host';host.mkdir(mode=0o700)
    identity=secrets.token_hex(32)
    c.atomic_secret(host/'delivery.key',identity.encode())
    recipient='age1'+secrets.token_hex(29)
    with patch.object(sys,'argv',['sfctl','delivery-recipient']),patch.object(c.r,'locked',__import__('contextlib').nullcontext),patch.object(c,'HOST',host),patch.object(c.r,'run',return_value=recipient),patch.object(c.r.signal,'signal'):
        c.main()
    assert capsys.readouterr().out.strip()==recipient


def test_verify_staging_refuses_kit_directory():
    args=SimpleNamespace(fenced=True,identity=Path('/run/key'),verify_only=True,work_dir=KIT/'container')
    with patch.object(r,'private_file'),patch.object(r,'run') as run,pytest.raises(r.RecoveryError,match='outside the kit'):
        r.restore(args)
    run.assert_not_called()


@pytest.mark.parametrize('missing', [False, True])
def test_rollback_inventory_allows_candidate_volumes_but_requires_originals(tmp_path, missing):
    stack = Stack(tmp_path)
    point = tmp_path / 'rollback'
    with patch.object(c, 'volume_map', return_value=stack.volumes), patch.object(c, 'fence_project', side_effect=stack.fence):
        c.rollback_point('create', point, stack)
        original = dict(stack.volumes)
        added = tmp_path / 'candidate_only'
        added.mkdir()
        (added / 'state').write_text('candidate-only data')
        stack.volumes['candidate_only'] = added
        for volume in original.values():
            (volume / 'state').write_text('candidate migration')
        if missing:
            del stack.volumes['postgres']
            with pytest.raises(r.RecoveryError, match='volume inventory changed'):
                c.rollback_point('restore', point, stack)
            assert all((v / 'state').read_text() == 'candidate migration' for v in original.values())
        else:
            c.rollback_point('restore', point, stack)
            assert all((v / 'state').read_text() == 'old-data' for v in original.values())
        assert (added / 'state').read_text() == 'candidate-only data'


@pytest.mark.parametrize('tls_status', ['401', '200', '502', None])
def test_hermes_health_uses_verified_curl_status(tmp_path, capsys, tls_status):
    compose = MagicMock()
    compose.services.return_value = ['hermes', 'sandbox', 'caddy']
    def call(*args):
        if args[0] == 'ps':
            return json.dumps([{'Health': 'healthy'}] * 3)
        assert args[:4] == ('exec', '-T', 'caddy', 'curl')
        assert args[args.index('--cacert') + 1] == '/public-ca/root.crt'
        assert args[-1] == 'https://caddy/health/detailed'
        assert '--insecure' not in args and '-k' not in args
        if tls_status is None:
            raise r.RecoveryError('TLS verification failed')
        return tls_status
    compose.side_effect = call
    with patch.object(r, 'Compose', return_value=compose), patch.object(c, 'probe', return_value=None):
        assert not c.health()  # Host controls are deliberately absent here.
    checks = json.loads(capsys.readouterr().out)['checks']
    assert checks.get('hermes_tls_401', False) == (tls_status == '401')


def test_checkout_under_private_umask_is_container_readable(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    subprocess.run(['git', 'init', str(source)], check=True, capture_output=True)
    container = source / 'hosting/paperclip-hermes/kit/container'
    container.mkdir(parents=True)
    (container / 'gitconfig').write_text('[credential]\n')
    (container / 'gh.sh').write_text('#!/bin/sh\nexit 0\n')
    (container / 'gh.sh').chmod(0o755)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    # Reuse the repository identity without changing any Git configuration.
    identity = subprocess.check_output(['git', '-C', str(KIT), 'log', '-1', '--format=%an%n%ae'], text=True).splitlines()
    env = {**os.environ, 'GIT_AUTHOR_NAME': identity[0], 'GIT_AUTHOR_EMAIL': identity[1],
           'GIT_COMMITTER_NAME': identity[0], 'GIT_COMMITTER_EMAIL': identity[1]}
    subprocess.run(['git', '-C', str(source), 'commit', '-m', 'test: checkout fixture'], env=env, check=True, capture_output=True)
    sha = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    original = r.run
    def local_fetch(args, **kwargs):
        args = [str(a) for a in args]
        if 'fetch' in args:
            args[args.index('fetch') + 2] = str(source)
        return original(args, **kwargs)
    mask = os.umask(0o077)
    try:
        with patch.object(c, 'INSTALL', tmp_path / 'install'), patch.object(r, 'run', side_effect=local_fetch):
            kit = c.checkout(sha)
            assert c.checkout(sha) == kit  # Cached rollback checkout too.
            assert os.umask(0o077) == 0o077
            c.atomic_secret(kit / '.secrets/key', b'fixture')
        for path in (kit, *kit.rglob('*')):
            if '.secrets' in path.parts:
                continue
            expected = 0o755 if path.is_dir() or path.name == 'gh.sh' else 0o644
            assert path.stat().st_mode & 0o777 == expected
        assert (kit / '.secrets').stat().st_mode & 0o777 == 0o700
        assert (kit / '.secrets/key').stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(mask)


def test_checkout_failure_restores_private_umask(tmp_path):
    mask = os.umask(0o077)
    try:
        with patch.object(c, 'INSTALL', tmp_path / 'install'), patch.object(c, 'fetch_checkout', side_effect=r.RecoveryError('fetch failed')):
            with pytest.raises(r.RecoveryError, match='fetch failed'):
                c.checkout('a' * 40)
        assert os.umask(0o077) == 0o077
        assert not list((tmp_path / 'install').iterdir())
    finally:
        os.umask(mask)


@pytest.mark.parametrize('limit', [1, 100])
def test_large_inventories_have_bounded_public_metadata(tmp_path, limit):
    archive = tmp_path / 'backup.age'
    archive.write_bytes(secrets.token_bytes(64))
    schema = {'journal': 'drizzle.__drizzle_migrations', 'applied': [
        {'id': i, 'hash': secrets.token_hex(32), 'created_at': 1700000000000 + i}
        for i in range(1000)]}
    pins = ['example/image' + str(i) + '@sha256:' + secrets.token_hex(32) for i in range(500)]
    for name in r.PAYLOADS:
        (tmp_path / name).write_bytes(b'fixture')
    with patch.object(r, 'image_pins', return_value=pins):
        record = r.manifest(tmp_path, {}, schema, 'fixture', r.utc())
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.status = 201
        opener.open.return_value.__enter__.return_value.headers = {}
        args = SimpleNamespace(reason='nightly', canary=False, target='fixture', vm_id='fixture-vm')
        with patch.object(r, 'STATE_DIR', tmp_path / 'state'), patch.object(r, 'SINGLE_PUT_LIMIT', limit), \
             patch.object(r.urllib.request, 'build_opener', return_value=opener):
            receipt = r.publish_backup(args, 'age1' + secrets.token_hex(29),
                                       r.sas_url(delegation()), archive, 'unique.age', record)
    assert record['paperclip_schema'] == schema  # Full encrypted manifest inventory.
    assert record['image_pins'] == pins
    assert receipt['schema_count'] == 1000 and receipt['image_count'] == 500
    compact = lambda value: __import__('hashlib').sha256(json.dumps(
        value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert receipt['schema'] == compact(schema) and receipt['image_digests'] == compact(pins)
    for call in opener.open.call_args_list:
        metadata = {key: value for key, value in call.args[0].header_items() if key.lower().startswith('x-ms-meta-')}
        assert sum(len(key) + len(value) for key, value in metadata.items()) < 2048
    assert 'applied' not in json.dumps(receipt)


def test_fence_project_includes_stopped_and_candidate_only_containers():
    stack = SimpleNamespace(name='fixture')
    with patch.object(r, 'run', side_effect=['old\ncandidate-only\nstopped\n', '', '']) as run:
        c.fence_project(stack)
    assert run.call_args_list[0].args[0] == ['docker', 'container', 'ls', '--all', '--quiet',
                                           '--filter', 'label=com.docker.compose.project=fixture']
    assert run.call_args_list[1].args[0] == ['docker', 'container', 'stop', '--time', '90', 'old', 'candidate-only', 'stopped']
    assert run.call_args_list[2].args[0] == ['docker', 'container', 'rm', 'old', 'candidate-only', 'stopped']


@pytest.mark.parametrize('command', ['rollback', 'recover'])
@pytest.mark.parametrize('phase', ['complete', 'rolled-back', 'prepare-failed', 'restored-services-started', 'failed', None])
def test_recovery_rejects_resolved_or_other_journals_before_checkout(tmp_path, command, phase):
    r.atomic_json(tmp_path / 'state/last-op.json', {'operation': 'upgrade', 'phase': phase})
    with patch.object(sys, 'argv', ['sfctl', command, '--point', str(tmp_path / 'point')]), \
         patch.object(r, 'locked', __import__('contextlib').nullcontext), patch.object(r.signal, 'signal'), \
         patch.object(c, 'STATE', tmp_path / 'state'), patch.object(c, 'checkout') as checkout, \
         patch.object(c, 'activate') as activate, patch.object(r, 'run') as run, \
         pytest.raises(r.RecoveryError, match='unresolved upgrade'):
        c.main()
    checkout.assert_not_called()
    activate.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize('command', ['rollback', 'recover'])
@pytest.mark.parametrize('phase', ['installing', 'healthy-awaiting-handoff', 'rollback-failed', 'creating-rollback-point'])
def test_recovery_accepts_only_unresolved_upgrades_and_copies_restored_secrets(tmp_path, command, phase):
    source = tmp_path / 'restored'
    source.mkdir()
    c.atomic_secret(source / '.secrets/key', b'restored secret')
    previous = tmp_path / 'previous'
    previous.mkdir()
    fields = dict(previous_sha='a' * 40, candidate_sha='b' * 40, rollback_point=str(tmp_path / 'point'), run_id='fixture')
    r.atomic_json(tmp_path / 'state/last-op.json', {'operation': 'upgrade', 'phase': phase, **fields})
    with patch.object(sys, 'argv', ['sfctl', command]), patch.object(r, 'locked', return_value=__import__('contextlib').nullcontext(7)), \
         patch.object(r.signal, 'signal'), patch.object(c, 'STATE', tmp_path / 'state'), patch.object(c, 'KIT', source), \
         patch.object(c, 'checkout', return_value=previous), patch.object(c, 'activate') as activate, patch.object(r, 'run') as run:
        c.main()
    activate.assert_called_once_with(previous)
    assert (previous / '.secrets/key').read_bytes() == b'restored secret'
    assert 'recover-locked' in run.call_args.args[0]
    assert json.loads((tmp_path / 'state/last-op.json').read_text())['phase'] == 'rolled-back'


def test_recovery_rejects_non_upgrade_operation(tmp_path):
    r.atomic_json(tmp_path / 'state/last-op.json', {'operation': 'restore', 'phase': 'installing'})
    with patch.object(sys, 'argv', ['sfctl', 'recover']), patch.object(r, 'locked', __import__('contextlib').nullcontext), \
         patch.object(r.signal, 'signal'), patch.object(c, 'STATE', tmp_path / 'state'), \
         patch.object(c, 'checkout') as checkout, pytest.raises(r.RecoveryError, match='unresolved upgrade'):
        c.main()
    checkout.assert_not_called()


def test_fencing_failure_prevents_volume_wipe(tmp_path):
    stack = Stack(tmp_path)
    point = tmp_path / 'point'
    with patch.object(c, 'volume_map', return_value=stack.volumes):
        c.rollback_point('create', point, stack)
        for volume in stack.volumes.values():
            (volume / 'state').write_text('candidate data')
        with patch.object(c, 'fence_project', side_effect=r.RecoveryError('stop failed')), \
             pytest.raises(r.RecoveryError, match='stop failed'):
            c.rollback_point('restore', point, stack)
    assert all((volume / 'state').read_text() == 'candidate data' for volume in stack.volumes.values())


def run_until_crash(action):
    # os._exit bypasses all exception/finally cleanup, like losing the process.
    pid = os.fork()
    if pid == 0:
        try:
            action()
        except BaseException:
            os._exit(74)
        os._exit(75)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 73


@pytest.mark.parametrize('interruption', ['before-services', 'after-services', 'before-parent-journal'])
def test_interrupted_rollback_never_replays_after_writer_barrier(tmp_path, interruption):
    stack = Stack(tmp_path)
    point = tmp_path / 'point'
    state = tmp_path / 'state'
    fields = dict(previous_sha='a' * 40, candidate_sha='b' * 40,
                  rollback_point=str(point), run_id='crash', previous_running=stack.installed)
    original_journal = c.journal
    original_compose = Stack.__call__
    with patch.object(c, 'STATE', state), patch.object(r, 'private_stat'), \
         patch.object(r, 'private_directory'), patch.object(c, 'volume_map', return_value=stack.volumes), \
         patch.object(c, 'fence_project', side_effect=stack.fence):
        c.rollback_point('create', point, stack)
        c.journal('installing', **fields)

        def journal(phase, **values):
            if phase == 'rolled-back':
                os._exit(73)
            result = original_journal(phase, **values)
            if phase == 'restored-services-started' and interruption == 'before-services':
                os._exit(73)
            return result

        def compose(self, *args, **kwargs):
            if args[:3] == ('up', '-d', '--build'):
                assert json.loads((state / 'last-op.json').read_text())['phase'] == 'restored-services-started'
            result = original_compose(self, *args, **kwargs)
            if args[:3] == ('up', '-d', '--build'):
                for volume in stack.volumes.values():
                    (volume / 'state').write_text('acknowledged post-rollback write')
                if interruption == 'after-services':
                    os._exit(73)
            return result

        def crash():
            with patch.object(c, 'journal', side_effect=journal), patch.object(Stack, '__call__', compose):
                c.recover_point(point, stack)
                assert json.loads((state / 'last-op.json').read_text())['phase'] == 'restored-services-started'
                # The child has returned; the parent has not published its final phase.
                c.journal('rolled-back', **fields)

        run_until_crash(crash)
        assert json.loads((state / 'last-op.json').read_text())['phase'] == 'restored-services-started'
        expected = 'old-data' if interruption == 'before-services' else 'acknowledged post-rollback write'
        stack.calls.clear()
        for command in ('rollback', 'recover'):
            with patch.object(sys, 'argv', ['sfctl', command]), \
                 patch.object(r, 'locked', __import__('contextlib').nullcontext), \
                 patch.object(r.signal, 'signal'), patch.object(c, 'checkout') as checkout, \
                 pytest.raises(r.RecoveryError, match='unresolved upgrade'):
                c.main()
            checkout.assert_not_called()
        with pytest.raises(r.RecoveryError, match='unresolved upgrade'):
            c.recover_point(point, stack)
        assert not stack.calls
        assert all((volume / 'state').read_text() == expected for volume in stack.volumes.values())


@pytest.mark.parametrize('interruption', ['after-postgres-stop', 'before-manifest', 'after-manifest', 'during-cleanup'])
def test_interrupted_preparation_recovers_inventory_or_complete_point(tmp_path, interruption):
    stack = Stack(tmp_path)
    # Do not start services that were already stopped before the upgrade.
    stack.running.remove('caddy')
    previous_running = [service for service in stack.installed if service in stack.running]
    point = tmp_path / 'points/crash'
    state = tmp_path / 'state'
    original_atomic = r.atomic_json
    original_compose = Stack.__call__

    def compose(self, *args, **kwargs):
        if args[0] == 'stop':
            inventory = json.loads((state / 'last-op.json').read_text())
            assert inventory['previous_running'] == previous_running
        return original_compose(self, *args, **kwargs)

    def volumes(compose):
        assert not stack.running
        inventory = json.loads((state / 'last-op.json').read_text())
        assert inventory['previous_running'] == previous_running
        if interruption == 'after-postgres-stop':
            os._exit(73)
        return stack.volumes

    def atomic(path, record):
        if path.name == 'rollback.json' and interruption == 'before-manifest':
            os._exit(73)
        original_atomic(path, record)
        if path.name == 'rollback.json' and interruption == 'after-manifest':
            os._exit(73)
        if path.name == 'rollback.json' and interruption == 'during-cleanup':
            raise r.RecoveryError('failure after publishing manifest')

    original_cleanup = shutil.rmtree

    def cleanup(path):
        if interruption == 'during-cleanup':
            assert not (path / 'rollback.json').exists()
            next(path.glob('*.tar')).unlink()
            os._exit(73)
        original_cleanup(path)

    with patch.object(c, 'STATE', state), patch.object(c, 'ROLLBACK_ROOT', tmp_path / 'points'), \
         patch.object(r, 'Compose', return_value=stack), patch.object(c, 'required_space', return_value=0), \
         patch.object(r, 'kit_commit', return_value='a' * 40), patch.object(r, 'private_stat'), \
         patch.object(r, 'private_directory'):
        def crash():
            with patch.object(c, 'volume_map', side_effect=volumes), \
                 patch.object(r, 'atomic_json', side_effect=atomic), patch.object(Stack, '__call__', compose), \
                 patch.object(shutil, 'rmtree', side_effect=cleanup):
                c.upgrade('b' * 40, 'crash', 7)
        run_until_crash(crash)
        assert json.loads((state / 'last-op.json').read_text())['phase'] == 'creating-rollback-point'
        assert (point / 'rollback.json').exists() == (interruption == 'after-manifest')
        # Parent observes the stopped stack after the child abruptly exits.
        stack.running.clear()
        stack.calls.clear()
        for volume in stack.volumes.values():
            (volume / 'state').write_text('current data')
        with patch.object(c, 'volume_map', return_value=stack.volumes) as mapped, \
             patch.object(c, 'fence_project', side_effect=stack.fence) as fence:
            c.recover_point(point, stack)
        phase = json.loads((state / 'last-op.json').read_text())['phase']
        if interruption == 'after-manifest':
            assert phase == 'restored-services-started'
            fence.assert_called_once()
            expected = 'old-data'
        else:
            assert phase == 'prepare-failed'
            mapped.assert_not_called()
            fence.assert_not_called()
            assert stack.calls == [('up', '-d', '--no-recreate', '--no-build', '--pull', 'never',
                                    '--wait', '--wait-timeout', '300', *previous_running)]
            expected = 'current data'
        assert all((volume / 'state').read_text() == expected for volume in stack.volumes.values())


@pytest.mark.parametrize('failure', ['child', 'parent-journal'])
def test_upgrade_preserves_writer_barrier_when_recovery_completion_fails(tmp_path, failure):
    stack = Stack(tmp_path)
    original = r.run
    original_journal = c.journal

    def journal(phase, **fields):
        result = original_journal(phase, **fields)
        if phase == 'rolled-back' and failure == 'parent-journal':
            raise r.RecoveryError('injected completion failure')
        return result

    def execute(args, **kwargs):
        if args[0] == 'bash':
            raise r.RecoveryError('candidate failed')
        if 'recover-locked' in args:
            c.recover_point(Path(args[args.index('--point') + 1]), stack)
            for volume in stack.volumes.values():
                (volume / 'state').write_text('acknowledged write')
            if failure == 'child':
                raise r.RecoveryError('injected completion failure')
            return ''
        return original(args, **kwargs)

    with patch.object(c, 'STATE', tmp_path / 'state'), patch.object(c, 'ROLLBACK_ROOT', tmp_path / 'points'), \
         patch.object(r, 'Compose', return_value=stack), patch.object(c, 'required_space', return_value=0), \
         patch.object(r, 'kit_commit', return_value='a' * 40), patch.object(r, 'private_stat'), \
         patch.object(r, 'private_directory'), patch.object(c, 'checkout', return_value=KIT), \
         patch.object(c, 'activate'), patch.object(c, 'copy_secrets'), patch.object(r, 'run', side_effect=execute), \
         patch.object(c, 'journal', side_effect=journal), \
         patch.object(c, 'volume_map', return_value=stack.volumes), \
         patch.object(c, 'fence_project', side_effect=stack.fence) as fence, \
         pytest.raises(r.RecoveryError, match='injected completion failure'):
        c.upgrade('b' * 40, 'crash', 7)
    phase = 'restored-services-started' if failure == 'child' else 'rolled-back'
    assert json.loads((tmp_path / 'state/last-op.json').read_text())['phase'] == phase
    assert fence.call_count == 1
    assert stack.running == set(stack.installed)
    assert all((volume / 'state').read_text() == 'acknowledged write' for volume in stack.volumes.values())


def test_incomplete_preparation_restart_failure_remains_recoverable(tmp_path):
    stack = Stack(tmp_path)
    point = tmp_path / 'incomplete'
    fields = dict(previous_sha='a' * 40, candidate_sha='b' * 40,
                  rollback_point=str(point), run_id='crash', previous_running=stack.installed)
    with patch.object(c, 'STATE', tmp_path / 'state'), patch.object(r, 'private_stat'), \
         patch.object(r, 'private_directory'), patch.object(c, 'rollback_point') as restore:
        c.journal('creating-rollback-point', **fields)
        stack.fail_restart = True
        with pytest.raises(r.RecoveryError, match='restart failed'):
            c.recover_point(point, stack)
        assert json.loads((tmp_path / 'state/last-op.json').read_text())['phase'] == 'creating-rollback-point'
        stack.fail_restart = False
        c.recover_point(point, stack)
        assert json.loads((tmp_path / 'state/last-op.json').read_text())['phase'] == 'prepare-failed'
    restore.assert_not_called()
