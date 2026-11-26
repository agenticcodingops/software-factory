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


@pytest.mark.parametrize('permissions', ['c', 'cw'])
def test_user_delegation_accept(permissions):
    assert r.sas_url(delegation(sp=permissions)).scheme == 'https'


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
        self.fail_dump = self.fail_restart = False

    def images(self):
        return {service: {'image_id': 'fixture'} for service in self.installed}

    def services(self):
        return self.installed

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
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
    with patch.object(c, 'volume_map', return_value=stack.volumes):
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
    with patch.object(c, 'volume_map', return_value=stack.volumes):
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


def test_upgrade_failed_health_restores_previous_code_and_data(tmp_path):
    stack=Stack(tmp_path)
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
            c.rollback_point('restore',Path(args[args.index('--point')+1]),stack)
            return ''
        return original(args,**kwargs)
    with patch.object(c,'KIT',old),patch.object(c,'INSTALL',install),patch.object(c,'STATE',tmp_path/'status'), \
         patch.object(c,'ROLLBACK_ROOT',tmp_path/'points'),patch.object(c.r,'kit_commit',return_value=previous), \
         patch.object(c,'volume_map',return_value=stack.volumes),patch.object(c,'required_space',return_value=0), \
         patch.object(c.r,'Compose',side_effect=compose_factory),patch.object(c,'checkout',side_effect=lambda sha: old if sha==previous else candidate), \
         patch.object(c.r,'run',side_effect=execute),pytest.raises(r.RecoveryError,match='previous kit and data restored'):
        c.upgrade(candidate_sha,'upgrade-1',0)
    assert events==['previous-activated-before-restore']
    assert all((v/'state').read_text()=='old-data' for v in stack.volumes.values())
    assert stack.running==set(stack.installed)
    assert json.loads((tmp_path/'status/last-op.json').read_text())['phase']=='rolled-back'


def test_upgrade_handoff_journal_blocks_another_candidate(tmp_path):
    status=tmp_path/'status';status.mkdir(mode=0o700)
    r.atomic_json(status/'last-op.json',dict(protocol=1,operation='upgrade',phase='healthy-awaiting-handoff'))
    with patch.object(c,'STATE',status),patch.object(c.r,'Compose') as compose,pytest.raises(r.RecoveryError,match='handoff'):
        c.upgrade('b'*40,'run',0)
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
            assert '--ca-certificate=/public-ca/root.crt' in args[-1]
            assert 'https://caddy/health/detailed' in args[-1]
            return '  HTTP/1.1 401 Unauthorized\n'
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
    stack.assert_called_once_with('start','--wait','--wait-timeout','300','paperclip','caddy')


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
