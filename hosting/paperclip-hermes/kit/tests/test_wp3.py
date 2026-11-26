"""WP3 host contracts and failure recovery; all credentials generated in memory."""
import argparse
import base64
import uuid
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


@pytest.fixture(autouse=True)
def isolate_host_state(tmp_path, monkeypatch):
    monkeypatch.setattr(r, 'STATE_DIR', tmp_path / 'default-state')
    monkeypatch.setattr(c, 'STATE', tmp_path / 'default-state')
    monkeypatch.setattr(r, 'LOCK_DIR', tmp_path / 'default-lock')
    monkeypatch.setattr(c, 'HOST', tmp_path / 'default-host')
    # Upgrade fixtures model an installed immutable checkout, not this edit tree.
    monkeypatch.setattr(r, 'kit_modified', lambda: False)


def delegation(**changes):
    now = datetime.now(timezone.utc)
    query = dict(sv='2023-11-03', sr='c', sp='cw', spr='https', sig=base64.b64encode(secrets.token_bytes(32)).decode(),
                 skoid=str(uuid.uuid4()), sktid=str(uuid.uuid4()), sks='b', skv='2023-11-03',
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

    @property
    def inventory(self):
        return {name: {'name': 'fixture_' + name, 'mountpoint': str(path), 'created_at': 'fixture-generation'}
                for name, path in self.volumes.items()}

    def fence(self, compose):
        assert compose is self
        self.calls.append(('fence-project',))
        self.running.clear()

    def images(self):
        return {service: {'image_id': 'fixture'} for service in self.installed}

    def services(self):
        return self.installed

    def preserve_build_images(self):
        pass  # This stack models volumes and service state, without Docker images.

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
            selected = [arg for arg in args[2:] if arg in self.installed]
            self.running.update(selected if '--no-deps' in args else self.installed)
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
    with patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory), patch.object(c, 'fence_project', side_effect=stack.fence):
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
    assert any(args[:7] == ('up','-d','--no-recreate','--no-build','--pull','never','--no-deps')
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
         patch.object(c, 'checkout', return_value=KIT) as checkout, patch.object(c, 'ROLLBACK_ROOT', tmp_path/'points'), \
         pytest.raises(r.RecoveryError, match='dump failed'):
        c.upgrade('b'*40, 'run-1', 0)
    checkout.assert_called_once_with('a' * 40)
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
    with patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory), patch.object(c, 'fence_project', side_effect=stack.fence):
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
    env = {name: value for name, value in os.environ.items() if not name.startswith('COMPOSE_')}
    result=subprocess.run(['docker','compose','--env-file','.secrets/compose.env','config','--format','json'],cwd=root,env=env,capture_output=True,text=True,check=True)
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


def test_secret_cli_rejects_non_tty(tmp_path):
    copied = tmp_path / 'kit'
    shutil.copytree(KIT, copied)
    recovery = copied / 'backup-restore.py'
    recovery.write_text(recovery.read_text().replace("LOCK_DIR = Path('/run/paperclip-hermes')",
                                                    'LOCK_DIR = Path(' + repr(str(tmp_path / 'lock')) + ')'))
    result=subprocess.run([str(copied/'sfctl'),'secret','set','openai-api-key'],input='generated-fixture',text=True,capture_output=True)
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
    old=tmp_path/'old'; shutil.copytree(KIT, old); (old/'.secrets').mkdir(mode=0o700)
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
        if 'recover-locked' in args and '--help' not in args:
            assert (install/'current').resolve()==old
            events.append('previous-activated-before-restore')
            c.recover_point(Path(args[args.index('--point')+1]), stack)
            return ''
        return original(args,**kwargs)
    with patch.object(c,'KIT',old),patch.object(c,'INSTALL',install),patch.object(c,'STATE',tmp_path/'status'), \
         patch.object(c,'ROLLBACK_ROOT',tmp_path/'points'),patch.object(c.r,'kit_commit',return_value=previous), \
         patch.object(c,'volume_map',side_effect=lambda *args: stack.inventory),patch.object(c,'required_space',return_value=0), \
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
    with patch.object(sys,'argv',argv),patch.object(r,'locked',__import__('contextlib').nullcontext),patch.object(r,'STATE_DIR',tmp_path/'state'),patch.object(r,'restore',side_effect=r.RecoveryError('restore failed')),patch.object(r.signal,'signal'),pytest.raises(r.RecoveryError,match='restore failed'):
        r.main()
    assert not key.exists()
    assert json.loads((tmp_path / 'state/last-op.json').read_text())['phase'] == 'failed'


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
            return json.dumps([dict(Service=service, Health='healthy') for service in compose.services()])
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
        return json.dumps({'TCP': {'443': {'HTTPS': True}, '9443': {'HTTPS': True}}, 'Web': {'fixture:443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:3100'}}},
                                   'fixture:9443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:9119'}}}}})
    with patch.object(c.r,'Compose',return_value=compose),patch.object(c,'probe',side_effect=probe),patch.object(c,'settings',return_value={'TAILSCALE_HOSTNAME':'fixture'}),patch.object(c,'DOCKER_DAEMON',daemon),patch.object(c.r,'schema_state',return_value={'applied':[1]}):
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
                                  '--no-deps','--wait','--wait-timeout','300','paperclip','caddy')


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
    with patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory), patch.object(c, 'fence_project', side_effect=stack.fence):
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
            return json.dumps([{'Service': service, 'Health': 'healthy'} for service in compose.services()])
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
    git_env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    git_env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    subprocess.run(['git', 'init', str(source)], env=git_env, check=True, capture_output=True)
    container = source / 'hosting/paperclip-hermes/kit/container'
    container.mkdir(parents=True)
    (container / 'gitconfig').write_text('[credential]\n')
    (container / 'gh.sh').write_text('#!/bin/sh\nexit 0\n')
    (container / 'gh.sh').chmod(0o755)
    subprocess.run(['git', '-C', str(source), 'add', '.'], env=git_env, check=True)
    # Reuse the repository identity without changing any Git configuration.
    identity = subprocess.check_output(['git', '-C', str(KIT), 'log', '-1', '--format=%an%n%ae'], env=git_env, text=True).splitlines()
    env = {**git_env, 'GIT_AUTHOR_NAME': identity[0], 'GIT_AUTHOR_EMAIL': identity[1],
           'GIT_COMMITTER_NAME': identity[0], 'GIT_COMMITTER_EMAIL': identity[1]}
    subprocess.run(['git', '-C', str(source), 'commit', '-m', 'test: checkout fixture'], env=env, check=True, capture_output=True)
    sha = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], env=git_env, text=True).strip()
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
@pytest.mark.parametrize('phase', ['complete', 'rolled-back', 'prepare-failed', 'failed', None])
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
    r.atomic_json(tmp_path / 'state/last-op.json', {'protocol': 1, 'operation': 'upgrade', 'phase': phase, 'operation_id': 'c' * 64, 'previous_running': [], **fields})
    with patch.object(sys, 'argv', ['sfctl', command]), patch.object(r, 'locked', return_value=__import__('contextlib').nullcontext(7)), \
         patch.object(r.signal, 'signal'), patch.object(c, 'STATE', tmp_path / 'state'), patch.object(c, 'KIT', source), \
         patch.object(c, 'checkout', return_value=previous), patch.object(c, 'activate') as activate, patch.object(r, 'run') as run, patch.object(c, 'validate_point'):
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
    with patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory):
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
                  rollback_point=str(point), run_id='crash', operation_id='c' * 64, previous_running=stack.installed)
    original_journal = c.journal
    original_compose = Stack.__call__
    with patch.object(c, 'STATE', state), patch.object(r, 'private_stat'), patch.object(r, 'kit_commit', return_value='a' * 40), \
         patch.object(r, 'private_directory'), patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory), \
         patch.object(c, 'fence_project', side_effect=stack.fence):
        with patch.object(r, 'kit_commit', return_value=fields['previous_sha']):
            c.rollback_point('create', point, stack, operation=fields)
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
        # Resume startup repeatedly, with no archive replay or fencing.
        with patch.object(c, 'rollback_point') as restore, patch.object(c, 'fence_project') as fence:
            c.recover_point(point, stack)
            c.recover_point(point, stack)
        restore.assert_not_called()
        fence.assert_not_called()
        assert stack.running == set(stack.installed)
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
        return stack.inventory

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
         patch.object(c, 'checkout', return_value=KIT), \
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
        with patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory) as mapped, \
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
                                    '--no-deps', '--wait', '--wait-timeout', '300', *previous_running)]
            expected = 'current data'
        assert stack.running == set(previous_running)
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
        if 'recover-locked' in args and '--help' not in args:
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
         patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory), \
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
                  rollback_point=str(point), run_id='crash', operation_id='c' * 64, previous_running=stack.installed)
    with patch.object(c, 'STATE', tmp_path / 'state'), patch.object(r, 'private_stat'), \
         patch.object(r, 'private_directory'), patch.object(c, 'rollback_point') as restore:
        c.journal('creating-rollback-point', **fields)
        stack.fail_restart = True
        with pytest.raises(r.RecoveryError, match='restart failed'):
            c.recover_point(point, stack)
        assert json.loads((tmp_path / 'state/last-op.json').read_text())['phase'] == 'preparation-restarting'
        stack.fail_restart = False
        c.recover_point(point, stack)
        assert json.loads((tmp_path / 'state/last-op.json').read_text())['phase'] == 'prepare-failed'
    restore.assert_not_called()


@pytest.mark.parametrize('change', [
    {'st': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    {'st': '2026-99-01T00:00:00Z'}, {'st': ''}, {'st': '2026-01-01'},
    {'se': '2026-99-01T00:00:00Z'}, {'skt': '2026-01-01T00:00:00+01:00'},
    {'sv': '2023-99-99'}, {'sv': '2099-01-01'}, {'skv': 'nonsense'},
    {'skoid': 'bad-id'}, {'sktid': 'bad-id'}, {'sig': 'not-base64'},
    {'sig': base64.b64encode(b'short').decode()}, {'sp': 'cw '},
    {'sr': 'b'}, {'spr': 'https,http'}, {'sduoid': str(uuid.uuid4())},
])
def test_invalid_sas_never_replaces_working_credential(tmp_path, change):
    host = tmp_path / 'host'
    host.mkdir(mode=0o700)
    original = ('BACKUP_AGE_RECIPIENT=age1' + secrets.token_hex(29) + '\n'
                + 'BACKUP_CONTAINER_SAS_URL=' + delegation() + '\n')
    c.atomic_secret(host / 'backup.env', original.encode())
    replacement = original.split('BACKUP_CONTAINER_SAS_URL=')[0] + 'BACKUP_CONTAINER_SAS_URL=' + delegation(**change) + '\n'
    with patch.object(sys, 'argv', ['sfctl', 'set-backup-credential']), \
         patch.object(r, 'locked', __import__('contextlib').nullcontext), patch.object(r.signal, 'signal'), \
         patch.object(c, 'HOST', host), patch.object(c, 'decrypt_input', return_value=replacement), \
         patch.object(r, 'run', return_value='') as age, \
         pytest.raises(r.RecoveryError, match='container SAS'):
        c.main()
    age.assert_not_called()
    assert (host / 'backup.env').read_text() == original


@pytest.mark.parametrize('start', [None, (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
                                   (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()])
def test_sas_optional_active_start(start):
    assert r.sas_url(delegation(st=start))


@pytest.mark.parametrize('cleanup', ['remove', 'sync'])
@pytest.mark.parametrize('restart_fails', [False, True])
def test_dump_cleanup_failure_still_restarts_and_alerts(tmp_path, capsys, cleanup, restart_fails):
    stack = Stack(tmp_path)
    stack.fail_dump = True
    stack.fail_restart = restart_fails
    target = 'rmtree' if cleanup == 'remove' else 'sync_directory'
    owner = shutil if cleanup == 'remove' else r
    def cleanup_failure(*args):
        if stack.running == {'postgres'}:
            raise OSError('cleanup failed')
    with patch.object(owner, target, side_effect=cleanup_failure), \
         pytest.raises(r.RecoveryError, match='restart failed' if restart_fails else 'dump failed'):
        c.rollback_point('create', tmp_path / 'point', stack)
    assert stack.running == ({'postgres'} if restart_fails else set(stack.installed))
    alert = capsys.readouterr().err
    assert 'ops-alert: rollback point cleanup failed' in alert
    assert ('ops-alert: previous stack restart failed' in alert) == restart_fails


def test_uncertain_manifest_invalidation_never_restarts_writers(tmp_path, capsys):
    stack = Stack(tmp_path)
    atomic = r.atomic_json
    published = False
    def publish_then_fail(path, record):
        nonlocal published
        atomic(path, record)
        published = True
        raise OSError('publication durability uncertain')
    def sync(path):
        if published and path.name == 'point':
            raise OSError('invalidation failed')
    with patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory), \
         patch.object(r, 'atomic_json', side_effect=publish_then_fail), \
         patch.object(r, 'sync_directory', side_effect=sync), \
         pytest.raises(OSError):
        c.rollback_point('create', tmp_path / 'point', stack)
    assert not stack.running
    assert 'ops-alert: rollback point invalidation failed' in capsys.readouterr().err


@pytest.mark.parametrize('wrong', ['run_id', 'operation_id', 'candidate_sha', 'previous_running'])
def test_point_bound_to_run_before_activation_and_in_child(tmp_path, wrong):
    stack = Stack(tmp_path)
    stack.name = 'paperclip-hermes'
    point = tmp_path / 'point'
    fields = dict(previous_sha=r.kit_commit(), candidate_sha='b' * 40, run_id='run-b',
                  operation_id='c' * 64, previous_running=stack.installed, rollback_point=str(point))
    with patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory), \
         patch.object(c, 'STATE', tmp_path / 'state'), patch.object(r, 'LOCK_DIR', tmp_path / 'lock'):
        c.rollback_point('create', point, stack, operation=fields)
        bad = {**fields, wrong: [] if wrong == 'previous_running' else ('d' * 40 if wrong == 'candidate_sha' else 'other')}
        manifest = json.loads((point / 'rollback.json').read_text())
        manifest['operation'] = bad
        r.atomic_json(point / 'rollback.json', manifest)
        c.journal('installing', **fields)
        for command in ('recover', 'recover-locked'):
            with r.locked() as fd:
                argv = ['sfctl', command, '--point', str(point)]
                if command == 'recover-locked':
                    argv += ['--lock-fd', str(fd)]
                with patch.object(sys, 'argv', argv), patch.object(r.signal, 'signal'), \
                     patch.object(r, 'locked', __import__('contextlib').nullcontext), \
                     patch.object(c, 'checkout') as checkout, patch.object(c, 'activate') as activate, \
                     patch.object(r, 'Compose', return_value=stack), \
                     pytest.raises(r.RecoveryError, match='does not belong'):
                    c.main()
                checkout.assert_not_called()
                activate.assert_not_called()


def test_matching_point_can_be_relocated(tmp_path):
    stack = Stack(tmp_path)
    fields = dict(previous_sha=r.kit_commit(), candidate_sha='b' * 40, run_id='run', operation_id='c' * 64,
                  previous_running=stack.installed, rollback_point=str(tmp_path / 'point'))
    with patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory):
        c.rollback_point('create', tmp_path / 'point', stack, operation=fields)
    (tmp_path / 'point').rename(tmp_path / 'relocated')
    c.validate_point(tmp_path / 'relocated', {**fields, 'phase': 'installing'})


@pytest.mark.parametrize('recreated', [False, True])
def test_volume_resolution_uses_recorded_name_and_generation(tmp_path, recreated):
    original = tmp_path / 'original'
    candidate = tmp_path / 'candidate'
    original.mkdir(); candidate.mkdir()
    model = MagicMock(name='compose')
    model.name = 'fixture'
    model.return_value = json.dumps({'volumes': {'paperclip': {'name': 'original'}}})
    volumes = {'original': dict(Name='original', Mountpoint=str(original), CreatedAt='generation-a',
                Labels={'com.docker.compose.project': 'fixture', 'com.docker.compose.volume': 'paperclip'}),
               'candidate': dict(Name='candidate', Mountpoint=str(candidate), CreatedAt='generation-b',
                Labels={'com.docker.compose.project': 'fixture', 'com.docker.compose.volume': 'paperclip'})}
    def docker(args, **kwargs):
        if args[1:3] == ['volume', 'ls']:
            return 'original\ncandidate\n'
        return json.dumps([volumes[args[-1]]])
    with patch.object(r, 'run', side_effect=docker):
        captured = c.volume_map(model)
        assert captured['paperclip']['name'] == 'original'
        if recreated:
            volumes['original']['CreatedAt'] = 'generation-new'
            with pytest.raises(r.RecoveryError, match='inventory changed'):
                c.volume_map(model, captured)
        else:
            assert c.volume_map(model, captured) == captured


@pytest.mark.parametrize('selected', [['paperclip'], ['hermes'], ['paperclip', 'hermes']])
@pytest.mark.parametrize('broken', [None, 'missing', 'wrong-proxy', 'http-only'])
def test_health_checks_exact_routes_for_selected_profiles(tmp_path, capsys, selected, broken):
    compose = MagicMock()
    compose.services.return_value = selected
    compose.side_effect = lambda *args: json.dumps([{'Service': service, 'Health': 'healthy'} for service in selected]) if args[0] == 'ps' else ('401' if args[2] == 'caddy' else '')
    daemon = tmp_path / 'daemon.json'; daemon.write_text('{"ipv6":false}')
    config = {'TCP': {}, 'Web': {}}
    for service, port, target in [('paperclip', '443', '3100'), ('hermes', '9443', '9119')]:
        if service in selected:
            config['TCP'][port] = {'HTTPS': True}
            config['Web']['fixture:' + port] = {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:' + target}}}
    port = '9443' if 'hermes' in selected else '443'
    if broken == 'missing':
        del config['Web']['fixture:' + port]
    elif broken == 'wrong-proxy':
        config['Web']['fixture:' + port]['Handlers']['/']['Proxy'] = 'http://127.0.0.1:9999'
    elif broken == 'http-only':
        config['TCP'][port]['HTTPS'] = False
    def probe(args):
        return json.dumps(config) if args[0] == 'tailscale' else '1 1 DROP 169.254.169.254\n1 1 DROP 168.63.129.16 dpt:80\n1 1 DROP 168.63.129.16 dpt:32526'
    with patch.object(r, 'Compose', return_value=compose), patch.object(c, 'probe', side_effect=probe), \
         patch.object(c, 'settings', return_value={'TAILSCALE_HOSTNAME': 'fixture'}), \
         patch.object(c, 'DOCKER_DAEMON', daemon), patch.object(r, 'schema_state', return_value={'applied': [1]}):
        assert c.health() == (broken is None)
    assert json.loads(capsys.readouterr().out)['checks']['tailscale_serve'] == (broken is None)


@pytest.mark.parametrize('crash', ['receipt-published', 'token-published'])
def test_token_expiry_matches_installed_generation_after_crash(tmp_path, capsys, crash):
    old = secrets.token_hex(32).encode(); new = secrets.token_hex(32).encode()
    response = MagicMock()
    response.__enter__.return_value.headers = {'github-authentication-token-expiration': '2099-01-01 00:00:00 UTC'}
    original_json, original_secret = r.atomic_json, c.atomic_secret
    with patch.object(c, 'KIT', tmp_path), patch.object(c, 'STATE', tmp_path / 'state'), patch.object(r.signal, 'signal'):
        c.atomic_secret(tmp_path / '.secrets/github-token', old + b'\n')
        def publish_receipt(path, record):
            original_json(path, record)
            if crash == 'receipt-published':
                os._exit(73)
        def publish_token(path, value):
            original_secret(path, value)
            os._exit(73)
        def rotate():
            with patch.object(r, 'atomic_json', side_effect=publish_receipt), \
                 patch.object(c, 'atomic_secret', side_effect=publish_token), \
                 patch.object(c.urllib.request, 'urlopen', return_value=response), \
                 patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(new))):
                c.secret_set('github-token', True)
        run_until_crash(rotate)
        with patch.object(sys, 'argv', ['sfctl', 'status']):
            c.main()
    receipt = json.loads(capsys.readouterr().out)['github-token']
    assert receipt['expires_at'] == ('2099-01-01 00:00:00 UTC' if crash == 'token-published' else None)


def test_empty_restart_inventory_does_not_start_entire_project():
    compose = MagicMock()
    r.restart_services(compose, [])
    compose.assert_not_called()


@pytest.mark.parametrize('expiry', ['not a date', '2099-99-01 00:00:00 UTC', '2000-01-01 00:00:00 UTC'])
def test_invalid_github_expiry_preserves_token_and_receipt(tmp_path, expiry):
    response = MagicMock(); response.__enter__.return_value.headers = {'github-authentication-token-expiration': expiry}
    with patch.object(c, 'KIT', tmp_path), patch.object(c, 'STATE', tmp_path / 'state'):
        c.atomic_secret(tmp_path / '.secrets/github-token', b'old-token\n')
        with patch.object(c.urllib.request, 'urlopen', return_value=response), \
             patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(secrets.token_hex(32).encode()))), \
             pytest.raises(r.RecoveryError):
            c.secret_set('github-token', True)
    assert (tmp_path / '.secrets/github-token').read_bytes() == b'old-token\n'
    assert not (tmp_path / 'state/github-token.json').exists()


@pytest.mark.parametrize('phase', ['restored-services-started', 'preparation-restarting'])
def test_no_replay_phases_block_another_upgrade(tmp_path, phase):
    with patch.object(c, 'STATE', tmp_path / 'state'), patch.object(c, 'ROLLBACK_ROOT', tmp_path / 'points'), \
         patch.object(r, 'STATE_DIR', tmp_path / 'state'), patch.object(r, 'Compose') as compose:
        c.journal(phase)
        with pytest.raises(r.RecoveryError, match='previous upgrade'):
            c.upgrade('a' * 40, 'new-run', 7)
    compose.assert_not_called()


def test_preparation_restart_has_durable_barrier_before_writer_start(tmp_path):
    stack = Stack(tmp_path)
    point = tmp_path / 'point'
    fields = dict(previous_sha=r.kit_commit(), candidate_sha='b' * 40, run_id='run', operation_id='c' * 64,
                  previous_running=stack.installed, rollback_point=str(point))
    with patch.object(c, 'STATE', tmp_path / 'state'):
        c.journal('creating-rollback-point', **fields)
        def start(compose, running):
            assert json.loads((tmp_path / 'state/last-op.json').read_text())['phase'] == 'preparation-restarting'
            # A failure after writers start must remain resumable without replay.
            (tmp_path / 'paperclip/state').write_text('new acknowledged write')
            raise r.RecoveryError('interrupted restart')
        with patch.object(r, 'restart_services', side_effect=start), pytest.raises(r.RecoveryError):
            c.recover_point(point, stack)
        # Even if an old manifest reappears, this phase never reads it.
        point.mkdir(); (point / 'rollback.json').write_text('stale manifest')
        c.recover_point(point, stack)
        assert (tmp_path / 'paperclip/state').read_text() == 'new acknowledged write'
        assert json.loads((tmp_path / 'state/last-op.json').read_text())['phase'] == 'prepare-failed'


def test_restore_flushes_volumes_before_database_and_barrier(tmp_path):
    stack = Stack(tmp_path)
    fields = dict(previous_sha=r.kit_commit(), candidate_sha='b' * 40, run_id='run', operation_id='c' * 64,
                  previous_running=stack.installed, rollback_point=str(tmp_path / 'point'))
    original_run, original_compose = r.run, Stack.__call__
    events = []
    def run(args, **kwargs):
        if args[:2] == ['sync', '-f']:
            events.append('sync -f')
        return original_run(args, **kwargs)
    def compose(self, *args, **kwargs):
        for command in ('pg_restore', 'CHECKPOINT'):
            if command in args:
                events.append(command)
        return original_compose(self, *args, **kwargs)
    def barrier(phase, **kwargs):
        assert phase == 'restored-services-started'
        events.append('barrier')
        assert events == ['sync -f'] * len(stack.volumes) + ['pg_restore', 'CHECKPOINT', 'barrier']
        assert not set(r.SERVICES) & stack.running
    with patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory), \
         patch.object(c, 'fence_project', side_effect=stack.fence):
        c.rollback_point('create', tmp_path / 'point', stack, operation=fields)
        with patch.object(r, 'run', side_effect=run), patch.object(Stack, '__call__', compose), \
             patch.object(c, 'journal', side_effect=barrier) as journal:
            c.rollback_point('restore', tmp_path / 'point', stack, operation=fields)
        journal.assert_called_once()


@pytest.mark.parametrize('bad', [{'previous_running': ['--privileged']}, {'operation_id': ''},
                                  {'run_id': '../old'}, {'candidate_sha': 'HEAD'},
                                  {'rollback_point': 'relative'}])
def test_invalid_operation_fields_fail_before_checkout(tmp_path, bad):
    fields = dict(previous_sha='a' * 40, candidate_sha='b' * 40, run_id='run', operation_id='c' * 64,
                  previous_running=['paperclip'], rollback_point=str(tmp_path / 'point'))
    with patch.object(c, 'STATE', tmp_path / 'state'), patch.object(r, 'STATE_DIR', tmp_path / 'state'):
        c.journal('restored-services-started', **{**fields, **bad})
        with patch.object(sys, 'argv', ['sfctl', 'recover']), patch.object(r.signal, 'signal'), \
             patch.object(r, 'locked', __import__('contextlib').nullcontext), \
             patch.object(c, 'checkout') as checkout, pytest.raises(r.RecoveryError, match='Invalid upgrade'):
            c.main()
        checkout.assert_not_called()


@pytest.mark.parametrize('operation', ['backup', 'restore'])
@pytest.mark.parametrize('crash_at', ['before-side-effect', 'after-side-effect'])
def test_interrupted_service_operation_recovers_without_data_replay(tmp_path, operation, crash_at):
    stack = Stack(tmp_path)
    stack.container = lambda service: 'container-' + service
    stack.kit = tmp_path
    args = SimpleNamespace(command=operation, _track_service_operation=True)
    with patch.object(r, 'STATE_DIR', tmp_path / 'state'), patch.object(c, 'STATE', tmp_path / 'state'), \
         patch.object(c, 'KIT', tmp_path), patch.object(r, 'kit_commit', return_value='a' * 40), patch.object(r, 'Compose', return_value=stack):
        def crash():
            r.service_operation(args, stack, ['paperclip'] if operation == 'backup' else None)
            if crash_at == 'after-side-effect':
                (tmp_path / 'paperclip/state').write_text('new data')
            os._exit(73)
        run_until_crash(crash)
        expected = 'new data' if crash_at == 'after-side-effect' else 'old-data'
        if operation == 'backup':
            stack.running.remove('paperclip')
            with patch.object(r, 'run', return_value=''):
                c.recover_service_operation()
            assert 'paperclip' in stack.running
        else:
            with patch.object(r, 'run', side_effect=['restore-container', '', '']) as run:
                c.recover_service_operation()
            assert 'label=org.software-factory.recovery-run=' in ' '.join(run.call_args_list[0].args[0])
            assert run.call_args_list[-1].args[0] == ['docker', 'container', 'rm', '--volumes', 'restore-container']
        assert (tmp_path / 'paperclip/state').read_text() == expected
        assert not c.recover_service_operation()


def test_backup_recovery_rejects_replaced_container(tmp_path):
    stack = Stack(tmp_path)
    stack.container = lambda service: 'original-container'
    stack.kit = tmp_path
    args = SimpleNamespace(command='backup', _track_service_operation=True)
    with patch.object(r, 'STATE_DIR', tmp_path / 'state'), patch.object(c, 'STATE', tmp_path / 'state'), \
         patch.object(c, 'KIT', tmp_path), patch.object(r, 'kit_commit', return_value='a' * 40), patch.object(r, 'Compose', return_value=stack):
        r.service_operation(args, stack, ['paperclip'])
        stack.container = lambda service: 'replacement-container'
        with pytest.raises(r.RecoveryError, match='generation changed'):
            c.recover_service_operation()
        assert not stack.calls


@pytest.mark.parametrize('command', [['messaging', 'on'], ['set-hostname', 'host.example.ts.net']])
def test_other_startup_commands_cannot_bypass_pending_recovery(tmp_path, command):
    c.journal('rollback-failed')
    with patch.object(sys, 'argv', ['sfctl', *command]), patch.object(r.signal, 'signal'), \
         patch.object(r, 'locked', __import__('contextlib').nullcontext), patch.object(c, 'update_settings') as update, \
         patch.object(r, 'Compose') as compose, pytest.raises(r.RecoveryError, match='before changing'):
        c.main()
    update.assert_not_called()
    compose.assert_not_called()


def test_secret_transfer_failure_keeps_previous_complete_file(tmp_path):
    source, destination = tmp_path / 'source', tmp_path / 'destination'
    c.atomic_secret(source / '.secrets/key', b'new-generation')
    c.atomic_secret(destination / '.secrets/key', b'old-generation')
    with patch.object(os, 'replace', side_effect=OSError('replacement failed')), pytest.raises(OSError):
        c.copy_secrets(source, destination)
    assert (destination / '.secrets/key').read_bytes() == b'old-generation'
    assert list((destination / '.secrets').iterdir()) == [destination / '.secrets/key']


def test_locked_barrier_recovery_records_completion_after_health(tmp_path):
    stack = Stack(tmp_path)
    fields = dict(previous_sha='a' * 40, candidate_sha='b' * 40, run_id='run', operation_id='c' * 64,
                  previous_running=['postgres', 'paperclip'], rollback_point=str(tmp_path / 'missing'))
    c.journal('restored-services-started', **fields)
    with r.locked() as fd, patch.object(sys, 'argv', ['sfctl', 'recover-locked', '--point', fields['rollback_point'], '--lock-fd', str(fd)]), \
         patch.object(r.signal, 'signal'), patch.object(r, 'Compose', return_value=stack), \
         patch.object(c, 'health', return_value=True) as health:
        assert c.main() == 0
    health.assert_called_once_with(['postgres', 'paperclip'])
    assert json.loads((c.STATE / 'last-op.json').read_text())['phase'] == 'rolled-back'


def test_partial_recovery_health_does_not_probe_stopped_caddy(tmp_path, capsys):
    stack = MagicMock()
    stack.services.return_value = ['paperclip', 'postgres', 'caddy']
    def compose(*args):
        if args[0] == 'ps':
            return json.dumps([{'Service': service, 'Health': 'healthy'} for service in ['postgres', 'paperclip']])
        assert args[:4] == ('exec', '-T', 'paperclip', 'node')
        assert '127.0.0.1:3100' in args[-1]
        return ''
    stack.side_effect = compose
    with patch.object(r, 'Compose', return_value=stack), patch.object(c, 'probe', return_value=None), \
         patch.object(r, 'schema_state', return_value={'applied': [1]}):
        c.health(['postgres', 'paperclip'])
    assert json.loads(capsys.readouterr().out)['checks']['paperclip']


@pytest.mark.parametrize('serve', ['[]', '{"Web":[]}', '{"Web":{"fixture:443":[]}}', '{"TCP":[]}'])
def test_malformed_serve_status_still_returns_bounded_health_json(tmp_path, capsys, serve):
    compose = MagicMock()
    compose.services.return_value = ['paperclip']
    compose.side_effect = lambda *args: '[{"Service":"paperclip","Health":"healthy"}]' if args[0] == 'ps' else ''
    with patch.object(r, 'Compose', return_value=compose), patch.object(c, 'probe', return_value=serve), \
         patch.object(c, 'settings', return_value={'TAILSCALE_HOSTNAME': 'fixture'}), \
         patch.object(r, 'schema_state', return_value={'applied': [1]}):
        assert not c.health()
    output = capsys.readouterr().out
    assert len(output.encode()) < 4096 and len(output.splitlines()) == 1
    assert not json.loads(output)['checks']['tailscale_serve']


@pytest.mark.parametrize('phase', ['restored-services-started', 'preparation-restarting'])
def test_barrier_rejects_changing_point_identity(tmp_path, phase):
    with pytest.raises(r.RecoveryError, match='Cannot change rollback point'):
        c.validate_point(tmp_path / 'old-run', {'phase': phase, 'rollback_point': str(tmp_path / 'this-run')})


@pytest.mark.parametrize('field,value', [('target', 'bad target'), ('vm_id', 'bad\r\nheader'),
                                        ('vm_id', 'v' * 513), ('stop_operation_id', '../old')])
def test_invalid_backup_identifiers_fail_before_downtime(tmp_path, field, value):
    args = SimpleNamespace(local_dir=None, recipient=None, env_file=tmp_path / 'backup.env',
                           target='fixture', vm_id='fixture-vm', reason='pre-stop', stop_operation_id='stop')
    setattr(args, field, value)
    with patch.object(r, 'BACKUP_ENV_FILE', tmp_path / 'backup.env'), \
         patch.object(r, 'backup_settings', return_value=('age1' + secrets.token_hex(29), r.sas_url(delegation()))), \
         patch.object(r, 'Compose') as compose, pytest.raises(r.RecoveryError, match='Invalid'):
        r.data_backup(args)
    compose.assert_not_called()


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_interrupted_backup_removes_oneoffs_before_cleanup_and_restart(tmp_path, cleanup_fails, capsys):
    stack = Stack(tmp_path)
    stack.kit = tmp_path
    stack.container = lambda service: 'original'
    stage = tmp_path / 'paperclip-hermes-recovery-generation'
    stage.mkdir(mode=0o700); (stage / 'plaintext').write_text('private')
    args = SimpleNamespace(command='backup', _track_service_operation=True)
    with patch.object(c, 'KIT', tmp_path), patch.object(r, 'kit_commit', return_value='a' * 40), \
         patch.object(r, 'Compose', return_value=stack):
        r.service_operation(args, stack, ['paperclip'], stage)
        events = []
        original = shutil.rmtree
        def remove(path):
            events.append('purge')
            assert events[:2] == ['list', 'rm']
            if cleanup_fails:
                raise OSError('purge failed')
            original(path)
        def docker(argv, **kwargs):
            events.append('list' if argv[1:3] == ['container', 'ls'] else 'rm')
            if events[-1] == 'list':
                assert 'label=org.software-factory.recovery-run=' + args._service_operation['run_id'] in argv
                return 'oneoff'
            assert argv == ['docker', 'rm', '--force', '--volumes', 'oneoff']
            return ''
        with patch.object(r, 'run', side_effect=docker), patch.object(shutil, 'rmtree', side_effect=remove), \
             patch.object(r, 'restart_services', side_effect=lambda *args: events.append('restart')):
            if cleanup_fails:
                with pytest.raises(OSError):
                    c.recover_service_operation()
            else:
                c.recover_service_operation()
        assert events == ['list', 'rm', 'purge', 'restart']
    assert ('ops-alert: interrupted backup plaintext cleanup failed' in capsys.readouterr().err) == cleanup_fails


def test_documented_compose_minimum_supports_gateway_priority():
    import re
    readme = (KIT.parents[2] / 'README.md').read_text()
    version = re.search(r'Docker Compose v([0-9]+)\.([0-9]+)\.([0-9]+) or later', readme)
    assert version and tuple(map(int, version.groups())) >= (2, 33, 1)
    assert 'no daemon needed' in readme
    assert any('gw_priority' in network for service in yaml.safe_load((KIT / 'compose.yaml').read_text())['services'].values()
               if isinstance(service.get('networks'), dict)
               for network in service['networks'].values() if isinstance(network, dict))


def test_persistent_container_lookup_excludes_abandoned_oneoffs():
    compose = r.Compose(Path('/kit'), 'fixture')
    def docker(args, **kwargs):
        assert 'label=com.docker.compose.project=fixture' in args
        assert 'label=com.docker.compose.service=hermes' in args
        return 'original\n' if 'label=com.docker.compose.oneoff=False' in args else 'original\noneoff\n'
    with patch.object(r, 'run', side_effect=docker):
        assert compose.container('hermes') == 'original'


def test_database_started_for_restore_does_not_escape_recorded_inventory(tmp_path):
    stack = Stack(tmp_path)
    point = tmp_path / 'point'
    with patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory), \
         patch.object(c, 'fence_project', side_effect=stack.fence):
        record = c.rollback_point('create', point, stack)
        record['running'].remove('postgres')
        r.atomic_json(point / 'rollback.json', record)
        c.rollback_point('restore', point, stack)
    assert stack.running == {'paperclip', 'caddy'}


@pytest.mark.parametrize('old,new', [('storage.example.test', 'bad%00host.example.test'),
                                    ('storage.example.test', '-bad.example.test'),
                                    ('storage.example.test', 'bad..example.test'),
                                    ('/backups?', '/bad--container?')])
def test_invalid_sas_resource_uri_is_rejected(old, new):
    with pytest.raises(r.RecoveryError):
        r.sas_url(delegation().replace(old, new))


@pytest.mark.parametrize('action', ['create', 'restore'])
def test_standalone_points_cannot_bypass_pending_upgrade(action, tmp_path):
    c.journal('restored-services-started')
    with patch.object(r.signal, 'signal'), patch.object(r, 'locked', __import__('contextlib').nullcontext), \
         pytest.raises(r.RecoveryError, match='before standalone'):
        r.main(['rollback-point', action, '--path', str(tmp_path / 'point')])


def test_failed_preparation_journal_cannot_skip_safe_restart(tmp_path, capsys):
    stack = Stack(tmp_path)
    stack.fail_dump = True
    fields = dict(previous_running=stack.installed)
    with patch.object(c, 'journal', side_effect=OSError('state disk failed')), \
         pytest.raises(r.RecoveryError, match='dump failed'):
        c.rollback_point('create', tmp_path / 'point', stack, operation=fields)
    assert stack.running == set(stack.installed)
    assert 'ops-alert: preparation restart journal failed' in capsys.readouterr().err


@pytest.mark.parametrize('modified', [True, 'unknown'])
def test_upgrade_refuses_source_that_cannot_be_recovered_from_its_commit(tmp_path, modified):
    with patch.object(r, 'kit_modified', return_value=modified), patch.object(r, 'Compose', return_value=Stack(tmp_path)) as compose, \
         patch.object(c, 'required_space', return_value=0), patch.object(c, 'ROLLBACK_ROOT', tmp_path / 'points'), \
         patch.object(c, 'rollback_point') as snapshot, pytest.raises(r.RecoveryError, match='recorded commit'):
        c.upgrade('b' * 40, 'run', 7)
    snapshot.assert_not_called()
    assert not compose.return_value.calls


@pytest.mark.parametrize('unsafe', ['writable', 'other-owner'])
def test_staging_and_stale_cleanup_refuse_renameable_ancestors(tmp_path, unsafe):
    parent = tmp_path / 'parent'
    parent.mkdir(mode=0o700)
    work = parent / 'work'; work.mkdir(mode=0o700)
    stale = work / 'paperclip-hermes-recovery-retained'; stale.mkdir(mode=0o700)
    (stale / 'sentinel').write_text('retained')
    if unsafe == 'writable':
        parent.chmod(0o777)
    else:
        os.chown(parent, 10000, 10000)
    with pytest.raises(r.RecoveryError, match='protected against renames'):
        with r.staging(work):
            pytest.fail('unsafe staging was entered')
    with pytest.raises(r.RecoveryError, match='protected against renames'):
        r.clear_stale(work)
    assert (stale / 'sentinel').read_text() == 'retained'


def test_host_git_commands_cannot_touch_inherited_repository_or_index(tmp_path):
    victim = tmp_path / 'victim'; victim.mkdir()
    index = tmp_path / 'host-index'; index.write_bytes(b'preserve host index')
    intended = tmp_path / 'intended'
    with patch.dict(os.environ, GIT_DIR=str(victim), GIT_WORK_TREE=str(victim), GIT_INDEX_FILE=str(index)):
        r.run(['git', 'init', intended])
        (intended / 'file').write_text('fixture')
        r.run(['git', '-C', intended, 'add', 'file'])
    assert index.read_bytes() == b'preserve host index'
    assert not list(victim.iterdir())
    assert (intended / '.git/index').is_file()


def test_staging_uses_validated_canonical_parent_even_if_input_link_changes(tmp_path):
    secure = tmp_path / 'secure'; secure.mkdir(mode=0o700)
    unsafe = tmp_path / 'unsafe'; unsafe.mkdir(mode=0o777); unsafe.chmod(0o777)
    link = tmp_path / 'link'; link.symlink_to(secure)
    original = r.tempfile.TemporaryDirectory
    def swap_link(**kwargs):
        link.unlink(); link.symlink_to(unsafe)
        return original(**kwargs)
    with patch.object(r.tempfile, 'TemporaryDirectory', side_effect=swap_link):
        with r.staging(link) as stage:
            assert stage.parent == secure
    assert not list(unsafe.iterdir())


@pytest.mark.parametrize('recipient_usable', [False, True])
def test_backup_recipient_must_encrypt_before_credential_replacement(tmp_path, recipient_usable):
    host = tmp_path / 'host'; host.mkdir(mode=0o700)
    original = b'working credential'
    c.atomic_secret(host / 'backup.env', original)
    public = 'age1' + secrets.token_hex(29)
    replacement = 'BACKUP_AGE_RECIPIENT=' + public + '\nBACKUP_CONTAINER_SAS_URL=' + delegation() + '\n'
    def encrypt(args, **kwargs):
        assert (host / 'backup.env').read_bytes() == original
        assert args == ['age', '--encrypt', '--recipient', public]
        assert kwargs['output'].tell() == 0
        assert 'sig=' not in repr(args)
        if not recipient_usable:
            raise r.RecoveryError('invalid age recipient')
        kwargs['output'].write(b'encrypted empty payload')
        return ''
    with patch.object(c, 'HOST', host), patch.object(c, 'decrypt_input', return_value=replacement), \
         patch.object(sys, 'argv', ['sfctl', 'set-backup-credential']), patch.object(r.signal, 'signal'), \
         patch.object(r, 'run', side_effect=encrypt), patch.object(r, 'locked', __import__('contextlib').nullcontext):
        if recipient_usable:
            c.main()
        else:
            with pytest.raises(r.RecoveryError, match='invalid age recipient'):
                c.main()
    assert (host / 'backup.env').read_bytes() == (replacement.encode() if recipient_usable else original)


@pytest.mark.parametrize('name', ['postgres-password', 'paperclip-secrets-master-key'])
@pytest.mark.parametrize('phase', ['installing', 'restored-services-started'])
def test_snapshot_bound_secrets_cannot_rotate_during_recovery(tmp_path, name, phase):
    c.journal(phase)
    original = secrets.token_hex(32).encode() + b'\n'
    with patch.object(c, 'KIT', tmp_path):
        c.atomic_secret(tmp_path / '.secrets' / name, original)
        with patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(secrets.token_hex(32).encode()))), \
             pytest.raises(r.RecoveryError, match='Recover the interrupted upgrade'):
            c.secret_set(name, True)
        assert (tmp_path / '.secrets' / name).read_bytes() == original


@pytest.mark.parametrize('legacy', ['missing', 'no-command', 'broken-import'])
def test_upgrade_refuses_previous_without_locked_recovery_before_capture(tmp_path, legacy):
    previous = tmp_path / 'previous'
    previous.mkdir()
    if legacy == 'no-command':
        (previous / 'kit-control.py').write_text(
            "import argparse\np = argparse.ArgumentParser()\np.add_subparsers().add_parser('status')\np.parse_args()\n")
    elif legacy == 'broken-import':
        (previous / 'kit-control.py').write_text('import missing_legacy_dependency\n')
    stack = Stack(tmp_path)
    with patch.object(r, 'Compose', return_value=stack), patch.object(r, 'kit_commit', return_value='a' * 40), \
         patch.object(c, 'required_space', return_value=0), patch.object(c, 'checkout', return_value=previous), \
         patch.object(c, 'ROLLBACK_ROOT', tmp_path / 'points'), patch.object(c, 'rollback_point') as capture, \
         pytest.raises(r.RecoveryError, match='Previous kit cannot run recover-locked; pre-WP3.*migration'):
        c.upgrade('b' * 40, 'first-wp3', 0)
    capture.assert_not_called()
    assert not stack.calls
    assert not (c.STATE / 'last-op.json').exists()


def test_current_kit_can_run_locked_recovery_help():
    c.require_recovery_support(KIT)


def test_backup_retry_runbook_resolves_pending_operation_before_restart():
    text = (KIT / 'README.md').read_text()
    retry = text[text.index('To re-run a failed backup,'):text.index('The host-level')]
    assert retry.index('sfctl status') < retry.index('sfctl recover') < retry.index('systemctl start')
    assert 'no pending\noperation, retry directly' in retry


@pytest.mark.parametrize('start,expiry,accepted', [
    (-2, 6, False), (-1, 6, True), (None, 6, True), (None, 8, False),
])
def test_sas_signed_lifetime_is_at_most_seven_days(start, expiry, accepted):
    now = datetime(2026, 11, 26, tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    url = delegation(st=None if start is None else (now + timedelta(days=start)).isoformat(),
                     se=(now + timedelta(days=expiry)).isoformat(),
                     skt=(now - timedelta(hours=1)).isoformat(),
                     ske=(now + timedelta(days=6, hours=1)).isoformat())
    with patch.object(r, 'datetime', Clock):
        if accepted:
            assert r.sas_url(url)
        else:
            with pytest.raises(r.RecoveryError, match='at most 7 days'):
                r.sas_url(url)


@pytest.mark.parametrize('phase', c.RECOVERY_PHASES)
def test_backup_refuses_unresolved_upgrade_before_capture_and_preserves_journal(tmp_path, phase):
    c.journal(phase, previous_sha='a' * 40, candidate_sha='b' * 40)
    before = (c.STATE / 'last-op.json').read_bytes()
    with patch.object(r.signal, 'signal'), patch.object(r, 'data_backup') as capture, \
         patch.object(r, 'Compose') as compose:
        with pytest.raises(r.RecoveryError, match='unresolved upgrade'):
            r.main(['backup'])
        with pytest.raises(r.RecoveryError, match='unresolved upgrade'):
            r.backup(SimpleNamespace())
    capture.assert_not_called()
    compose.assert_not_called()
    assert (c.STATE / 'last-op.json').read_bytes() == before
    assert not (c.STATE / 'service-op.json').exists()


@pytest.mark.parametrize('phase', c.RESOLVED_PHASES)
def test_backup_accepts_resolved_upgrade(phase):
    c.journal(phase)
    with patch.object(r, 'data_backup', return_value='captured'):
        assert r.backup(SimpleNamespace()) == 'captured'


@pytest.mark.parametrize('rows,healthy', [
    (['hermes', 'sandbox', 'caddy'], True),
    (['hermes', 'orphan', 'caddy'], False),
    (['hermes', 'hermes', 'caddy'], False),
    (['hermes', 'sandbox', 'caddy', 'orphan'], False),
    (['hermes', 'caddy'], False),
])
def test_health_requires_exact_service_inventory(tmp_path, capsys, rows, healthy):
    compose = MagicMock()
    compose.services.return_value = ['hermes', 'sandbox', 'caddy']
    compose.side_effect = lambda *args: json.dumps(
        [{'Service': name, 'Health': 'healthy'} for name in rows]) if args[0] == 'ps' else '401'
    with patch.object(r, 'Compose', return_value=compose), patch.object(c, 'probe', return_value=None):
        c.health()
    assert json.loads(capsys.readouterr().out)['checks']['compose'] is healthy


@pytest.mark.parametrize('rows', [[], [{'Service': 'orphan', 'Health': 'healthy'}]])
def test_empty_recovery_inventory_checks_only_that_project_is_stopped(capsys, rows):
    compose = MagicMock(return_value=json.dumps(rows))
    compose.services.return_value = []
    with patch.object(r, 'Compose', return_value=compose), patch.object(c, 'probe') as probe:
        assert c.health([]) is (not rows)
        probe.assert_not_called()
        assert compose.call_count == 1
    assert json.loads(capsys.readouterr().out)['healthy'] is (not rows)
    with patch.object(r, 'Compose', return_value=compose), patch.object(c, 'probe', return_value=None):
        assert not c.health()  # Ordinary health remains strict even with an empty model.


def test_stopped_hermes_rollback_completes_and_unblocks_upgrade(tmp_path, capsys):
    stack = Stack(tmp_path, paperclip=False)
    stack.running.clear()
    point = tmp_path / 'point'
    fields = dict(previous_sha='a' * 40, candidate_sha='b' * 40, run_id='stopped', operation_id='c' * 64,
                  previous_running=[], rollback_point=str(point))
    with patch.object(r, 'kit_commit', return_value='a' * 40), \
         patch.object(c, 'volume_map', side_effect=lambda *args: stack.inventory), \
         patch.object(c, 'fence_project', side_effect=stack.fence):
        c.rollback_point('create', point, stack, operation=fields)
        c.journal('installing', **fields)
        # Stack.ps emits an empty string when stopped, as Compose does.
        with r.locked() as fd, patch.object(sys, 'argv', [
                'sfctl', 'recover-locked', '--point', str(point), '--lock-fd', str(fd)]), \
             patch.object(r.signal, 'signal'), patch.object(r, 'Compose', return_value=stack):
            assert c.main() == 0
    assert json.loads((c.STATE / 'last-op.json').read_text())['phase'] == 'rolled-back'
    assert not stack.running
    assert not any(args[0] == 'up' for args in stack.calls)
    r.require_resolved_upgrade()


def test_kit_build_images_are_distinct_and_recovery_uses_previous_tag(tmp_path, monkeypatch):
    previous, candidate = tmp_path / 'previous', tmp_path / 'candidate'
    for kit in (previous, candidate):
        shutil.copytree(KIT, kit)
        (kit / '.secrets').mkdir(mode=0o700)
        c.atomic_secret(kit / '.secrets/compose.env', b'COMPOSE_PROFILES=hermes\nKIT_IMAGE_TAG=stale\n')
    with (candidate / 'Dockerfile.hermes').open('a') as stream:
        stream.write('\nLABEL candidate=true\n')
    monkeypatch.setenv('KIT_IMAGE_TAG', 'inherited-candidate')
    old, new = r.Compose(previous, 'fixture'), r.Compose(candidate, 'fixture')
    assert old.image_tag != new.image_tag
    old_model, new_model = [json.loads(compose('config', '--format', 'json')) for compose in (old, new)]
    for service in ('hermes', 'sandbox'):
        assert old_model['services'][service]['image'] == 'software-factory-' + service + ':' + old.image_tag
        assert new_model['services'][service]['image'] != old_model['services'][service]['image']
    c.journal('restored-services-started', previous_sha='a' * 40, candidate_sha='b' * 40,
              run_id='run', operation_id='c' * 64, previous_running=['hermes'],
              rollback_point=str(tmp_path / 'missing'))
    with patch.object(r, 'run', return_value='') as execute, patch.object(c, 'rollback_point') as replay:
        c.recover_point(tmp_path / 'missing', old)
    replay.assert_not_called()
    command = execute.call_args.args[0]
    assert '--no-build' in command and command[-1] == 'hermes'
    assert execute.call_args.kwargs['env']['KIT_IMAGE_TAG'] == old.image_tag


def test_restore_completion_failure_running_record_recovers_with_no_containers(tmp_path, capsys):
    # A failed completion write leaves running intent after the fencing handler.
    r.atomic_json(c.STATE / 'service-op.json', {
        'protocol': 1, 'operation': 'restore', 'phase': 'running', 'run_id': 'c' * 32,
        'kit_commit': r.kit_commit(), 'project': 'fixture', 'stage': None,
    })
    commit = r.kit_commit()
    with patch.object(r, 'run', return_value='') as execute, patch.object(r, 'restart_services') as restart, \
         patch.object(r, 'kit_commit', return_value=commit):
        assert c.recover_service_operation()
        assert not c.recover_service_operation()
    assert execute.call_count == 1  # Only list the generation: it is already fenced.
    restart.assert_not_called()
    assert json.loads((c.STATE / 'service-op.json').read_text())['phase'] == 'complete'
    assert 'Interrupted restore fenced' in capsys.readouterr().out


def test_identical_code_from_distinct_kit_commits_has_distinct_image_tags(tmp_path):
    previous, restored = tmp_path / 'previous', tmp_path / 'restored'
    previous.mkdir()
    (previous / 'compose.yaml').write_text('services: {}\n')
    files = {name: r.digest(path) for name, path in r.kit_files(previous, r.CODE).items()}
    r.atomic_json(previous / '.kit-provenance.json', {'kit_commit': 'a' * 40, 'files': files})
    shutil.copytree(previous, restored)
    assert r.kit_image_tag(previous) == r.kit_image_tag(restored)
    r.atomic_json(restored / '.kit-provenance.json', {'kit_commit': 'b' * 40, 'files': files})
    assert r.kit_image_tag(previous) != r.kit_image_tag(restored)


def test_previous_builds_are_tagged_from_actual_container_images_even_when_stopped():
    compose = r.Compose(KIT, 'fixture')
    def docker(args, **kwargs):
        if args[:2] == ['docker', 'inspect']:
            return 'sha256:' + ('a' if args[-1] == 'hermes-container' else 'b') * 64
        return ''
    with patch.object(compose, 'services', return_value=['hermes', 'sandbox', 'caddy']), \
         patch.object(compose, 'container', side_effect=lambda name: name + '-container'), \
         patch.object(r, 'run', side_effect=docker) as execute:
        compose.preserve_build_images()
    assert [call.args[0] for call in execute.call_args_list] == [
        ['docker', 'inspect', '--format', '{{.Image}}', 'hermes-container'],
        ['docker', 'image', 'tag', 'sha256:' + 'a' * 64, 'software-factory-hermes:' + compose.image_tag],
        ['docker', 'inspect', '--format', '{{.Image}}', 'sandbox-container'],
        ['docker', 'image', 'tag', 'sha256:' + 'b' * 64, 'software-factory-sandbox:' + compose.image_tag],
    ]


@pytest.mark.parametrize('tagging_fails', [False, True])
def test_upgrade_preserves_previous_build_before_snapshot(tmp_path, tagging_fails):
    stack = Stack(tmp_path, paperclip=False)
    events = []
    def preserve():
        events.append('preserve')
        assert stack.running == set(stack.installed)
        if tagging_fails:
            raise r.RecoveryError('tag failed')
    def capture(*args, **kwargs):
        events.append('capture')
        raise r.RecoveryError('capture reached')
    with patch.object(r, 'Compose', return_value=stack), patch.object(r, 'kit_commit', return_value='a' * 40), \
         patch.object(c, 'required_space', return_value=0), patch.object(c, 'checkout', return_value=KIT), \
         patch.object(c, 'ROLLBACK_ROOT', tmp_path / 'points'), \
         patch.object(stack, 'preserve_build_images', side_effect=preserve), \
         patch.object(c, 'rollback_point', side_effect=capture), \
         pytest.raises(r.RecoveryError, match='tag failed' if tagging_fails else 'capture reached'):
        c.upgrade('b' * 40, 'preserve', 0)
    assert events == (['preserve'] if tagging_fails else ['preserve', 'capture'])
    assert (c.STATE / 'last-op.json').exists() is (not tagging_fails)
    assert stack.running == set(stack.installed)
