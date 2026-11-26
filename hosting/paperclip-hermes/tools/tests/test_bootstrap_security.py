"""Security review regressions: exercise real orchestration with isolated transports."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('security_bootstrap', Path(__file__).resolve().parents[2] / 'bootstrap/bootstrap.py')
b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b)
REF = 'sample/source/.github/workflows/sf-oidc-probe.yml@' + 'a' * 40


class BootstrapSecurityTests(unittest.TestCase):
    def setUp(self):
        b.OUTPUT_FAILED = False
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.app = b.Bootstrap(b.parser().parse_args(['init', '--apply', '--instance-dir', str(self.home), '--org', 'sample', '--instance-repo', 'instance']))
        self.app.config['operator_login'] = 'operator'

    def test_closed_output_cannot_skip_role_or_token_cleanup(self):
        class Closed(io.StringIO):
            def write(self, value):
                raise BrokenPipeError('closed terminal')
        self.app.ledger = [{'id': '/scope/providers/Microsoft.Authorization/roleAssignments/id', 'subscription': 'sub'}]
        self.app.token = 'tskey-api-id-value'
        def transport(args, **kwargs):
            output = b'[]' if args[0] == 'az' and 'list' in args else b'401' if '--write-out' in args else b'{}'
            return subprocess.CompletedProcess(args, 0, output, b'')
        with patch.object(b.subprocess, 'run', side_effect=transport), contextlib.redirect_stdout(Closed()):
            self.app.revoke_token()
            self.app.cleanup()
        self.assertIsNone(self.app.token)
        self.assertEqual(self.app.ledger, [])

    def test_repeated_signals_cannot_unwind_cleanup(self):
        handlers = {}
        completed = []
        def install(sig, handler):
            if handler == signal.SIG_IGN and handlers:
                # Deliver another signal in the first SIG_IGN installation.
                handlers[signal.SIGHUP](signal.SIGHUP, None)
            elif callable(handler):
                handlers[sig] = handler
        def operation():
            handlers[signal.SIGINT](signal.SIGINT, None)
        self.app.init = operation
        self.app.revoke_token = lambda: completed.append('token')
        self.app.cleanup = lambda: completed.append('roles')
        with patch.object(b, 'Bootstrap', return_value=self.app), patch.object(b.sys, 'argv', ['bootstrap', 'init', '--apply']), patch.object(b.signal, 'signal', side_effect=install), patch.object(b.signal, 'pthread_sigmask') as mask:
            with self.assertRaises(b.Refusal):
                b.main()
        self.assertEqual(completed, ['token', 'roles'])
        mask.assert_any_call(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})

    def test_children_receive_only_documented_environment(self):
        secrets = {'GH_TOKEN': 'secret', 'GITHUB_TOKEN': 'secret', 'GH_ENTERPRISE_TOKEN': 'secret', 'GITHUB_ENTERPRISE_TOKEN': 'secret', 'GH_HOST': 'wrong', 'OTHER_TOKEN': 'secret', 'ARM_CLIENT_SECRET': 'secret', 'TF_CLI_ARGS': '-bad', 'CURL_CA_BUNDLE': '/wrong'}
        with patch.dict(os.environ, secrets), patch.object(b.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{}', b'')) as run:
            for command in ('gh', 'curl', 'terraform', 'escrow', 'key-reader'):
                self.app.run(command)
                self.assertFalse(set(secrets) & set(run.call_args.kwargs['env']))
                self.assertIn('PATH', run.call_args.kwargs['env'])

    def test_age_pipeline_does_not_inherit_adapter_or_cloud_tokens(self):
        decrypt, encrypt = Mock(), Mock()
        decrypt.wait.return_value = 0
        encrypt.returncode = 0
        encrypt.communicate.return_value = (b'ciphertext', None)
        with patch.dict(os.environ, {'GH_TOKEN': 'secret', 'ARM_CLIENT_SECRET': 'secret', 'AZURE_CONFIG_DIR': '/private/cache'}), patch.object(b.subprocess, 'Popen', side_effect=[decrypt, encrypt]) as popen:
            self.assertEqual(self.app.reencrypt(b'archive', b'identity', 'age1recipient'), b'ciphertext')
        for call in popen.call_args_list:
            self.assertLessEqual(set(call.kwargs['env']), {'PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ'})

    def test_gh_verifies_expected_operator_before_write(self):
        with patch.object(self.app, 'run', return_value={'login': 'some-bot', 'type': 'Bot'}) as run:
            with self.assertRaisesRegex(b.Refusal, 'operator'):
                self.app.gh('orgs/sample/repos', 'POST', {})
            self.assertEqual(len(run.call_args_list), 1)
            self.assertIn('user', run.call_args.args)

    def test_admin_bypass_readback_is_required(self):
        def gh(path):
            env = path.split('/environments/')[1].split('/')[0]
            if path.endswith('deployment-branch-policies'):
                return {'branch_policies': [{'name': 'main', 'type': 'branch'}]}
            return {'deployment_branch_policy': None if env == 'plan' else {'protected_branches': False, 'custom_branch_policies': True}, 'can_admins_bypass': True,
                    'protection_rules': [{'type': 'required_reviewers', 'prevent_self_review': False, 'reviewers': [{'reviewer': {'id': 1}}]}] if env in ('production', 'restore') else []}
        with patch.object(self.app, 'gh', side_effect=gh):
            with self.assertRaisesRegex(b.Refusal, 'bypass'):
                self.app.check_environments([1])

    def test_var_files_are_private_json_and_unlinked(self):
        def run(*args, **kwargs):
            var = Path(next(a.split('=', 1)[1] for a in args if a.startswith('-var-file=')))
            self.assertEqual(var.suffix, '.json')
            self.assertEqual(var.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(var.read_bytes()), {'name': 'test'})
            self.assertIsNone(kwargs.get('data'))
            self.var = var
            return b''
        with patch.object(self.app, 'run', side_effect=run):
            self.app.tf('host', 'apply', variables={'name': 'test'}, change=True)
        self.assertFalse(self.var.exists())
        self.app.remove_tf_tree()
        self.assertFalse(list(self.home.glob('terraform-run-*')))

    def test_errored_state_is_reported_and_tree_removed(self):
        def run(*args, **kwargs):
            directory = Path(args[1].split('=', 1)[1])
            (directory / 'errored.tfstate').write_text('private-state')
            raise b.Refusal('terraform operation failed')
        with patch.object(self.app, 'run', side_effect=run):
            with self.assertRaisesRegex(b.Refusal, 'terraform state push'):
                self.app.tf('host', 'apply', change=True)
        self.app.remove_tf_tree()
        self.assertFalse(list(self.home.glob('terraform-run-*')))
        recovery = list(self.home.glob('recovery/*/errored.tfstate'))
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0].stat().st_mode & 0o777, 0o600)

    def test_teardown_preserves_and_refuses_fallback_state(self):
        def run(*args, **kwargs):
            directory = Path(args[1].split('=', 1)[1])
            (directory / 'errored.tfstate').write_text('unsaved-state')
            return b''
        with patch.object(self.app, 'run', side_effect=run):
            self.app.tf('host', 'apply', change=True)
        with self.assertRaisesRegex(b.Refusal, 'terraform state push'):
            self.app.remove_tf_tree()
        self.assertFalse(list(self.home.glob('terraform-run-*')))
        self.assertEqual(len(list(self.home.glob('recovery/*/errored.tfstate'))), 1)

    def test_main_always_removes_rendered_trees(self):
        self.app.init = Mock(side_effect=b.Refusal('failed'))
        self.app.revoke_token = Mock()
        self.app.cleanup = Mock(side_effect=b.Refusal('cleanup failed'))
        self.app.remove_tf_tree = Mock()
        with patch.object(b, 'Bootstrap', return_value=self.app), patch.object(b.sys, 'argv', ['bootstrap', 'init', '--apply']), patch.object(b.signal, 'signal'), patch.object(b.signal, 'pthread_sigmask'):
            with self.assertRaises(b.Refusal):
                b.main()
        self.app.remove_tf_tree.assert_called_once()

    def test_generated_codeowners_has_no_unowned_overrides(self):
        self.app.config['reviewer'] = 'first'
        self.app.a.probe_run = '1'
        files = {}
        def gh(path, method='GET', body=None, **kwargs):
            if path == 'orgs/sample':
                return {'plan': {'name': 'enterprise'}}
            if path.endswith('actions/permissions'):
                return {'enabled_repositories': 'all', 'allowed_actions': 'all'}
            if path.startswith('users/'):
                return {'id': 1}
            if path.startswith('orgs/sample/repos'):
                return [{'name': 'instance'}]
            if path == 'repos/sample/instance':
                return {'private': True, 'default_branch': 'main'}
            if path.endswith('deployment-branch-policies'):
                return {'branch_policies': [{'name': 'main', 'type': 'branch'}]}
            if '/environments/' in path:
                return {}
            if path.endswith('pending_deployments'):
                return [{'environment': {'name': e}, 'reviewers': [{'id': 1}]} for e in ('production', 'restore')]
            if path.endswith('/rulesets'):
                return []
            self.fail(path)
        with patch.object(self.app, 'gh', side_effect=gh), patch.object(self.app, 'put_file', side_effect=lambda p, c, **kw: files.update({p: c})), patch.object(self.app, 'publish_files', create=True), patch.object(self.app, 'setting'), patch.object(self.app, 'check_environments'), patch.object(self.app, 'probe_metadata', return_value={'id': 1}):
            self.app.init()
        for line in files['.github/CODEOWNERS'].splitlines():
            self.assertGreaterEqual(len(line.split()), 2, line)
        self.assertEqual(files['.github/CODEOWNERS'].splitlines()[-1], '.github/workflows/ph-*.yml @first')

    def test_probe_files_use_reviewed_pr_and_resume_nonce(self):
        files, branches, prs = {}, {}, []
        self.app.save('config/instance.tfvars.json', {'workflow_refs': {'plan': {'one': REF}}})
        def gh(path, method='GET', body=None, **kwargs):
            if '/git/trees/' in path:
                return {'tree': [{'path': k, 'sha': 'file'} for k in files], 'truncated': False}
            if '/git/ref/heads/main' in path:
                return {'object': {'sha': 'merged-sha'}}
            if '/git/matching-refs/' in path:
                return []
            if path.endswith('/git/refs'):
                branches[body['ref']] = body['sha']; return {}
            if '/contents/' in path:
                name = path.split('/contents/')[1].split('?')[0]
                if method == 'PUT':
                    self.assertNotEqual(body['branch'], 'main')
                    branches[name] = body['content']; return {}
                return {'content': files.get(name, branches.get(name)), 'sha': 'file'}
            if '/pulls' in path:
                if method == 'POST':
                    prs.append(body); return {'number': 1, 'html_url': 'https://example.test/pr/1'}
                return []
            if path.endswith('/dispatches'):
                return {}
            self.fail(path)
        with patch.object(self.app, 'gh', side_effect=gh):
            with self.assertRaisesRegex(b.Refusal, 'PR|pull request'):
                self.app.dispatch_probe()
            self.assertEqual(len(prs), 1)
            staged = base64.b64decode(branches['.github/workflows/sf-bootstrap-probe.yml'])
            files['.github/workflows/sf-bootstrap-probe.yml'] = branches['.github/workflows/sf-bootstrap-probe.yml']
            self.app.dispatch_probe()
        request = self.app.load('probe-request.json')
        self.assertIn(request['nonce'].encode(), staged)
        self.assertEqual(request['caller_sha'], 'merged-sha')

    def test_federation_filters_withdrawn_claims_for_all_consumers(self):
        claims = {'one': {'job_workflow_ref': REF, 'sub': 'prefix:environment:plan:job_workflow_ref:' + REF}, 'removed': {'job_workflow_ref': REF, 'sub': 'stale'}}
        self.app.save('config/receipts/oidc.json', {'sub_claim_prefix': 'prefix', 'claims': {'plan': claims}})
        self.app.save('config/instance.tfvars.json', {'workflow_refs': {'plan': {'one': REF}}})
        with patch.object(self.app, 'gate_check'):
            receipt, _ = self.app.federation()
        self.assertEqual(set(receipt['claims']['plan']), {'one'})

    def test_new_static_callers_parse_with_jobs_after_mapping_key(self):
        import yaml
        refs = {w: 'sample/source/.github/workflows/' + w + '.yml@' + 'a' * 40 for w in ('ph-ops', 'ph-power', 'ph-cost-guard', 'ph-backup-verify')}
        def gh(path):
            return {'tree': [], 'truncated': False} if '/git/trees/' in path else [{'name': 'v1.0.0', 'commit': {'sha': 'a' * 40}}]
        with patch.object(self.app, 'gh', side_effect=gh):
            self.app.ops_callers('primary', refs, False)
        for workflow, ref in refs.items():
            caller = yaml.safe_load((self.home / '.github/workflows' / (workflow + '.yml')).read_text())
            self.assertEqual(caller['jobs']['target-primary']['uses'], ref)

    def test_revoke_grants_backend_data_access_before_init(self):
        backend = {'subscription_id': 'sub', 'storage_account_name': 'state', 'resource_group_name': 'vault', 'tenant_id': 'tenant', 'key': 'trust.tfstate'}
        self.app.config['backends'] = {'trust': backend}
        events = []
        def tf(*args, **kwargs):
            events.append(args)
            return b'module.trust.azurerm_role_assignment.operator["backups"]\n' if 'list' in args and not any('destroy' in e for e in events) else b''
        with patch.object(self.app, 'storage_grant', side_effect=lambda *a: events.append(('grant', *a))), patch.object(self.app, 'az', side_effect=lambda *a: events.append(('az', *a))), patch.object(self.app, 'backend', side_effect=lambda *a: events.append(('backend', *a))), patch.object(self.app, 'tf', side_effect=tf):
            self.app.revoke()
        self.assertEqual(events[0], ('grant', '/subscriptions/sub/resourceGroups/vault/providers/Microsoft.Storage/storageAccounts/state/blobServices/default/containers/tfstate', 'Storage Blob Data Contributor', 'sub'))
        self.assertEqual(events[1][:4], ('az', 'storage', 'blob', 'list'))
        self.assertEqual(events[2][0], 'backend')

    def test_state_dry_runs_never_pull_or_list_uninitialized_backend(self):
        self.app.a.apply = False
        self.app.config['backends'] = {'trust': {'storage_account_name': 'state', 'resource_group_name': 'vault', 'subscription_id': 'sub', 'tenant_id': 'tenant', 'key': 'trust.tfstate'}}
        with patch.object(self.app, 'tf', return_value=b'') as tf, patch.object(self.app, 'storage_grant'):
            self.app.revoke()
        self.assertFalse(any('state' in c.args[1:] for c in tf.call_args_list))


if __name__ == '__main__':
    unittest.main()
