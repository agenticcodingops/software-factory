"""Run credential/firewall scripts against disposable executables, never the host.

POSIX integration cases need /bin/sh and python3. The Hermes regression uses
the same ruamel.yaml dependency as the pinned image (0.18.16).
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
POSIX = os.name == 'posix' and shutil.which('sh') and shutil.which('python3')
try:
    from ruamel.yaml import YAML
except ImportError:
    YAML = None


@unittest.skipUnless(POSIX, 'requires a POSIX shell and python3')
class HermesSecretTests(unittest.TestCase):
    def test_shared_loader_exports_file_secrets_for_one_shot_configurator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapping = {'API_SERVER_KEY': 'hermes_gateway_key',
                       **{key: key.lower() for key in ('OPENAI_API_KEY',
                          'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD', 'HERMES_DASHBOARD_BASIC_AUTH_SECRET')}}
            for key, filename in mapping.items():
                (root / filename).write_text('fixture-' + key)
            loader = root / 'loader.sh'
            loader.write_text((KIT / 'container/hermes-secrets.sh').read_text().replace('/run/secrets', str(root)))
            command = '. "$1"; exec python3 -c \'import os,json; print(json.dumps(dict(os.environ)))\''
            env = {'PATH': os.environ['PATH']}
            result = subprocess.run(['sh', '-ec', command, 'configure', str(loader)],
                                    env=env, text=True, capture_output=True, check=True)
            exported = json.loads(result.stdout)
            for key in mapping:
                self.assertEqual(exported[key], 'fixture-' + key)
            self.assertIn('. /kit/hermes-secrets.sh', (KIT / 'container/hermes-entrypoint.sh').read_text())


@unittest.skipUnless(POSIX, 'requires a POSIX shell and python3')
class GhTests(unittest.TestCase):
    def invoke(self, inherited, token_exists=True, launcher_active=False, github_token=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / 'github-token'
            if token_exists:
                token.write_text('fixture-fallback\n')
            real_gh = root / 'gh'
            real_gh.write_text('#!/bin/sh\nprintf "%s\\n" "$GH_TOKEN" "$@"\n')
            real_gh.chmod(0o755)
            script = (KIT / 'container/gh.sh').read_text()
            script = script.replace('/run/kit/github-token', str(token))
            script = script.replace('/usr/bin/gh', str(real_gh))
            env = os.environ.copy()
            env.pop('GH_TOKEN', None)
            env.pop('PAPERCLIP_GITHUB_OPERATION_ACTIVE', None)
            env.pop('GITHUB_TOKEN', None)
            if github_token is not None:
                env['GITHUB_TOKEN'] = github_token
            if launcher_active:
                env['PAPERCLIP_GITHUB_OPERATION_ACTIVE'] = '1'
            if inherited is not None:
                env['GH_TOKEN'] = inherited
            return subprocess.run(['sh', '-c', script, 'gh', 'api', 'fixture argument'],
                                  env=env, text=True, capture_output=True, check=False)

    def test_broker_token_survives_without_runtime_file(self):
        result = self.invoke('fixture-broker', token_exists=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['fixture-broker', 'api', 'fixture argument'])

    def test_unset_and_empty_tokens_use_runtime_file(self):
        for inherited in (None, ''):
            with self.subTest(inherited=inherited):
                result = self.invoke(inherited)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines()[0], 'fixture-fallback')

    def test_caller_github_token_gets_no_fallback(self):
        result = self.invoke(None, github_token='fixture-caller')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['', 'api', 'fixture argument'])

    def test_launcher_without_broker_token_gets_no_fallback(self):
        result = self.invoke(None, launcher_active=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['', 'api', 'fixture argument'])

    def test_missing_fallback_fails_without_running_gh(self):
        result = self.invoke(None, token_exists=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')


@unittest.skipUnless(POSIX, 'requires a POSIX shell and python3')
class FirewallTests(unittest.TestCase):
    def test_installs_once_before_return_preserving_dns_and_other_rules(self):
        imds = '-A DOCKER-USER -d 169.254.169.254/32 -j DROP'
        wire = '-A DOCKER-USER -d 168.63.129.16/32 -p tcp -m tcp --dport '
        required = [imds, wire + '80 -j DROP', wire + '32526 -j DROP']
        unrelated = ['-A DOCKER-USER -d 168.63.129.16/32 -p udp --dport 53 -j ACCEPT',
                     '-A DOCKER-USER -j RETURN']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state.json'
            fake = root / 'iptables'
            # Store rules, chain existence and mutations. This executable models
            # only the iptables operations used by the hook; it changes no rules.
            fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
path = pathlib.Path(os.environ['FIREWALL_STATE'])
state = json.loads(path.read_text())
args = sys.argv[1:]
assert args.pop(0) == '-w'
args = [a for a in args if a != '-n']
op, chain, *rest = args
assert chain == 'DOCKER-USER'
rules = state['rules']
rule = '-A DOCKER-USER ' + ' '.join(rest)
if op == '-L':
    sys.exit(0 if state['exists'] else 1)
elif op == '-N':
    state['exists'] = True
elif op == '-S':
    print('-N DOCKER-USER')
    print('\\n'.join(rules), end='\\n' if rules else '')
    sys.exit(0)
elif op == '-C':
    sys.exit(0 if rule in rules else 1)
elif op == '-D':
    rules.remove(rule)
elif op == '-I':
    position = int(rest.pop(0))
    rules.insert(position - 1, '-A DOCKER-USER ' + ' '.join(rest))
else:
    raise AssertionError(args)
state['mutations'].append(args)
path.write_text(json.dumps(state))
''')
            fake.chmod(0o755)
            env = dict(os.environ, FIREWALL_STATE=str(state),
                       PATH=str(root) + os.pathsep + os.environ['PATH'])
            # Existing rules below RETURN and duplicates must move to the top.
            # Also cover the first daemon start, when the chain does not exist.
            for exists, initial in [(True, unrelated + [imds, imds, required[2]]),
                                    (False, [])]:
                with self.subTest(chain_exists=exists):
                    state.write_text(json.dumps({'exists': exists, 'rules': initial, 'mutations': []}))
                    subprocess.run(['sh', str(KIT / 'block-metadata.sh')], env=env, check=True)
                    first = json.loads(state.read_text())
                    self.assertEqual(first['rules'], required + (unrelated if exists else []))
                    # A second invocation must not rewrite rules or reset counters.
                    subprocess.run(['sh', str(KIT / 'block-metadata.sh')], env=env, check=True)
                    self.assertEqual(json.loads(state.read_text()), first)


def load_script(name, module):
    spec = importlib.util.spec_from_file_location(module, KIT / 'container' / name)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def fake_keep_host(bots=('discord', 'telegram'), helpers=True):
    # Stands in for hermes_cli.backup_keep_host of the pinned image: the bot
    # list and the blocks its gateway reads 'enabled' from (root, top level,
    # gateway, gateway.platforms), as gateway/config_loader.py merges them.
    module = types.ModuleType('hermes_cli.backup_keep_host')
    if helpers:
        module._bot_platforms = lambda: {name: (name.title(), ()) for name in bots}

        def platform_blocks(cfg, pid):
            gateway = cfg.get('gateway') if isinstance(cfg.get('gateway'), dict) else {}
            holders = (cfg.get('platforms'), cfg, gateway, gateway.get('platforms'))
            return [h[pid] for h in holders if isinstance(h, dict) and isinstance(h.get(pid), dict)]
        module._platform_blocks = platform_blocks
    package = types.ModuleType('hermes_cli')
    package.backup_keep_host = module
    return {'hermes_cli': package, 'hermes_cli.backup_keep_host': module}


@unittest.skipUnless(YAML, 'requires ruamel.yaml, as installed in the Hermes image')
class HermesTests(unittest.TestCase):
    NAMES = [
        'HERMES_MODEL', 'HERMES_DASHBOARD_PUBLIC_URL', 'DASHBOARD_GATEWAY',
        'OPENAI_API_KEY', 'API_SERVER_ENABLED', 'API_SERVER_HOST', 'API_SERVER_PORT',
        'API_SERVER_KEY', 'HERMES_DASHBOARD_BASIC_AUTH_USERNAME',
        'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD', 'HERMES_DASHBOARD_BASIC_AUTH_SECRET',
        'TERMINAL_ENV', 'TERMINAL_SSH_HOST', 'TERMINAL_SSH_USER',
        'TERMINAL_SSH_PORT', 'TERMINAL_SSH_KEY',
    ]

    def configure(self, home, policy=None, messaging_off=None, modules=None, profile_bots=None):
        # Run configure-hermes.py against a disposable home: only its three
        # absolute container paths change, and chown is recorded.
        script = load_script('configure-hermes.py', 'configure_hermes')
        script.HOME = home
        script.POLICY = policy or KIT / 'hermes-config.yaml'
        script.VALIDATOR = KIT / 'container/validate-hermes-config.py'
        env = {name: 'fixture-' + name.lower() for name in self.NAMES}
        if messaging_off is not None:
            env['KIT_MESSAGING_OFF'] = messaging_off
        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(patch.dict(os.environ, env))
            if messaging_off is None:
                os.environ.pop('KIT_MESSAGING_OFF', None)
            stack.enter_context(patch.dict(sys.modules, modules or {}))
            # bot_names() runs _bot_platforms() in a child as the Hermes user,
            # in a profile's own plugin scope for a profile
            # (test_bot_names_runs_hermes_as_its_own_user); here it calls the
            # fake module in-process. profile_bots: {profile name: extra ids,
            # or an exception for a listing that fails}.
            fake = (modules or {}).get('hermes_cli.backup_keep_host')
            listed = []

            def bot_names(profile=None):
                listed.append(None if profile is None else profile.name)
                extra = (profile_bots or {}).get(profile.name, ()) if profile is not None else ()
                if isinstance(extra, BaseException):
                    raise extra
                return sorted({*fake._bot_platforms(), *extra})
            script.bot_names = bot_names
            self.listed = listed
            chown = stack.enter_context(patch.object(os, 'chown', create=True))
            fchown = stack.enter_context(patch.object(os, 'fchown', create=True))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            script.main()
        self.env = env
        return chown, fchown

    def test_writes_safe_yaml_and_authoritative_env_with_private_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            chown, _ = self.configure(home)
            saved = YAML(typ='safe').load((home / 'config.yaml').read_text())
            self.assertEqual(saved['model']['default'], self.env['HERMES_MODEL'])
            self.assertEqual(saved['dashboard']['trusted_proxies'], [self.env['DASHBOARD_GATEWAY']])
            self.assertEqual(saved['terminal']['backend'], 'ssh')
            self.assertNotIn('platforms', saved)
            self.assertIn('API_SERVER_KEY=' + self.env['API_SERVER_KEY'], (home / '.env').read_text())
            self.assertEqual(chown.call_count, 2)
            if os.name == 'posix':
                for name in ('config.yaml', '.env'):
                    self.assertEqual((home / name).stat().st_mode & 0o777, 0o600)

    def test_policy_outside_the_baseline_stops_startup_before_writing(self):
        template = (KIT / 'hermes-config.yaml').read_text()
        self.assertIn('backend: ssh', template)
        for text in (template + 'hooks:\n  on_start: [id]\n', template.replace('backend: ssh', 'backend: local')):
            with self.subTest(text=text[-30:]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                policy = root / 'policy.yaml'
                policy.write_bytes(text.encode())
                with self.assertRaises(SystemExit) as error:
                    self.configure(root / 'home', policy)
                self.assertIn('rejected', str(error.exception))
                self.assertFalse((root / 'home/config.yaml').exists())

    def test_messaging_off_switches_every_platform_off_in_the_root(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            self.configure(home, messaging_off='1', modules=fake_keep_host())
            saved = YAML(typ='safe').load((home / 'config.yaml').read_text())
            self.assertEqual(saved['platforms'], {'discord': {'enabled': False}, 'telegram': {'enabled': False}})
            # A second start writes the same policy: the switch survives restarts.
            self.configure(home, messaging_off='1', modules=fake_keep_host())
            self.assertEqual(YAML(typ='safe').load((home / 'config.yaml').read_text()), saved)

    def test_messaging_off_fails_closed_without_the_hermes_helpers(self):
        for modules in (fake_keep_host(helpers=False), fake_keep_host(bots=())):
            with self.subTest(modules=sorted(vars(modules['hermes_cli.backup_keep_host']))), \
                 tempfile.TemporaryDirectory() as directory:
                home = Path(directory) / 'home'
                with self.assertRaisesRegex(SystemExit, 'KIT_MESSAGING_OFF'):
                    self.configure(home, messaging_off='1', modules=modules)
                self.assertFalse((home / 'config.yaml').exists())

    def test_messaging_off_accepts_only_one(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SystemExit, 'KIT_MESSAGING_OFF'):
                self.configure(Path(directory) / 'home', messaging_off='true', modules=fake_keep_host())

    @unittest.skipUnless(os.name == 'posix', 'directory descriptors (dir_fd) are POSIX only')
    def test_messaging_off_switches_every_profile_off_and_keeps_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            work, bare = home / 'profiles/work', home / 'profiles/bare'
            work.mkdir(parents=True)
            bare.mkdir()
            # A bot enabled in two merged blocks; a profile with only a .env token.
            (work / 'config.yaml').write_text('# operator note\nplatforms:\n  telegram:\n    enabled: true\n'
                                              '    home_channel: x\ngateway:\n  telegram:\n    enabled: true\n')
            os.chmod(work / 'config.yaml', 0o640)
            (bare / '.env').write_text('TELEGRAM_BOT_TOKEN=fixture\n')
            _, fchown = self.configure(home, messaging_off='1', modules=fake_keep_host())
            off = {'enabled': False}
            text = (work / 'config.yaml').read_text()
            self.assertIn('# operator note', text)
            saved = YAML(typ='safe').load(text)
            self.assertEqual(saved['platforms'], {'telegram': {**off, 'home_channel': 'x'}, 'discord': off})
            self.assertEqual(saved['gateway'], {'telegram': {}})
            self.assertEqual((work / 'config.yaml').stat().st_mode & 0o777, 0o640)
            self.assertEqual(YAML(typ='safe').load((bare / 'config.yaml').read_text()),
                             {'platforms': {'discord': off, 'telegram': off}})
            self.assertEqual(fchown.call_count, 2)
            self.assertEqual(sorted(path.name for path in work.iterdir()), ['config.yaml'])
            # Already off: the next start rewrites nothing.
            before = (work / 'config.yaml').stat().st_ino
            _, fchown = self.configure(home, messaging_off='1', modules=fake_keep_host())
            fchown.assert_not_called()
            self.assertEqual((work / 'config.yaml').stat().st_ino, before)

    @unittest.skipUnless(os.name == 'posix', 'directory descriptors (dir_fd) are POSIX only')
    def test_messaging_off_reaches_platforms_installed_only_in_a_profile(self):
        # A platform plugin installed in profiles/p/plugins and enabled there
        # is listed only in that profile's plugin scope, never the root's.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            profile = home / 'profiles/p'
            profile.mkdir(parents=True)
            (profile / 'config.yaml').write_text(
                'plugins:\n  enabled: [myplat]\nplatforms:\n  myplat:\n    enabled: true\n'
                'gateway:\n  platforms:\n    unlisted:\n      enabled: true\n')
            self.configure(home, messaging_off='1', modules=fake_keep_host(), profile_bots={'p': ('myplat',)})
            self.assertEqual(self.listed, [None, 'p'])
            saved = YAML(typ='safe').load((profile / 'config.yaml').read_text())
            off = {'enabled': False}
            # The profile's own platform, the root list, and (defence in
            # depth) every id that already has a block in that profile.
            self.assertEqual(saved['platforms'], {'myplat': off, 'discord': off, 'telegram': off, 'unlisted': off})
            self.assertEqual(saved['gateway'], {'platforms': {'unlisted': {}}})
            self.assertEqual(saved['plugins'], {'enabled': ['myplat']})
            root = YAML(typ='safe').load((home / 'config.yaml').read_text())
            self.assertNotIn('myplat', root['platforms'])

    @unittest.skipUnless(os.name == 'posix', 'directory descriptors (dir_fd) are POSIX only')
    def test_messaging_off_stops_startup_when_a_profile_listing_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            (home / 'profiles/p').mkdir(parents=True)
            (home / 'profiles/p/config.yaml').write_text('platforms:\n  telegram:\n    enabled: true\n')
            failure = SystemExit('KIT_MESSAGING_OFF: could not list the Hermes messaging platforms of profile p')
            with self.assertRaisesRegex(SystemExit, 'profile p'):
                self.configure(home, messaging_off='1', modules=fake_keep_host(), profile_bots={'p': failure})
            self.assertIn('enabled: true', (home / 'profiles/p/config.yaml').read_text())
            self.assertFalse((home / 'config.yaml').exists())

    @unittest.skipUnless(os.name == 'posix', 'symlinks and dir_fd are POSIX only')
    def test_messaging_off_refuses_linked_profiles_and_configs(self):
        for linked in ('profile', 'config'):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home, victim = root / 'home', root / 'victim'
                victim.mkdir()
                (victim / 'config.yaml').write_text('kept: true\n')
                (home / 'profiles').mkdir(parents=True)
                if linked == 'profile':
                    (home / 'profiles/evil').symlink_to(victim)
                else:
                    (home / 'profiles/evil').mkdir()
                    (home / 'profiles/evil/config.yaml').symlink_to(victim / 'config.yaml')
                with self.assertRaises((SystemExit, OSError)):
                    self.configure(home, messaging_off='1', modules=fake_keep_host())
                self.assertEqual((victim / 'config.yaml').read_text(), 'kept: true\n')
                self.assertFalse((home / 'config.yaml').exists())

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0, 'drops to the Hermes uid; needs root')
    def test_bot_names_runs_hermes_as_its_own_user(self):
        # _bot_platforms() seeds the home; whatever it creates must belong to
        # uid 10000, never root. A fake hermes_cli stands in for the image's.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o755)
            package, seed = root / 'hermes_cli', root / 'home'
            package.mkdir()
            seed.mkdir()
            os.chown(seed, 10000, 10000)
            (package / '__init__.py').write_text('')
            module = package / 'backup_keep_host.py'
            module.write_text('import os, pathlib\n'
                              'def _bot_platforms():\n'
                              '    pathlib.Path(os.environ["FIXTURE_SEED"], "SOUL.md").write_text("x")\n'
                              '    print("a log line")\n'
                              '    bots = {"telegram": ("Telegram", ()), "discord": ("Discord", ())}\n'
                              '    if os.environ["HERMES_HOME"].endswith("/profiles/p"):\n'
                              '        bots["myplat"] = ("My platform", ())\n'
                              '    return bots\n')
            script = load_script('configure-hermes.py', 'configure_hermes')
            with patch.dict(os.environ, PYTHONPATH=str(root), FIXTURE_SEED=str(seed), HERMES_HOME=str(seed)):
                self.assertEqual(script.bot_names(), ['discord', 'telegram'])
                self.assertEqual((seed / 'SOUL.md').stat().st_uid, 10000)
                # A profile is listed in its own plugin scope (HERMES_HOME).
                self.assertEqual(script.bot_names(seed / 'profiles/p'), ['discord', 'myplat', 'telegram'])
                module.write_text('def _bot_platforms():\n    raise RuntimeError("changed upstream")\n')
                with self.assertRaisesRegex(SystemExit, 'could not list'):
                    script.bot_names()

    def validate(self, *texts):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for number, text in enumerate(texts):
                paths.append(Path(directory) / f'policy-{number}.yaml')
                paths[-1].write_bytes(text.encode())
            return subprocess.run([sys.executable, '-B', str(KIT / 'container/validate-hermes-config.py'),
                                   *map(str, paths)], capture_output=True, text=True, check=False)

    def test_template_and_configure_hermes_dashboard_keys_are_accepted(self):
        template = (KIT / 'hermes-config.yaml').read_text()
        # Assert the edited line exists, so the replace cannot silently no-op.
        self.assertIn('trusted_proxies: []', template)
        dashboard = template.replace(
            'trusted_proxies: []', 'public_url: https://pilot.example.ts.net:9443\n  trusted_proxies: [172.30.99.1]')
        self.assertEqual(YAML(typ='safe').load(dashboard)['dashboard'],
                         {'public_url': 'https://pilot.example.ts.net:9443', 'trusted_proxies': ['172.30.99.1']})
        for text in (template, dashboard):
            with self.subTest(text=text[-60:]):
                result = self.validate(text)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_baseline_is_the_validators_own_not_the_files(self):
        # Restore used to pass the kit policy as both the policy and its own
        # template: a weakened file then matched itself. The baseline is now
        # fixed in the validator and the file is its only argument.
        template = (KIT / 'hermes-config.yaml').read_text()
        self.assertIn('backend: ssh', template)
        for text in (template + 'hooks:\n  on_start: [sh, -c, id]\n', template.replace('backend: ssh', 'backend: local')):
            with self.subTest(text=text[-30:]):
                result = self.validate(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('rejected', result.stderr)
                # The old two-argument form, the same file twice, is refused too.
                self.assertNotEqual(self.validate(text, text).returncode, 0)
        validator = load_script('validate-hermes-config.py', 'validate_hermes_config')
        self.assertEqual(YAML(typ='safe').load(template)['terminal'], validator.TERMINAL)

    def test_policy_weakening_sandbox_or_provider_is_rejected(self):
        template = (KIT / 'hermes-config.yaml').read_text()
        for old, new in [('backend: ssh', 'backend: local'), ('ssh_host: sandbox', 'ssh_host: attacker'),
                         ('env_passthrough: []', 'env_passthrough: [OPENAI_API_KEY]'),
                         ('base_url: https://api.openai.com/v1', 'base_url: https://proxy.example'),
                         ('  cwd: /workspace', '  cwd: /workspace\n  docker_image: attacker'),
                         ('  default: gpt-5-mini', '  default: gpt-5-mini\n  api_key_env: OTHER'),
                         ('trusted_proxies: []', 'trusted_proxies: []\n  oauth: {client_id: x}')]:
            with self.subTest(change=new):
                self.assertIn(old, template)
                result = self.validate(template.replace(old, new, 1))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('rejected', result.stderr)

    def test_policy_with_any_other_top_level_key_is_rejected(self):
        template = (KIT / 'hermes-config.yaml').read_text()
        for extra in ('mcp_servers:\n  evil: {command: sh, args: [-c, id]}\n', 'hooks:\n  on_start: [id]\n',
                      'plugins: [evil]\n', 'toolsets: [all]\n', 'platforms:\n  telegram:\n    enabled: true\n',
                      '_config_version: 99\n', '1: numeric\n'):
            with self.subTest(extra=extra.split(':')[0]):
                result = self.validate(template + extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('does not allow', result.stderr)

    def test_malformed_policy_is_rejected(self):
        for text in ('- a list\n', '!!python/object:os.system {}\n', 'model: {}\n'):
            with self.subTest(text=text):
                self.assertNotEqual(self.validate(text).returncode, 0)


if __name__ == '__main__':
    unittest.main()
