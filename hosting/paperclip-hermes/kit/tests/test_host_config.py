"""Regression checks for repeatable config edits and fail-closed secret inputs.

Run: python3 -B -m unittest discover -s tests
These do not substitute for the Ubuntu bootstrap/reboot acceptance checks.
"""
import copy
import importlib.util
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('host_config', Path(__file__).parents[1] / 'host-config.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        # Posix path canonicalization runs on the actual host, while these
        # serialization checks also run on Windows without modifying /etc.
        self.patcher = patch.object(host, 'safe_path', side_effect=lambda p: p)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def desired(self, docker=None, text='version = 2\n'):
        with patch.object(host, 'docker_config', return_value=copy.deepcopy(docker or {})), \
             patch.object(host, 'containerd_config', return_value=(text, tomllib.loads(text))):
            return host.desired('/mnt/data')

    def test_replaces_an_indented_top_level_root(self):
        original = 'version = 2\n  root = "/var/lib/containerd"\n[plugins.example]\nroot = "/plugin/root"\n'
        _, text = self.desired(None, original)
        parsed = tomllib.loads(text)
        self.assertEqual(parsed['root'], '/mnt/data/containerd')
        self.assertEqual(parsed['plugins']['example']['root'], '/plugin/root')

    def test_preserves_unrelated_settings_and_plugin_roots(self):
        original = '# a comment with [brackets]\nversion = 2\nroot = "/var/lib/containerd"\n[plugins.example]\nroot = "/plugin/root"\nfeatures = ["one", "two"]\n'
        docker, text = self.desired({'dns': ['192.0.2.1']}, original)
        parsed = tomllib.loads(text)
        self.assertEqual(parsed['root'], '/mnt/data/containerd')
        self.assertEqual(parsed['plugins']['example']['root'], '/plugin/root')
        self.assertEqual(parsed['plugins']['example']['features'], ['one', 'two'])
        self.assertEqual(docker['dns'], ['192.0.2.1'])
        self.assertEqual(docker['data-root'], '/mnt/data/docker')
        self.assertEqual(docker['log-driver'], 'local')
        self.assertEqual(docker['log-opts'], {'max-size': '10m', 'max-file': '3'})

    def test_second_serialization_has_no_drift(self):
        first = self.desired()
        self.assertEqual(self.desired(*first), first)

    def test_second_write_does_not_touch_existing_files(self):
        files, writes = {}, []

        class MemoryPath:
            def __init__(self, name):
                self.name = name

            @property
            def parent(self):
                return self

            def exists(self):
                return self.name in files

            def read_text(self):
                return files[self.name]

            def write_text(self, content):
                files[self.name] = content
                writes.append(self.name)

            def mkdir(self, **kwargs):
                pass

        with patch.object(host, 'Path', MemoryPath), patch.object(host.os, 'chmod'):
            host.write_config('/mnt/data')
            self.assertEqual(len(writes), 2)
            self.assertFalse(host.changed('/mnt/data'))
            host.write_config('/mnt/data')
            self.assertEqual(len(writes), 2)

    def test_refuses_firewall_bypass_and_live_restore(self):
        for docker in [{'iptables': False}, {'firewall-backend': 'nftables'}, {'live-restore': True}]:
            with self.subTest(docker=docker), self.assertRaises(SystemExit):
                self.desired(docker)


class InputTests(unittest.TestCase):
    def valid_values(self):
        values = {}
        for number, name in enumerate([
            'POSTGRES_PASSWORD', 'BETTER_AUTH_SECRET', 'PAPERCLIP_AGENT_JWT_SECRET',
            'PAPERCLIP_TOOL_ACTION_SIGNING_SECRET', 'PAPERCLIP_SECRETS_MASTER_KEY',
            'HERMES_GATEWAY_KEY', 'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD',
            'HERMES_DASHBOARD_BASIC_AUTH_SECRET',
        ], start=1):
            # Synthetic test inputs, generated in memory; never usable credentials.
            values[name] = f'{number:064x}'
        values.update(OPENAI_API_KEY='fixture-test-only', TAILSCALE_HOSTNAME='fixture.test.ts.net',
                      FIXTURE_REPOSITORY='fixture-owner/fixture', HERMES_MODEL='fixture-model')
        return values

    def validate(self, values):
        text = '\n'.join(f'{key}={value}' for key, value in values.items())
        with patch.object(host, 'private_file') as checked, \
             patch.object(Path, 'read_text', autospec=True, return_value=text) as read:
            host.validate_env()
        self.assertEqual([call.args[0] for call in checked.call_args_list],
                         [Path('.secrets/compose.env'), '.secrets/github-token'])
        read.assert_called_once_with(Path('.secrets/compose.env'))

    def test_accepts_literal_independent_inputs(self):
        self.validate(self.valid_values())

    def test_rejects_reused_or_empty_secret(self):
        for value in ['', self.valid_values()['PAPERCLIP_AGENT_JWT_SECRET']]:
            values = self.valid_values()
            values['BETTER_AUTH_SECRET'] = value
            with self.subTest(value_is_empty=not value), self.assertRaises(SystemExit):
                self.validate(values)

    def test_rejects_interpolation_and_multiline_values(self):
        for value in ['${ANOTHER_KEY}', 'some value', "'quoted'", 'one\nTWO=three']:
            values = self.valid_values()
            values['OPENAI_API_KEY'] = value
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.validate(values)

    def test_rejects_example_host_and_repository(self):
        for name, value in [('TAILSCALE_HOSTNAME', 'pilot.tailnet.example'),
                            ('FIXTURE_REPOSITORY', 'owner/fixture-repository')]:
            values = self.valid_values()
            values[name] = value
            with self.subTest(name=name), self.assertRaises(SystemExit):
                self.validate(values)

    def test_messaging_off_setting_is_one_or_absent(self):
        values = self.valid_values()
        values['KIT_MESSAGING_OFF'] = '1'
        self.validate(values)
        for value in ('0', 'true', ''):
            values['KIT_MESSAGING_OFF'] = value
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.validate(values)

    def test_rejects_gateway_outside_dashboard_subnet(self):
        values = self.valid_values()
        values.update(DASHBOARD_SUBNET='172.30.99.0/24', DASHBOARD_GATEWAY='172.30.98.1')
        with self.assertRaises(SystemExit):
            self.validate(values)


if __name__ == '__main__':
    unittest.main()
