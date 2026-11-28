"""Bootstrap containment and interface regressions alongside the CLI Bats suite."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

BOOT = Path(__file__).resolve().parents[2] / 'bootstrap/bootstrap.py'
BASE = BOOT.parent.parent / 'terraform'
spec = importlib.util.spec_from_file_location('bootstrap', BOOT)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapContractTests(unittest.TestCase):
    def test_allowlist_equals_actual_trust_root_variables(self):
        import re
        for stack, allowed in (('trust', bootstrap.TRUST_VARIABLES), ('host', bootstrap.HOST_VARIABLES)):
            variables = {name for path in (BASE / 'stacks' / stack).glob('*.tf')
                         for name in re.findall(r'^variable "([^"]+)"', path.read_text(), re.M)}
            self.assertEqual(allowed, variables)

    def test_renderers_reject_source_symlink_escapes_before_execution(self):
        for selected in ('modules/host/main.tf', 'stacks/trust', 'modules/host'):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / 'source/terraform'
                shutil.copytree(BASE, source, ignore=shutil.ignore_patterns('.terraform'), symlinks=True)
                outside = root / 'outside'
                outside.mkdir()
                (outside / 'sentinel').write_text('unchanged')
                path = source / selected
                if path.is_dir():
                    shutil.rmtree(path)
                    path.symlink_to(outside, target_is_directory=True)
                else:
                    path.unlink()
                    path.symlink_to(outside / 'sentinel')
                private = root / 'private'
                private.mkdir()
                (private / 'config').mkdir()
                (private / 'config/instance.tfvars.json').write_text(json.dumps({'targets': {'primary': {'subscription_resource_id': '/subscriptions/' + str(__import__('uuid').uuid4())}}}))
                app = bootstrap.Bootstrap(bootstrap.parser().parse_args(['trust', '--apply', '--instance-dir', str(private)]))
                with patch.object(bootstrap, 'HERE', root / 'source/bootstrap'), patch.object(app, 'run') as run:
                    with self.assertRaisesRegex(bootstrap.Refusal, 'symlink escapes'):
                        app.tf('trust', 'init', change=True)
                    run.assert_not_called()
                self.assertEqual((outside / 'sentinel').read_text(), 'unchanged')
                self.assertEqual(list(outside.iterdir()), [outside / 'sentinel'])

    def test_private_parent_symlinks_cannot_write_outside_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private, outside = root / 'private', root / 'outside'
            private.mkdir(); outside.mkdir()
            (private / 'config').symlink_to(outside, target_is_directory=True)
            app = bootstrap.Bootstrap(bootstrap.parser().parse_args(['cleanup', '--apply', '--instance-dir', str(private)]))
            with self.assertRaisesRegex(bootstrap.Refusal, 'escapes instance'):
                app.save('config/receipt.json', {'success': True})
            self.assertEqual(list(outside.iterdir()), [])


class FederationAllowlistTests(unittest.TestCase):
    def test_bootstrap_reads_every_allowlist_environment(self):
        allow = json.loads((BASE / 'modules/trust/federation-allowlist.json').read_text())
        for identity, files in allow['identities'].items():
            for file, envs in files.items():
                ref = 'example-org/example-instance/.github/workflows/' + file + '@' + 'a' * 40
                self.assertEqual(bootstrap.envs_for(identity, ref), envs)
        for identity, file in (('ops', 'ph-deploy.yml'), ('mint', 'ph-ops.yml'), ('plan', 'sf-oidc-probe.yml')):
            with self.assertRaisesRegex(bootstrap.Refusal, identity + '.*' + file):
                bootstrap.envs_for(identity, 'example-org/example-instance/.github/workflows/' + file + '@' + 'a' * 40)

    def test_expansion_preserves_primary_keys_and_skips_gate_probe(self):
        ref = 'example-org/example-instance/.github/workflows/ph-restore.yml@' + 'a' * 40
        gate = ref.replace('ph-restore.yml', 'sf-merge-gate.yml')
        entries = list(bootstrap.credential_entries({'deploy': {'release': ref}, 'plan': {'gate': gate}}, probe=True))
        self.assertEqual(entries, [('deploy', 'release', ref, 'production'), ('deploy', 'release-restore', ref, 'restore')])


if __name__ == '__main__':
    unittest.main()
