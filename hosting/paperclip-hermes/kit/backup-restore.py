#!/usr/bin/env python3
"""Host-only recovery for this kit; Python 3.12 standard library, age and Docker.

No SAS is passed to a child, inherited environment, log or exception message.
Application output is private staging data; failures print only the operation.
"""
import argparse
import base64
from contextlib import contextmanager, suppress
from datetime import datetime, timezone, timedelta
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
import http.client
import urllib.error
import urllib.request
import uuid

# Importing the kit's host-config.py as root must leave no root-owned __pycache__.
sys.dont_write_bytecode = True

KIT = Path(__file__).resolve().parent
LOCK_DIR = Path('/run/paperclip-hermes')
BACKUP_ENV_FILE = Path('/etc/paperclip-hermes/backup.env')
SERVICES = ('paperclip', 'hermes', 'sandbox', 'caddy')
VOLUMES = {
    'paperclip': ('paperclip', '/paperclip'),
    'caddy_data': ('caddy', '/data'),
    'caddy_config': ('caddy', '/config'),
    'public_ca': ('caddy', '/public-ca'),
    'sandbox_hostkeys': ('sandbox', '/etc/ssh/hostkeys'),
}
CONFIG = (
    'compose.yaml', 'Dockerfile.hermes', 'hermes-config.yaml', 'Caddyfile',
    'bootstrap.sh', 'host-config.py', 'block-metadata.sh', 'backup.sh',
    'restore.sh', 'backup-restore.py', 'kit-control.py', 'upgrade.sh', 'health.sh',
    'sfctl', 'join-tailnet.sh', 'container', 'systemd', 'tests',
    '.env.example', '.dockerignore', '.gitignore', '.gitattributes', 'README.md',
    'tailscale-policy.hujson', 'backup.env.example', 'recovery-sources.md',
)
# Restore trusts only data and secrets from an archive. Code and configuration,
# the Hermes startup policy included, come from the kit running the restore;
# archived copies must match it exactly. Documentation is not compared.
DOCS = ('README.md', 'recovery-sources.md', 'tests')
# Host units are edited for the checkout path at install time and restore
# never installs them, so they are not compared either.
HOST_LOCAL = ('systemd',)
CODE = tuple(name for name in CONFIG if name not in DOCS + HOST_LOCAL)
MEMBER_LIMIT = 16 * 1024 * 1024
SECRET_NAMES = {
    'postgres-password': 'POSTGRES_PASSWORD', 'better-auth-secret': 'BETTER_AUTH_SECRET',
    'paperclip-agent-jwt-secret': 'PAPERCLIP_AGENT_JWT_SECRET',
    'paperclip-tool-action-signing-secret': 'PAPERCLIP_TOOL_ACTION_SIGNING_SECRET',
    'paperclip-secrets-master-key': 'PAPERCLIP_SECRETS_MASTER_KEY',
    'hermes-gateway-key': 'API_SERVER_KEY',
    'hermes-dashboard-basic-auth-password': 'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD',
    'hermes-dashboard-basic-auth-secret': 'HERMES_DASHBOARD_BASIC_AUTH_SECRET',
    'openai-api-key': 'OPENAI_API_KEY',
}
SECRET_FILES = ('compose.env', 'github-token', 'sandbox_ed25519', 'sandbox_ed25519.pub', *SECRET_NAMES)
PAYLOADS = {'config.tar', 'database.dump', 'hermes.zip',
            *(name + '.tar' for name in VOLUMES)}
PYTHON = '/opt/hermes/.venv/bin/python'
SINGLE_PUT_LIMIT = 64 * 1024 * 1024
BLOCK_SIZE = 16 * 1024 * 1024
MAX_BLOCKS = 50000
RETRIES = 4
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
# What utc() writes: datetime.isoformat() of an aware UTC time.
UTC_TIME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?\+00:00')
# Restore leaves Hermes messaging off until the operator removes this setting.
MESSAGING_OFF = 'KIT_MESSAGING_OFF=1'
SIGNALS = tuple(getattr(signal, name) for name in ('SIGTERM', 'SIGINT', 'SIGHUP', 'SIGQUIT')
                if hasattr(signal, name))


class RecoveryError(Exception):
    pass


def fail(message):
    raise RecoveryError(message)


def private_stat(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        fail('Require a nonempty regular root-owned file with mode 0600 or 0400')


def read_private(path):
    # One descriptor for the checks and the read, so the file cannot be swapped
    # between them; a link is refused (O_NOFOLLOW) and a FIFO cannot block the
    # open (O_NONBLOCK; it then fails the regular-file check).
    flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    # A plain validation error raised while handling another exception is
    # raised 'from None' throughout: report_failure() names an attached
    # context as an earlier, unexpected failure.
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise RecoveryError('Cannot open a private input file; links are refused') from None
    with os.fdopen(descriptor, 'rb') as stream:
        private_stat(os.fstat(stream.fileno()))
        data = stream.read()
    if not data.strip():
        fail('Private input file is empty')
    return data


def private_file(path):
    read_private(path)


def private_directory(path):
    info = Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        fail('Require a root-owned directory with mode 0700')


def recipient(value):
    # Intentionally only native age recipients; no plugin identity on the host.
    if not re.fullmatch(r'age1[0-9a-z]{58}', value):
        raise argparse.ArgumentTypeError('Supply a native age public recipient')
    return value


def project(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', value):
        raise argparse.ArgumentTypeError('Invalid Compose project name')
    return value


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('backup', help='Encrypt and upload a consistent recovery set')
    backup.add_argument('--env-file', type=Path, default=BACKUP_ENV_FILE,
                        help='Host-only backup settings (never the Compose environment)')
    backup.add_argument('--local-dir', type=Path,
                        help='Existing private local destination for the offline drill')
    backup.add_argument('--recipient', type=recipient,
                        help='Public recipient; required with --local-dir')
    backup.add_argument('--project', type=project, default='paperclip-hermes')
    backup.add_argument('--work-dir', type=Path, default=Path('/var/tmp'),
                        help='Staging parent; use a host encrypted filesystem with sufficient space')
    backup.add_argument('--canary', action='store_true')
    backup.add_argument('--reason', choices=['nightly', 'pre-stop', 'pre-upgrade', 'canary', 'final'], default='nightly')
    backup.add_argument('--target')
    backup.add_argument('--vm-id')
    backup.add_argument('--stop-operation-id')
    point = commands.add_parser('rollback-point')
    point.add_argument('action', choices=['create', 'restore'])
    point.add_argument('--path', type=Path, required=True)
    point.add_argument('--project', type=project, default='paperclip-hermes')
    restore = commands.add_parser('restore', help='Restore into an empty kit directory and new volumes')
    restore.add_argument('--fenced', action='store_true', required=True,
                         help='Confirm the old host/scheduler cannot run, including after reboot')
    restore.add_argument('--archive', type=Path, required=True, help='Downloaded encrypted backup')
    restore.add_argument('--identity', type=Path, required=True,
                         help='Temporary root-only age identity supplied by the operator')
    restore.add_argument('--target', type=Path, required=True, help='New, empty kit directory')
    restore.add_argument('--project', type=project, default='paperclip-hermes')
    restore.add_argument('--hostname', help='Destination MagicDNS FQDN; other secret values are preserved')
    restore.add_argument('--work-dir', type=Path, default=Path('/var/tmp'))
    restore.add_argument('--verify-only', action='store_true')
    restore.add_argument('--staged', action='store_true', help='Require an ephemeral identity in /run; destroy it afterwards')
    return result


def sas_url(value):
    # User delegation only. Shared Key is never a fallback. Duplicate fields
    # are ambiguous and the key expiry bounds both the grant and the SAS.
    try:
        url = urlsplit(value)
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        now = datetime.now(timezone.utc)
        def when(name):
            value = query[name][0]
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,7})?(?:Z|\+00:00)', value):
                fail('Invalid container SAS timestamp')
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        if (url.scheme != 'https' or not url.hostname
                or not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', url.hostname)
                or url.username or url.password
                or url.port not in (None, 443) or url.fragment
                or not re.fullmatch(r'/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', url.path)
                or '--' in url.path
                or any(len(values) != 1 for values in query.values())
                or query.get('sr') != ['c'] or query.get('sp') != ['cw']
                or any(name in query for name in ('si', 'ss', 'srt'))
                or any(not query.get(name, [''])[0] for name in
                       ('sig', 'sv', 'skoid', 'sktid', 'skt', 'ske', 'sks', 'skv', 'se'))
                or query.get('sks') != ['b'] or query.get('spr') != ['https']
                or not now < when('ske') <= now + timedelta(days=7)
                or not now < when('se') <= when('ske')
                or when('se') - (when('st') if 'st' in query else now) > timedelta(days=7)
                or ('st' in query and when('st') > now)
                or when('skt') > now or when('ske') - when('skt') > timedelta(days=7)):
            fail('Invalid container SAS settings; require a write-only user-delegation SAS, at most 7 days')
        for name in ('skoid', 'sktid'):
            if not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}', query[name][0]):
                fail('Invalid container SAS identity')
        # Support the version used by the pinned uploader. Unknown service
        # versions need explicit qualification before replacing a working grant.
        if any(query[name][0] != '2023-11-03' for name in ('sv', 'skv')):
            fail('Unsupported container SAS version; require 2023-11-03')
        if set(query) - {'sv', 'sr', 'sp', 'spr', 'sig', 'skoid', 'sktid', 'skt', 'ske', 'sks', 'skv', 'se', 'st'}:
            fail('Unsupported container SAS fields')
        if len(base64.b64decode(query['sig'][0], validate=True)) != 32:
            fail('Invalid container SAS signature')
        return url
    except (ValueError, TypeError, KeyError, OverflowError):
        raise RecoveryError('Invalid container SAS settings') from None


def backup_settings(path, kit):
    private_directory(Path(path).parent)
    resolved = Path(path).resolve()
    if resolved == kit or kit in resolved.parents:
        fail('The backup settings file must be outside the entire kit directory')
    # Checked and parsed from one descriptor (read_private). Do not source
    # shell syntax, expand variables or export these settings.
    values = {}
    for line in read_private(path).decode().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name, sep, value = line.partition('=')
        if (not sep or name in values or name not in
                ('BACKUP_AGE_RECIPIENT', 'BACKUP_CONTAINER_SAS_URL')
                or not value or re.search(r'[\s\x00-\x1f\x7f]', value)):
            fail('Use two unique literal NAME=value assignments in the backup settings file')
        values[name] = value
    if set(values) != {'BACKUP_AGE_RECIPIENT', 'BACKUP_CONTAINER_SAS_URL'}:
        fail('Backup settings need BACKUP_AGE_RECIPIENT and BACKUP_CONTAINER_SAS_URL')
    try:
        public_key = recipient(values['BACKUP_AGE_RECIPIENT'])
    except argparse.ArgumentTypeError:
        raise RecoveryError('Backup recipient must be a native age public key') from None
    return public_key, sas_url(values['BACKUP_CONTAINER_SAS_URL'])


def blob_url(container, name):
    return urlunsplit(container._replace(path=container.path + '/' + quote(name, safe='')))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def upload(container, archive, name, metadata=None):
    # PUT only, no list/read/delete or redirects. Transient failures are retried
    # with backoff; a retried conditional request that finds the blob present
    # may mean the earlier attempt succeeded unseen, so it fails as uncertain.
    # Larger archives use staged blocks; the final conditional commit is the
    # only publication. An error can leave uncommitted ciphertext blocks only.
    try:
        size = archive.stat().st_size
        if size > BLOCK_SIZE * MAX_BLOCKS:
            fail('Encrypted backup exceeds the supported block count')
        url = blob_url(container, name)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

        def put(destination, data, length, headers):
            conditional = 'If-None-Match' in headers
            for attempt in range(RETRIES + 1):
                if attempt:
                    time.sleep(2 ** attempt)
                    if hasattr(data, 'seek'):
                        data.seek(0)
                request = urllib.request.Request(destination, data=data, method='PUT',
                    headers={'x-ms-version': '2023-11-03', 'Content-Length': str(length), **headers})
                try:
                    with opener.open(request, timeout=300) as response:
                        status = response.status
                        result.update(version=response.headers.get('x-ms-version-id', ''),
                                      etag=response.headers.get('ETag', ''))
                except urllib.error.HTTPError as error:
                    status = error.code
                except (OSError, http.client.HTTPException):
                    # Transient transport failures: URLError, timeouts, resets,
                    # TLS errors (ssl.SSLEOFError), IncompleteRead, BadStatusLine.
                    # Never rendered: the exception may carry the signed URL.
                    status = None
                if status == 201:
                    return
                if conditional and attempt and status in (409, 412):
                    fail('Backup upload outcome uncertain: a retried conditional PUT found the blob '
                         'already present; check it with a separate read credential before relying on it')
                if status is not None and status not in RETRY_STATUS:
                    fail('Backup upload failed (expected HTTP 201)')
            fail('Backup upload failed after retries')

        meta = {'x-ms-meta-' + key: str(value) for key, value in (metadata or {}).items()}
        result = {}
        with archive.open('rb') as stream:
            if size <= SINGLE_PUT_LIMIT:
                put(url, stream, size, {'x-ms-blob-type': 'BlockBlob',
                    'Content-Type': 'application/octet-stream', 'If-None-Match': '*', **meta})
            else:
                blocks = []
                while data := stream.read(BLOCK_SIZE):
                    block_id = base64.b64encode(f'{len(blocks):08d}'.encode()).decode()
                    put(url + '&comp=block&blockid=' + quote(block_id, safe=''), data, len(data),
                        {'Content-Type': 'application/octet-stream'})
                    blocks.append(block_id)
                body = ('<?xml version="1.0" encoding="utf-8"?><BlockList>' +
                        ''.join('<Latest>' + block_id + '</Latest>' for block_id in blocks) +
                        '</BlockList>').encode()
                put(url + '&comp=blocklist', body, len(body),
                    {'Content-Type': 'application/xml', 'x-ms-blob-content-type': 'application/octet-stream',
                     'If-None-Match': '*', **meta})
        return result
    except RecoveryError:
        raise
    except Exception:
        # HTTPError/URLError can contain the signed URL. Never stringify them.
        raise RecoveryError('Backup upload failed; no backup was published successfully') from None


def restart_services(compose, services):
    # Compose 2.38 (the CI runner) has no `start --wait`. Use the loaded
    # project model and health wait without replacing containers or images.
    if not services:
        return
    try:
        compose('up', '-d', '--no-recreate', '--no-build', '--pull', 'never',
                '--no-deps', '--wait', '--wait-timeout', '300', *services)
    except BaseException:
        print('ops-alert: service restart failed', file=sys.stderr)
        raise


def sync_directory(path):
    # Make a new directory entry durable (POSIX; the host job runs on Linux).
    if not hasattr(os, 'O_DIRECTORY'):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_local(archive, destination, name):
    # The final name appears only complete: copy to a temporary name in the
    # same directory, fsync, then link it (link never replaces an existing
    # name). The temporary is removed on any failure or signal, including one
    # right after it is created. Both names carry this job's UUID.
    private_directory(destination)
    output = destination / name
    temporary = destination / ('.' + name + '.partial')
    for path in (output, temporary):
        if os.path.lexists(path):
            raise FileExistsError(errno.EEXIST, 'Local backup name already exists', path.name)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_BINARY', 0))
    descriptor = target = None
    try:
        # No handled signal between the open and the stream taking over the
        # descriptor, so exactly one of them owns it when cleanup runs.
        with deferred_signals():
            descriptor = os.open(temporary, flags, 0o600)
            target = os.fdopen(descriptor, 'wb')
            descriptor = None  # the stream owns and closes it now
        with target, archive.open('rb') as source:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        with deferred_signals():
            os.link(temporary, output)
            temporary.unlink()
        sync_directory(destination)
    except BaseException:
        hold_signals()
        try:
            # Idempotent: a close that fails (already closed, EBADF) must not
            # replace the error that started cleanup or skip the unlink.
            with suppress(OSError):
                if target is not None:
                    target.close()
                elif descriptor is not None:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
        raise


def run(args, *, cwd=KIT, output=None, input_file=None, pass_fds=(), env=None):
    # Capture output privately rather than leaking config, archive filenames or
    # application/provider diagnostics into journald. No secret in args either.
    kwargs = {'cwd': cwd, 'stdout': output if output is not None else subprocess.PIPE,
              'stderr': subprocess.PIPE, 'stdin': input_file or subprocess.DEVNULL, 'pass_fds': pass_fds}
    if env is not None:
        kwargs['env'] = env
    if str(args[0]) == 'git':
        # Host operations always name the intended checkout explicitly. Inherited
        # Git context must not redirect its repository, index or configuration.
        kwargs['env'] = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    try:
        result = subprocess.run([str(arg) for arg in args], check=False, **kwargs)
    except OSError:
        raise RecoveryError('Cannot execute ' + str(args[0])) from None
    if result.returncode:
        fail('Command failed: ' + str(args[0]) + ' (exit ' + str(result.returncode) + ')')
    return result.stdout.decode() if output is None else ''


class Compose:
    def __init__(self, kit, name):
        self.kit = kit
        self.name = name
        self.files = []
        self.image_tag = kit_image_tag(kit)
        env = kit / '.secrets/compose.env'
        profiles = 'paperclip,hermes'
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith('COMPOSE_PROFILES='):
                    profiles = line.split('=', 1)[1]
        if not profiles or not set(profiles.split(',')) <= {'paperclip', 'hermes'}:
            fail('Select paperclip, hermes or both profiles')
        self.profiles = [item for profile in profiles.split(',') for item in ('--profile', profile)]

    def __call__(self, *args, **kwargs):
        return run(['docker', 'compose', '--project-name', self.name,
                    '--env-file', '.secrets/compose.env', *self.profiles, *self.files, *args], cwd=self.kit,
                   env={**os.environ, 'KIT_IMAGE_TAG': self.image_tag}, **kwargs)

    def services(self):
        return self('config', '--services').split()

    def preserve_build_images(self):
        # An older upgrader or manual Compose invocation may have used a
        # fallback tag. Preserve the actual installed build before a candidate
        # can retag anything, including when every service is stopped.
        for service in self.services():
            if service in ('hermes', 'sandbox'):
                image = run(['docker', 'inspect', '--format', '{{.Image}}', self.container(service)]).strip()
                run(['docker', 'image', 'tag', image, 'software-factory-' + service + ':' + self.image_tag])

    def container(self, service):
        # Compose ps can include a run container left by a killed client.
        # Identify the persistent service independently of those one-offs.
        ids = run(['docker', 'container', 'ls', '--all', '--quiet', '--filter',
                   'label=com.docker.compose.project=' + self.name, '--filter',
                   'label=com.docker.compose.service=' + service, '--filter',
                   'label=com.docker.compose.oneoff=False']).split()
        if len(ids) != 1:
            fail('Expected exactly one container for ' + service)
        return ids[0]

    def mount(self, service, destination):
        raw = run(['docker', 'inspect', '--format', '{{json .Mounts}}', self.container(service)])
        matches = [m for m in json.loads(raw) if m['Destination'] == destination and m['Type'] == 'volume']
        if len(matches) != 1:
            fail('Expected a named volume for ' + service)
        return Path(matches[0]['Source'])

    def require_host_only(self, path):
        path = path.resolve()
        for service in self.services():
            raw = run(['docker', 'inspect', '--format', '{{json .Mounts}}', self.container(service)])
            for mount in json.loads(raw):
                if mount.get('Source'):
                    source = Path(mount['Source']).resolve()
                    if source == path or source in path.parents:
                        fail('Backup settings are reachable through a container mount')

    def images(self):
        images = {}
        for service in self.services():
            image_id = run(['docker', 'inspect', '--format', '{{.Image}}', self.container(service)]).strip()
            raw = run(['docker', 'image', 'inspect', '--format', '{{json .RepoDigests}}', image_id])
            images[service] = {'image_id': image_id, 'repo_digests': json.loads(raw) or []}
        return images

    def remove_containers(self, one_off_only=False):
        # A signal kills the 'docker compose run --rm' CLI (subprocess.run
        # kills its child), not the one-off container it started: that keeps
        # running with its volumes mounted, and 'compose rm'/'down' skip
        # one-offs. Remove this project's containers by label (with
        # one_off_only, only the one-offs) and their anonymous volumes.
        filters = ['--filter', 'label=com.docker.compose.project=' + self.name]
        if one_off_only:
            filters += ['--filter', 'label=com.docker.compose.oneoff=True']
        ids = run(['docker', 'container', 'ls', '--all', '--quiet', *filters]).split()
        if ids:
            run(['docker', 'rm', '--force', '--volumes', *ids])

    def hermes(self, stage, *args):
        # Bypass s6/the gateway completely. Only a one-shot CLI owns the stopped
        # home; neither backup nor import can start the application entrypoint.
        labels = (['--label', 'org.software-factory.recovery-run=' + self.recovery_run]
                  if hasattr(self, 'recovery_run') else [])
        return self('run', '--rm', '--no-deps', '-T', *labels, '--user', '10000:10000',
                    '--volume', str(stage) + ':/recovery',
                    '--env', 'HERMES_HOME=/opt/data', '--env', 'HOME=/opt/data',
                    '--entrypoint', PYTHON, 'hermes', '/kit/recovery-hermes.py', *args)


def schema_state(compose):
    # The tagged implementation discovers this journal in either schema.
    schema = compose('exec', '-T', 'postgres', 'psql', '-XAt', '-v', 'ON_ERROR_STOP=1',
        '-U', 'paperclip', '-d', 'paperclip', '-c',
        "SELECT table_schema FROM information_schema.tables WHERE table_name = '__drizzle_migrations' "
        "AND table_schema IN ('drizzle', 'public') ORDER BY table_schema").split()
    if len(schema) != 1:
        fail('Paperclip migration journal missing or ambiguous')
    rows = compose('exec', '-T', 'postgres', 'psql', '-XAt', '-v', 'ON_ERROR_STOP=1',
        '-U', 'paperclip', '-d', 'paperclip', '-c',
        'SELECT coalesce(json_agg(t ORDER BY t.id), \'[]\'::json) FROM ' + schema[0] + '.__drizzle_migrations t')
    applied = json.loads(rows)
    if not applied:
        fail('Paperclip migration journal is empty')
    return {'journal': schema[0] + '.__drizzle_migrations', 'applied': applied}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def image_pins(kit):
    text = (kit / 'compose.yaml').read_text() + (kit / 'Dockerfile.hermes').read_text()
    return sorted(set(re.findall(r'[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[a-f0-9]{64}', text)))


def kit_image_tag(kit):
    # Independent of the project name, checkout location and copied secrets.
    # The commit also separates identical code rebuilt for distinct releases.
    # Candidate builds cannot retag the previous kit's local images.
    files = {name: digest(path) for name, path in kit_files(kit, CODE).items()}
    return hashlib.sha256(json.dumps({'commit': kit_commit(kit), 'files': files}, sort_keys=True).encode()).hexdigest()


def installed_provenance(kit=None):
    kit = kit or KIT
    path = kit / '.kit-provenance.json'
    if not path.exists():
        return None
    record = json.loads(read_private(path))
    files = {name: digest(path) for name, path in kit_files(kit, CODE).items()}
    return record, files != record['files']


def kit_commit(kit=None):
    # The commit restore must check out. Git refuses a checkout root does not
    # own (safe.directory) rather than run its hooks or config: then 'unknown',
    # and the operator's pilot log is the record.
    kit = kit or KIT
    provenance = installed_provenance(kit)
    if provenance is not None:
        record, modified = provenance
        return 'unknown' if modified else commit_label(record['kit_commit'])
    try:
        value = run(['git', '-C', kit, 'rev-parse', '--verify', 'HEAD']).strip()
    except RecoveryError:
        return 'unknown'
    return commit_label(value)


def kit_modified():
    provenance = installed_provenance()
    if provenance is not None:
        return provenance[1]
    # Local edits to compared files make a later restore from the recorded
    # commit refuse, so record them.
    try:
        # Untracked files count: restore compares every file under the code
        # entries, tracked or not (ignored ones, such as .secrets, do not).
        # Scoped to the kit ('--', '.'): untracked files elsewhere in the
        # clone (docs/logs, other kits) are not the kit's code.
        return bool(run(['git', '-C', KIT, 'status', '--porcelain', '--', '.']).strip())
    except RecoveryError:
        return 'unknown'


def commit_label(value):
    # Archive data is untrusted: print only a well-formed object name.
    return value if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) else 'unknown'


def manifest(stage, images, schema, name, started):
    return {'format': 1, 'started_at': started, 'completed_at': utc(),
            'compose_project': name, 'kit_commit': kit_commit(),
            'kit_modified': kit_modified(),
            'images': images, 'image_pins': image_pins(KIT),
            'paperclip_schema': schema,
            'hermes_import': ['--force', '--keep-host', '--messaging-off'],
            'files': {name: digest(stage / name) for name in sorted(PAYLOADS)}}


def utc():
    return datetime.now(timezone.utc).isoformat()


def validate_staging_parent(parent):
    # A private child is insufficient if another uid can rename it through a
    # writable parent. Standard root-owned sticky /tmp and /var/tmp are safe.
    parent = Path(parent).resolve(strict=True)
    for directory in (parent, *parent.parents):
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            fail('Staging ancestors must be root-owned and protected against renames')
    return parent


@contextmanager
def staging(parent):
    # A root-only tree on host storage. Nothing below it is mounted into an
    # application; only the one-shot Hermes CLI sees its own uid-owned subdir.
    parent = validate_staging_parent(parent)
    with tempfile.TemporaryDirectory(prefix='paperclip-hermes-recovery-', dir=parent) as directory:
        os.chmod(directory, 0o700)
        try:
            yield Path(directory)
        finally:
            # The final removal of staged plaintext must not be interrupted.
            hold_signals()


def hold_signals():
    # From here on a handled signal cannot interrupt cleanup (rmtree, service
    # restart, abandon); the job still ends with the error already raised.
    for number in SIGNALS:
        signal.signal(number, signal.SIG_IGN)


@contextmanager
def deferred_signals():
    # Block (not ignore) handled signals: one that arrives meanwhile is
    # delivered when the block ends, and the job still stops through
    # interrupted(). systemd's KillMode=mixed signals only this process.
    mask = getattr(signal, 'pthread_sigmask', None)
    previous = mask(signal.SIG_BLOCK, SIGNALS) if mask else None
    try:
        yield
    finally:
        if mask:
            mask(signal.SIG_SETMASK, previous)


def purge(stage, keep=()):
    # Remove staged plaintext now, before any long wait (service restart).
    # keep names non-secret files cleanup still needs (the Compose override).
    for path in stage.iterdir():
        if path.name in keep and path.is_file() and not path.is_symlink():
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def clear_stale(parent):
    # Called under the lock: a root-owned staging tree is a remnant of a run
    # that could not clean up (SIGKILL, power loss). Other owners' are not ours.
    parent = validate_staging_parent(parent)
    for path in parent.glob('paperclip-hermes-recovery-*'):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == 0:
            shutil.rmtree(path)
            print('Removed stale private staging ' + path.name, file=sys.stderr)


@contextmanager
def locked():
    import fcntl

    # A root-only directory, not world-writable /run/lock; never follow a link.
    with suppress(FileExistsError):
        LOCK_DIR.mkdir(mode=0o700)
    private_directory(LOCK_DIR)
    descriptor = os.open(LOCK_DIR / 'op.lock',
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryError('Another host operation is running') from None
        yield descriptor
    finally:
        os.close(descriptor)


def validate_config(kit):
    # Always this kit's validator, never host code from a restored directory.
    spec = importlib.util.spec_from_file_location('host_config', KIT / 'host-config.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    previous = Path.cwd()
    try:
        os.chdir(kit)
        host.validate_env()
    finally:
        os.chdir(previous)
    private_directory(kit / '.secrets')
    profiles = Compose(kit, 'paperclip-hermes').profiles
    for name in SECRET_FILES:
        if name == 'openai-api-key' and 'hermes' not in profiles:
            continue
        if name.endswith('.pub'):
            continue
        private_file(kit / '.secrets' / name)


def config_backup(stage):
    # Fixed source set, never .git, runtime state, the host SAS, or an age identity.
    names = [*CONFIG, *('.secrets/' + name for name in SECRET_FILES)]
    for name in names:
        source = KIT / name
        entries = [source, *source.rglob('*')] if source.is_dir() else [source]
        if any(entry.is_symlink() for entry in entries):
            fail('Configuration backup refuses symlinks')
    run(['tar', '--numeric-owner', '--acls', '--xattrs', '-cf', stage / 'config.tar',
         '-C', KIT, *names])


def wait_stack(compose):
    # Persistent timers can fire while the daemon is still restoring containers.
    # Wait for existing services; never start a deliberately stopped project.
    expected = set(compose.services())
    for attempt in range(61):
        running = compose('ps', '--services', '--status', 'running').split()
        if set(running) == expected:
            healthy = [run(['docker', 'inspect', '--format', '{{.State.Health.Status}}',
                            compose.container(service)]).strip() == 'healthy'
                       for service in sorted(expected)]
            if all(healthy):
                return running
        if attempt < 60:
            time.sleep(5)
    missing = sorted(expected - set(running))
    fail('Backup requires every selected existing service healthy within five minutes'
         + ('; not running: ' + ', '.join(missing) if missing else '; some are not healthy'))


def service_operation(args, compose, running=None, stage=None):
    if not getattr(args, '_track_service_operation', False):
        return
    record = {'protocol': 1, 'operation': args.command, 'phase': 'running',
              'run_id': uuid.uuid4().hex, 'kit_commit': kit_commit(),
              'kit_path': str(compose.kit.resolve()), 'project': compose.name,
              'previous_running': running or [],
              'containers': {name: compose.container(name) for name in (running or [])},
              'stage': str(stage) if stage is not None else None}
    atomic_json(STATE_DIR / 'service-op.json', record)
    args._service_operation = record
    compose.recovery_run = record['run_id']


def finish_service_operation(args):
    record = getattr(args, '_service_operation', None)
    if record is not None:
        atomic_json(STATE_DIR / 'service-op.json', {**record, 'phase': 'complete'})


def require_no_service_operation():
    path = STATE_DIR / 'service-op.json'
    if path.exists() and json.loads(read_private(path)).get('phase') != 'complete':
        fail('Recover the interrupted backup/restore before another operation')


def require_resolved_upgrade():
    path = STATE_DIR / 'last-op.json'
    if path.exists():
        record = json.loads(read_private(path))
        if record.get('operation') == 'upgrade' and record.get('phase') not in ('complete', 'rolled-back', 'prepare-failed'):
            fail('Recover the unresolved upgrade before taking a backup')


def backup(args):
    require_resolved_upgrade()
    args._restart_services = None
    try:
        return data_backup(args)
    except BaseException:
        # Only a pre-stop backup that actually reached writer shutdown needs
        # this restart. Invalid credentials/arguments must not start a stack.
        if args._restart_services is not None:
            compose, writers = args._restart_services
            with deferred_signals():
                restart_services(compose, writers)
            finish_service_operation(args)
        raise


def data_backup(args):
    if args.local_dir:
        if not args.recipient:
            fail('--local-dir requires --recipient')
        public_key, container = args.recipient, None
        args.local_dir = args.local_dir.resolve()
        private_directory(args.local_dir)
        if KIT == args.local_dir or KIT in args.local_dir.parents:
            fail('Local upload destination must be outside the kit')
    else:
        if args.recipient:
            fail('--recipient is only for --local-dir; configure the host settings file')
        public_key, container = backup_settings(args.env_file, KIT)
    config = {}
    host_config = BACKUP_ENV_FILE.parent / 'config.json'
    if container and host_config.exists():
        config = json.loads(read_private(host_config))
    args.target = getattr(args, 'target', None) or config.get('target') or ('local' if not container else None)
    args.vm_id = getattr(args, 'vm_id', None) or config.get('vm_id') or ('local' if not container else None)
    if not args.target or not args.vm_id:
        fail('Off-site backup requires target and VM identity, from arguments or host config.json')
    for value in (args.target, args.vm_id):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,512}', value):
            fail('Invalid backup target or VM identity')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', args.target):
        fail('Invalid backup target name')
    reason = 'canary' if getattr(args, 'canary', False) else getattr(args, 'reason', 'nightly')
    if reason in ('pre-stop', 'final') and not getattr(args, 'stop_operation_id', None):
        fail('Pre-stop/final backups require --stop-operation-id')
    stop_id = getattr(args, 'stop_operation_id', None)
    if stop_id is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', stop_id):
        fail('Invalid stop operation identity')
    if reason == 'canary':
        return canary(args, public_key, container)
    validate_config(KIT)
    compose = Compose(KIT, args.project)
    if container:
        compose.require_host_only(args.env_file)
    running = wait_stack(compose)
    started = utc()
    work_dir = args.work_dir.resolve()
    if work_dir == KIT or KIT in work_dir.parents:
        fail('Staging must be outside the kit')
    compose.require_host_only(work_dir)
    clear_stale(work_dir)
    with staging(work_dir) as stage:
        # Finally restarts the original services even on dump/encryption/upload
        # failure; a partial stop also takes this path. A restart error fails.
        # On an error or signal, staged plaintext is deleted before the restart.
        try:
            service_operation(args, compose, running, stage)
            if reason in ('pre-stop', 'final'):
                args._restart_services = (compose, [s for s in SERVICES if s in running])
            compose('stop', '--timeout', '90', *(s for s in SERVICES if s in running))
            schema = schema_state(compose) if 'postgres' in running else {}
            with open(stage / 'database.dump', 'wb') as stream:
                if 'postgres' in running:
                    compose('exec', '-T', 'postgres', 'pg_dump', '-U', 'paperclip', '-d', 'paperclip',
                            '--format=custom', '--no-owner', '--no-acl', output=stream)
            config_backup(stage)
            for name, (service, destination) in VOLUMES.items():
                if service not in running:
                    with tarfile.open(stage / (name + '.tar'), 'w'):
                        pass
                    continue
                run(['tar', '--numeric-owner', '--acls', '--xattrs', '-cf', stage / (name + '.tar'),
                     '-C', compose.mount(service, destination), '.'])
            hermes_stage = stage / 'hermes-cli'
            hermes_stage.mkdir(mode=0o700)
            os.chown(hermes_stage, 10000, 10000)
            if 'hermes' in running:
                compose.hermes(hermes_stage, 'backup', '--output', '/recovery/hermes.zip', '--keep', '0')
                shutil.move(hermes_stage / 'hermes.zip', stage / 'hermes.zip')
            else:
                (stage / 'hermes.zip').touch()
            record = manifest(stage, compose.images(), schema, args.project, started)
            (stage / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
        except BaseException:
            hold_signals()
            try:
                # A signal during the one-shot Hermes CLI leaves its one-off
                # container writing the stage with the Hermes home mounted:
                # remove it before the purge and the restart.
                remove_containers(compose, one_off_only=True)
            finally:
                purge(stage)
            raise
        finally:
            # A stop signal during the restart waits until it ends, so the
            # services this job stopped always come back.
            if reason not in ('pre-stop', 'final'):
                with deferred_signals():
                    restart_services(compose, [service for service in SERVICES if service in running])
                    finish_service_operation(args)
        # No plaintext aggregate archive is written: stream tar directly to age.
        with open(stage / 'backup.age', 'xb') as encrypted:
            process = subprocess.Popen(['age', '--encrypt', '--recipient', public_key],
                                       stdin=subprocess.PIPE, stdout=encrypted, stderr=subprocess.PIPE)
            try:
                with tarfile.open(fileobj=process.stdin, mode='w|') as archive:
                    for name in sorted(PAYLOADS | {'manifest.json'}):
                        archive.add(stage / name, arcname=name, recursive=False)
                process.stdin.close()
                process.stdin = None
                process.communicate()
                if process.returncode:
                    fail('age encryption failed')
            except BaseException:
                process.kill()
                process.wait()
                raise
        name = 'paperclip-hermes-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex + '.tar.age'
        receipt = publish_backup(args, public_key, container, stage / 'backup.age', name, record)
        if reason in ('pre-stop', 'final'):
            compose('stop', '--timeout', '90', *(s for s in SERVICES if s in running))
        finish_service_operation(args)
        print('Backup published: ' + receipt['blob'])


def durable_private_directory(path):
    missing = []
    for directory in (path, *path.parents):
        if directory.exists():
            break
        missing.append(directory)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        sync_directory(directory.parent)
    private_directory(path)
    # Also covers retry after a previous mkdir's parent fsync failed.
    for directory in (path, *path.parents):
        sync_directory(directory)


def atomic_json(path, record):
    durable_private_directory(path.parent)
    descriptor, name = tempfile.mkstemp(prefix='.status-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(record, stream, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


STATE_DIR = Path('/data/paperclip-hermes')


def publish_backup(args, public_key, container, archive, name, record=None):
    reason = 'canary' if getattr(args, 'canary', False) else getattr(args, 'reason', 'nightly')
    schema = (record or {}).get('paperclip_schema', {})
    pins = image_pins(KIT)
    # Full inventories stay in the encrypted manifest. Metadata and the public
    # receipt contain bounded identifiers, comfortably below Azure's 8 KiB cap.
    compact_digest = lambda value: hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    metadata = {
        'kit_commit': kit_commit(), 'image_digests': compact_digest(pins),
        'image_count': len(pins), 'schema': compact_digest(schema),
        'schema_count': len(schema.get('applied', [])),
        'recipient_fp': hashlib.sha256(public_key.encode()).hexdigest(),
        'target': getattr(args, 'target', 'local'), 'vm_id': getattr(args, 'vm_id', 'local'),
        'backup_id': uuid.uuid4().hex, 'completed_at': utc(), 'reason': reason,
    }
    if reason in ('pre-stop', 'final'):
        metadata.update(pre_stop='true', stop_operation_id=args.stop_operation_id)
    receipt = {**metadata, 'blob': 'backups/' + name, 'sha256': digest(archive),
               'size': archive.stat().st_size, 'success': True, 'data_backup': reason != 'canary'}
    if container:
        receipt.update(upload(container, archive, receipt['blob'], metadata) or {})
        receipt['completed_at'] = utc()
        # The receipt is an independent unique object, accessible without the VM.
        with tempfile.TemporaryDirectory(dir=archive.parent) as directory:
            path = Path(directory) / 'receipt.json'
            path.write_text(json.dumps(receipt) + '\n')
            upload(container, path, 'receipts/' + quote(metadata['target'], safe='') + '/' + metadata['backup_id'] + '.json')
    else:
        publish_local(archive, args.local_dir, name)
    atomic_json(STATE_DIR / ('last-canary.json' if reason == 'canary' else 'last-backup.json'), receipt)
    return receipt


def canary(args, public_key, container):
    # Credential proof only: no application stop and no data-backup clock reset.
    if container is None:
        fail('Canary requires an off-site upload credential')
    args.canary = True
    with staging(args.work_dir) as stage:
        archive = stage / 'canary.age'
        with archive.open('wb') as encrypted:
            run(['age', '--encrypt', '--recipient', public_key], output=encrypted)
        receipt = publish_backup(args, public_key, container, archive, 'canary-' + uuid.uuid4().hex + '.age')
    print('Canary published: ' + receipt['blob'])
    return receipt


def unpack_bundle(bundle, stage):
    expected = PAYLOADS | {'manifest.json'}
    seen = set()
    with tarfile.open(bundle, 'r:') as archive:
        for member in archive:
            if member.name not in expected or member.name in seen or not member.isfile():
                fail('Invalid recovery bundle member')
            seen.add(member.name)
            with archive.extractfile(member) as source, (stage / member.name).open('xb') as target:
                shutil.copyfileobj(source, target)
    if seen != expected:
        fail('Incomplete recovery bundle')
    record = json.loads((stage / 'manifest.json').read_text())
    # Check every field restore reads before anything starts, so nothing after
    # the services are up can fail on the manifest. The recovery point is
    # printed, so it must be a well-formed UTC time, not arbitrary text.
    if (not isinstance(record, dict) or record.get('format') != 1
            or not isinstance(record.get('files'), dict) or set(record['files']) != PAYLOADS
            or not isinstance(record.get('images'), dict)
            or not set(record['images']) <= {'postgres', *SERVICES}
            or not record['images']
            or not isinstance(record.get('paperclip_schema'), dict)
            or ('postgres' in record['images'] and not record['paperclip_schema'].get('applied'))
            or not all(isinstance(record.get(key), str) and UTC_TIME.fullmatch(record[key])
                       for key in ('started_at', 'completed_at'))):
        fail('Unsupported or incomplete recovery manifest')
    try:
        started, completed = (datetime.fromisoformat(record[key]) for key in ('started_at', 'completed_at'))
        if completed < started:
            fail('Recovery manifest timestamps are reversed')
    except ValueError:
        raise RecoveryError('Invalid recovery manifest timestamp') from None
    if any(digest(stage / name) != record['files'][name] for name in PAYLOADS):
        fail('Recovery payload checksum mismatch')
    return record


def kit_files(root, names):
    # Regular files below the named kit entries, without Python bytecode caches.
    files = {}
    for name in names:
        path = root / name
        for entry in ([path, *sorted(path.rglob('*'))] if path.is_dir() else [path]):
            relative = entry.relative_to(root)
            if '__pycache__' not in relative.parts and entry.is_file():
                files[relative.as_posix()] = entry
    return files


def write_private(path, data, mode):
    # A new file only; never through an existing name or link.
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_BINARY', 0))
    with os.fdopen(os.open(path, flags, mode), 'wb') as stream:
        stream.write(data)
    os.chmod(path, mode)


def install_kit(destination):
    # Copy this trusted kit's code, configuration and documentation with
    # explicit modes: the job's umask 077 must not make /kit unreadable to the
    # unprivileged container users (uid 10000 Hermes, Paperclip workers).
    for name, source in kit_files(KIT, CONFIG).items():
        directory = destination
        for part in PurePosixPath(name).parent.parts:
            directory = directory / part
            if not directory.is_dir():
                directory.mkdir()
            os.chmod(directory, 0o755)
        shutil.copyfile(source, destination / name)
        os.chmod(destination / name, 0o755 if source.stat().st_mode & 0o111 else 0o644)


def restore_config(archive, destination, commit='unknown', modified=False):
    # Restore trusts only data and secrets from the archive: age gives
    # confidentiality, not authenticity, and the manifest is in the same
    # payload. Code and configuration, hermes-config.yaml included, come from
    # this kit (KIT); archived copies must match it byte for byte, or the
    # restore is refused, naming the kit commit the manifest recorded.
    contents = {}
    with tarfile.open(archive) as source:
        for member in source:
            member_path = PurePosixPath(member.name)
            parts = member_path.parts
            if (not parts or member_path.is_absolute() or '..' in parts
                    or not (member.isfile() or member.isdir())
                    or (parts[0] not in CONFIG and member.name not in
                        ('.secrets', *('.secrets/' + name for name in SECRET_FILES)))):
                fail('Unsafe configuration archive')
            if member.isfile():
                if member.name in contents or member.size > MEMBER_LIMIT:
                    fail('Unsafe configuration archive')
                with source.extractfile(member) as stream:
                    contents[member.name] = stream.read()
    archived = {name: data for name, data in contents.items()
                if PurePosixPath(name).parts[0] in CODE and '__pycache__' not in PurePosixPath(name).parts}
    trusted = {name: path.read_bytes() for name, path in kit_files(KIT, CODE).items()}
    if archived != trusted:
        fail('The backup was made with different kit code (kit commit ' + commit_label(commit)
             + (', with local changes' if modified else '') + '); '
             'check out the kit commit that made the backup and run its restore.sh '
             '(restore takes only data and secrets from the archive)')
    secrets = {name: contents.get('.secrets/' + name) for name in SECRET_FILES}
    if any(value is None for value in secrets.values()):
        fail('Configuration archive lacks the kit secrets')
    install_kit(destination)
    # Provenance comes from the trusted restore code's checkout, never the
    # untrusted archive's claimed commit. Hashes detect later local code edits.
    write_private(destination / '.kit-provenance.json', json.dumps({
        'kit_commit': kit_commit(),
        'files': {name: digest(path) for name, path in kit_files(destination, CODE).items()},
    }).encode(), 0o600)
    (destination / '.secrets').mkdir(mode=0o700)
    os.chmod(destination / '.secrets', 0o700)
    for name, data in secrets.items():
        write_private(destination / '.secrets' / name, data, 0o644 if name.endswith('.pub') else 0o600)


def restore_target(path):
    # A new root-only directory, or an existing empty one, reached only through
    # root-owned parents that nobody else can write (no swap of the path).
    target = Path(path).absolute()
    if not target.name:
        fail('Restore target must be a new directory')
    target = target.parent.resolve() / target.name
    for directory in (target.parent, *target.parent.parents):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            fail('Every parent of the restore target must be root-owned and not group/other-writable')
    try:
        target.mkdir(mode=0o700)
        return target
    except FileExistsError:
        pass
    # Checked outside the handler, so a refusal carries no exception context.
    private_directory(target)
    if any(target.iterdir()):
        fail('Restore target must be a new or empty directory')
    return target


def restored_settings(text, hostname=None):
    # The hostname is the only Compose setting restore changes. It also adds
    # KIT_MESSAGING_OFF=1: configure-hermes.py then switches every messaging
    # platform off at each start, so a restored bot cannot answer beside the
    # source host. The operator removes the line to turn messaging back on.
    if hostname:
        text = re.sub(r'(?m)^TAILSCALE_HOSTNAME=.*$', 'TAILSCALE_HOSTNAME=' + hostname, text)
    lines = [line for line in text.splitlines() if not line.startswith('KIT_MESSAGING_OFF=')]
    return '\n'.join([*lines, MESSAGING_OFF]) + '\n'


def empty_target(target):
    # The target is this job's root-only directory (restore_target): remove
    # what restore wrote, keep the directory itself.
    for path in target.iterdir():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def remove_containers(compose, one_off_only=False):
    # A cleanup step: report a failure and let the remaining steps run.
    try:
        compose.remove_containers(one_off_only)
    except RecoveryError:
        print('Cleanup step failed: docker rm (' + ('one-off ' if one_off_only else '')
              + 'project containers)', file=sys.stderr)


def abandon(compose):
    # Preserve named volumes for deliberate inspection; never start a partially
    # restored application on the next reboot. Removing the containers with -v
    # deletes only their anonymous volumes (the sandbox's inherited /opt/data).
    # Every project container goes first, one-offs from 'run --rm' included:
    # compose rm and down skip those, and down cannot remove the network
    # while one still runs.
    remove_containers(compose)
    for args in (('rm', '--force', '--stop', '--volumes'), ('down',)):
        try:
            compose(*args)
        except RecoveryError:
            print('Cleanup step failed: docker compose ' + args[0], file=sys.stderr)


def remove_import_undo(home):
    # --keep-host takes a rollback ZIP of the freshly seeded destination.
    # It is not part of the recovery set and must not remain as plaintext.
    directory = home / 'backups'
    if directory.is_dir() and not directory.is_symlink():
        for path in directory.glob('pre-transfer-*.zip'):
            path.unlink()


def restore(args):
    # Defense in depth for direct callers as well as argparse's required flag.
    if not args.fenced:
        fail('Restore requires --fenced before any operation')
    private_file(args.identity)
    if getattr(args, 'verify_only', False):
        work = args.work_dir.resolve()
        if work == KIT or KIT in work.parents:
            fail('Verification staging must be outside the kit')
        with staging(work) as stage:
            with (stage / 'bundle.tar').open('xb') as stream:
                run(['age', '--decrypt', '--identity', args.identity.resolve(), args.archive.resolve()], output=stream)
            unpack_bundle(stage / 'bundle.tar', stage)
            print('Verified recovery bundle hashes and manifest')
        return
    # Check labels, including stopped containers and orphan volumes, before any
    # overwrite. Never restore into a project which has previous durable state.
    for kind, extra in (('container', ['--all']), ('volume', []), ('network', [])):
        if run(['docker', kind, 'ls', *extra, '--quiet', '--filter',
                'label=com.docker.compose.project=' + args.project]).strip():
            fail('Restore requires a fresh Compose project (no containers, volumes or networks)')
    target = restore_target(args.target)
    compose = Compose(target, args.project)
    work_dir = args.work_dir.resolve()
    if work_dir == KIT or KIT in work_dir.parents or work_dir == target or target in work_dir.parents:
        fail('Restore staging must be outside both kit directories')
    try:
        clear_stale(work_dir)
    except BaseException:
        hold_signals()
        empty_target(target)
        raise
    with staging(work_dir) as stage:
        try:
            with open(stage / 'bundle.tar', 'xb') as stream:
                run(['age', '--decrypt', '--identity', args.identity.resolve(), args.archive.resolve()], output=stream)
            record = unpack_bundle(stage / 'bundle.tar', stage)
            restore_config(stage / 'config.tar', target, record.get('kit_commit', 'unknown'),
                           record.get('kit_modified') is True)
            if image_pins(target) != record.get('image_pins'):
                fail('Restored image pins differ from the manifest')
            if args.hostname and not re.fullmatch(r'[a-z0-9-]+\.[a-z0-9.-]+\.ts\.net', args.hostname):
                fail('Use the destination MagicDNS FQDN ending .ts.net')
            env_file = target / '.secrets/compose.env'
            env_file.write_text(restored_settings(env_file.read_text(), args.hostname))
            validate_config(target)
            # The destination was empty when the first Compose object was
            # constructed. Load the restored profile selection before create.
            compose = Compose(target, args.project)
            if set(compose.services()) != set(record['images']):
                fail('Restored profile selection differs from the backup')
            # A prepared host's mount marker belongs to this host, never the backup.
            if (KIT / '.secrets/bootstrap-mount').exists():
                shutil.copyfile(KIT / '.secrets/bootstrap-mount', target / '.secrets/bootstrap-mount')
            # Empty volumes must not inherit image-baked defaults before import.
            override = stage / 'restore-volumes.json'
            mounts = {service: [] for service in ('postgres', *SERVICES)}
            for name, (service, destination) in {**VOLUMES, 'hermes': ('hermes', '/opt/data'),
                    'sandbox': ('sandbox', '/workspace'), 'postgres': ('postgres', '/var/lib/postgresql/data')}.items():
                mounts[service].append({'type': 'volume', 'source': name, 'target': destination,
                                        'volume': {'nocopy': True}})
            mounts['paperclip'].append({'type': 'volume', 'source': 'public_ca',
                'target': '/caddy-ca', 'read_only': True, 'volume': {'nocopy': True}})
            override.write_text(json.dumps({'services': {service: {'volumes': volumes}
                                 for service, volumes in mounts.items()}}))
            compose.files = ['-f', 'compose.yaml', '-f', str(override)]
            compose('config', '--quiet')
        except BaseException:
            # Nothing was created yet: remove the decrypted secrets and copied
            # kit so the target holds no plaintext and a retry can reuse it.
            hold_signals()
            try:
                purge(stage)
            finally:
                empty_target(target)
            raise
        home = None
        installed = set(record['images'])
        try:
            service_operation(args, compose, stage=stage)
            intent = getattr(args, '_service_operation', None)
            if intent is not None:
                # Generation label survives the ephemeral volume override.
                override_config = json.loads(override.read_text())
                for item in override_config['services'].values():
                    item['labels'] = {'org.software-factory.recovery-run': intent['run_id']}
                override.write_text(json.dumps(override_config))
            compose('create', '--build')
            # The policy is this kit's; defence in depth: check it against the
            # validator's own safety baseline with the pinned image's YAML
            # parser before any service reads it.
            if 'hermes' in installed:
                compose('run', '--rm', '--no-deps', '-T', '--entrypoint', PYTHON, 'hermes',
                        '/kit/validate-hermes-config.py', '/kit-config.yaml')
            for name, (service, destination) in VOLUMES.items():
                if service not in installed:
                    continue
                volume = compose.mount(service, destination)
                if any(volume.iterdir()):
                    fail('Restore refuses a nonempty destination volume')
                run(['tar', '--extract', '--file', stage / (name + '.tar'), '--directory', volume,
                     '--numeric-owner', '--same-owner', '--acls', '--xattrs'])
            # Start only the DB while restoring. No application scheduler exists
            # in any one-shot container: entrypoint is explicitly overridden.
            if 'postgres' in installed:
                compose('up', '-d', '--no-deps', '--wait', '--wait-timeout', '180', 'postgres')
                # Commit restore statements separately to bound schema locks.
                # Applications start only after a successful, checked restore.
                with open(stage / 'database.dump', 'rb') as stream:
                    compose('exec', '-T', 'postgres', 'pg_restore', '--exit-on-error',
                            '--no-owner', '--no-acl', '-U', 'paperclip', '-d', 'paperclip', input_file=stream)
                if schema_state(compose) != record['paperclip_schema']:
                    fail('Restored migration journal differs from the backup')
            if 'hermes' in installed:
                hermes_stage = stage / 'hermes-cli'
                hermes_stage.mkdir(mode=0o700)
                shutil.copyfile(stage / 'hermes.zip', hermes_stage / 'hermes.zip')
                os.chown(hermes_stage, 10000, 10000)
                os.chown(hermes_stage / 'hermes.zip', 10000, 10000)
                # A nocopy volume root is root:root 0755; give the home to the
                # Hermes user as the upstream s6 entrypoint would before import.
                home = compose.mount('hermes', '/opt/data')
                os.chown(home, 10000, 10000)
                os.chmod(home, 0o700)
                # Seed the kit policy before --keep-host merges the portable state.
                compose('run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh', 'hermes',
                        '-ec', '. /kit/hermes-secrets.sh; exec ' + PYTHON + ' /kit/configure-hermes.py')
                try:
                    compose.hermes(hermes_stage, 'import', '/recovery/hermes.zip', '--force', '--keep-host', '--messaging-off')
                finally:
                    # On a signal the import may still be running in its one-off
                    # container; the failure path removes it and repeats this.
                    remove_import_undo(home)
            # The root config.yaml is rewritten from this kit's policy on every
            # start (configure-hermes.py), as on the original host: no archive
            # setting reaches it. KIT_MESSAGING_OFF (restored_settings) makes
            # every start switch off each messaging platform in the root and
            # every profile, whatever credential the restored home holds.
            compose('up', '-d', '--wait', '--wait-timeout', '300')
            finish_service_operation(args)
        except BaseException:
            hold_signals()
            # abandon() still runs compose with the override file, which
            # holds only volume mounts: keep it until the project is down,
            # and take the project down even if the purge fails. A one-off
            # (validator, configure-hermes, Hermes import) whose compose
            # client a signal killed still runs with the stage and volumes
            # mounted: remove every project container before the purge.
            try:
                try:
                    remove_containers(compose)
                    purge(stage, keep=(override.name,))
                finally:
                    abandon(compose)
            finally:
                # Again, now that no import can still be writing the home.
                if home is not None:
                    remove_import_undo(home)
            raise
        # The manifest was checked before applications started.
        print('Restore complete; messaging is off until you remove KIT_MESSAGING_OFF. '
              'Recovery point: ' + record['completed_at'])
        print('Reauthenticate OAuth logins and reconcile in-flight run IDs before submitting work.')


def interrupted(signum, frame):
    # One signal starts cleanup; later ones must not interrupt it (rmtree,
    # service restart), so every handled signal is ignored from here on.
    hold_signals()
    raise RecoveryError('Recovery interrupted; cleaning private staging')


def main(argv=None):
    args = parser().parse_args(argv)
    if os.geteuid() != 0:
        fail('Run the host recovery job as root')
    os.umask(0o077)
    for number in SIGNALS:
        signal.signal(number, interrupted)
    with locked():
        if args.command in ('backup', 'restore'):
            require_no_service_operation()
            if args.command == 'backup':
                require_resolved_upgrade()
            args._track_service_operation = True
            status = {'protocol': 1, 'operation': args.command, 'phase': 'running', 'started_at': utc()}
            old = STATE_DIR / 'last-op.json'
            prior = json.loads(read_private(old)) if old.exists() else {}
            preserve = prior.get('operation') == 'upgrade' and prior.get('phase') not in ('complete', 'rolled-back', 'prepare-failed')
            if not preserve:
                atomic_json(old, status)
            try:
                if args.command == 'backup':
                    backup(args)
                else:
                    if args.staged and Path('/run') not in args.identity.resolve().parents:
                        fail('Staged restore identity must be in /run')
                    try:
                        restore(args)
                    finally:
                        if args.staged:
                            args.identity.unlink(missing_ok=True)
                status['phase'] = 'complete'
            except BaseException:
                status['phase'] = 'failed'
                raise
            finally:
                if not preserve:
                    atomic_json(old, {**status, 'completed_at': utc()})
        elif args.command == 'rollback-point':
            require_no_service_operation()
            prior_path = STATE_DIR / 'last-op.json'
            prior = json.loads(read_private(prior_path)) if prior_path.exists() else {}
            if prior.get('operation') == 'upgrade' and prior.get('phase') not in ('complete', 'rolled-back', 'prepare-failed'):
                fail('Recover the interrupted upgrade before standalone rollback-point actions')
            spec = importlib.util.spec_from_file_location('kit_control', KIT / 'kit-control.py')
            control = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(control)
            control.r.RecoveryError = RecoveryError
            control.rollback_point(args.action, args.path, Compose(KIT, args.project))


GENERIC_FAILURE = ('Recovery failed; no successful result. '
                   'Check prerequisites, private inputs and service state.')


def report_failure(error):
    # The stderr lines for a failed job. Only this job's own messages are
    # rendered: network and subprocess exceptions may embed a signed URL,
    # credential or application configuration. A cleanup failure (such as the
    # service restart) can replace the error that started cleanup, so that one
    # is named too, unless it was deliberately suppressed (raise ... from None).
    lines = [str(error) if isinstance(error, RecoveryError) else GENERIC_FAILURE]
    earlier = None if error.__suppress_context__ else error.__context__
    if isinstance(earlier, RecoveryError):
        lines.append('Earlier error: ' + str(earlier))
    elif earlier is not None:
        lines.append('Earlier error: an unexpected failure before cleanup')
    return lines


if __name__ == '__main__':
    try:
        main()
    except (RecoveryError, OSError, ValueError, tarfile.TarError) as failure:
        print(*report_failure(failure), sep='\n', file=sys.stderr)
        sys.exit(1)
