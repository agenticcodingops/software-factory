#!/usr/bin/env python3
"""Bootstrap validation and conservative Docker/containerd configuration edits."""
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib


def fail(message):
    raise SystemExit(message)


def private_file(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        fail(f'{path}: require a regular root-owned file, mode 0600 or 0400')
    if not path.read_bytes().strip():
        fail(f'{path}: empty file')


ENV_FILE = Path('.secrets/compose.env')


def validate_env():
    private_file(ENV_FILE)
    private_file('.secrets/github-token')
    secrets = [
        'POSTGRES_PASSWORD', 'BETTER_AUTH_SECRET', 'PAPERCLIP_AGENT_JWT_SECRET',
        'PAPERCLIP_TOOL_ACTION_SIGNING_SECRET', 'PAPERCLIP_SECRETS_MASTER_KEY',
        'HERMES_GATEWAY_KEY', 'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD',
        'HERMES_DASHBOARD_BASIC_AUTH_SECRET',
    ]
    allowed = set(secrets) | {'OPENAI_API_KEY', 'TAILSCALE_HOSTNAME',
        'FIXTURE_REPOSITORY', 'HERMES_MODEL', 'DASHBOARD_SUBNET', 'DASHBOARD_GATEWAY',
        'KIT_MESSAGING_OFF'}
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', name) or name in values:
            fail('Use unique NAME=value assignments in .secrets/compose.env')
        if name not in allowed:
            fail(f'Unknown setting {name} in .secrets/compose.env')
        # The kit intentionally accepts a small, literal subset of dotenv.
        # No shell expansion, inline comments, quotes, escapes or multiline keys.
        if re.search(r'''[\s'"\\$#]''', value):
            fail(f'{name}: use a literal value without whitespace or quotes')
        values[name] = value
    for name in secrets:
        if not re.fullmatch(r'[a-fA-F0-9]{64}', values.get(name, '')):
            fail(f'{name}: supply an independently generated 32-byte hex value')
    if len({values[name] for name in secrets}) != len(secrets):
        fail('Every signing key, password and master key must be different')
    if not values.get('OPENAI_API_KEY'):
        fail('OPENAI_API_KEY is required; API project key only')
    host = values.get('TAILSCALE_HOSTNAME', '')
    if not re.fullmatch(r'[a-z0-9-]+\.[a-z0-9.-]+\.ts\.net', host):
        fail('TAILSCALE_HOSTNAME must be the actual MagicDNS FQDN ending .ts.net')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', values.get('FIXTURE_REPOSITORY', '')):
        fail('FIXTURE_REPOSITORY must be owner/repository')
    if values['FIXTURE_REPOSITORY'] == 'owner/fixture-repository':
        fail('Set the actual fixture repository')
    # Restore writes KIT_MESSAGING_OFF=1; the operator removes the line to
    # turn Hermes messaging back on. Any other value would be ambiguous.
    if values.get('KIT_MESSAGING_OFF', '1') != '1':
        fail('KIT_MESSAGING_OFF: use KIT_MESSAGING_OFF=1 or remove the line')
    if not values.get('HERMES_MODEL'):
        fail('Set HERMES_MODEL to a model available to the API project')
    subnet = ipaddress.ip_network(values.get('DASHBOARD_SUBNET', '172.30.99.0/24'))
    gateway = ipaddress.ip_address(values.get('DASHBOARD_GATEWAY', '172.30.99.1'))
    if subnet.version != 4 or gateway not in subnet or gateway in (subnet.network_address, subnet.broadcast_address):
        fail('DASHBOARD_GATEWAY must be a usable address in DASHBOARD_SUBNET')
    if not subnet.is_private:
        fail('DASHBOARD_SUBNET must be private IPv4 space')


def docker_config():
    path = Path('/etc/docker/daemon.json')
    return json.loads(path.read_text()) if path.exists() else {}


def containerd_config():
    path = Path('/etc/containerd/config.toml')
    text = path.read_text() if path.exists() else 'version = 2\n'
    return text, tomllib.loads(text)


def safe_path(value):
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', value) or str(Path(value).resolve()) != value or value == '/':
        fail('Data directories must be canonical absolute paths without whitespace')
    return value


def roots():
    _, conf = containerd_config()
    return safe_path(docker_config().get('data-root', '/var/lib/docker')), safe_path(conf.get('root', '/var/lib/containerd'))


def desired(mount):
    safe_path(mount)
    config = docker_config()
    if config.get('live-restore'):
        fail('Disable live restore before relocating Docker state')
    if config.get('iptables') is False or config.get('firewall-backend', 'iptables') != 'iptables':
        fail('This kit requires Docker iptables and the DOCKER-USER chain')
    config.update({'data-root': mount + '/docker', 'log-driver': 'local',
                   'log-opts': {'max-size': '10m', 'max-file': '3'},
                   'iptables': True, 'firewall-backend': 'iptables'})
    text, _ = containerd_config()
    # Only edit the top-level root, never a plugin's root inside a TOML table.
    table = re.search(r'(?m)^\s*\[', text)
    header, tables = (text[:table.start()], text[table.start():]) if table else (text, '')
    header = re.sub(r'(?m)^[ \t]*root[ \t]*=.*\n?', '', header)
    text = 'root = ' + json.dumps(mount + '/containerd') + '\n' + header + tables
    tomllib.loads(text)  # Refuse an invalid edit before stopping any service.
    return config, text


def changed(mount):
    config, text = desired(mount)
    return config != docker_config() or text != containerd_config()[0]


def write_config(mount):
    config, text = desired(mount)
    for path, content in [(Path('/etc/docker/daemon.json'), json.dumps(config, indent=2) + '\n'),
                          (Path('/etc/containerd/config.toml'), text)]:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text() != content:
            path.write_text(content)
            os.chmod(path, 0o644)


if __name__ == '__main__':
    command = sys.argv[1]
    if command == 'validate-env':
        validate_env()
    elif command == 'roots':
        print(*roots())
    elif command == 'changed':
        print('yes' if changed(sys.argv[2]) else 'no')
    elif command == 'write':
        write_config(sys.argv[2])
    else:
        fail('Unknown command')
