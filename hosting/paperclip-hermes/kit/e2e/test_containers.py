"""Container acceptance: real pinned Paperclip/PostgreSQL/Caddy, tar, age, OpenSSL and Git.

Run through ci/kit-e2e.sh on a Docker-capable root Linux runner. No cloud,
GitHub credential, tailnet or model API call is used. The two fixture commits
use the source commit's identity, without changing any Git configuration.
"""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest
import yaml

SOURCE = Path(__file__).resolve().parents[1]
REPO = SOURCE.parents[2]


def run(*args, **kwargs):
    return subprocess.run(list(map(str,args)), check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def load(kit):
    spec=importlib.util.spec_from_file_location('e2e_control', kit/'kit-control.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installation():
    # Restore's root-owned-parent contract deliberately excludes /tmp.
    with tempfile.TemporaryDirectory(prefix='sf-kit-e2e-',dir='/root') as directory:
        root=Path(directory)
        old=root/'previous'
        shutil.copytree(SOURCE,old)
        # A local auth boundary for the TLS client regression; no Hermes API call.
        caddyfile = old / 'Caddyfile'
        caddyfile.write_text(caddyfile.read_text().replace('https://caddy {',
                             'https://caddy {\n\trespond /health/detailed 401'))
        run('git','init',old)
        author=run('git','-C',REPO,'log','-1','--format=%an%n%ae').splitlines()
        env={**os.environ,'GIT_AUTHOR_NAME':author[0],'GIT_AUTHOR_EMAIL':author[1],
             'GIT_COMMITTER_NAME':author[0],'GIT_COMMITTER_EMAIL':author[1]}
        run('git','-C',old,'add','.')
        run('git','-C',old,'commit','-m','test: previous kit fixture',env=env)
        previous=run('git','-C',old,'rev-parse','HEAD')
        candidate=root/'candidate'
        run('git','clone','--no-hardlinks',old,candidate)
        # A real code/config delta: restoring under this commit must fail its
        # byte comparison, whereas the original commit succeeds.
        with (candidate/'Caddyfile').open('a') as stream:
            stream.write('\n# Candidate release fixture.\n')
        config = yaml.safe_load((candidate / 'compose.yaml').read_text())
        config['volumes']['candidate_only'] = {}
        config['services']['candidate_writer'] = {
            'image': config['services']['postgres']['image'],
            'entrypoint': ['/bin/sh', '-c'],
            'command': ['while true; do echo candidate >> /shared/candidate-writes; sleep 0.1; done'],
            'volumes': ['paperclip:/shared'], 'network_mode': 'none',
        }
        config['services']['caddy']['volumes'].append('candidate_only:/candidate-only')
        (candidate / 'compose.yaml').write_text(yaml.safe_dump(config))
        run('git','-C',candidate,'add','Caddyfile','compose.yaml')
        run('git','-C',candidate,'commit','-m','test: candidate kit fixture',env=env)
        candidate_sha=run('git','-C',candidate,'rev-parse','HEAD')
        (root/'installation').mkdir()
        control=load(old)
        r=control.r
        (old/'.secrets').mkdir(mode=0o700)
        (old/'.secrets/compose.env').write_text('COMPOSE_PROFILES=paperclip\nTAILSCALE_HOSTNAME=fixture.example.ts.net\nFIXTURE_REPOSITORY=fixture-owner/fixture\nHERMES_MODEL=fixture-model\n')
        (old/'.secrets/compose.env').chmod(0o600)
        with patch.object(control,'KIT',old):
            control.prepare_config()
        for name in ('github-token','sandbox_ed25519','sandbox_ed25519.pub'):
            control.atomic_secret(old/'.secrets'/name,secrets.token_hex(32).encode())
        compose=r.Compose(old,'paperclip-hermes')
        with patch.object(r,'STATE_DIR',root/'state'),patch.object(control,'STATE',root/'state'),patch.object(control,'ROLLBACK_ROOT',root/'rollback'):
            try:
                compose('up','-d','--wait','--wait-timeout','300')
                compose('exec','-T','postgres','psql','-U','paperclip','-d','paperclip','-c',
                        "CREATE TABLE wp3_sentinel (value text); INSERT INTO wp3_sentinel VALUES ('previous');")
                yield root,old,candidate,previous,candidate_sha,control,compose
            finally:
                for name in ('paperclip-hermes','sf-e2e-restored','sf-e2e-wrong'):
                    with patch.object(compose,'name',name):
                        try:
                            compose('down','--volumes','--remove-orphans')
                        except r.RecoveryError:
                            pass
                # The previous Compose file cannot remove the candidate-only volume.
                for project in ('paperclip-hermes', 'sf-e2e-restored', 'sf-e2e-wrong'):
                    candidate_volume = project + '_candidate_only'
                    if run('docker', 'volume', 'ls', '--quiet', '--filter', 'name=^' + candidate_volume + '$'):
                        run('docker', 'volume', 'rm', candidate_volume)


def sentinel(compose):
    return compose('exec','-T','postgres','psql','-XAt','-U','paperclip','-d','paperclip','-c','SELECT value FROM wp3_sentinel').strip()


def container_status(compose):
    raw = compose('ps', '--format', 'json').strip()
    # Compose releases emit either a JSON array or one JSON object per line.
    return json.loads(raw) if raw.startswith('[') else [json.loads(line) for line in raw.splitlines()]


def test_caddy_tls_probe_supports_private_ca_and_rejects_untrusted_ca(installation):
    root,old,candidate,previous,candidate_sha,c,compose=installation
    # Exercise the exact health() command in the pinned Alpine image.
    probe = ['exec', '-T', 'caddy', 'curl', '--silent', '--show-error',
             '--cacert', '/public-ca/root.crt', '--output', '/dev/null',
             '--write-out', '%{http_code}', 'https://caddy/health/detailed']
    assert compose(*probe) == '401'
    # Caddy installs its own root into the system trust store at startup.
    # Use an independent CA and an empty CA directory for the negative probe.
    run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', root / 'untrusted.key', '-out', root / 'untrusted.crt',
        '-subj', '/CN=untrusted-e2e', '-days', '1')
    with (root / 'untrusted.crt').open('rb') as cert:
        compose('exec', '-T', 'caddy', 'sh', '-c', 'cat > /tmp/untrusted.crt', input_file=cert)
    compose('exec', '-T', 'caddy', 'mkdir', '/tmp/empty-ca')
    probe[probe.index('/public-ca/root.crt')] = '/tmp/untrusted.crt'
    probe[4:4] = ['--capath', '/tmp/empty-ca']
    with pytest.raises(c.r.RecoveryError):
        compose(*probe)


def test_upgrade_rollback(installation):
    root,old,candidate,previous,candidate_sha,c,compose=installation
    commands=[]
    original=c.r.run
    def execute(args,**kwargs):
        commands.append(list(map(str,args)))
        if 'pg_dump' in args:
            assert set(compose('ps','--services','--status','running').split()) == {'postgres'}
        if str(args[0])=='bash' and str(args[1])==str(candidate/'health.sh'):
            compose('exec','-T','postgres','psql','-U','paperclip','-d','paperclip','-c',"UPDATE wp3_sentinel SET value='candidate-migration';")
            raise c.r.RecoveryError('Injected candidate health failure')
        if str(args[0]) == 'tar' and '--extract' in args:
            assert not original(['docker', 'container', 'ls', '--all', '--quiet', '--filter',
                                 'label=com.docker.compose.project=paperclip-hermes']).strip()
        if 'recover-locked' in args:
            # Invoke the previous checkout's actual restore implementation,
            # while the caller retains the real host operation lock.
            c.recover_point(Path(args[args.index('--point')+1]), compose)
            return ''
        return original(args,**kwargs)
    with patch.object(c,'checkout',side_effect=lambda sha: old if sha==previous else candidate), \
         patch.object(c,'INSTALL',root/'installation'),patch.object(c.r,'run',side_effect=execute), \
         c.r.locked() as lock_fd,pytest.raises(c.r.RecoveryError,match='previous kit and data restored'):
        c.upgrade(candidate_sha,'e2e-upgrade',lock_fd)
    assert run('docker', 'volume', 'ls', '--quiet', '--filter', 'name=^paperclip-hermes_candidate_only$')
    assert not run('docker', 'container', 'ls', '--all', '--quiet', '--filter',
                   'label=com.docker.compose.project=paperclip-hermes', '--filter',
                   'label=com.docker.compose.service=candidate_writer')
    assert sentinel(compose)=='previous'
    rows=container_status(compose)
    assert all(row['Health']=='healthy' for row in rows)
    assert json.loads((root/'state/last-op.json').read_text())['phase']=='rolled-back'
    dump=next(i for i,cmd in enumerate(commands) if 'pg_dump' in cmd)
    stop_db=next(i for i,cmd in enumerate(commands) if cmd[-4:]==['stop','--timeout','90','postgres'])
    archive=next(i for i,cmd in enumerate(commands) if cmd[0]=='tar' and '-cf' in cmd)
    assert dump<stop_db<archive
    compose('exec', '-T', 'postgres', 'psql', '-U', 'paperclip', '-d', 'paperclip', '-c',
            "UPDATE wp3_sentinel SET value='post-rollback-write';")
    for command in ('rollback', 'recover'):
        with patch.object(sys, 'argv', ['sfctl', command]), \
             patch.object(c.os, 'umask'), patch.object(c.r.signal, 'signal'), \
             pytest.raises(c.r.RecoveryError, match='unresolved upgrade'):
            c.main()
        assert sentinel(compose) == 'post-rollback-write'


def test_dump_failure_keeps_old_stack_healthy(installation):
    root,old,candidate,previous,candidate_sha,c,compose=installation
    containers = {service: compose.container(service) for service in compose.services()}
    original=c.r.run
    def execute(args,**kwargs):
        if 'pg_dump' in args:
            raise c.r.RecoveryError('Injected pg_dump failure')
        assert not (str(args[0])=='tar' and '-cf' in args)
        return original(args,**kwargs)
    with patch.object(c.r,'run',side_effect=execute),pytest.raises(c.r.RecoveryError,match='pg_dump failure'):
        c.rollback_point('create',root/'failed-point',compose)
    assert sentinel(compose)=='previous'
    assert all(row['Health']=='healthy' for row in container_status(compose))
    assert not (root/'failed-point').exists()
    assert containers == {service: compose.container(service) for service in compose.services()}


def test_cross_version_restore(installation):
    root,old,candidate,previous,candidate_sha,c,compose=installation
    identity=root/'key'
    run('age-keygen','-o',identity)
    identity.chmod(0o600)
    recipient=run('age-keygen','-y',identity)
    uploads=root/'uploads';uploads.mkdir(mode=0o700)
    r=c.r
    r.backup(r.parser().parse_args(['backup','--local-dir',str(uploads),'--recipient',recipient,'--work-dir',str(root)]))
    archive=next(uploads.glob('*.age'))
    candidate_control=load(candidate)
    args=candidate_control.r.parser().parse_args(['restore','--fenced','--archive',str(archive),'--identity',str(identity),'--target',str(root/'wrong'),'--project','sf-e2e-wrong','--work-dir',str(root)])
    with pytest.raises(candidate_control.r.RecoveryError,match='different kit code'):
        candidate_control.r.restore(args)
    args=r.parser().parse_args(['restore','--fenced','--archive',str(archive),'--identity',str(identity),'--target',str(root/'restored'),'--project','sf-e2e-restored','--work-dir',str(root)])
    compose('stop','--timeout','90')
    r.restore(args)  # The backup's own commit, not the candidate's code.
    restored=r.Compose(root/'restored','sf-e2e-restored')
    assert sentinel(restored)=='previous'
    assert 'KIT_MESSAGING_OFF=1' in (root/'restored/.secrets/compose.env').read_text()
    assert r.image_pins(root/'restored')==r.image_pins(old)

    restored_control = load(root / 'restored')
    assert restored_control.r.kit_commit() == previous
    assert restored_control.r.kit_modified() is False
    # Upgrade the restored installation and fail candidate health. Roll back
    # through a fresh checkout, which has no secrets until upgrade copies them.
    rollback_kit = root / 'rollback-kit'
    run('git', 'clone', '--no-hardlinks', old, rollback_kit)
    assert not (rollback_kit / '.secrets').exists()
    commands = restored_control.r.run
    compose_type = restored_control.r.Compose
    def execute(args, **kwargs):
        if str(args[0]) == 'bash' and str(args[1]) == str(candidate / 'health.sh'):
            restored('exec', '-T', 'postgres', 'psql', '-U', 'paperclip', '-d', 'paperclip',
                     '-c', "UPDATE wp3_sentinel SET value='restored-candidate';")
            raise restored_control.r.RecoveryError('Injected restored candidate health failure')
        if 'recover-locked' in args:
            previous_control = load(rollback_kit)
            with patch.object(previous_control, 'STATE', root / 'restored-state'):
                previous_control.recover_point(Path(args[args.index('--point') + 1]),
                                               compose_type(rollback_kit, 'sf-e2e-restored'))
            return ''
        return commands(args, **kwargs)
    with patch.object(restored_control, 'STATE', root / 'restored-state'), \
         patch.object(restored_control, 'ROLLBACK_ROOT', root / 'restored-points'), \
         patch.object(restored_control, 'INSTALL', root / 'installation'), \
         patch.object(restored_control, 'checkout', side_effect=lambda sha: rollback_kit if sha == previous else candidate), \
         patch.object(restored_control.r, 'Compose', side_effect=lambda kit, project: compose_type(kit, 'sf-e2e-restored')), \
         patch.object(restored_control.r, 'run', side_effect=execute), \
         restored_control.r.locked() as lock_fd, \
         pytest.raises(restored_control.r.RecoveryError, match='previous kit and data restored'):
        restored_control.upgrade(candidate_sha, 'restored-upgrade', lock_fd)
    assert sentinel(compose_type(rollback_kit, 'sf-e2e-restored')) == 'previous'
    assert (rollback_kit / '.secrets/compose.env').read_bytes() == (root / 'restored/.secrets/compose.env').read_bytes()


def test_paperclip_tini_forwards_sigterm(installation):
    root,old,candidate,previous,candidate_sha,c,compose=installation
    container = compose.container('paperclip')
    compose('stop', '--timeout', '90', 'paperclip')
    state = json.loads(run('docker', 'inspect', container))[0]['State']
    assert state['ExitCode'] in (0, 143)  # No EPERM or forced SIGKILL.
    assert 'Operation not permitted' not in compose('logs', '--no-color', 'paperclip')
