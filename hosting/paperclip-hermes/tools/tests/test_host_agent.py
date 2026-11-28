"""Offline bootstrap boundary tests: absolute tools and a local Git remote only."""

import ast
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import time
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[2]
BOOT = BASE / 'terraform/modules/host/bootstrap'
AGENT = BOOT / 'sf-host-agent'


class HostAgentTests(unittest.TestCase):
    def setUp(self):
        loader = importlib.machinery.SourceFileLoader('host_agent', str(AGENT))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.agent = importlib.util.module_from_spec(spec)
        loader.exec_module(self.agent)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.uid = os.getuid()
        self.agent_path = self.root / 'sf-host-agent'
        self.agent_path.write_bytes(AGENT.read_bytes())
        self.agent_path.chmod(0o755)
        self.install = self.root / 'install'
        self.state = self.root / 'state'
        self.key_dir = self.root / 'identity'
        self.key_dir.mkdir(mode=0o700)
        self.key = self.key_dir / 'delivery.key'
        self.data = Path('/data')
        self.secret = '-'.join(('AGE', 'SECRET', 'KEY', '1' + 'Q' * 58)).encode()
        self.native = 'age1' + 'a' * 58
        self.age = self.stub('age-keygen', "import sys\nassert sys.argv[1:] == ['-y']\nsys.stdin.buffer.read()\nprint(" + repr(self.native) + ")\n")
        self.cloud = self.stub('cloud-init', 'print(\'{"status":"done","extended_status":"done"}\')\n')
        # An accidental Python network request is an immediate test failure.
        patch = mock.patch.object(socket, 'socket', side_effect=AssertionError('network in test'))
        patch.start()
        self.addCleanup(patch.stop)

    def stub(self, name, body):
        path = self.root / name
        path.write_text('#!/usr/bin/python3\n' + body)
        path.chmod(0o755)
        return str(path)

    def private_key(self):
        self.key.write_bytes(self.secret)
        self.key.chmod(0o600)

    def capture(self, function, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = function(*args, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def recipient(self, uid=None):
        return self.capture(self.agent.recipient, self.key, self.key_dir,
                            self.uid if uid is None else uid, self.age)

    def git(self, *args, cwd=None):
        env = self.agent.cleared_env(self.root)
        return subprocess.run(['/usr/bin/git', '-c', 'protocol.file.allow=always',
                               '-c', 'user.name=Fixture Operator', '-c', 'user.email=' + 'operator@example.com',
                               *map(str, args)], cwd=cwd, env=env, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout.decode().strip()

    def repository(self):
        source = self.root / 'source'
        source.mkdir()
        self.git('init', source)
        kit = source / self.agent.KIT
        kit.mkdir(parents=True)
        self.marker = self.root / 'marker'
        (kit / 'bootstrap.sh').write_text('#!/usr/bin/bash\nset -eu\nprintf "%s\\n" "$PWD" "$@" > ' + str(self.marker) + '\n')
        (kit / 'sfctl').write_text('#!/usr/bin/bash\nexit 0\n')
        (source / '.gitignore').write_text('.secrets/\n')
        (source / 'link').symlink_to('hosting/paperclip-hermes/kit/sfctl')
        for path in source.rglob('*'):
            if '.git' not in path.parts and not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)
        self.git('add', '.', cwd=source)
        self.git('commit', '-m', 'fixture: initial kit', cwd=source)
        self.sha = self.git('rev-parse', 'HEAD', cwd=source)
        self.git('tag', 'v1.0.0', cwd=source)
        (source / 'second').write_text('second\n')
        (source / 'second').chmod(0o644)
        self.git('add', '.', cwd=source)
        self.git('commit', '-m', 'fixture: second kit', cwd=source)
        self.annotated = self.git('rev-parse', 'HEAD', cwd=source)
        self.git('tag', '-a', 'v1.0.1', '-m', 'release', cwd=source)
        self.tag_object = self.git('rev-parse', 'v1.0.1', cwd=source)
        (source / 'third').write_text('third\n')
        self.git('add', '.', cwd=source)
        self.git('commit', '-m', 'fixture: untagged', cwd=source)
        self.untagged = self.git('rev-parse', 'HEAD', cwd=source)
        self.git('tag', 'candidate', cwd=source)
        bare = self.root / 'remote.git'
        self.git('clone', '--bare', source, bare)
        self.git('config', 'uploadpack.allowAnySHA1InWant', 'true', cwd=bare)
        self.remote = bare.as_uri()
        self.source = source
        self.destination = self.install / ('kit-' + self.sha)
        return self.sha

    def fetch(self, sha=None, prepare=False, git_tool='/usr/bin/git'):
        return self.capture(self.agent.fetch, self.sha if sha is None else sha, prepare,
                            self.install, self.state, self.key, self.key_dir, self.data,
                            self.uid, git_tool, '/usr/bin/bash', self.remote, 'file')

    def wrapper(self, mode):
        # Only verification faults use a Git wrapper. All fetches still use the local remote.
        return self.stub('git-wrapper', '''import os, sys
args = sys.argv[1:]
if args[-2:] == ['rev-parse', 'HEAD'] and MODE == 'mismatch':
    print('b' * 40); sys.exit(0)
if args[-2:] == ['rev-parse', '--show-object-format'] and MODE == 'format':
    print('sha256'); sys.exit(0)
if 'status' in args and MODE == 'dirty':
    print(' M hosting/paperclip-hermes/kit/bootstrap.sh'); sys.exit(0)
os.execv('/usr/bin/git', ['/usr/bin/git'] + args)
'''.replace("args =", 'MODE = ' + repr(mode) + '\nargs =', 1))

    @contextlib.contextmanager
    def main_paths(self, lock=None):
        paths = {'AGENT': self.agent_path, 'SBIN': self.root, 'PYTHON': Path(self.stub('python3', 'pass')),
                 'GIT': self.stub('git', 'pass'), 'BASH': self.stub('bash', 'pass'), 'AGE': self.age,
                 'CLOUD_INIT': self.cloud, 'INSTALL': self.install, 'STATE': self.state,
                 'KEY': self.key, 'KEY_DIR': self.key_dir, 'DATA': self.data,
                 'LOCK': lock or self.root / 'agent.lock'}
        with contextlib.ExitStack() as stack:
            for name, value in paths.items():
                stack.enter_context(mock.patch.object(self.agent, name, value))
            # Main always supplies uid 0 in production; use this user's uid for
            # fixture ownership without sudo or chown.
            runtime = self.agent.require_runtime
            stack.enter_context(mock.patch.object(self.agent, 'require_runtime',
                side_effect=lambda agent, sbin, interpreter, tools, expected, effective:
                    runtime(agent, sbin, interpreter, tools, self.uid, self.uid)))
            status, lock_fn = self.agent.status, self.agent.exclusive_lock
            stack.enter_context(mock.patch.object(self.agent, 'status', side_effect=
                lambda agent, install, state, key, parent, data, uid, cloud, *tools:
                    status(agent, install, state, key, parent, data, self.uid, cloud, *tools)))
            stack.enter_context(mock.patch.object(self.agent, 'exclusive_lock', side_effect=
                lambda path, uid: lock_fn(path, self.uid)))
            yield

    def test_status_missing_tools_remains_diagnostic(self):
        for missing in ('age-keygen', 'git', 'bash', 'cloud-init'):
            for cloud_state in ('running', 'error'):
                with self.subTest(missing=missing, cloud=cloud_state):
                    self.cloud = self.stub('cloud-init', 'print(' + repr(json.dumps({'status': cloud_state})) + ')')
                    with self.main_paths(), mock.patch.object(self.agent,
                            {'age-keygen': 'AGE', 'git': 'GIT', 'bash': 'BASH', 'cloud-init': 'CLOUD_INIT'}[missing],
                            str(self.root / 'absent' / missing)):
                        code, out, _ = self.capture(self.agent.main, ['status'])
                    self.assertEqual(code, 0)
                    value = json.loads(out)
                    self.assertEqual(value['tools'][missing], 'missing')
                    self.assertEqual(value['cloud_init'], 'unknown' if missing == 'cloud-init' else cloud_state)
                    self.assertEqual(value['delivery_key'], 'missing')

    def test_commands_require_only_used_tools(self):
        for command, tools in ((['recipient'], ('age-keygen',)),
                               (['fetch', 'a' * 40], ('git',)),
                               (['fetch', 'a' * 40, '--prepare-only'], ('git', 'bash'))):
            with self.subTest(command=command), self.main_paths(), \
                    mock.patch.object(self.agent, 'require_runtime') as runtime, \
                    mock.patch.object(self.agent, 'recipient', return_value=0), \
                    mock.patch.object(self.agent, 'fetch', return_value=0):
                self.assertEqual(self.agent.main(command), 0)
                self.assertEqual(tuple(Path(tool).name for tool in runtime.call_args.args[3]), tools)

    def test_status_rejects_existing_unsafe_tools(self):
        original = Path(self.age).read_text()
        for variant in ('writable', 'owner', 'escape', 'dangling', 'directory'):
            with self.subTest(variant=variant):
                tool = Path(self.age)
                tool.unlink()
                if variant in ('escape', 'dangling'):
                    tool.symlink_to('/usr/bin/bash' if variant == 'escape' else self.root / 'missing-target')
                elif variant == 'directory':
                    tool.mkdir()
                else:
                    tool.write_text(original)
                    tool.chmod(0o775 if variant == 'writable' else 0o755)
                with self.main_paths():
                    if variant == 'owner':
                        with mock.patch.object(self.agent, 'safe_path', side_effect=self.agent.Failure(10)):
                            code, out, _ = self.capture(self.agent.main, ['status'])
                    else:
                        code, out, _ = self.capture(self.agent.main, ['status'])
                self.assertEqual((code, out), (10, ''))
                if tool.is_dir() and not tool.is_symlink():
                    tool.rmdir()
                    tool.write_text(original)
                elif tool.is_symlink():
                    tool.unlink()
                    tool.write_text(original)
                tool.chmod(0o755)

    def test_recipient_requires_private_key(self):
        for variant in ('missing', 'symlink', 'fifo', 'directory', 'mode', 'uid', 'empty', 'parent'):
            with self.subTest(variant=variant):
                self.key_dir.chmod(0o700)
                if self.key.is_dir():
                    self.key.rmdir()
                else:
                    self.key.unlink(missing_ok=True)
                if variant == 'symlink':
                    self.key.symlink_to(self.root / 'absent')
                elif variant == 'fifo':
                    os.mkfifo(self.key)
                elif variant == 'directory':
                    self.key.mkdir()
                elif variant != 'missing':
                    self.private_key()
                if variant == 'mode':
                    self.key.chmod(0o640)
                if variant == 'empty':
                    self.key.write_bytes(b'')
                if variant == 'parent':
                    self.key_dir.chmod(0o755)
                code, out, _ = self.recipient(self.uid + 1 if variant == 'uid' else self.uid)
                self.assertEqual(code, 3)
                self.assertEqual(out, '')
                if variant == 'missing':
                    self.assertFalse(os.path.lexists(self.key))

    def test_recipient_prints_one_native_recipient(self):
        self.private_key()
        self.assertEqual(self.recipient(), (0, self.native + '\n', ''))
        for body in ('print("plugin")', *(f'print({value!r})' for value in (
                'age1abc1' + 'a' * 54, *("age1" + c * 58 for c in '1bio'))),
                     'raise SystemExit(1)', 'import time; time.sleep(3)'):
            with self.subTest(body=body):
                self.age = self.stub('age-fault', body)
                run = subprocess.run
                def short_timeout(*args, **kwargs):
                    self.assertEqual(kwargs['timeout'], 30)
                    kwargs['timeout'] = 0.05 if 'time.sleep' in body else 30
                    return run(*args, **kwargs)
                with mock.patch.object(self.agent.subprocess, 'run', side_effect=short_timeout):
                    self.assertEqual(self.recipient()[:2], (3, ''))

    def test_recipient_drops_stderr(self):
        self.private_key()
        self.age = self.stub('age-noisy', 'import sys\nsys.stderr.buffer.write(' + repr(self.secret) + ')\nprint(' + repr(self.native) + ')\n')
        code, out, err = self.recipient()
        self.assertEqual(code, 0)
        for text in (out, err, (self.state / 'last.json').read_text() if self.state.exists() else ''):
            with self.subTest(text=text):
                self.assertNotIn(self.secret.decode(), text)

    def test_agent_never_creates_key(self):
        tree = ast.parse(AGENT.read_text())
        functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        recipient = ast.unparse(functions['recipient'])
        self.assertIn("[age_tool, '-y']", recipient)
        self.assertNotIn('O_CREAT', ast.unparse(functions['private_descriptor']))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                with self.subTest(call=ast.unparse(node)):
                    if node.func.attr == 'open' and any('key' in ast.unparse(a).lower() for a in node.args):
                        self.assertNotIn('O_CREAT', ast.unparse(node))
                    if node.func.attr in ('write_text', 'write_bytes', 'touch'):
                        self.assertNotRegex(ast.unparse(node.func.value), r'(?i)key')
        self.assertEqual(AGENT.read_text().count("[age_tool, '-y']"), 1)

    def test_fetch_rejects_bad_sha(self):
        self.remote = 'file:///unused'
        for sha in ('a', 'A' * 40, 'a' * 41, 'a' * 40 + '\n', '--help'):
            with self.subTest(sha=sha), mock.patch.object(self.agent.subprocess, 'run') as run:
                self.assertEqual(self.fetch(sha)[0], 2)
                run.assert_not_called()
                self.assertFalse(self.state.exists())

    def test_fetch_requires_release_tag(self):
        self.repository()
        for sha in (self.untagged, self.tag_object):
            with self.subTest(sha=sha):
                self.assertEqual(self.fetch(sha)[0], 4)
                self.assertFalse((self.install / ('kit-' + sha)).exists())
                self.assertEqual(list(self.install.glob('.kit-*')), [])
                self.assertFalse(self.marker.exists())
        for name, value in (
                ('duplicate', self.sha + '\trefs/tags/v1.0.0\n' + self.sha + '\trefs/tags/v1.0.0\n'),
                ('short-version', self.sha + '\trefs/tags/v1.0\n'),
                ('candidate', self.sha + '\trefs/tags/candidate\n')):
            with self.subTest(refs=name):
                wrapper = self.stub('git-refs', 'import os, sys\n'
                    'if "ls-remote" in sys.argv:\n    print(' + repr(value) + ', end=""); sys.exit(0)\n'
                    'os.execv("/usr/bin/git", ["/usr/bin/git"] + sys.argv[1:])\n')
                self.private_key()
                with mock.patch.object(self.agent.os.path, 'ismount', return_value=True):
                    self.assertEqual(self.fetch(prepare=True, git_tool=wrapper)[0], 4)
                self.assertFalse(self.destination.exists())
                self.assertEqual(list(self.install.glob('.kit-*')), [])
                self.assertFalse(self.marker.exists())
        # Both lightweight and peeled annotated release commits are accepted.
        for sha in (self.sha, self.annotated):
            with self.subTest(accepted=sha):
                self.assertEqual(self.fetch(sha)[0], 0)

    def test_fetch_verifies_before_execution(self):
        self.repository()
        for mode in ('mismatch', 'dirty', 'format'):
            with self.subTest(mode=mode):
                self.assertEqual(self.fetch(prepare=True, git_tool=self.wrapper(mode))[0], 4)
                self.assertFalse(self.destination.exists())
                self.assertEqual(list(self.install.glob('.kit-*')), [])
                self.assertFalse(self.marker.exists())

    def test_checkout_permissions(self):
        self.repository()
        for mask in (0o000, 0o077):
            with self.subTest(umask=mask):
                previous = os.umask(mask)
                try:
                    self.assertEqual(self.fetch()[0], 0)
                    self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o755)
                    tracked = self.git('ls-files', '-z', cwd=self.destination).split('\0')
                    for name in filter(None, tracked):
                        self.assertFalse((self.destination / name).stat().st_mode & 0o022)
                    self.assertEqual(os.umask(mask), mask)
                finally:
                    os.umask(previous)
                shutil.rmtree(self.destination)
        self.assertEqual(self.fetch()[0], 0)
        (self.destination / '.gitignore').chmod(0o664)
        self.assertEqual(self.fetch()[0], 4)
        # Unsafe state cannot be journaled; still fail verification before Git.
        for uid, mode in ((self.uid + 1, 0o700), (self.uid, 0o770)):
            with self.subTest(state_uid=uid, state_mode=mode):
                self.state.chmod(mode)
                with mock.patch.object(self.agent.subprocess, 'run') as run:
                    code, out, _ = self.capture(self.agent.fetch, self.sha, False, self.install,
                                               self.state, self.key, self.key_dir, self.data, uid,
                                               '/usr/bin/git', '/usr/bin/bash', self.remote, 'file')
                self.assertEqual((code, out), (4, ''))
                run.assert_not_called()
        self.state.chmod(0o700)

    def test_prepare_timeout_releases_child_lock(self):
        self.repository()
        self.private_key()
        child_lock, child_pid = self.root / 'child.lock', self.root / 'child.pid'
        child = self.root / 'child.py'
        child.write_text('import fcntl, os, signal, time\n'
                         'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                         'lock = open(' + repr(str(child_lock)) + ', "w")\n'
                         'fcntl.flock(lock, fcntl.LOCK_EX)\n'
                         'open(' + repr(str(child_pid)) + ', "w").write(str(os.getpid()))\n'
                         'time.sleep(60)\n')
        bootstrap = self.source / self.agent.KIT / 'bootstrap.sh'
        bootstrap.write_text('#!/usr/bin/bash\ntrap "" TERM\n/usr/bin/python3 ' + str(child) + ' &\nwait\n')
        self.git('add', '.', cwd=self.source)
        self.git('commit', '-m', 'fixture: child holds lock', cwd=self.source)
        self.sha = self.git('rev-parse', 'HEAD', cwd=self.source)
        self.git('tag', 'v2.0.0', cwd=self.source)
        self.git('push', self.remote, 'HEAD', 'refs/tags/v2.0.0', cwd=self.source)
        run, wait, killpg = subprocess.run, subprocess.Popen.wait, os.killpg
        def short_run(*args, **kwargs):
            if kwargs.get('timeout') == 3600:
                kwargs['timeout'] = 0.5
            return run(*args, **kwargs)
        def short_wait(process, timeout=None):
            return wait(process, timeout=0.5 if timeout == 3600 else 0.1 if timeout == 5 else timeout)
        try:
            with mock.patch.object(self.agent.os.path, 'ismount', return_value=True), \
                    mock.patch.object(self.agent.subprocess, 'run', side_effect=short_run), \
                    mock.patch.object(subprocess.Popen, 'wait', autospec=True, side_effect=short_wait), \
                    mock.patch.object(self.agent.os, 'killpg', wraps=killpg) as signals:
                self.assertEqual(self.fetch(prepare=True)[0], 7)
            self.assertTrue(child_pid.exists(), 'fixture child never acquired its lock')
            import fcntl
            with child_lock.open('r') as lock:
                deadline = time.monotonic() + 2
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.fail('bootstrap child kept the lock after timeout')
                        time.sleep(0.01)
            self.assertEqual([call.args[1] for call in signals.call_args_list], [signal.SIGTERM, signal.SIGKILL])
            self.assertEqual(json.loads((self.state / 'last.json').read_text())['exit'], 7)
            self.assertFalse((self.install / 'current').exists())
        finally:
            if child_pid.exists():
                try:
                    os.kill(int(child_pid.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_fetch_journals_malformed_checkout_metadata(self):
        self.repository()
        self.assertEqual(self.fetch()[0], 0)
        record = self.state / 'checkouts' / (self.sha + '.json')
        original = record.read_text()
        for value in ([], None, 1, 'text'):
            with self.subTest(record=value):
                (self.state / 'last.json').unlink(missing_ok=True)
                record.write_text(json.dumps(value))
                code, out, _ = self.fetch(prepare=True)
                self.assertEqual((code, out), (4, ''))
                self.assertEqual(json.loads((self.state / 'last.json').read_text())['exit'], 4)
                self.assertFalse(self.marker.exists())
        record.write_text(original)
        # The tracked link remains unchanged; its ignored target loops. Force
        # Python 3.12's RuntimeError as well on interpreters that use OSError.
        link = self.destination / 'link'
        original_resolve = Path.resolve
        for variant in ('actual-loop', 'python312'):
            with self.subTest(loop=variant):
                (self.state / 'last.json').unlink(missing_ok=True)
                def resolve(path, *args, **kwargs):
                    if path == link:
                        raise RuntimeError('symlink loop')
                    return original_resolve(path, *args, **kwargs)
                if variant == 'actual-loop':
                    # Keep Git clean so resolution is the failing operation.
                    target = self.destination / 'loop'
                    target.symlink_to('loop')
                    link.unlink()
                    link.symlink_to('loop')
                    original_git = self.agent.git_call
                    def clean(tool, args, *rest, **kwargs):
                        if args[0] == 'status':
                            return b''
                        return original_git(tool, args, *rest, **kwargs)
                    patch = mock.patch.object(self.agent, 'git_call', side_effect=clean)
                else:
                    patch = mock.patch.object(Path, 'resolve', resolve)
                try:
                    with patch:
                        self.assertEqual(self.fetch(prepare=True)[:2], (4, ''))
                    self.assertEqual(json.loads((self.state / 'last.json').read_text())['exit'], 4)
                    self.assertFalse(self.marker.exists())
                finally:
                    if variant == 'actual-loop':
                        target.unlink()
                        link.unlink()
                        link.symlink_to('hosting/paperclip-hermes/kit/sfctl')

    def test_successful_activation_reports_journal_failure_separately(self):
        self.repository()
        self.private_key()
        atomic = self.agent.atomic_json
        for failure in (OSError('journal unavailable'), self.agent.Failure(10)):
            with self.subTest(failure=type(failure).__name__):
                def fail_journal(path, *args):
                    if path.name == 'last.json':
                        raise failure
                    return atomic(path, *args)
                with mock.patch.object(self.agent, 'atomic_json', side_effect=fail_journal), \
                        mock.patch.object(self.agent.os.path, 'ismount', return_value=True):
                    code, out, _ = self.fetch(prepare=True)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out), {'fetched': self.sha,
                    'path': str(self.destination / self.agent.KIT), 'prepared': True, 'journal_error': True})
                self.assertEqual((self.install / 'current').resolve(), self.destination / self.agent.KIT)
                self.assertTrue(self.marker.exists())

    def test_tracked_link_checks_ignored_target_parents(self):
        self.repository()
        self.assertEqual(self.fetch()[0], 0)
        kit = self.destination / self.agent.KIT
        hidden = kit / '.secrets'
        hidden.mkdir(mode=0o700)
        target = hidden / 'nested'
        target.mkdir(mode=0o700)
        (target / 'file').write_text('fixture\n')
        (target / 'file').chmod(0o600)
        link = self.destination / 'link'
        link.unlink()
        link.symlink_to((target / 'file').relative_to(self.destination))
        for parent in (hidden, target):
            for mode in (0o770, 0o707):
                with self.subTest(parent=parent.name, mode=mode):
                    parent.chmod(mode)
                    with self.assertRaises(self.agent.Failure) as failure:
                        self.agent.ownership_walk(self.destination, b'link\0', self.uid)
                    self.assertEqual(failure.exception.code, 4)
                    parent.chmod(0o700)
        self.agent.ownership_walk(self.destination, b'link\0', self.uid)

    def test_prepare_runs_after_verification(self):
        self.repository()
        self.private_key()
        with mock.patch.object(self.agent.os.path, 'ismount', return_value=True):
            self.assertEqual(self.fetch(prepare=True)[0], 0)
        kit = self.destination / self.agent.KIT
        self.assertEqual(self.marker.read_text().splitlines(), [str(kit), '--non-interactive', '--prepare-only', '/data'])
        self.assertEqual((self.install / 'current').resolve(), kit)
        self.assertEqual(stat.S_IMODE((self.state / 'last.json').stat().st_mode), 0o600)
        self.assertEqual(json.loads((self.state / 'last.json').read_text())['phase'], 'prepared')
        # Kit-private ignored state remains valid on a subsequent verification.
        (kit / '.secrets').mkdir(mode=0o700)
        (kit / '.secrets' / 'state').write_text('fixture\n')
        self.assertEqual(self.fetch()[0], 0)

    def test_prepare_refuses_without_key(self):
        self.repository()
        with mock.patch.object(self.agent.os.path, 'ismount', return_value=True):
            self.assertEqual(self.fetch(prepare=True)[0], 3)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.key.exists())
        self.assertEqual(json.loads((self.state / 'last.json').read_text())['exit'], 3)

    def test_prepare_refuses_without_data_mount(self):
        self.repository()
        self.private_key()
        with mock.patch.object(self.agent.os.path, 'ismount', return_value=False):
            self.assertEqual(self.fetch(prepare=True)[0], 6)
        self.assertFalse(self.marker.exists())

    def test_existing_checkout_reverified(self):
        self.repository()
        self.assertEqual(self.fetch()[0], 0)
        original_git = self.agent.git_call
        for variant in ('head', 'modified', 'untracked', 'config', 'digest', 'permissions'):
            with self.subTest(variant=variant):
                if self.destination.exists():
                    shutil.rmtree(self.destination)
                self.assertEqual(self.fetch()[0], 0)
                if variant == 'head':
                    self.git('commit', '--allow-empty', '-m', 'fixture: moved head', cwd=self.destination)
                elif variant == 'modified':
                    (self.destination / '.gitignore').write_text('changed\n')
                elif variant == 'untracked':
                    (self.destination / 'unexpected').write_text('untracked\n')
                elif variant == 'config':
                    with (self.destination / '.git/config').open('a') as stream:
                        stream.write('\n[core]\n hooksPath = /unsafe\n')
                elif variant == 'digest':
                    (self.state / 'checkouts' / (self.sha + '.json')).unlink()
                else:
                    (self.destination / '.gitignore').chmod(0o664)
                calls = []
                def record(tool, args, *rest, **kwargs):
                    calls.append(args)
                    return original_git(tool, args, *rest, **kwargs)
                with mock.patch.object(self.agent, 'git_call', side_effect=record):
                    self.assertEqual(self.fetch()[0], 4)
                self.assertFalse(any('fetch' in args or 'ls-remote' in args for args in calls))
                if variant in ('config', 'digest'):
                    self.assertEqual(calls, [])
                self.assertFalse(self.marker.exists())

    def test_git_hardening(self):
        self.repository()
        run, calls = subprocess.run, []
        def record(argv, **kwargs):
            calls.append((argv, kwargs.copy()))
            env = kwargs['env']
            self.assertEqual(set(env), {'PATH', 'LANG', 'HOME', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL',
                                        'GIT_TERMINAL_PROMPT', 'GIT_HTTP_LOW_SPEED_LIMIT', 'GIT_HTTP_LOW_SPEED_TIME'})
            self.assertEqual(env, self.agent.cleared_env(env['HOME']))
            self.assertEqual(list(Path(env['HOME']).iterdir()), [])
            self.assertEqual(stat.S_IMODE(Path(env['HOME']).stat().st_mode), 0o700)
            return run(argv, **kwargs)
        with mock.patch.object(self.agent.subprocess, 'run', side_effect=record):
            self.assertEqual(self.fetch()[0], 0)
        for argv, kwargs in calls:
            with self.subTest(argv=argv):
                for flag in ('protocol.allow=never', 'protocol.file.allow=always', 'core.hooksPath=/dev/null',
                             'core.fsmonitor=false', 'transfer.fsckObjects=true', 'submodule.recurse=false'):
                    self.assertIn(flag, argv)
                    self.assertEqual(argv[argv.index(flag) - 1], '-c')
                if 'fetch' in argv:
                    self.assertEqual(argv[-6:], ['fetch', '--depth=1', '--no-tags', '--no-recurse-submodules', self.remote, self.sha])
                self.assertEqual(kwargs['timeout'], 120 if 'fetch' in argv or 'ls-remote' in argv else 30)
        with mock.patch.object(self.agent, 'require_runtime') as safe, mock.patch.object(self.agent, 'exclusive_lock', return_value=contextlib.nullcontext()), mock.patch.object(self.agent, 'fetch', return_value=0) as fetch:
            self.assertEqual(self.agent.main(['fetch', self.sha]), 0)
            self.assertEqual(fetch.call_args.args[-2:], (self.agent.SOURCE_REMOTE, 'https'))
            self.assertEqual(safe.call_args.args[-2:], (0, os.geteuid()))

    def test_subprocess_timeouts(self):
        tree = ast.parse(AGENT.read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == 'subprocess']
        self.assertGreaterEqual(len(calls), 4)
        for call in calls:
            with self.subTest(call=ast.unparse(call)):
                if call.func.attr == 'Popen':
                    self.assertTrue(next(k.value.value for k in call.keywords if k.arg == 'start_new_session'))
                else:
                    self.assertEqual(call.func.attr, 'run')
                    self.assertIn('timeout', [k.arg for k in call.keywords])
        waits = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == 'process' and n.func.attr == 'wait']
        self.assertTrue(waits)
        for call in waits:
            with self.subTest(wait=ast.unparse(call)):
                self.assertIn('timeout', [k.arg for k in call.keywords])
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'tempfile':
                with self.subTest(temporary=ast.unparse(node)):
                    self.assertIn('dir', [kw.arg for kw in node.keywords])
        self.repository()
        self.private_key()
        run, wait = subprocess.run, subprocess.Popen.wait
        def short_wait(process, timeout=None):
            return wait(process, timeout=0.05 if timeout == 3600 else timeout)
        hang = self.stub('hanging-tool', 'import time; time.sleep(3)\n')
        def short_timeout(*args, **kwargs):
            kwargs['timeout'] = 0.05
            return run(*args, **kwargs)
        with mock.patch.object(self.agent.subprocess, 'run', side_effect=short_timeout):
            self.age = hang
            self.assertEqual(self.recipient()[0], 3)
            self.assertEqual(self.fetch(git_tool=hang)[0], 4)
            self.assertEqual(self.agent.cloud_status(hang), 'unknown')
            with mock.patch.object(self.agent.os.path, 'ismount', return_value=True), \
                    mock.patch.object(subprocess.Popen, 'wait', autospec=True, side_effect=short_wait), \
                    self.assertRaises(self.agent.Failure) as failure:
                self.agent.prepare(self.source / self.agent.KIT, self.key, self.key_dir,
                                   self.data, self.uid, hang, self.agent.cleared_env(self.root))
            self.assertEqual(failure.exception.code, 7)

    def test_remote_matches_kit_control(self):
        text = (BASE / 'kit/kit-control.py').read_text()
        remote = re.search(r'https://github\.com/agenticcodingops/software-factory\.git', text).group()
        self.assertEqual(remote, self.agent.SOURCE_REMOTE)

    def test_status_json(self):
        self.repository()
        self.private_key()
        self.assertEqual(self.fetch()[0], 0)
        self.agent.activate(self.install, self.destination / self.agent.KIT)
        for index in range(12):
            (self.install / ('kit-' + format(index, '040x'))).mkdir()
        lock = self.root / 'agent.lock'
        holder = subprocess.Popen(['/usr/bin/python3', '-c',
            'import fcntl,sys; f=open(sys.argv[1],"w"); import os; os.chmod(sys.argv[1],0o600); fcntl.flock(f,fcntl.LOCK_EX); print("ready",flush=True); sys.stdin.read()', str(lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(holder.stderr.close)
        self.addCleanup(holder.stdout.close)
        try:
            self.assertEqual(holder.stdout.readline(), b'ready\n')
            for body, expected in (('print(\'{"status":"done","extended_status":"degraded done"}\'); raise SystemExit(2)', 'error'),
                                   ('print("unparseable")', 'unknown'), ('print(\'{"status":"done"}\')', 'done')):
                with self.subTest(cloud=expected):
                    self.cloud = self.stub('cloud-status', body)
                    with self.main_paths(lock), mock.patch.object(self.agent, 'require_runtime'):
                        code, out, _ = self.capture(self.agent.main, ['status'])
                    self.assertEqual(code, 0)
                    value = json.loads(out)
                    self.assertLessEqual(len(out.encode()), 2048)
                    self.assertEqual(set(value), {'tools', 'agent', 'agent_sha256', 'cloud_init', 'delivery_key', 'data_mount', 'current', 'checkouts', 'last', 'truncated'})
                    self.assertEqual(value['cloud_init'], expected)
                    self.assertEqual(value['delivery_key'], 'ok')
                    self.assertEqual(value['current'], self.sha)
                    self.assertTrue(value['truncated'])
                    self.assertEqual(value['checkouts'], sorted(value['checkouts']))
                    self.assertEqual(len(value['checkouts']), 8)
                    self.assertNotIn(self.secret.decode(), out)
            with self.assertRaises(self.agent.Failure) as failure:
                with self.agent.exclusive_lock(lock, self.uid):
                    self.fail('acquired busy lock')
            self.assertEqual(failure.exception.code, 8)
        finally:
            holder.communicate(timeout=5)
        for status, extended, expected in (('running', 'running', 'running'), ('not started', '', 'running'),
                                           ('done', 'disabled', 'error'), ('error', '', 'error')):
            with self.subTest(status=status, extended=extended):
                self.cloud = self.stub('cloud-values', 'print(' + repr(json.dumps({'status': status, 'extended_status': extended})) + ')')
                self.assertEqual(self.agent.cloud_status(self.cloud), expected)

    def test_contract_json_matches_agent(self):
        value = json.loads((BOOT / 'contract.json').read_text())
        self.assertEqual(value, {'path': str(self.agent.AGENT), 'agent_sha256': hashlib.sha256(AGENT.read_bytes()).hexdigest(),
                                'commands': self.agent.COMMANDS, 'exit': self.agent.EXIT, 'absent_exit': 127, 'retry_exit': 8,
                                'runtime_outcome': {**self.agent.RUNTIME_OUTCOME, '127': 'agent-unavailable', 'unknown': 'agent-unavailable'},
                                'max_runtime_seconds': self.agent.MAX_RUNTIME_SECONDS, 'status_max_bytes': self.agent.STATUS_MAX_BYTES})
        self.assertEqual(set(value['runtime_outcome']), {str(code) for code in self.agent.EXIT.values()} | {'127', 'unknown'})
        readme = (BOOT / 'README.md').read_text()
        rows = dict(re.findall(r'^\| ([0-9]+) \| [^|]+ \| ([a-z-]+) \|$', readme, re.M))
        self.assertEqual(rows, self.agent.RUNTIME_OUTCOME)


    def test_requires_root_and_safe_paths(self):
        agent = self.root / 'agent'
        agent.write_text('fixture\n')
        tool_dir = self.root / 'bin'
        tool_dir.mkdir(mode=0o755)
        tool = tool_dir / 'tool'
        tool.write_text('fixture\n')
        tool.chmod(0o755)
        for variant in ('euid', 'agent', 'tool_dir', 'owner'):
            with self.subTest(variant=variant):
                agent.chmod(0o775 if variant == 'agent' else 0o755)
                tool_dir.chmod(0o775 if variant == 'tool_dir' else 0o755)
                with self.assertRaises(self.agent.Failure) as failure:
                    self.agent.require_runtime(agent, self.root, tool, [tool],
                                               self.uid + 1 if variant == 'owner' else self.uid,
                                               self.uid + 1 if variant in ('owner', 'euid') else self.uid)
                self.assertEqual(failure.exception.code, 10)
        with mock.patch.object(self.agent.os, 'geteuid', return_value=1):
            self.assertEqual(self.agent.main(['status']), 10)

    def test_internal_error_exits_1(self):
        with mock.patch.object(self.agent, 'require_runtime', side_effect=RuntimeError(self.secret.decode())):
            code, out, err = self.capture(self.agent.main, ['status'])
        self.assertEqual((code, out, err), (1, '', 'sf-host-agent: internal error\n'))

    def test_agent_stdlib_only(self):
        for node in ast.walk(ast.parse(AGENT.read_text())):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module]
                for name in names:
                    with self.subTest(module=name):
                        self.assertIn(name.split('.')[0], sys.stdlib_module_names)

    def test_never_exits_127(self):
        self.assertNotIn(127, self.agent.EXIT.values())
        for node in ast.walk(ast.parse(AGENT.read_text())):
            if isinstance(node, ast.Constant):
                with self.subTest(value=node.value):
                    self.assertNotEqual(node.value, 127)
