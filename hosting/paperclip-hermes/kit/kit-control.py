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
RESOLVED_PHASES = ('complete', 'rolled-back', 'prepare-failed', 'restored-services-started')
RECOVERY_PHASES = ('installing', 'healthy-awaiting-handoff', 'rollback-failed', 'creating-rollback-point')


def operation_fields(record):
    return {key: record[key] for key in
            ('previous_sha', 'candidate_sha', 'rollback_point', 'run_id', 'previous_running') if key in record}


def settings(kit=None):
    path = (kit or KIT) / '.secrets/compose.env'
    return dict(line.split('=', 1) for line in path.read_text().splitlines()
                if line and not line.startswith('#') and '=' in line) if path.exists() else {}


def atomic_secret(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    r.private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
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


def volume_map(compose):
    raw = r.run(['docker', 'volume', 'ls', '--quiet', '--filter',
                 'label=com.docker.compose.project=' + compose.name]).split()
    result = {}
    for name in raw:
        info = json.loads(r.run(['docker', 'volume', 'inspect', name]))[0]
        logical = info['Labels']['com.docker.compose.volume']
        if not re.fullmatch(r'[a-z_]+', logical):
            r.fail('Unexpected volume label')
        result[logical] = Path(info['Mountpoint'])
    if not result:
        r.fail('No installed volumes found')
    return result


def required_space(compose):
    volumes = volume_map(compose)
    size = sum(p.stat().st_size for root in volumes.values() for p in root.rglob('*') if p.is_file())
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
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
        r.private_directory(path)
        running = (operation['previous_running'] if operation is not None else
                   compose('ps', '--services', '--status', 'running').split())
        installed = compose.services()
        writers = [s for s in r.SERVICES if s in installed]
        record = {'protocol': 1, 'kit_commit': r.kit_commit(), 'image_pins': r.image_pins(compose.kit),
                  'running': running, 'project': compose.name, 'images': compose.images(), 'volumes': {}, 'files': {}}
        try:
            compose('stop', '--timeout', '90', *writers)
            if 'postgres' in installed:
                with (path / 'database.dump').open('xb') as dump:
                    compose('exec', '-T', 'postgres', 'pg_dump', '-U', 'paperclip', '-d', 'paperclip',
                            '--format=custom', '--no-owner', '--no-acl', output=dump)
                # This must never happen until pg_dump has completed successfully.
                compose('stop', '--timeout', '90', 'postgres')
            for name, source in volume_map(compose).items():
                destination = path / (name + '.tar')
                r.run(['tar', '--numeric-owner', '--acls', '--xattrs', '-cf', destination, '-C', source, '.'])
                record['volumes'][name] = str(source)
                record['files'][destination.name] = r.digest(destination)
            if 'postgres' in installed:
                record['files']['database.dump'] = r.digest(path / 'database.dump')
            for name in record['files']:
                with (path / name).open('rb') as stream:
                    os.fsync(stream.fileno())
            r.atomic_json(path / 'rollback.json', record)
        except BaseException:
            # A failed dump or partial stop must restore availability of the old
            # stack. Remove even a just-published point before writers restart:
            # interrupted cleanup must never leave a replayable, stale point.
            (path / 'rollback.json').unlink(missing_ok=True)
            r.sync_directory(path)
            shutil.rmtree(path)
            r.sync_directory(path.parent)
            try:
                if running:
                    r.restart_services(compose, running)
            except BaseException:
                print('ops-alert: previous stack restart failed after rollback-point failure', file=sys.stderr)
                raise
            if operation is not None:
                journal('prepare-failed', **operation_fields(operation))
            raise
        return record
    r.private_directory(path)
    record = json.loads(r.read_private(path / 'rollback.json'))
    if record['kit_commit'] != r.kit_commit() or record['image_pins'] != r.image_pins(compose.kit):
        r.fail('Check out the previous kit before restoring its rollback point')
    if any(r.digest(path / name) != value for name, value in record['files'].items()):
        r.fail('Rollback checksum mismatch')
    current = volume_map(compose)
    if not set(record['volumes']) <= set(current):
        r.fail('Rollback volume inventory changed')
    fence_project(compose)
    # Candidate-only volumes are outside the recorded recovery inventory.
    for name in record['volumes']:
        destination = current[name]
        for entry in destination.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        # Archives are local, private, hashed files generated by this kit.
        r.run(['tar', '--extract', '--file', path / (name + '.tar'), '--directory', destination,
               '--numeric-owner', '--same-owner', '--acls', '--xattrs'])
    if 'database.dump' in record['files']:
        compose('up', '-d', '--no-deps', '--wait', '--wait-timeout', '180', 'postgres')
        # Paperclip's schema exceeds PostgreSQL's default lock budget when
        # cleaned and recreated in one transaction. Writers stay stopped;
        # any restore error leaves the stack stopped for operator recovery.
        with (path / 'database.dump').open('rb') as dump:
            compose('exec', '-T', 'postgres', 'pg_restore', '--clean', '--if-exists',
                    '--exit-on-error', '--no-owner', '--no-acl',
                    '-U', 'paperclip', '-d', 'paperclip', input_file=dump)
    if operation is not None:
        # This is durable before application writers can accept new data. Even
        # if the child/parent dies or health fails, never replay these archives.
        journal('restored-services-started', **operation_fields(operation))
    if record['running']:
        compose('up', '-d', '--build', '--wait', '--wait-timeout', '300', *record['running'])
    return record


def recover_point(point, compose):
    operation = json.loads(r.read_private(STATE / 'last-op.json'))
    if operation.get('operation') != 'upgrade' or operation.get('phase') not in RECOVERY_PHASES:
        r.fail('Recovery requires an unresolved upgrade')
    operation['rollback_point'] = str(point)
    if operation['phase'] == 'creating-rollback-point' and not (Path(point) / 'rollback.json').exists():
        if 'previous_running' not in operation:
            r.fail('Interrupted preparation has no previous running-service inventory')
        if operation['previous_running']:
            r.restart_services(compose, operation['previous_running'])
        journal('prepare-failed', **operation_fields(operation))
        return
    return rollback_point('restore', point, compose, operation=operation)


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
        shutil.copytree(source / '.secrets', destination / '.secrets', dirs_exist_ok=True)


def activate(kit):
    temporary = INSTALL / '.current'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(kit)
    os.replace(temporary, INSTALL / 'current')
    r.sync_directory(INSTALL)


def upgrade(sha, run_id, lock_fd):
    if not re.fullmatch(r'[a-f0-9]{40}', sha):
        r.fail('Require a full candidate kit commit SHA')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', run_id):
        r.fail('Invalid operation ID')
    if (STATE / 'last-op.json').exists():
        prior = json.loads(r.read_private(STATE / 'last-op.json'))
        if prior.get('operation') == 'upgrade' and prior.get('phase') not in RESOLVED_PHASES:
            r.fail('Resolve the previous upgrade/handoff before another upgrade')
    compose = r.Compose(KIT, 'paperclip-hermes')
    point = ROLLBACK_ROOT / run_id
    point.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if shutil.disk_usage(point.parent).free < required_space(compose):
        r.fail('Insufficient free space: require twice volumes plus dump estimate, plus 10 percent')
    previous = r.kit_commit()
    if previous == 'unknown':
        r.fail('Previous kit commit is unknown')
    fields = {'previous_sha': previous, 'candidate_sha': sha, 'rollback_point': str(point), 'run_id': run_id,
              'previous_running': compose('ps', '--services', '--status', 'running').split()}
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
            if current.get('phase') not in RESOLVED_PHASES:
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


def health():
    checks = {}
    try:
        compose = r.Compose(KIT, 'paperclip-hermes')
        rows = compose('ps', '--format', 'json').strip()
        data = json.loads(rows) if rows.startswith('[') else [json.loads(line) for line in rows.splitlines()]
        checks['compose'] = bool(data) and all(row.get('Health') == 'healthy' for row in data) and len(data) == len(compose.services())
        installed = compose.services()
        if 'paperclip' in installed:
            checks['paperclip'] = compose('exec', '-T', 'paperclip', 'node', '-e',
                'fetch("https://paperclip.caddy/api/health").then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))') == ''
            checks['migration_journal'] = bool(r.schema_state(compose).get('applied'))
        if 'hermes' in installed:
            status = compose('exec', '-T', 'caddy', 'curl', '--silent', '--show-error',
                             '--cacert', '/public-ca/root.crt', '--output', '/dev/null',
                             '--write-out', '%{http_code}', 'https://caddy/health/detailed')
            checks['hermes_tls_401'] = status.strip() == '401'
    except (r.RecoveryError, ValueError, KeyError):
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
    except (OSError, ValueError):
        checks['docker_ipv6_off'] = False
    serve = probe(['tailscale', 'serve', 'status', '--json'])
    try:
        checks['tailscale_serve'] = bool(json.loads(serve or '{}').get('Web'))
    except ValueError:
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
        r.atomic_json(STATE / 'github-token.json', {'expires_at': expiry})
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
        for name in ('last-op', 'last-backup', 'github-token'):
            path = STATE / (name + '.json')
            result[name] = json.loads(r.read_private(path)) if path.exists() else None
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
        restored = recover_point(args.point, r.Compose(KIT, 'paperclip-hermes'))
        if restored is not None and not health():
            r.fail('Restored stack is unhealthy')
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
            record = json.loads(r.read_private(STATE / 'last-op.json'))
            if record.get('operation') != 'upgrade' or record.get('phase') not in RECOVERY_PHASES:
                r.fail('Recovery requires an unresolved upgrade')
            if args.point:
                record['rollback_point'] = str(args.point)
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
                r.backup_settings(path, KIT)
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
            if not re.fullmatch(r'[a-z0-9-]+\.[a-z0-9.-]+\.ts\.net', args.hostname):
                r.fail('Require a MagicDNS FQDN')
            update_settings(TAILSCALE_HOSTNAME=args.hostname)
            r.Compose(KIT, 'paperclip-hermes')('up', '-d', '--wait')
        elif args.command == 'messaging':
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
