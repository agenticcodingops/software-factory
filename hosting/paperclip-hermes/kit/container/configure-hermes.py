#!/usr/bin/env python3
"""Reapply the kit's sandbox and API-only provider policy before s6 starts.

With KIT_MESSAGING_OFF=1 (written by restore into .secrets/compose.env), every
start also switches each messaging platform off in the root config.yaml and in
every named profile's config.yaml, whatever credential the home holds.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from ruamel.yaml import YAML

HOME = Path('/opt/data')
POLICY = Path('/kit-config.yaml')
VALIDATOR = Path('/kit/validate-hermes-config.py')
HERMES_UID = 10000


def validator():
    spec = importlib.util.spec_from_file_location('validate_hermes_config', VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIST_BOTS = ('import json\n'
             'from hermes_cli.backup_keep_host import _bot_platforms\n'
             'print(json.dumps(sorted(_bot_platforms())))\n')


def bot_names(profile=None):
    # _bot_platforms() discovers plugins and seeds the Hermes home (SOUL.md,
    # cache and log folders) as a side effect, so it runs as the Hermes user,
    # like the gateway: nothing it creates is root-owned. A named profile has
    # its own plugin scope (profiles/<name>/plugins, enabled in its own
    # config.yaml), so it is listed in a child whose HERMES_HOME is that
    # profile, as its gateway would run.
    env = dict(os.environ)
    if profile is not None:
        env['HERMES_HOME'] = str(profile)
    result = subprocess.run([sys.executable, '-c', LIST_BOTS], user=HERMES_UID, group=HERMES_UID,
                            extra_groups=[], cwd='/', env=env, capture_output=True, text=True,
                            check=False)
    lines = result.stdout.strip().splitlines()
    try:
        names = json.loads(lines[-1]) if result.returncode == 0 and lines else None
    except ValueError:
        names = None
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        where = 'profile ' + Path(profile).name if profile is not None else 'the root profile'
        raise SystemExit('KIT_MESSAGING_OFF: could not list the Hermes messaging platforms of ' + where)
    return names


def messaging_platforms():
    # Hermes's own list of messaging bots and of the config blocks its gateway
    # reads 'enabled' from (the helpers behind import --messaging-off). Refuse
    # to start rather than guess if the pinned implementation changes.
    try:
        from hermes_cli import backup_keep_host as keep
    except ImportError:
        keep = None
    blocks = getattr(keep, '_platform_blocks', None)
    if not callable(getattr(keep, '_bot_platforms', None)) or not callable(blocks):
        raise SystemExit('KIT_MESSAGING_OFF: unsupported Hermes messaging implementation')
    names = sorted(bot_names())
    if not names:
        raise SystemExit('KIT_MESSAGING_OFF: Hermes lists no messaging platforms')
    return names, blocks


def switch_off(config, names, blocks):
    # As import --messaging-off writes it, but for every platform: an explicit
    # platforms.<id>.enabled: false, which environment credentials cannot
    # override, and no 'enabled' in any other block the gateway merges.
    before = copy.deepcopy(config)
    for name in names:
        for block in blocks(config, name):
            block.pop('enabled', None)
        if not isinstance(config.get('platforms'), dict):
            config['platforms'] = {}
        if not isinstance(config['platforms'].get(name), dict):
            config['platforms'][name] = {}
        config['platforms'][name]['enabled'] = False
    return config != before


def configured_platforms(config):
    # Every id that already has a block under platforms or gateway.platforms:
    # defence in depth beside the plugin listing, these are switched off too.
    gateway = config.get('gateway') if isinstance(config.get('gateway'), dict) else {}
    ids = set()
    for holder in (config.get('platforms'), gateway.get('platforms')):
        if isinstance(holder, dict):
            ids.update(name for name, block in holder.items() if isinstance(name, str) and isinstance(block, dict))
    return ids


def switch_off_profile(profile, names, blocks):
    # Runs as root in a directory the Hermes user owns: never follow a link,
    # and replace config.yaml through a new file in the same directory.
    # names: the root list and this profile's own plugin platforms.
    directory = os.open(profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        yaml = YAML()
        mode = 0o600
        try:
            source = os.open('config.yaml', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
        except FileNotFoundError:
            config = {}
        else:
            with os.fdopen(source, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise SystemExit(f'Profile {profile.name}: config.yaml must be a regular file')
                mode = stat.S_IMODE(info.st_mode)
                config = yaml.load(stream.read().decode('utf-8-sig')) or {}
        if not isinstance(config, dict):
            raise SystemExit(f'Profile {profile.name}: config.yaml is not a mapping')
        if not switch_off(config, sorted(set(names) | configured_platforms(config)), blocks):
            return
        temporary = '.config.yaml.kit-messaging-off'
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
        with os.fdopen(target, 'w', encoding='utf-8') as stream:
            yaml.dump(config, stream)
            stream.flush()
            os.fchown(stream.fileno(), HERMES_UID, HERMES_UID)
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, 'config.yaml', src_dir_fd=directory, dst_dir_fd=directory)
        print(f'Messaging switched off in profile {profile.name} (KIT_MESSAGING_OFF)')
    finally:
        os.close(directory)


def switch_off_profiles(home, names, blocks):
    profiles = home / 'profiles'
    if profiles.is_symlink():
        raise SystemExit('The Hermes profiles directory must not be a symlink')
    if not profiles.is_dir():
        return
    for profile in sorted(profiles.iterdir()):
        if profile.is_symlink():
            raise SystemExit(f'Profile {profile.name} must not be a symlink')
        if profile.is_dir():
            # A plugin installed only in this profile is listed in its scope
            # only; a listing that fails stops the start (bot_names).
            switch_off_profile(profile, sorted(set(names) | set(bot_names(profile))), blocks)


def main():
    messaging_off = os.environ.get('KIT_MESSAGING_OFF', '')
    if messaging_off not in ('', '1'):
        raise SystemExit('KIT_MESSAGING_OFF must be 1 or unset')
    yaml = YAML(typ='safe')
    yaml.default_flow_style = False
    HOME.mkdir(parents=True, exist_ok=True)
    config = yaml.load(POLICY.read_text())
    # The same fixed baseline restore checks: a policy that weakens the
    # sandbox or adds hooks, plugins or platforms never reaches the home.
    validator().check(config)
    config['model']['default'] = os.environ['HERMES_MODEL']
    config['dashboard']['public_url'] = os.environ['HERMES_DASHBOARD_PUBLIC_URL']
    config['dashboard']['trusted_proxies'] = [os.environ['DASHBOARD_GATEWAY']]
    if messaging_off:
        names, blocks = messaging_platforms()
        switch_off(config, names, blocks)
        switch_off_profiles(HOME, names, blocks)
        print('Messaging switched off in the root profile (KIT_MESSAGING_OFF)')
    path = HOME / 'config.yaml'
    if path.is_symlink():
        raise SystemExit('Hermes config must not be a symlink')
    with open(path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        yaml.dump(config, stream)
    os.chown(path, HERMES_UID, HERMES_UID)
    os.chmod(path, 0o600)
    # Hermes's s6 stage2 hook later sets config.yaml to 0640 hermes:hermes; it holds
    # no secrets (keys live in the 0600 .env written below).
    # Hermes loads .env with override=True. Persist the authoritative values so
    # stale saved keys and backend settings cannot silently override Compose.
    path = HOME / '.env'
    if path.is_symlink():
        raise SystemExit('Hermes .env must not be a symlink')
    names = [
        'OPENAI_API_KEY', 'API_SERVER_ENABLED', 'API_SERVER_HOST', 'API_SERVER_PORT',
        'API_SERVER_KEY', 'HERMES_DASHBOARD_PUBLIC_URL',
        'HERMES_DASHBOARD_BASIC_AUTH_USERNAME', 'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD',
        'HERMES_DASHBOARD_BASIC_AUTH_SECRET', 'TERMINAL_ENV', 'TERMINAL_SSH_HOST',
        'TERMINAL_SSH_USER', 'TERMINAL_SSH_PORT', 'TERMINAL_SSH_KEY',
    ]
    with open(path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        for name in names:
            value = os.environ[name]
            if any(c in value for c in '\r\n\x00\'"\\$'):
                raise SystemExit(f'Unsupported character in {name}')
            stream.write(f'{name}={value}\n')
    os.chown(path, HERMES_UID, HERMES_UID)
    os.chmod(path, 0o600)


if __name__ == '__main__':
    main()
