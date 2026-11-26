#!/usr/bin/env python3
"""Root host commands. Journals contain protocol version 1 and no credentials."""
import argparse
import getpass
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
import urllib.request
from contextlib import suppress

sys.dont_write_bytecode = True
KIT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('recovery', KIT / 'backup-restore.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
INSTALL = Path('/opt/software-factory')
HOST = Path('/etc/paperclip-hermes')
STATE = r.STATE_DIR
ROLLBACK_ROOT = Path('/data/rollback')
UNITS = Path('/etc/systemd/system')
DOCKER_DAEMON = Path('/etc/docker/daemon.json')
RESOLVED_PHASES = ('complete', 'rolled-back', 'prepare-failed')
NO_REPLAY_PHASES = (*RESOLVED_PHASES, 'restored-services-started', 'preparation-restarting')
RECOVERY_PHASES = ('installing', 'healthy-awaiting-handoff', 'rollback-failed', 'creating-rollback-point', 'restored-services-started', 'preparation-restarting')


def operation_fields(record):
    return {key: record[key] for key in
            ('previous_sha', 'candidate_sha', 'rollback_point', 'run_id', 'previous_running', 'operation_id') if key in record}


def settings(kit=None):
    path = (kit or KIT) / '.secrets/compose.env'
    return dict(line.split('=', 1) for line in path.read_text().splitlines()
                if line and not line.startswith('#') and '=' in line) if path.exists() else {}


def atomic_secret(path, data, mode=0o600):
    r.durable_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        r.sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def journal(phase, **fields):
    record = {'protocol': 1, 'operation': 'upgrade', 'phase': phase, 'updated_at': r.utc(), **fields}
    r.atomic_json(STATE / 'last-op.json', record)
    return record


def volume_map(compose, recorded=None):
    # Resolve names from this model at capture, and exact physical identities
    # from the manifest at restore. Labels alone are ambiguous after upgrades.
    if recorded is None:
        model = json.loads(compose('config', '--format', 'json'))['volumes']
        available = r.run(['docker', 'volume', 'ls', '--quiet', '--filter',
                           'label=com.docker.compose.project=' + compose.name]).split()
        names = {logical: item['name'] for logical, item in model.items() if item['name'] in available}
    else:
        names = {logical: item['name'] for logical, item in recorded.items()}
    result = {}
    for logical, name in names.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]+', name):
            r.fail('Invalid physical volume name')
        info = json.loads(r.run(['docker', 'volume', 'inspect', name]))[0]
        labels = info.get('Labels') or {}
        if (not re.fullmatch(r'[a-z_]+', logical)
                or labels.get('com.docker.compose.project') != compose.name
                or labels.get('com.docker.compose.volume') != logical
                or info['Name'] != name or not Path(info['Mountpoint']).is_absolute()
                or not info.get('CreatedAt')):
            r.fail('Rollback volume identity mismatch')
        mount = Path(info['Mountpoint'])
        rinfo = mount.stat()
        if mount.is_symlink() or not mount.is_dir():
            r.fail('Invalid physical volume directory')
        result[logical] = {'name': name, 'mountpoint': str(mount), 'created_at': info['CreatedAt'],
                           'device': rinfo.st_dev, 'inode': rinfo.st_ino}
    if not result or (recorded is not None and result != recorded):
        r.fail('Rollback volume inventory changed')
    return result


def required_space(compose):
    volumes = volume_map(compose)
    size = sum(p.stat().st_size for item in volumes.values() for p in Path(item['mountpoint']).rglob('*') if p.is_file())
    dump = 0
    if 'postgres' in compose.services():
        dump = int(compose('exec', '-T', 'postgres', 'psql', '-XAt', '-U', 'paperclip', '-d',
                           'paperclip', '-c', "SELECT pg_database_size('paperclip')").strip())
    return math.ceil((size + dump) * 2.2)


def fence_project(compose):
    # Compose's old model omits candidate-only services. Remove every project
    # container before touching shared data, without deleting any volumes.
    containers = r.run(['docker', 'container', 'ls', '--all', '--quiet', '--filter',
                        'label=com.docker.compose.project=' + compose.name]).split()
    if containers:
        r.run(['docker', 'container', 'stop', '--time', '90', *containers])
        r.run(['docker', 'container', 'rm', *containers])


def rollback_point(action, path, compose, operation=None):
    path = Path(path)
    if action == 'create':
        r.durable_private_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=False)
        r.private_directory(path)
        r.sync_directory(path.parent)
        running = (operation['previous_running'] if operation is not None else
                   compose('ps', '--services', '--status', 'running').split())
        installed = compose.services()
        writers = [s for s in r.SERVICES if s in installed]
        record = {'protocol': 1, 'kit_commit': r.kit_commit(), 'image_pins': r.image_pins(compose.kit),
                  'running': running, 'project': compose.name, 'images': compose.images(), 'volumes': {}, 'files': {},
                  'operation': operation_fields(operation) if operation is not None else None}
        manifest_attempted = False
        try:
            compose('stop', '--timeout', '90', *writers)
            if 'postgres' in installed:
                with (path / 'database.dump').open('xb') as dump:
                    compose('exec', '-T', 'postgres', 'pg_dump', '-U', 'paperclip', '-d', 'paperclip',
                            '--format=custom', '--no-owner', '--no-acl', output=dump)
                # This must never happen until pg_dump has completed successfully.
                compose('stop', '--timeout', '90', 'postgres')
            for name, identity in volume_map(compose).items():
                source = Path(identity['mountpoint'])
                destination = path / (name + '.tar')
                r.run(['tar', '--numeric-owner', '--acls', '--xattrs', '-cf', destination, '-C', source, '.'])
                record['volumes'][name] = identity
                record['files'][destination.name] = r.digest(destination)
            if 'postgres' in installed:
                record['files']['database.dump'] = r.digest(path / 'database.dump')
            for name in record['files']:
                with (path / name).open('rb') as stream:
                    os.fsync(stream.fileno())
            manifest_attempted = True
            r.atomic_json(path / 'rollback.json', record)
        except BaseException:
            r.hold_signals()
            # If publication was attempted, invalidate it durably before any
            # writer restarts. An uncertain fsync must leave writers fenced.
            if manifest_attempted:
                try:
                    (path / 'rollback.json').unlink(missing_ok=True)
                    r.sync_directory(path)
                except BaseException:
                    print('ops-alert: rollback point invalidation failed; writers remain stopped', file=sys.stderr)
                    raise
            try:
                shutil.rmtree(path)
                r.sync_directory(path.parent)
            except BaseException:
                print('ops-alert: rollback point cleanup failed', file=sys.stderr)
            if operation is not None:
                try:
                    journal('preparation-restarting', **operation_fields(operation))
                except BaseException:
                    # No manifest exists, or its invalidation is already durable.
                    # A state write failure cannot prevent this safe restart.
                    print('ops-alert: preparation restart journal failed', file=sys.stderr)
            try:
                if running:
                    r.restart_services(compose, running)
            except BaseException:
                print('ops-alert: previous stack restart failed after rollback-point failure', file=sys.stderr)
                raise
            if operation is not None:
                try:
                    journal('prepare-failed', **operation_fields(operation))
                except BaseException:
                    print('ops-alert: preparation completion journal failed', file=sys.stderr)
            raise
        return record
    r.private_directory(path)
    record = read_point(path, operation, compose.name)
    if operation is None and record.get('operation') is not None:
        r.fail('Operation-bound points require sfctl recover')
    if record['kit_commit'] != r.kit_commit() or record['image_pins'] != r.image_pins(compose.kit):
        r.fail('Check out the previous kit before restoring its rollback point')
    if any(r.digest(path / name) != value for name, value in record['files'].items()):
        r.fail('Rollback checksum mismatch')
    current = volume_map(compose, record['volumes'])
    if any(current.get(name) != item for name, item in record['volumes'].items()):
        r.fail('Rollback volume inventory changed')
    fence_project(compose)
    # Candidate-only volumes are outside the recorded recovery inventory.
    for name in record['volumes']:
        destination = Path(current[name]['mountpoint'])
        for entry in destination.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        # Archives are local, private, hashed files generated by this kit.
        r.run(['tar', '--extract', '--file', path / (name + '.tar'), '--directory', destination,
               '--numeric-owner', '--same-owner', '--acls', '--xattrs'])
    for identity in current.values():
        r.run(['sync', '-f', identity['mountpoint']])
    if 'database.dump' in record['files']:
        compose('up', '-d', '--no-deps', '--wait', '--wait-timeout', '180', 'postgres')
        # Paperclip's schema exceeds PostgreSQL's default lock budget when
        # cleaned and recreated in one transaction. Writers stay stopped;
        # any restore error leaves the stack stopped for operator recovery.
        with (path / 'database.dump').open('rb') as dump:
            compose('exec', '-T', 'postgres', 'pg_restore', '--clean', '--if-exists',
                    '--exit-on-error', '--no-owner', '--no-acl',
                    '-U', 'paperclip', '-d', 'paperclip', input_file=dump)
    if 'database.dump' in record['files']:
        compose('exec', '-T', 'postgres', 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'paperclip',
                '-d', 'paperclip', '-c', 'CHECKPOINT')
    if 'database.dump' in record['files'] and 'postgres' not in record['running']:
        compose('stop', '--timeout', '90', 'postgres')
    if operation is not None:
        # This is durable before application writers can accept new data. Even
        # if the child/parent dies or health fails, never replay these archives.
        journal('restored-services-started', **operation_fields(operation))
    if record['running']:
        compose('up', '-d', '--build', '--no-deps', '--wait', '--wait-timeout', '300', *record['running'])
    return record


def read_point(point, operation=None, project=None):
    record = json.loads(r.read_private(Path(point) / 'rollback.json'))
    if (record.get('protocol') != 1 or not isinstance(record.get('files'), dict)
            or not isinstance(record.get('volumes'), dict) or not record['volumes']
            or not isinstance(record.get('running'), list)
            or len(set(record['running'])) != len(record['running'])
            or not set(record['running']) <= {'postgres', *r.SERVICES}
            or (project is not None and record.get('project') != project)):
        r.fail('Invalid rollback manifest')
    expected = {name + '.tar' for name in record['volumes']}
    if 'database.dump' in record['files']:
        expected.add('database.dump')
    if (set(record['files']) != expected
            or any(not re.fullmatch(r'[a-z_]+', name) for name in record['volumes'])
            or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
                   for value in record['files'].values())):
        r.fail('Invalid rollback file inventory')
    if operation is not None:
        # Path relocation is allowed; the operation identity must remain exact.
        binding = record.get('operation') or {}
        keys = ('previous_sha', 'candidate_sha', 'run_id', 'operation_id', 'previous_running')
        if (any(key not in operation or binding.get(key) != operation[key] for key in keys)
                or record['running'] != operation['previous_running']
                or record.get('kit_commit') != operation['previous_sha']):
            r.fail('Rollback point does not belong to this operation')
    return record


def validate_point(point, operation, project=None):
    if operation['phase'] in ('restored-services-started', 'preparation-restarting'):
        # Only inventory is needed past the barrier; archives may be unavailable.
        if Path(point).resolve() != Path(operation['rollback_point']).resolve():
            r.fail('Cannot change rollback point after the no-replay barrier')
        return
    if operation['phase'] == 'creating-rollback-point' and not (Path(point) / 'rollback.json').exists():
        if Path(point).absolute() != Path(operation['rollback_point']).absolute():
            r.fail('Cannot relocate an incomplete rollback point')
        return
    read_point(point, operation, project)


def read_operation():
    operation = json.loads(r.read_private(STATE / 'last-op.json'))
    if operation.get('operation') != 'upgrade' or operation.get('phase') not in RECOVERY_PHASES:
        r.fail('Recovery requires an unresolved upgrade')
    if (operation.get('protocol') != 1
            or any(not isinstance(operation.get(key), str) or not re.fullmatch(pattern, operation[key])
                   for key, pattern in (('previous_sha', r'[a-f0-9]{40}'), ('candidate_sha', r'[a-f0-9]{40}'),
                                        ('run_id', r'[A-Za-z0-9_-]{1,255}'), ('operation_id', r'[a-f0-9]{64}')))
            or not isinstance(operation.get('rollback_point'), str)
            or not Path(operation['rollback_point']).is_absolute()
            or not isinstance(operation.get('previous_running'), list)
            or any(not isinstance(service, str) or service not in ('postgres', *r.SERVICES)
                   for service in operation['previous_running'])
            or len(set(operation['previous_running'])) != len(operation['previous_running'])):
        r.fail('Invalid upgrade journal identity or inventory')
    return operation


def recover_point(point, compose):
    operation = read_operation()
    validate_point(point, operation, compose.name)
    if operation['phase'] in ('restored-services-started', 'preparation-restarting'):
        journal(operation['phase'], **operation_fields(operation))
        if operation['previous_running']:
            r.restart_services(compose, operation['previous_running'])
        if operation['phase'] == 'preparation-restarting':
            journal('prepare-failed', **operation_fields(operation))
            return None
        return operation
    operation['rollback_point'] = str(point)
    if operation['phase'] == 'creating-rollback-point' and not (Path(point) / 'rollback.json').exists():
        if 'previous_running' not in operation:
            r.fail('Interrupted preparation has no previous running-service inventory')
        journal('preparation-restarting', **operation_fields(operation))
        if operation['previous_running']:
            r.restart_services(compose, operation['previous_running'])
        journal('prepare-failed', **operation_fields(operation))
        return
    return rollback_point('restore', point, compose, operation=operation)


def require_resolved_upgrade_for_startup():
    r.require_no_service_operation()
    path = STATE / 'last-op.json'
    if path.exists():
        record = json.loads(r.read_private(path))
        if record.get('operation') == 'upgrade' and record.get('phase') not in RESOLVED_PHASES:
            r.fail('Recover the interrupted upgrade before changing service startup')


def recover_service_operation():
    path = STATE / 'service-op.json'
    if not path.exists():
        return False
    record = json.loads(r.read_private(path))
    if record.get('phase') == 'complete':
        return False
    if (record.get('protocol') != 1 or record.get('operation') not in ('backup', 'restore')
            or record.get('phase') != 'running'
            or not isinstance(record.get('run_id'), str) or not re.fullmatch(r'[a-f0-9]{32}', record['run_id'])
            or record.get('kit_commit') != r.kit_commit()
            or not isinstance(record.get('project'), str)
            or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', record['project'])):
        r.fail('Invalid interrupted service operation identity')
    stage_value = record.get('stage')
    if stage_value is not None and (not isinstance(stage_value, str)
            or not Path(stage_value).is_absolute()
            or not Path(stage_value).name.startswith('paperclip-hermes-recovery-')):
        r.fail('Invalid interrupted staging identity')
    if record['operation'] == 'backup':
        if (record.get('kit_path') != str(KIT.resolve())
                or not isinstance(record.get('previous_running'), list)
                or any(service not in ('postgres', *r.SERVICES) for service in record['previous_running'])
                or set(record.get('containers', {})) != set(record['previous_running'])):
            r.fail('Interrupted backup inventory mismatch')
        compose = r.Compose(KIT, record['project'])
        for service, container in record['containers'].items():
            if compose.container(service) != container:
                r.fail('Interrupted backup container generation changed')
        # A killed Compose client may leave its backup CLI writing the home.
        # Remove only this generation's one-offs before application startup.
        ids = r.run(['docker', 'container', 'ls', '--all', '--quiet', '--filter',
                     'label=com.docker.compose.project=' + record['project'], '--filter',
                     'label=com.docker.compose.oneoff=True', '--filter',
                     'label=org.software-factory.recovery-run=' + record['run_id']]).split()
        if ids:
            r.run(['docker', 'rm', '--force', '--volumes', *ids])
        try:
            if record.get('stage') is not None:
                stage = Path(record['stage'])
                if stage.exists():
                    r.private_directory(stage)
                    shutil.rmtree(stage)
        except BaseException:
            print('ops-alert: interrupted backup plaintext cleanup failed', file=sys.stderr)
            # Cleanup cannot skip the required restart once one-offs are gone.
            r.restart_services(compose, record['previous_running'])
            raise
        r.restart_services(compose, record['previous_running'])
    else:
        # An interrupted off-site restore is abandoned, never replayed. Only
        # containers carrying this restore's generation label can be removed.
        ids = r.run(['docker', 'container', 'ls', '--all', '--quiet', '--filter',
                     'label=com.docker.compose.project=' + record['project'], '--filter',
                     'label=org.software-factory.recovery-run=' + record['run_id']]).split()
        if ids:
            r.run(['docker', 'container', 'stop', '--time', '90', *ids])
            r.run(['docker', 'container', 'rm', '--volumes', *ids])
        if stage_value is not None and Path(stage_value).exists():
            r.private_directory(Path(stage_value))
            shutil.rmtree(stage_value)
    r.atomic_json(path, {**record, 'phase': 'complete'})
    if record['operation'] == 'restore':
        print('Interrupted restore fenced; inspect retained volumes and use a fresh project to restore again.')
    return True


def fetch_checkout(temporary, sha):
    r.run(['git', 'init', temporary])
    r.run(['git', '-C', temporary, 'fetch', '--depth=1',
           'https://github.com/agenticcodingops/software-factory.git', sha])
    r.run(['git', '-C', temporary, 'checkout', '--detach', 'FETCH_HEAD'])
    if r.run(['git', '-C', temporary, 'rev-parse', 'HEAD']).strip() != sha:
        r.fail('Fetched kit SHA mismatch')


def checkout(sha):
    if not re.fullmatch(r'[a-f0-9]{40}', sha):
        r.fail('Require a full kit commit SHA')
    INSTALL.mkdir(mode=0o755, parents=True, exist_ok=True)
    if INSTALL.stat().st_uid != 0 or INSTALL.stat().st_mode & 0o022:
        r.fail('Installation directory must be root-owned and not writable by others')
    destination = INSTALL / ('kit-' + sha)
    if not destination.exists():
        temporary = Path(tempfile.mkdtemp(prefix='.kit-', dir=INSTALL))
        try:
            # Code is bind-mounted into containers with unprivileged users.
            # Keep the host's private umask for secrets and operation state.
            private_umask = os.umask(0o022)
            try:
                fetch_checkout(temporary, sha)
            finally:
                os.umask(private_umask)
            os.chmod(temporary, 0o755)
            os.rename(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    kit = destination / 'hosting/paperclip-hermes/kit'
    if r.run(['git', '-C', destination, 'rev-parse', 'HEAD']).strip() != sha:
        r.fail('Existing checkout SHA mismatch')
    if r.run(['git', '-C', destination, 'status', '--porcelain', '--untracked-files=no']).strip():
        r.fail('Existing checkout has modified tracked files')
    return kit


def copy_secrets(source, destination):
    if source.resolve() != destination.resolve():
        r.private_directory(source / '.secrets')
        for path in (source / '.secrets').iterdir():
            if path.is_symlink() or not path.is_file():
                r.fail('Secrets transfer requires regular files')
            atomic_secret(destination / '.secrets' / path.name, path.read_bytes(),
                          mode=0o644 if path.name.endswith('.pub') else 0o600)
        values = settings(destination)
        values['KIT_IMAGE_TAG'] = r.kit_image_tag(destination)
        atomic_secret(destination / '.secrets/compose.env', ''.join(f'{k}={v}\n' for k, v in values.items()).encode())


def activate(kit):
    temporary = INSTALL / '.current'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(kit)
    os.replace(temporary, INSTALL / 'current')
    r.sync_directory(INSTALL)


def require_recovery_support(kit):
    try:
        help_text = r.run(['python3', kit / 'kit-control.py', 'recover-locked', '--help'], cwd=kit)
        if '--lock-fd' not in help_text or '--point' not in help_text:
            r.fail('Locked recovery command is unavailable')
    except (r.RecoveryError, OSError):
        r.fail('Previous kit cannot run recover-locked; pre-WP3 upgrades are unsupported. '
               'Follow the pre-WP3 migration in the kit README before upgrading')


def upgrade(sha, run_id, lock_fd):
    r.require_no_service_operation()
    if not re.fullmatch(r'[a-f0-9]{40}', sha):
        r.fail('Require a full candidate kit commit SHA')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', run_id):
        r.fail('Invalid operation ID')
    point = ROLLBACK_ROOT / run_id
    # Retained snapshots belong to earlier operations, even after rollback.
    # Check before replacing the journal; include dangling symlinks.
    if os.path.lexists(point):
        r.fail('Rollback path already exists; choose a new operation ID')
    if (STATE / 'last-op.json').exists():
        prior = json.loads(r.read_private(STATE / 'last-op.json'))
        if prior.get('operation') == 'upgrade' and prior.get('phase') not in RESOLVED_PHASES:
            r.fail('Resolve the previous upgrade/handoff before another upgrade')
    compose = r.Compose(KIT, 'paperclip-hermes')
    r.durable_private_directory(point.parent)
    if shutil.disk_usage(point.parent).free < required_space(compose):
        r.fail('Insufficient free space: require twice volumes plus dump estimate, plus 10 percent')
    previous = r.kit_commit()
    if previous == 'unknown':
        r.fail('Previous kit commit is unknown')
    if r.kit_modified() is not False:
        r.fail('Previous kit must match its recorded commit before upgrade')
    previous_kit = checkout(previous)
    require_recovery_support(previous_kit)
    compose.preserve_build_images()
    fields = {'previous_sha': previous, 'candidate_sha': sha, 'rollback_point': str(point), 'run_id': run_id,
              'previous_running': compose('ps', '--services', '--status', 'running').split(),
              'operation_id': secrets.token_hex(32)}
    journal('creating-rollback-point', **fields)
    rollback_point('create', point, compose, operation=fields)
    journal('installing', **fields)
    try:
        candidate = checkout(sha)
        copy_secrets(KIT, candidate)
        # The project remains stable across checkout directory names.
        candidate_compose = r.Compose(candidate, compose.name)
        candidate_compose('pull', '--ignore-buildable')
        candidate_compose('up', '-d', '--build', '--wait', '--wait-timeout', '300')
        r.run(['bash', candidate / 'health.sh'], cwd=candidate)
        activate(candidate)
        journal('healthy-awaiting-handoff', **fields)
    except BaseException:
        try:
            # Already hold op.lock: the child inherits only this explicit fd.
            previous_kit = checkout(previous)
            copy_secrets(KIT, previous_kit)
            activate(previous_kit)
            r.run(['python3', previous_kit / 'kit-control.py', 'recover-locked', '--point', point, '--lock-fd', str(lock_fd)], cwd=previous_kit, pass_fds=(lock_fd,))
            journal('rolled-back', **fields)
        except BaseException:
            # A child can fail after restarting writers (including its health
            # check). Preserve its durable barrier against replaying old data.
            current = json.loads(r.read_private(STATE / 'last-op.json'))
            if current.get('phase') not in NO_REPLAY_PHASES:
                with suppress(Exception):
                    fence_project(compose)
                journal('rollback-failed', **fields)
            print('ops-alert: rollback failed; use the off-site blob in last-backup.json for approved ph-restore', file=sys.stderr)
            raise
        r.fail('Candidate failed health; previous kit and data restored')


def probe(command):
    try:
        return r.run(command).strip()
    except (r.RecoveryError, ValueError):
        return None


def health(expected=None):
    checks = {}
    installed = []
    try:
        compose = r.Compose(KIT, 'paperclip-hermes')
        rows = compose('ps', '--format', 'json').strip()
        data = json.loads(rows) if rows.startswith('[') else [json.loads(line) for line in rows.splitlines()]
        installed = compose.services() if expected is None else expected
        observed = [row.get('Service') for row in data]
        checks['compose'] = (len(observed) == len(installed)
                             and len(observed) == len(set(observed))
                             and set(observed) == set(installed)
                             and all(row.get('Health') == 'healthy' for row in data)
                             and (bool(installed) or expected == []))
        if expected == []:
            # A stopped recovery inventory has no application routes to probe.
            result = {'healthy': checks['compose'], 'checks': checks}
            print(json.dumps(result, separators=(',', ':')))
            return checks['compose']
        if 'paperclip' in installed:
            health_url = ('fetch("http://127.0.0.1:3100/api/health", {headers: {host: process.env.PAPERCLIP_ALLOWED_HOSTNAMES}})'
                          if expected is not None and 'caddy' not in installed else 'fetch("https://paperclip.caddy/api/health")')
            checks['paperclip'] = compose('exec', '-T', 'paperclip', 'node', '-e',
                health_url + '.then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))') == ''
            checks['migration_journal'] = bool(r.schema_state(compose).get('applied'))
        if 'hermes' in installed and (expected is None or 'caddy' in installed):
            status = compose('exec', '-T', 'caddy', 'curl', '--silent', '--show-error',
                             '--resolve', 'caddy:443:127.0.0.1',
                             '--cacert', '/public-ca/root.crt', '--output', '/dev/null',
                             '--write-out', '%{http_code}', 'https://caddy/health/detailed')
            checks['hermes_tls_401'] = status.strip() == '401'
    except (r.RecoveryError, ValueError, KeyError, TypeError, AttributeError):
        checks['application'] = False
    counters = probe(['iptables', '-w', '-L', 'DOCKER-USER', '-v', '-n', '-x'])
    drops = [line for line in (counters or '').splitlines() if ' DROP ' in line]
    checks['metadata_rules'] = (any('169.254.169.254' in line for line in drops)
        and all(any('168.63.129.16' in line and 'dpt:' + port in line for line in drops)
                for port in ('80', '32526')))
    checks['metadata_drop_counters'] = [line.split()[:2] for line in (counters or '').splitlines() if 'DROP' in line][:3]
    daemon = DOCKER_DAEMON
    try:
        checks['docker_ipv6_off'] = json.loads(daemon.read_text()).get('ipv6') is False
    except (OSError, ValueError, AttributeError):
        checks['docker_ipv6_off'] = False
    serve = probe(['tailscale', 'serve', 'status', '--json'])
    try:
        serve_config = json.loads(serve or '{}')
        web = serve_config.get('Web', {})
        hostname = settings().get('TAILSCALE_HOSTNAME')
        routes = {'paperclip': ('443', 'http://127.0.0.1:3100'),
                  'hermes': ('9443', 'http://127.0.0.1:9119')}
        checks['tailscale_serve'] = bool(hostname) and bool(installed) and all(
            serve_config.get('TCP', {}).get(port, {}).get('HTTPS') is True
            and web.get(hostname + ':' + port, {}).get('Handlers', {}).get('/', {}).get('Proxy') == target
            for service, (port, target) in routes.items() if service in installed)
    except (ValueError, TypeError, AttributeError):
        checks['tailscale_serve'] = False
    ok = all(value for key, value in checks.items() if key != 'metadata_drop_counters')
    result = {'healthy': ok, 'checks': checks}
    encoded = json.dumps(result, separators=(',', ':'))
    if len(encoded.encode()) >= 4096:
        r.fail('Health output exceeded 4 KB')
    print(encoded)
    return ok


def secret_set(name, stdin=False):
    if name not in (*r.SECRET_NAMES, 'github-token'):
        r.fail('Unknown secret name')
    if not stdin and not sys.stdin.isatty():
        r.fail('Non-TTY stdin requires --stdin')
    if name in r.SECRET_NAMES and name != 'openai-api-key':
        # Rollback data remains tied to the installed application key material.
        require_resolved_upgrade_for_startup()
    value = sys.stdin.buffer.read().rstrip(b'\n') if stdin else getpass.getpass('Secret: ').encode()
    if not value or b'\n' in value or b'\x00' in value:
        r.fail('Secret must be a nonempty single line')
    if name in r.SECRET_NAMES and name != 'openai-api-key' and not re.fullmatch(rb'[a-fA-F0-9]{64}', value):
        r.fail('Application secrets require a 32-byte hexadecimal value')
    if name == 'github-token':
        request = urllib.request.Request('https://api.github.com/', headers={
            'Authorization': 'Bearer ' + value.decode(), 'User-Agent': 'software-factory'})
        with urllib.request.urlopen(request, timeout=30) as response:
            expiry = response.headers.get('github-authentication-token-expiration')
        if not expiry:
            r.fail('GitHub token has no real expiration header; use a fine-grained expiring token')
        try:
            expires = r.datetime.strptime(expiry, '%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=r.timezone.utc)
            if expires <= r.datetime.now(r.timezone.utc):
                r.fail('GitHub token is expired')
        except ValueError:
            r.fail('Invalid GitHub token expiration header')
        r.atomic_json(STATE / 'github-token.json', {
            'expires_at': expiry, 'token_sha256': r.hashlib.sha256(value).hexdigest()})
    atomic_secret(KIT / '.secrets' / name, value + b'\n')


def update_settings(**changes):
    values = settings()
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    atomic_secret(KIT / '.secrets/compose.env', ''.join(f'{k}={v}\n' for k, v in values.items()).encode())


def decrypt_input():
    # Only ciphertext arrives on this path. No inherited environment or argv secret.
    r.private_file(HOST / 'delivery.key')
    with tempfile.TemporaryFile() as incoming:
        shutil.copyfileobj(sys.stdin.buffer, incoming)
        incoming.seek(0)
        return r.run(['age', '--decrypt', '--identity', HOST / 'delivery.key'], input_file=incoming)


def join_tailnet(receipt):
    evidence = json.loads(r.read_private(receipt))
    if (evidence.get('mode') != 'enforcing' or evidence.get('passed') is not True
            or evidence.get('applied') is not True or evidence.get('reviewed') is not True
            or not re.fullmatch(r'[a-f0-9]{64}', evidence.get('policy_sha256', ''))
            or evidence.get('applied_policy_sha256') != evidence['policy_sha256']):
        r.fail('Join requires reviewed, enforcing hardening with matching applied policy evidence')
    key = decrypt_input().strip()
    with tempfile.TemporaryDirectory(dir='/run') as directory:
        path = Path(directory) / 'join.key'
        atomic_secret(path, key.encode())
        r.run(['tailscale', 'up', '--auth-key=file:' + str(path), '--ssh', '--advertise-tags=tag:pcpilot'])
    services = r.Compose(KIT, 'paperclip-hermes').services()
    if 'paperclip' in services:
        r.run(['tailscale', 'serve', '--bg', '--https=443', 'http://127.0.0.1:3100'])
    if 'hermes' in services:
        r.run(['tailscale', 'serve', '--bg', '--https=9443', 'http://127.0.0.1:9119'])


def prepare_config():
    values = settings()
    values['KIT_IMAGE_TAG'] = r.kit_image_tag(KIT)
    profiles = values.setdefault('COMPOSE_PROFILES', 'paperclip,hermes').split(',')
    if not profiles or not set(profiles) <= {'paperclip', 'hermes'}:
        r.fail('Invalid COMPOSE_PROFILES')
    for name, key in r.SECRET_NAMES.items():
        path = KIT / '.secrets' / name
        legacy = 'HERMES_GATEWAY_KEY' if key == 'API_SERVER_KEY' else key
        if not path.exists():
            value = values.get(legacy) or ('' if name == 'openai-api-key' else secrets.token_hex(32))
            if value:
                atomic_secret(path, value.encode() + b'\n')
        values.pop(legacy, None)
    # Missing optional service credentials still need files for Compose config.
    # They remain empty until sfctl delivers the human secret for that service.
    for name in r.SECRET_NAMES:
        path = KIT / '.secrets' / name
        if not path.exists():
            atomic_secret(path, b'')
    atomic_secret(KIT / '.secrets/compose.env', ''.join(f'{k}={v}\n' for k, v in values.items()).encode())


def configure_host(prepare_only=False):
    config = json.loads((HOST / 'config.json').read_text()) if (HOST / 'config.json').exists() else {}
    zone = config.get('timezone', 'Etc/UTC')
    if not re.fullmatch(r'[A-Za-z0-9_+/-]+', zone) or '..' in zone:
        r.fail('Invalid timezone')
    r.run(['timedatectl', 'set-timezone', zone])
    shutdown = config.get('shutdown_time', '22:00')
    if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', shutdown):
        r.fail('Invalid shutdown_time')
    hours, minutes = map(int, shutdown.split(':'))
    backup_minute = (hours * 60 + minutes - 45) % 1440
    timer = (KIT / 'systemd/paperclip-hermes-backup.timer').read_text()
    timer = re.sub(r'OnCalendar=.*', f'OnCalendar=*-*-* {backup_minute//60:02}:{backup_minute%60:02}:00 {zone}', timer)
    for name, text in [('paperclip-hermes-backup.timer', timer),
                       ('paperclip-hermes-backup.service', (KIT / 'systemd/paperclip-hermes-backup.service').read_text())]:
        destination = UNITS / name
        destination.write_text(text)
        destination.chmod(0o644)
    r.run(['systemctl', 'daemon-reload'])
    if not prepare_only and (HOST / 'backup.env').exists():
        r.backup_settings(HOST / 'backup.env', KIT)
        r.run(['systemctl', 'enable', '--now', 'paperclip-hermes-backup.timer'])
    else:
        r.run(['systemctl', 'disable', '--now', 'paperclip-hermes-backup.timer'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('prepare-config')
    cmd = sub.add_parser('configure-host')
    cmd.add_argument('--prepare-only', action='store_true')
    sub.add_parser('health')
    sub.add_parser('status')
    for name in ('upgrade',):
        cmd = sub.add_parser(name)
        cmd.add_argument('sha')
        cmd.add_argument('--run-id', required=True)
    for name in ('rollback', 'recover', 'recover-locked'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--point', type=Path)
        if name == 'recover-locked':
            cmd.add_argument('--lock-fd', type=int, required=True)
    cmd = sub.add_parser('secret')
    cmd.add_argument('action', choices=['set'])
    cmd.add_argument('name')
    cmd.add_argument('--stdin', action='store_true')
    sub.add_parser('claim-url')
    sub.add_parser('set-backup-credential')
    sub.add_parser('delivery-recipient')
    sub.add_parser('restore-key-ephemeral')
    cmd = sub.add_parser('set-hostname')
    cmd.add_argument('hostname')
    cmd = sub.add_parser('messaging')
    cmd.add_argument('mode', choices=['on', 'off'])
    cmd = sub.add_parser('join-tailnet')
    cmd.add_argument('--policy-receipt', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        r.fail('Run sfctl as root')
    os.umask(0o077)
    for number in r.SIGNALS:
        r.signal.signal(number, r.interrupted)
    if args.command == 'health':
        return 0 if health() else 1
    if args.command == 'status':
        result = {}
        for name in ('last-op', 'last-backup', 'github-token', 'service-op'):
            path = STATE / (name + '.json')
            result[name] = json.loads(r.read_private(path)) if path.exists() else None
        receipt = result['github-token']
        if receipt is not None:
            try:
                installed = r.read_private(KIT / '.secrets/github-token').rstrip(b'\n')
                matches = receipt.get('token_sha256') == r.hashlib.sha256(installed).hexdigest()
            except (OSError, r.RecoveryError):
                matches = False
            result['github-token'] = {'expires_at': receipt.get('expires_at') if matches else None}
        print(json.dumps(result))
        return 0
    # Every mutation shares the operation lock. recover-locked is internal:
    # parent retains the lock while synchronously invoking previous code.
    if args.command == 'recover-locked':
        import fcntl
        info = os.fstat(args.lock_fd)
        expected = (r.LOCK_DIR / 'op.lock').stat()
        if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
            r.fail('Recovery requires the inherited operation lock')
        fcntl.flock(args.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        operation = read_operation()
        restored = recover_point(args.point, r.Compose(KIT, 'paperclip-hermes'))
        if restored is not None:
            if not health(operation['previous_running']):
                r.fail('Restored stack is unhealthy')
            journal('rolled-back', **operation_fields(read_operation()))
        return 0
    with r.locked() as lock_fd:
        if args.command == 'prepare-config':
            prepare_config()
        elif args.command == 'configure-host':
            configure_host(args.prepare_only)
        elif args.command == 'secret':
            secret_set(args.name, args.stdin)
        elif args.command == 'upgrade':
            upgrade(args.sha, args.run_id, lock_fd)
        elif args.command in ('rollback', 'recover'):
            if args.command == 'recover' and recover_service_operation():
                return 0
            record = read_operation()
            point = (args.point or Path(record['rollback_point'])).resolve()
            validate_point(point, record, 'paperclip-hermes')
            record['rollback_point'] = str(point)
            previous = checkout(record['previous_sha'])
            copy_secrets(KIT, previous)
            activate(previous)
            r.run(['python3', previous / 'kit-control.py', 'recover-locked', '--point', record['rollback_point'], '--lock-fd', str(lock_fd)], pass_fds=(lock_fd,))
            current = json.loads(r.read_private(STATE / 'last-op.json'))
            if current.get('phase') != 'prepare-failed':
                journal('rolled-back', **operation_fields(record))
        elif args.command == 'set-backup-credential':
            text = decrypt_input()
            # Validate before replacing an existing usable credential.
            with tempfile.TemporaryDirectory(dir=HOST) as directory:
                path = Path(directory) / 'backup.env'
                atomic_secret(path, text.encode())
                public_key, _ = r.backup_settings(path, KIT)
                # Shape alone does not validate an age recipient's checksum or
                # X25519 point. Encrypt an empty payload before replacement.
                with tempfile.TemporaryFile() as encrypted:
                    r.run(['age', '--encrypt', '--recipient', public_key], output=encrypted)
                atomic_secret(HOST / 'backup.env', text.encode())
        elif args.command == 'delivery-recipient':
            r.private_file(HOST / 'delivery.key')
            print(r.run(['age-keygen', '-y', HOST / 'delivery.key']).strip())
        elif args.command == 'restore-key-ephemeral':
            directory = Path('/run/paperclip-hermes')
            directory.mkdir(mode=0o700, exist_ok=True)
            path = directory / 'restore.key'
            if path.exists():
                r.fail('A restore identity already exists')
            r.run(['age-keygen', '-o', path])
            os.chmod(path, 0o600)
            print(r.run(['age-keygen', '-y', path]).strip())
        elif args.command == 'set-hostname':
            require_resolved_upgrade_for_startup()
            if not re.fullmatch(r'[a-z0-9-]+\.[a-z0-9.-]+\.ts\.net', args.hostname):
                r.fail('Require a MagicDNS FQDN')
            update_settings(TAILSCALE_HOSTNAME=args.hostname)
            r.Compose(KIT, 'paperclip-hermes')('up', '-d', '--wait')
        elif args.command == 'messaging':
            require_resolved_upgrade_for_startup()
            update_settings(KIT_MESSAGING_OFF='1' if args.mode == 'off' else None)
            r.Compose(KIT, 'paperclip-hermes')('up', '-d', '--wait')
        elif args.command == 'claim-url':
            text = r.Compose(KIT, 'paperclip-hermes')('logs', '--no-color', 'paperclip')
            urls = re.findall(r'https?://[^\s]+/[^\s]*claim[^\s]*', text)
            if not urls:
                r.fail('No claim URL available')
            print(urls[-1])
        elif args.command == 'join-tailnet':
            join_tailnet(args.policy_receipt)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(*r.report_failure(error), sep='\n', file=sys.stderr)
        sys.exit(1)
