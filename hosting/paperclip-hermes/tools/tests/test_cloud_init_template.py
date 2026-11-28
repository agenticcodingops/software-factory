"""Offline checks of the creation-only bootstrap payload and Terraform render."""

import base64
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

BASE = Path(__file__).resolve().parents[2]
HOST = BASE / 'terraform/modules/host'
BOOT = HOST / 'bootstrap'
FIXTURE = Path(__file__).parent / 'fixtures/host-cloud-init.yaml'


class CloudInitTemplateTests(unittest.TestCase):
    def setUp(self):
        self.text = (BOOT / 'cloud-init.yaml.tftpl').read_text()
        self.config = yaml.safe_load(self.text)
        self.agent = (BOOT / 'sf-host-agent').read_bytes()

    def test_template_shape(self):
        self.assertTrue(self.text.startswith('#cloud-config\n'))
        self.assertEqual(re.findall(r'(?<!\$)\$\{[^}]+\}', self.text), ['${agent_gz_b64}'])
        self.assertNotIn('%{', self.text)
        self.assertLessEqual(len(self.agent), 24 * 1024)
        entry, = self.config['write_files']
        self.assertEqual(entry, {'path': '/usr/local/sbin/sf-host-agent', 'encoding': 'gz+b64',
                                 'content': '${agent_gz_b64}', 'owner': 'root:root', 'permissions': '0755'})
        self.assertTrue(self.agent.startswith(b'#!/usr/bin/python3 -I\n'))

    def test_delivery_key_created_only_when_absent(self):
        script, = self.config['runcmd']
        for command in ('install -d -m 0700 -o root -g root /etc/paperclip-hermes',
                        'chown root:root /etc/paperclip-hermes', 'chmod 0700 /etc/paperclip-hermes'):
            with self.subTest(command=command):
                self.assertLess(script.index(command), script.index('if [ -e'))
        for part in ('[ -e /etc/paperclip-hermes/delivery.key ] || [ -L /etc/paperclip-hermes/delivery.key ]',
                     'umask 077', 'tmp=$(mktemp /etc/paperclip-hermes/.delivery.XXXXXX)',
                     'trap \'rm -f "$tmp"\' EXIT', "trap 'exit 1' HUP INT TERM",
                     '/usr/bin/age-keygen >"$tmp" 2>/dev/null', 'chmod 0600 "$tmp"',
                     'chown root:root "$tmp"', 'ln "$tmp" /etc/paperclip-hermes/delivery.key', 'rm -f "$tmp"'):
            with self.subTest(part=part):
                self.assertIn(part, script)
        self.assertIn('set -eu', script)
        self.assertNotIn('mv -n', script)
        self.assertNotIn('.new', script)
        self.assertLess(script.index('chmod 0600'), script.index('ln "$tmp"'))

    def test_delivery_key_synced_before_publication(self):
        script, = self.config['runcmd']
        commands = ['chown root:root "$tmp"', 'sync "$tmp"',
                    'ln "$tmp" /etc/paperclip-hermes/delivery.key',
                    'sync /etc/paperclip-hermes', 'rm -f "$tmp"\n']
        positions = [script.index(command) for command in commands]
        self.assertEqual(positions, sorted(positions))
        # Execute the actual template shell against a private filesystem fixture.
        # Failure before publication leaves no key; failure after publication
        # leaves the already-synced key and cleans up the temporary in both cases.
        for failure in ('none', 'file', 'directory'):
            with self.subTest(sync_failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                identity = root / 'identity'
                identity.mkdir(mode=0o700)
                calls = root / 'calls'
                rewritten = script.replace('/etc/paperclip-hermes', str(identity))
                rewritten = rewritten.replace('/usr/bin/age-keygen', 'printf fixture-identity')
                prefix = ('chown() { :; }\n'
                          'sync() { printf "%s\n" "$1" >> ' + str(calls) + '; '
                          'if [ "' + failure + '" = file ] && [ -f "$1" ]; then return 1; fi; '
                          'if [ "' + failure + '" = directory ] && [ -d "$1" ]; then return 1; fi; }\n')
                run = subprocess.run(['/usr/bin/bash', '-c', prefix + rewritten],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                self.assertEqual(run.returncode, 0 if failure == 'none' else 1)
                key = identity / 'delivery.key'
                self.assertEqual(key.exists(), failure != 'file')
                if key.exists():
                    self.assertEqual(key.read_text(), 'fixture-identity')
                self.assertEqual(list(identity.glob('.delivery.*')), [])
                self.assertEqual(len(calls.read_text().splitlines()), 1 if failure == 'file' else 2)

    def test_age_identity_forbidden_in_receipt_bytes(self):
        pattern = re.search(r'forbidden_pattern\s*=\s*"([^"\n]+)"', (HOST / 'bootstrap.tf').read_text()).group(1)
        prefix = '-'.join(('AGE', 'SECRET', 'KEY', '1'))
        for value in (prefix, prefix.lower(), prefix.title()):
            with self.subTest(case=value):
                self.assertIsNotNone(re.search(pattern, '#cloud-config\n' + value + 'Q' * 58))

    def test_retained_disk_rebuild_probe_documents_undetected_filesystem(self):
        readme = (BOOT / 'README.md').read_text()
        storage = readme.split('## Storage and package pins', 1)[1].split('The age pin', 1)[0]
        for phrase in ('P19', 'blkid-undetected', 'backup superblock', 'can be reformatted',
                       'vm_size', 'NVMe', 'exit 6'):
            with self.subTest(requirement=phrase):
                self.assertIn(phrase, storage)

    def test_packages_pinned_exactly(self):
        packages = self.config['packages']
        self.assertEqual(packages, [['age', '1.1.1-1ubuntu0.24.04.3'], ['tailscale', '1.104.7']])
        for name, version in packages:
            with self.subTest(package=name):
                self.assertRegex(version, r'^\d[^ *<>=]+$')
        source = self.config['apt']['sources']['tailscale.list']
        self.assertEqual(source['source'], 'deb [signed-by=$KEY_FILE] https://pkgs.tailscale.com/stable/ubuntu noble main')
        readme = (BOOT / 'README.md').read_text()
        expected = re.search(r'armor has this SHA-256 checksum:\s*`([a-f0-9]{64})`', readme).group(1)
        self.assertEqual(hashlib.sha256(source['key'].encode()).hexdigest(), expected)
        self.assertNotIn('keyserver', source)
        self.assertNotIn('keyid', source)

    def test_no_fetch_or_kit_in_cloud_init(self):
        for value in ('sf-host-agent fetch', 'git ', 'sfctl', 'bootstrap.sh', 'tailscale up',
                      'kit_ref', 'kit_commit', 'tskey-', 'sig='):
            with self.subTest(value=value):
                self.assertNotIn(value, self.text)
        device = '/dev/disk/azure/scsi1/lun0'
        self.assertEqual(self.config['disk_setup'][device], {'table_type': 'gpt', 'layout': True, 'overwrite': False})
        self.assertEqual(self.config['fs_setup'], [{'label': 'sf-data', 'filesystem': 'ext4', 'device': device,
                                                  'partition': 'any', 'overwrite': False}])
        self.assertEqual(self.config['mounts'], [['LABEL=sf-data', '/data', 'ext4', 'defaults,nofail', '0', '2']])

    def test_forbidden_patterns_match_module_regex(self):
        text = (HOST / 'bootstrap.tf').read_text()
        pattern = re.search(r'forbidden_pattern\s*=\s*"([^"]+)"', text).group(1)
        variables = (HOST / 'variables.tf').read_text()
        self.assertIn('regexall(local.forbidden_pattern, try(base64decode(local.bootstrap_custom_data)', variables)
        self.assertIn('regexall(local.forbidden_pattern, file(', variables)
        self.assertIn('regexall(local.forbidden_pattern, local.rendered_template)', variables)
        for value in (self.text, self.agent.decode()):
            with self.subTest(source='template' if value == self.text else 'agent'):
                self.assertIsNone(re.search(pattern, value))

    def test_python_render_matches_terraform(self):
        # Terraform uses Go gzip; Python/zlib does not emit identical deflate
        # streams. Reuse the Terraform-recorded encoding only after independently
        # verifying that it decompresses to the current agent source. Then render
        # the entire template in Python and compare every byte with the fixture.
        rendered = FIXTURE.read_text()
        compressed = yaml.safe_load(rendered)['write_files'][0]['content']
        self.assertEqual(gzip.decompress(base64.b64decode(compressed, validate=True)), self.agent)
        python_render = self.text.replace('${agent_gz_b64}', compressed)
        self.assertEqual(python_render, rendered)
        self.assertLessEqual(len(base64.b64encode(python_render.encode())), 65536)
        contract = json.loads((BOOT / 'contract.json').read_text())
        self.assertEqual(contract['agent_sha256'], hashlib.sha256(self.agent).hexdigest())
