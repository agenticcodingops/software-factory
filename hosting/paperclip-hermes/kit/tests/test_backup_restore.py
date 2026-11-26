"""Docker-free recovery contracts, failure cleanup and signed-URL redaction.

Run: python3 -B -m unittest discover -s tests
All credential-like fixture values are generated at test time.
"""
from datetime import datetime, timezone, timedelta
import argparse
import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import ssl
import stat
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.parse

spec = importlib.util.spec_from_file_location('recovery', Path(__file__).parents[1] / 'backup-restore.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def public_key():
    return 'age1' + ''.join(secrets.choice('023456789acdefghjklmnpqrstuvwxyz') for _ in range(58))


def signed_url(**changes):
    query = {'sv': '2023-11-03', 'sr': 'c', 'sp': 'cw', 'spr': 'https',
             'skoid': secrets.token_hex(16), 'sktid': secrets.token_hex(16),
             'skt': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
             'ske': (datetime.now(timezone.utc) + timedelta(days=6)).isoformat(),
             'se': (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
             'sks': 'b', 'skv': '2023-11-03',
             'sig': secrets.token_hex(32)}
    query.update(changes)
    return 'https://storage.example.test/recovery?' + urllib.parse.urlencode(query)


class ArgumentTests(unittest.TestCase):
    def test_backup_defaults_and_local_target(self):
        args = recovery.parser().parse_args(['backup'])
        self.assertEqual(args.env_file, Path('/etc/paperclip-hermes/backup.env'))
        self.assertEqual(args.project, 'paperclip-hermes')
        args = recovery.parser().parse_args(['backup', '--recipient', public_key(), '--local-dir', '/tmp/upload'])
        self.assertEqual(args.local_dir, Path('/tmp/upload'))

    def test_restore_requires_fenced_flag_before_any_work(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            recovery.parser().parse_args(['restore', '--archive', 'backup.age', '--identity', 'key', '--target', 'new'])
        self.assertEqual(error.exception.code, 2)
        with patch.object(recovery, 'private_file') as private, patch.object(recovery, 'run') as run:
            with self.assertRaisesRegex(recovery.RecoveryError, '--fenced'):
                recovery.restore(SimpleNamespace(fenced=False))
            private.assert_not_called()
            run.assert_not_called()

    def test_rejects_unknown_options_and_private_recipient(self):
        for args in (['backup', '--unknown'], ['backup', '--recipient', 'AGE-SECRET-KEY-' + secrets.token_hex(32)],
                     ['backup', '--project', '../existing']):
            with self.subTest(option=args[:2]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                recovery.parser().parse_args(args)


class SasTests(unittest.TestCase):
    def test_delegation_sas_cw_uses_unique_blob_path(self):
        for extra in ({'sp': 'cw'},):
            container = recovery.sas_url(signed_url(**extra))
            result = recovery.blob_url(container, 'unique.tar.age')
            self.assertIn('/recovery/unique.tar.age?', result)
            self.assertEqual(result.split('?', 1)[1], container.query)

    def test_rejects_broad_permissions_malformed_or_non_container_urls(self):
        bad = [signed_url(sp='c'), signed_url(sp='r'), signed_url(sp='rcwl'), signed_url(sp=''), signed_url(si=''),
               signed_url(sr='b'), signed_url(spr='https,http'), signed_url().replace('https:', 'http:'),
               signed_url().replace('/recovery?', '/recovery/blob?'), signed_url() + '&sp=cw&sp=cw',
               signed_url().replace('storage.example.test', 'user:password@storage.example.test')]
        for value in bad:
            signature = value.split('sig=')[1].split('&')[0]
            with self.subTest(case=bad.index(value)), self.assertRaises(recovery.RecoveryError) as error:
                recovery.sas_url(value)
            self.assertNotIn(signature, str(error.exception))

    def test_put_streams_ciphertext_and_never_logs_signed_url_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'encrypted.age'
            archive.write_bytes(secrets.token_bytes(128))
            value = signed_url()
            opener = MagicMock()
            opener.open.side_effect = urllib.error.HTTPError(value, 403, value, {}, None)
            with patch.object(recovery.urllib.request, 'build_opener', return_value=opener), \
                 contextlib.redirect_stdout(io.StringIO()) as output, \
                 contextlib.redirect_stderr(io.StringIO()) as errors, \
                 self.assertRaises(recovery.RecoveryError) as failure:
                recovery.upload(recovery.sas_url(value), archive, 'unique.age')
            self.assertNotIn(value, str(failure.exception) + output.getvalue() + errors.getvalue())
            self.assertNotIn(value.split('sig=')[1], str(failure.exception))
            request = opener.open.call_args.args[0]
            self.assertEqual(request.method, 'PUT')
            self.assertEqual(request.get_header('If-none-match'), '*')
            self.assertEqual(request.get_header('X-ms-blob-type'), 'BlockBlob')
            self.assertEqual(request.get_header('Content-length'), '128')
            self.assertTrue(request.data.closed)

    def test_large_upload_commits_blocks_conditionally(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'cipher.age'
            archive.write_bytes(secrets.token_bytes(13))
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.status = 201
            with patch.object(recovery.urllib.request, 'build_opener', return_value=opener), \
                 patch.object(recovery, 'SINGLE_PUT_LIMIT', 4), patch.object(recovery, 'BLOCK_SIZE', 5):
                recovery.upload(recovery.sas_url(signed_url()), archive, 'new.age')
            requests = [call.args[0] for call in opener.open.call_args_list]
            self.assertEqual(len(requests), 4)
            self.assertTrue(all('&comp=block&blockid=' in req.full_url for req in requests[:-1]))
            self.assertTrue(requests[-1].full_url.endswith('&comp=blocklist'))
            self.assertEqual(requests[-1].get_header('If-none-match'), '*')
            self.assertEqual(requests[-1].data.count(b'<Latest>'), 3)

    def test_unexpected_upload_status_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'cipher.age'
            archive.write_bytes(secrets.token_bytes(16))
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.status = 202
            with patch.object(recovery.urllib.request, 'build_opener', return_value=opener), \
                 self.assertRaises(recovery.RecoveryError):
                recovery.upload(recovery.sas_url(signed_url()), archive, 'new.age')

    def upload_with(self, responses, size=16, block=None):
        # responses: one item per opener.open call; an int is an HTTP status
        # (201 succeeds, others raise HTTPError), an exception is raised.
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'cipher.age'
            archive.write_bytes(secrets.token_bytes(size))
            value = signed_url()
            positions = []
            def open_(request, timeout):
                if hasattr(request.data, 'read'):
                    positions.append(request.data.tell())
                    request.data.read()  # as the HTTP client would consume it
                item = responses.pop(0)
                if isinstance(item, BaseException):
                    raise item
                if item != 201:
                    raise urllib.error.HTTPError(request.full_url, item, value, {}, None)
                response = MagicMock()
                response.__enter__.return_value.status = item
                return response
            opener = MagicMock()
            opener.open.side_effect = open_
            limits = {} if block is None else {'SINGLE_PUT_LIMIT': block - 1, 'BLOCK_SIZE': block}
            with patch.object(recovery.urllib.request, 'build_opener', return_value=opener), \
                 patch.object(recovery.time, 'sleep') as sleep, \
                 contextlib.ExitStack() as stack:
                for name, limit in limits.items():
                    stack.enter_context(patch.object(recovery, name, limit))
                try:
                    recovery.upload(recovery.sas_url(value), archive, 'new.age')
                    error = None
                except recovery.RecoveryError as failure:
                    error = failure
                    self.assertNotIn(value.split('sig=')[1], str(failure))
            self.positions = positions
            return opener.open.call_args_list, sleep.call_args_list, error

    def test_transient_failures_are_retried_with_backoff(self):
        for transient in (503, 429, 408, urllib.error.URLError('down'), TimeoutError(),
                          ssl.SSLEOFError(), http.client.IncompleteRead(b''),
                          http.client.BadStatusLine('')):
            with self.subTest(transient=repr(transient)):
                calls, sleeps, error = self.upload_with([transient, transient, 201])
                self.assertIsNone(error)
                self.assertEqual(len(calls), 3)
                self.assertEqual([call.args[0] for call in sleeps], [2, 4])
                # The streamed body is rewound for every attempt.
                self.assertEqual(self.positions, [0, 0, 0])

    def test_transport_errors_carrying_the_url_are_retried_and_never_rendered(self):
        # A TLS or HTTP-protocol error can embed the signed URL in its text.
        value = signed_url()
        failures = [ssl.SSLEOFError(value), http.client.IncompleteRead(value.encode()),
                    http.client.BadStatusLine(value), ConnectionResetError(value), OSError(value)]
        calls, _, error = self.upload_with(failures)
        self.assertEqual(len(calls), 1 + recovery.RETRIES)
        self.assertIsInstance(error, recovery.RecoveryError)
        self.assertNotIn(value.split('sig=')[1], str(error))

    def test_retries_stop_after_four_and_fail(self):
        calls, sleeps, error = self.upload_with([500] * 6)
        self.assertIsInstance(error, recovery.RecoveryError)
        self.assertEqual(len(calls), 1 + recovery.RETRIES)
        self.assertEqual(len(sleeps), recovery.RETRIES)

    def test_permanent_status_is_not_retried(self):
        for status in (400, 403, 404, 409, 412):
            with self.subTest(status=status):
                calls, _, error = self.upload_with([status, 201])
                self.assertIsInstance(error, recovery.RecoveryError)
                self.assertEqual(len(calls), 1)

    def test_retried_conditional_conflict_fails_as_uncertain(self):
        # Put Blob and Put Block List carry If-None-Match: a conflict after a
        # lost response may be our own success; never report either outcome.
        for status in (409, 412):
            with self.subTest(single=status):
                _, _, error = self.upload_with([TimeoutError(), status])
                self.assertIn('uncertain', str(error))
            with self.subTest(blocklist=status):
                calls, _, error = self.upload_with([201, 201, 201, 504, status], size=13, block=5)
                self.assertIn('uncertain', str(error))
                self.assertTrue(calls[-1].args[0].full_url.endswith('&comp=blocklist'))

    def test_block_retry_resends_same_block(self):
        calls, _, error = self.upload_with([503, 201, 201, 201, 201], size=13, block=5)
        self.assertIsNone(error)
        urls = [call.args[0].full_url for call in calls]
        self.assertEqual(urls[0], urls[1])
        self.assertEqual(calls[0].args[0].data, calls[1].args[0].data)

    def test_redirects_are_refused(self):
        self.assertIsNone(recovery.NoRedirect().redirect_request(None, None, 307, '', {}, 'https://other.test'))

    def test_settings_are_literal_and_outside_kit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / 'backup.env'
            key = public_key()
            settings.write_text('BACKUP_AGE_RECIPIENT=' + key + '\nBACKUP_CONTAINER_SAS_URL=' + signed_url() + '\n')
            with patch.object(recovery, 'private_stat'), patch.object(recovery, 'private_directory'):
                self.assertEqual(recovery.backup_settings(settings, root / 'kit')[0], key)
                with self.assertRaisesRegex(recovery.RecoveryError, 'outside'):
                    recovery.backup_settings(settings, root)
                settings.write_text('BACKUP_AGE_RECIPIENT=$(command)\n')
                with self.assertRaises(recovery.RecoveryError):
                    recovery.backup_settings(settings, root / 'kit')

    def test_settings_are_checked_and_parsed_from_one_descriptor(self):
        # No second open by path: the owner and mode checks run on fstat of
        # the descriptor the settings are then read from, opened without
        # following a link.
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'backup.env'
            settings.write_text('BACKUP_AGE_RECIPIENT=' + public_key() + '\nBACKUP_CONTAINER_SAS_URL=' + signed_url() + '\n')
            opened, checked = [], []
            real_open, real_fstat = os.open, os.fstat
            def open_(path, flags, *args, **kwargs):
                descriptor = real_open(path, flags, *args, **kwargs)
                opened.append((Path(path), flags, descriptor))
                return descriptor
            def fstat(descriptor):
                checked.append(descriptor)
                return real_fstat(descriptor)
            with patch.object(recovery, 'private_stat') as private, patch.object(recovery, 'private_directory'), \
                 patch.object(recovery.os, 'open', side_effect=open_), \
                 patch.object(recovery.os, 'fstat', side_effect=fstat), \
                 patch.object(Path, 'read_text', side_effect=AssertionError('reopened by path')), \
                 patch.object(Path, 'read_bytes', side_effect=AssertionError('reopened by path')):
                recovery.backup_settings(settings, Path(directory) / 'kit')
            self.assertEqual(len(opened), 1)
            self.assertEqual(opened[0][0], settings)
            self.assertEqual(checked, [opened[0][2]])
            private.assert_called_once()
            if hasattr(os, 'O_NOFOLLOW'):
                self.assertTrue(opened[0][1] & os.O_NOFOLLOW and opened[0][1] & os.O_CLOEXEC)

    def test_private_stat_requires_a_root_owned_private_regular_file(self):
        def info(mode, uid=0):
            return SimpleNamespace(st_mode=mode, st_uid=uid)
        for good in (stat.S_IFREG | 0o600, stat.S_IFREG | 0o400):
            recovery.private_stat(info(good))
        for bad in (info(stat.S_IFREG | 0o644), info(stat.S_IFREG | 0o600, 1000),
                    info(stat.S_IFDIR | 0o700), info(stat.S_IFIFO | 0o600)):
            with self.subTest(bad=bad), self.assertRaises(recovery.RecoveryError):
                recovery.private_stat(bad)

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0, 'requires root file ownership on POSIX')
    def test_settings_refuse_a_link_to_a_valid_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            real = root / 'real.env'
            real.write_text('BACKUP_AGE_RECIPIENT=' + public_key() + '\nBACKUP_CONTAINER_SAS_URL=' + signed_url() + '\n')
            real.chmod(0o600)
            recovery.backup_settings(real, root / 'kit')
            link = root / 'backup.env'
            link.symlink_to(real)
            with self.assertRaisesRegex(recovery.RecoveryError, 'links are refused'):
                recovery.backup_settings(link, root / 'kit')

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0, 'requires root file ownership on POSIX')
    def test_private_input_refuses_world_read_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings'
            path.write_text(secrets.token_hex(32))
            path.chmod(0o600)
            recovery.private_file(path)
            path.chmod(0o644)
            with self.assertRaises(recovery.RecoveryError):
                recovery.private_file(path)
            path.chmod(0o600)
            link = path.with_name('link')
            link.symlink_to(path)
            with self.assertRaises(recovery.RecoveryError):
                recovery.private_file(link)


class BundleTests(unittest.TestCase):
    def make_payloads(self, stage):
        for name in recovery.PAYLOADS:
            (stage / name).write_bytes(secrets.token_bytes(16))
        images = {service: {'image_id': 'sha256:' + secrets.token_hex(32), 'repo_digests': []}
                  for service in ('postgres', *recovery.SERVICES)}
        schema = {'journal': 'drizzle.__drizzle_migrations',
                  'applied': [{'id': 1, 'hash': secrets.token_hex(32), 'created_at': 123}]}
        return recovery.manifest(stage, images, schema, 'source', recovery.utc())

    def pack(self, root, record, extra=None):
        (root / 'manifest.json').write_text(json.dumps(record))
        bundle = root / 'bundle.tar'
        with tarfile.open(bundle, 'w') as archive:
            for name in sorted(recovery.PAYLOADS | {'manifest.json'}):
                archive.add(root / name, arcname=name)
            if extra is not None:
                archive.addfile(extra)
        return bundle

    def test_manifest_has_images_applied_schema_times_and_payload_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self.make_payloads(root)
            self.assertEqual(set(record['files']), recovery.PAYLOADS)
            self.assertTrue(record['images']['hermes']['image_id'].startswith('sha256:'))
            self.assertEqual(len(record['image_pins']), 4)
            self.assertEqual(recovery.commit_label(record['kit_commit']), record['kit_commit'])
            self.assertIn(record['kit_modified'], (True, False, 'unknown'))
            self.assertTrue(record['paperclip_schema']['applied'])
            self.assertIn('+00:00', record['started_at'])
            self.assertGreaterEqual(record['completed_at'], record['started_at'])
            output = root / 'unpacked'
            output.mkdir()
            self.assertEqual(recovery.unpack_bundle(self.pack(root, record), output), record)

    def test_checksum_failure_and_unsafe_member_are_fatal(self):
        for tamper in (True, False):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                record = self.make_payloads(root)
                if tamper:
                    (root / 'database.dump').write_bytes(b'damaged')
                extra = None if tamper else tarfile.TarInfo('../escape')
                bundle = self.pack(root, record, extra)
                output = root / 'unpacked'
                output.mkdir()
                with self.assertRaises(recovery.RecoveryError):
                    recovery.unpack_bundle(bundle, output)
                self.assertFalse((root / 'escape').exists())

    def test_configuration_rejects_symlinks_and_unexpected_secret_files(self):
        for name, kind in [('compose.yaml', tarfile.SYMTYPE), ('.secrets/identity', tarfile.REGTYPE),
                           ('../compose.yaml', tarfile.REGTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bundle = root / 'config.tar'
                with tarfile.open(bundle, 'w') as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = '/etc'
                    archive.addfile(member)
                with self.assertRaises(recovery.RecoveryError):
                    recovery.restore_config(bundle, root / 'target')

    def test_import_rollback_zip_removed_without_touching_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            backups = home / 'backups'
            backups.mkdir()
            rollback = backups / 'pre-transfer-fixture.zip'
            rollback.write_bytes(secrets.token_bytes(16))
            kept = backups / 'operator-note'
            kept.write_bytes(secrets.token_bytes(16))
            recovery.remove_import_undo(home)
            self.assertFalse(rollback.exists())
            self.assertTrue(kept.exists())

    def test_local_publish_does_not_overwrite_existing_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, existing = root / 'cipher.age', root / 'existing.age'
            archive.write_bytes(secrets.token_bytes(16))
            original = secrets.token_bytes(16)
            existing.write_bytes(original)
            with patch.object(recovery, 'private_directory'), self.assertRaises(FileExistsError):
                recovery.publish_local(archive, root, existing.name)
            self.assertEqual(existing.read_bytes(), original)

    def publish(self, failure=None):
        # failure: (patched name, error) injected into publish_local's steps.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        archive, upload = root / 'cipher.age', root / 'upload'
        upload.mkdir()
        archive.write_bytes(secrets.token_bytes(64))
        name = 'paperclip-hermes-fixture-' + secrets.token_hex(16) + '.tar.age'
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(recovery, 'private_directory'))
            stack.enter_context(patch.object(recovery.signal, 'signal'))
            if failure:
                target, error = failure
                stack.enter_context(patch.object(*target, side_effect=error))
            try:
                recovery.publish_local(archive, upload, name)
                raised = None
            except BaseException as caught:  # noqa: B036 - signals arrive as RecoveryError
                raised = caught
        return archive, upload, name, raised

    def test_local_publish_appears_complete_under_its_final_name_only(self):
        archive, upload, name, raised = self.publish()
        self.assertIsNone(raised)
        self.assertEqual([path.name for path in upload.iterdir()], [name])
        self.assertEqual((upload / name).read_bytes(), archive.read_bytes())
        if os.name == 'posix':
            self.assertEqual((upload / name).stat().st_mode & 0o777, 0o600)

    def test_local_publish_failure_or_signal_leaves_no_file(self):
        interrupted = recovery.RecoveryError('Recovery interrupted; cleaning private staging')
        for failure in [((recovery.os, 'fdopen'), interrupted),            # right after the open
                        ((recovery.shutil, 'copyfileobj'), interrupted),   # during the copy
                        ((recovery.os, 'fsync'), OSError('disk full')),
                        ((recovery.os, 'link'), OSError('link failed'))]:
            with self.subTest(step=failure[0][1]):
                _, upload, _, raised = self.publish(failure)
                self.assertIs(raised, failure[1])
                self.assertEqual(list(upload.iterdir()), [])

    def test_local_publish_signal_after_the_stream_owns_the_descriptor_leaves_no_file(self):
        # The stream already owns (and here has closed) the descriptor when the
        # signal lands: cleanup must not fail closing it again (EBADF), which
        # would replace the error and skip removing the temporary.
        interrupted = recovery.RecoveryError('Recovery interrupted; cleaning private staging')
        fdopen = os.fdopen
        def closed_then_signal(descriptor, *args, **kwargs):
            fdopen(descriptor, *args, **kwargs).close()
            raise interrupted
        @contextlib.contextmanager
        def signal_when_unblocked():
            yield
            raise interrupted  # a signal held during the block is delivered now
        for name, failure in (('fdopen', ((recovery.os, 'fdopen'), closed_then_signal)),
                              ('deferred', ((recovery, 'deferred_signals'), signal_when_unblocked))):
            with self.subTest(step=name):
                _, upload, _, raised = self.publish(failure)
                self.assertIs(raised, interrupted)
                self.assertEqual(list(upload.iterdir()), [])

    def test_manifest_fields_read_after_startup_are_checked_first(self):
        # Restore prints completed_at once the services are up; a malformed
        # manifest must fail in unpack_bundle, before anything is created.
        changes = [('completed_at', None), ('completed_at', 7), ('started_at', None),
                   ('completed_at', '2026-11-26T00:15:00+00:00\x1b]0;x\x07'),
                   ('completed_at', 'yesterday'), ('paperclip_schema', ['applied']),
                   ('files', ['database.dump']), ('images', None)]
        for key, value in changes:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                record = self.make_payloads(root)
                if value is None:
                    del record[key]
                else:
                    record[key] = value
                output = root / 'unpacked'
                output.mkdir()
                with self.assertRaisesRegex(recovery.RecoveryError, 'manifest'):
                    recovery.unpack_bundle(self.pack(root, record), output)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_payloads(root)
            bundle = self.pack(root, ['not', 'a', 'mapping'])
            (root / 'unpacked').mkdir()
            with self.assertRaisesRegex(recovery.RecoveryError, 'manifest'):
                recovery.unpack_bundle(bundle, root / 'unpacked')


class BackupFailureTests(unittest.TestCase):
    def exercise_failure(self, failure, signal_in_purge=False):
        # A fake signal table: main() installs interrupted() for every handled
        # signal; signal_in_purge delivers one during the first rmtree.
        handlers = dict.fromkeys(recovery.SIGNALS, recovery.interrupted)
        def set_handler(number, handler):
            previous, handlers[number] = handlers.get(number), handler
            return previous
        rmtree = recovery.shutil.rmtree
        delivered = []
        def interrupting_rmtree(*args, **kwargs):
            handler = handlers.get(recovery.signal.SIGTERM)
            if signal_in_purge and not delivered:
                delivered.append(handler)
                if callable(handler):
                    handler(recovery.signal.SIGTERM, None)
            return rmtree(*args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upload = root / 'upload'
            upload.mkdir()
            work = root / 'work'
            work.mkdir()
            volume = root / 'volume'
            volume.mkdir()
            fake = MagicMock()
            staged_at_restart = []
            # Each injected failure records itself when it fires, so the test
            # proves that stub was reached and that its error is the one raised.
            injected = []
            def inject(message):
                injected.append(message)
                raise recovery.RecoveryError(message)
            def compose(*args, **kwargs):
                if args[0] == 'up' and '--no-recreate' in args:
                    staged_at_restart.extend(path for path in work.rglob('*') if path.is_file())
                if args[:3] == ('ps', '--services', '--status'):
                    return '\n'.join(('postgres', *recovery.SERVICES))
                if args[0] == 'exec' and 'pg_dump' in args:
                    kwargs['output'].write(b'private database content')
                    partial = Path(kwargs['output'].name).parent / 'partial'
                    partial.mkdir()
                    (partial / 'chunk').write_bytes(b'private partial content')
                    if failure == 'dump':
                        inject('dump failed')
                return ''
            fake.side_effect = compose
            staged_at_removal = []
            fake.remove_containers.side_effect = lambda *args: staged_at_removal.append(
                sorted(path.name for path in work.rglob('*') if path.is_file()))
            fake.mount.return_value = volume
            fake.images.return_value = {service: {} for service in ('postgres', *recovery.SERVICES)}
            def hermes(stage, *args):
                (stage / 'hermes.zip').write_bytes(b'private Hermes content')
                if failure == 'hermes':
                    inject('Hermes failed')
            fake.hermes.side_effect = hermes
            def tar(args, **kwargs):
                Path(args[args.index('-cf') + 1]).write_bytes(b'private files')
                if failure == 'tar':
                    inject('tar failed')
            class Age:
                def __init__(self, args, **kwargs):
                    self.stdin = io.BytesIO()
                    self.returncode = 0
                    kwargs['stdout'].write(b'age-encryption.org/v1\n')
                def communicate(self):
                    if failure == 'age':
                        # The real process exits nonzero; backup() then fails with this message.
                        injected.append('age encryption failed')
                        self.returncode = 1
                    return None, b''
                def kill(self):
                    pass
                def wait(self):
                    pass
            args = SimpleNamespace(local_dir=upload, recipient=public_key(), project='fixture', work_dir=work)
            with patch.object(recovery, 'private_directory'), patch.object(recovery, 'validate_config'), \
                 patch.object(recovery, 'Compose', return_value=fake), \
                 patch.object(recovery, 'schema_state', return_value={'applied': [{'id': 1}]}), \
                 patch.object(recovery, 'wait_stack', return_value=['postgres', *recovery.SERVICES]), \
                 patch.object(recovery, 'run', side_effect=tar), \
                 patch.object(recovery, 'kit_commit', return_value='unknown'), \
                 patch.object(recovery, 'kit_modified', return_value=False), \
                 patch.object(recovery, 'config_backup', side_effect=lambda stage: (stage / 'config.tar').write_bytes(b'private config')), \
                 patch.object(recovery.os, 'chown', create=True), \
                 patch.object(recovery.subprocess, 'Popen', side_effect=Age), \
                 patch.object(recovery.signal, 'signal', side_effect=set_handler), \
                 patch.object(recovery.shutil, 'rmtree', side_effect=interrupting_rmtree), \
                 patch.object(recovery, 'publish_local', side_effect=lambda *a: inject('upload failed')) as publish:
                with self.assertRaises(recovery.RecoveryError) as raised:
                    recovery.backup(args)
            expected = {'dump': 'dump failed', 'tar': 'tar failed', 'hermes': 'Hermes failed',
                        'age': 'age encryption failed', 'upload': 'upload failed'}[failure]
            self.assertEqual(injected, [expected])
            self.assertEqual(str(raised.exception), expected)
            self.assertEqual(publish.call_count, 1 if failure == 'upload' else 0)
            if failure in ('dump', 'tar', 'hermes'):
                # A one-off Hermes CLI left running by a killed compose client
                # is removed before the purge and before the restart.
                self.assertEqual(fake.remove_containers.call_args_list, [unittest.mock.call(True)])
                self.assertNotEqual(staged_at_removal[0], [])
                if failure == 'hermes':
                    self.assertIn('hermes.zip', staged_at_removal[0])
                calls = fake.mock_calls
                self.assertLess(calls.index(unittest.mock.call.remove_containers(True)),
                                calls.index(unittest.mock.call('up', '-d', '--no-recreate', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '300',
                                                               *recovery.SERVICES)))
            self.assertEqual(list(work.iterdir()), [])
            self.assertEqual(list(upload.iterdir()), [])
            self.assertIn(unittest.mock.call('up', '-d', '--no-recreate', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '300', *recovery.SERVICES), fake.call_args_list)
            if failure in ('dump', 'tar', 'hermes'):
                # Plaintext is deleted before waiting on the service restart.
                self.assertEqual(staged_at_restart, [])
        return delivered

    def test_plaintext_removed_and_original_services_restarted_on_each_failure(self):
        for stage in ('dump', 'tar', 'hermes', 'age', 'upload'):
            with self.subTest(stage=stage):
                self.exercise_failure(stage)

    @unittest.skipUnless(hasattr(recovery.signal, 'pthread_sigmask'), 'POSIX signal masks')
    def test_signal_during_restart_is_deferred_until_it_ends(self):
        seen = []
        previous = recovery.signal.signal(recovery.signal.SIGTERM, lambda signum, frame: seen.append('handled'))
        try:
            with recovery.deferred_signals():
                os.kill(os.getpid(), recovery.signal.SIGTERM)
                seen.append('restart finished')
            self.assertEqual(seen, ['restart finished', 'handled'])
        finally:
            recovery.signal.signal(recovery.signal.SIGTERM, previous)

    def test_signal_during_purge_after_dump_failure_cannot_stop_cleanup(self):
        # The error path ignores handled signals before purging, so a SIGTERM
        # arriving mid-rmtree neither aborts the purge nor the restart.
        delivered = self.exercise_failure('dump', signal_in_purge=True)
        self.assertEqual(delivered, [recovery.signal.SIG_IGN])

    def test_staging_cleanup_on_termination_exception(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(recovery.signal, 'signal') as handler:
            with self.assertRaises(recovery.RecoveryError):
                with recovery.staging(directory) as stage:
                    (stage / 'private').write_bytes(secrets.token_bytes(32))
                    recovery.interrupted(15, None)
            self.assertEqual(list(Path(directory).iterdir()), [])
        # After the first signal every handled signal is ignored, so the
        # cleanup and restart cannot themselves be interrupted.
        self.assertEqual(handler.call_args_list[:len(recovery.SIGNALS)],
                         [unittest.mock.call(number, recovery.signal.SIG_IGN) for number in recovery.SIGNALS])
        self.assertEqual({call.args[1] for call in handler.call_args_list}, {recovery.signal.SIG_IGN})

    @unittest.skipUnless(os.name == 'posix', 'POSIX signals')
    def test_hangup_and_quit_are_handled_like_terminate(self):
        names = {recovery.signal.Signals(number).name for number in recovery.SIGNALS}
        self.assertEqual(names, {'SIGTERM', 'SIGINT', 'SIGHUP', 'SIGQUIT'})

    def test_stale_root_staging_removed_and_other_entries_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / 'paperclip-hermes-recovery-old'
            (stale / 'hermes-cli').mkdir(parents=True)
            (stale / 'database.dump').write_bytes(secrets.token_bytes(16))
            other = root / 'unrelated'
            other.mkdir()
            owner = os.getuid() if hasattr(os, 'getuid') else 0
            with contextlib.redirect_stderr(io.StringIO()):
                recovery.clear_stale(root)
            self.assertEqual(stale.exists(), owner != 0)
            self.assertTrue(other.exists())

    def test_purge_keeps_only_the_named_override_for_cleanup(self):
        # The restore failure path still runs compose with the override file.
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / 'restore-volumes.json').write_text('{}')
            (stage / 'database.dump').write_bytes(secrets.token_bytes(16))
            (stage / 'hermes-cli').mkdir()
            (stage / 'hermes-cli' / 'restore-volumes.json').write_text('{}')
            recovery.purge(stage, keep=('restore-volumes.json',))
            self.assertEqual([path.name for path in stage.iterdir()], ['restore-volumes.json'])
            recovery.purge(stage)
            self.assertEqual(list(stage.iterdir()), [])

    def test_empty_target_removes_contents_and_keeps_the_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            (target / '.secrets').mkdir(parents=True)
            (target / '.secrets' / 'compose.env').write_bytes(secrets.token_bytes(16))
            (target / 'compose.yaml').write_text('services: {}')
            recovery.empty_target(target)
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])


class ReportFailureTests(unittest.TestCase):
    def caught(self, raiser):
        try:
            raiser()
        except Exception as error:  # the job's top level catches these types
            return error
        self.fail('nothing raised')

    def test_cleanup_failure_also_names_the_error_that_started_cleanup(self):
        def job():
            try:
                raise recovery.RecoveryError('Command failed: pg_dump (exit 1)')
            finally:
                raise recovery.RecoveryError('Command failed: docker (exit 1)')
        self.assertEqual(recovery.report_failure(self.caught(job)),
                         ['Command failed: docker (exit 1)', 'Earlier error: Command failed: pg_dump (exit 1)'])

    def test_a_single_error_is_one_line(self):
        def job():
            raise recovery.RecoveryError('Restore requires --fenced before any operation')
        self.assertEqual(recovery.report_failure(self.caught(job)),
                         ['Restore requires --fenced before any operation'])

    def test_foreign_exceptions_are_never_rendered(self):
        url = 'https://storage.example.test/recovery?sig=' + secrets.token_hex(16)
        def foreign_then_ours():
            try:
                raise ValueError(url)
            except ValueError:
                raise recovery.RecoveryError('Command failed: docker (exit 1)')
        def ours_then_foreign():
            try:
                raise recovery.RecoveryError('Command failed: pg_restore (exit 1)')
            except recovery.RecoveryError:
                raise OSError(url)
        def suppressed():
            try:
                raise ConnectionResetError(url)
            except OSError:
                raise recovery.RecoveryError('Backup upload failed') from None
        cases = [(foreign_then_ours, ['Command failed: docker (exit 1)',
                                      'Earlier error: an unexpected failure before cleanup']),
                 (ours_then_foreign, [recovery.GENERIC_FAILURE, 'Earlier error: Command failed: pg_restore (exit 1)']),
                 (suppressed, ['Backup upload failed'])]
        for job, expected in cases:
            with self.subTest(job=job.__name__):
                lines = recovery.report_failure(self.caught(job))
                self.assertEqual(lines, expected)
                self.assertNotIn(url, '\n'.join(lines))

    def test_validation_errors_raised_while_handling_another_are_one_line(self):
        # A plain refusal raised inside an except block is not a cleanup
        # failure: no 'Earlier error' line.
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'identity'
            self.assertEqual(recovery.report_failure(self.caught(lambda: recovery.private_file(missing))),
                             ['Cannot open a private input file; links are refused'])
        for value in ('https://example.test/c?sig=%zz&si=p&sr=c&spr=https', 'not a url\x00'):
            with self.subTest(value=value), patch.object(recovery, 'urlsplit', side_effect=ValueError(value)):
                lines = recovery.report_failure(self.caught(lambda: recovery.sas_url(value)))
                self.assertEqual(lines, ['Invalid container SAS settings'])
        settings = b'BACKUP_AGE_RECIPIENT=age1notakey\nBACKUP_CONTAINER_SAS_URL=https://example.test/c\n'
        with patch.object(recovery, 'private_directory'), patch.object(recovery, 'read_private', return_value=settings):
            lines = recovery.report_failure(self.caught(lambda: recovery.backup_settings(Path('/elsewhere/backup.env'),
                                                                                          recovery.KIT)))
        self.assertEqual(lines, ['Backup recipient must be a native age public key'])

    def test_a_held_lock_is_one_line(self):
        fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock(side_effect=BlockingIOError(11, 'busy')))
        def job():
            with recovery.locked():
                pass
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(sys.modules, {'fcntl': fcntl}), \
             patch.object(recovery, 'LOCK_DIR', Path(directory) / 'run'), \
             patch.object(recovery, 'private_directory'), \
             patch.object(recovery.os, 'O_NOFOLLOW', 0, create=True), \
             patch.object(recovery.os, 'O_CLOEXEC', 0, create=True):
            lines = recovery.report_failure(self.caught(job))
        self.assertEqual(lines, ['Another host operation is running'])

    def test_an_existing_nonempty_restore_target_is_one_line(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            target.mkdir()
            (target / 'file').write_text('x')
            with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)), \
                 patch.object(recovery, 'private_directory'):
                lines = recovery.report_failure(self.caught(lambda: recovery.restore_target(target)))
        self.assertEqual(lines, ['Restore target must be a new or empty directory'])

    def test_the_script_entry_point_reports_through_report_failure(self):
        source = Path(recovery.__file__).read_text(encoding='utf-8')
        entry = source[source.index("if __name__ == '__main__':"):]
        self.assertIn('report_failure(failure)', entry)
        self.assertNotIn('str(', entry)


class RestoreFlowTests(unittest.TestCase):
    RECORD = {'completed_at': '2026-11-26T00:15:02.123456+00:00', 'kit_commit': 'unknown',
              'images': {s: {} for s in ('postgres', *recovery.SERVICES)},
              'image_pins': ['fixture-pin'], 'paperclip_schema': {'applied': [{'id': 1}]}}

    def restore(self, fail_at=None, purge_error=False, import_error=None):
        # Drive restore() with Docker, age and the kit checks stubbed; fail_at
        # names the Compose command that fails, import_error the error the
        # Hermes import raises. Returns what cleanup saw.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        target, work, volume = root / 'target', root / 'work', root / 'volume'
        for path in (target, work, volume):
            path.mkdir()
        seen = {}
        def run(args, **kwargs):
            if args[0] == 'age':
                kwargs['output'].write(b'bundle')
            return ''
        def unpack(bundle, stage):
            for name in recovery.PAYLOADS:
                (stage / name).write_bytes(secrets.token_bytes(16))
            return self.RECORD
        def restore_config(archive, destination, *args):
            (destination / '.secrets').mkdir()
            (destination / '.secrets/compose.env').write_text('TAILSCALE_HOSTNAME=source.example.ts.net\n')
        def compose_call(*args, **kwargs):
            if args[0] == fail_at:
                raise recovery.RecoveryError(fail_at + ' failed')
            return ''
        compose = MagicMock(side_effect=compose_call)
        compose.services.return_value = ['postgres', *recovery.SERVICES]
        compose.mount.return_value = volume
        events = seen['events'] = []
        compose.hermes.side_effect = import_error
        compose.remove_containers.side_effect = lambda *args: events.append(
            ('remove containers', sorted(path.name for path in work.rglob('*') if path.is_file())))
        def abandon(project):
            events.append(('abandon',))
            seen['staged_at_abandon'] = sorted(path.name for path in work.rglob('*') if path.is_file())
        purge = recovery.purge
        def failing_purge(stage, keep=()):
            purge(stage, keep)
            raise OSError('purge failed')
        args = SimpleNamespace(fenced=True, identity=root / 'identity', archive=root / 'backup.age',
                               target=target, project='fixture', hostname=None, work_dir=work)
        output = io.StringIO()
        with patch.object(recovery, 'private_file'), patch.object(recovery, 'run', side_effect=run), \
             patch.object(recovery, 'restore_target', return_value=target), \
             patch.object(recovery, 'unpack_bundle', side_effect=unpack), \
             patch.object(recovery, 'restore_config', side_effect=restore_config), \
             patch.object(recovery, 'image_pins', return_value=self.RECORD['image_pins']), \
             patch.object(recovery, 'validate_config'), \
             patch.object(recovery, 'Compose', return_value=compose), \
             patch.object(recovery, 'schema_state', return_value=self.RECORD['paperclip_schema']), \
             patch.object(recovery, 'abandon', side_effect=abandon) as abandoned, \
             patch.object(recovery, 'remove_import_undo',
                          side_effect=lambda home: events.append(('remove import undo', home))), \
             patch.object(recovery, 'purge', side_effect=failing_purge if purge_error else purge), \
             patch.object(recovery.os, 'chown', create=True), \
             patch.object(recovery.signal, 'signal'), \
             contextlib.redirect_stdout(output):
            try:
                recovery.restore(args)
                seen['error'] = None
            except (recovery.RecoveryError, OSError) as error:
                seen['error'] = error
        seen.update(abandoned=abandoned.call_count, compose=compose.call_args_list, output=output.getvalue(),
                    volume=volume,
                    target=sorted(path.name for path in target.iterdir()), work=list(work.iterdir()))
        seen['settings'] = (target / '.secrets/compose.env').read_text() if seen['target'] else None
        return seen

    def test_success_turns_messaging_off_and_prints_the_checked_recovery_point(self):
        seen = self.restore()
        self.assertIsNone(seen['error'])
        self.assertIn('Recovery point: ' + self.RECORD['completed_at'], seen['output'])
        self.assertEqual(seen['settings'].splitlines()[-1], 'KIT_MESSAGING_OFF=1')
        self.assertEqual(seen['abandoned'], 0)
        self.assertEqual(seen['work'], [])
        # The policy check takes only the policy: the baseline is the validator's.
        check = [call.args for call in seen['compose'] if '/kit/validate-hermes-config.py' in call.args]
        self.assertEqual(len(check), 1)
        self.assertEqual(check[0][-2:], ('/kit/validate-hermes-config.py', '/kit-config.yaml'))

    def test_hermes_restore_seeds_with_file_secrets_without_service_entrypoint(self):
        seen = self.restore()
        self.assertIsNone(seen['error'])
        seeds = [call.args for call in seen['compose']
                 if any('/kit/configure-hermes.py' in str(arg) for arg in call.args)]
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0][1:8], ('--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh', 'hermes', '-ec'))
        self.assertEqual(seeds[0][-1], '. /kit/hermes-secrets.sh; exec ' + recovery.PYTHON + ' /kit/configure-hermes.py')

    def test_failed_database_restore_never_starts_applications(self):
        seen = self.restore(fail_at='exec')
        self.assertEqual(str(seen['error']), 'exec failed')
        starts = [call.args for call in seen['compose'] if call.args[0] == 'up']
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][-1], 'postgres')
        self.assertIn('--no-deps', starts[0])
        self.assertEqual(seen['abandoned'], 1)
        self.assertEqual([event[0] for event in seen['events']], ['remove containers', 'abandon'])

    def test_late_failure_keeps_only_the_override_until_the_project_is_down(self):
        seen = self.restore(fail_at='create')
        self.assertEqual(str(seen['error']), 'create failed')
        self.assertEqual(seen['abandoned'], 1)
        self.assertEqual(seen['staged_at_abandon'], ['restore-volumes.json'])
        self.assertEqual(seen['work'], [])
        # The target is kept for inspection beside the named volumes.
        self.assertEqual(seen['target'], ['.secrets'])

    def test_signal_during_the_import_removes_its_one_off_before_purge_and_undo(self):
        # A signal kills the compose client of 'run --rm', not the import's
        # one-off container: every project container goes before the purge
        # and down, and the rollback ZIP is removed again once none can run.
        interrupted = recovery.RecoveryError('Recovery interrupted; cleaning private staging')
        seen = self.restore(import_error=interrupted)
        self.assertIs(seen['error'], interrupted)
        events = seen['events']
        self.assertEqual([event[0] for event in events],
                         ['remove import undo', 'remove containers', 'abandon', 'remove import undo'])
        # Removed while the stage still held the import's input: before the purge.
        self.assertIn('hermes.zip', events[1][1])
        self.assertEqual(seen['staged_at_abandon'], ['restore-volumes.json'])
        self.assertEqual(events[-1][1], seen['volume'])
        self.assertEqual(seen['work'], [])

    def test_failure_before_the_hermes_home_is_known_still_removes_containers(self):
        seen = self.restore(fail_at='create')
        self.assertEqual([event[0] for event in seen['events']], ['remove containers', 'abandon'])

    def test_late_failure_takes_the_project_down_even_if_the_purge_fails(self):
        seen = self.restore(fail_at='up', purge_error=True)
        self.assertEqual(seen['abandoned'], 1)
        self.assertIsInstance(seen['error'], OSError)
        # The error that started cleanup stays attached and is reported.
        self.assertEqual(str(seen['error'].__context__), 'up failed')
        self.assertEqual(recovery.report_failure(seen['error']),
                         [recovery.GENERIC_FAILURE, 'Earlier error: up failed'])
        self.assertEqual(seen['work'], [])

    def test_early_failure_empties_the_target_and_creates_nothing(self):
        for purge_error in (False, True):
            with self.subTest(purge_error=purge_error):
                seen = self.restore(fail_at='config', purge_error=purge_error)
                self.assertEqual(str(seen['error'].__context__ if purge_error else seen['error']), 'config failed')
                self.assertEqual(seen['target'], [])
                self.assertEqual(seen['work'], [])
                self.assertEqual(seen['abandoned'], 0)
                self.assertNotIn('create', [call.args[0] for call in seen['compose']])

    def test_settings_keep_secrets_change_only_the_hostname_and_add_messaging_off(self):
        text = 'TAILSCALE_HOSTNAME=old.example.ts.net\nOPENAI_API_KEY=fixture\nKIT_MESSAGING_OFF=1\n'
        self.assertEqual(recovery.restored_settings(text, 'new.example.ts.net'),
                         'TAILSCALE_HOSTNAME=new.example.ts.net\nOPENAI_API_KEY=fixture\nKIT_MESSAGING_OFF=1\n')
        self.assertEqual(recovery.restored_settings('OPENAI_API_KEY=fixture\n'),
                         'OPENAI_API_KEY=fixture\nKIT_MESSAGING_OFF=1\n')


def kit_archive(path, changes=None, extra=None, omit=()):
    # A config.tar as backup would make it from this kit, with optional edits.
    changes, extra = changes or {}, extra or {}
    with tarfile.open(path, 'w') as archive:
        def add(name, data):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for name, source in recovery.kit_files(recovery.KIT, recovery.CONFIG).items():
            if name not in omit:
                add(name, changes.get(name, source.read_bytes()))
        for name in recovery.SECRET_FILES:
            if '.secrets/' + name not in omit:
                add('.secrets/' + name, secrets.token_bytes(16))
        for name, data in extra.items():
            add(name, data)
    return path


class RestoreConfigTests(unittest.TestCase):
    def restore(self, **options):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        target = root / 'target'
        target.mkdir()
        recovery.restore_config(kit_archive(root / 'config.tar', **options), target)
        return target

    def test_code_comes_from_this_kit_with_readable_modes_under_umask_077(self):
        previous = os.umask(0o077)
        try:
            target = self.restore()
        finally:
            os.umask(previous)
        for name, source in recovery.kit_files(recovery.KIT, recovery.CODE).items():
            self.assertEqual((target / name).read_bytes(), source.read_bytes())
        self.assertTrue((target / '.secrets/compose.env').is_file())
        self.assertFalse(any('__pycache__' in path.parts for path in target.rglob('*')))
        if os.name == 'posix':
            # uid 10000 Hermes and Paperclip workers read /kit (the container dir).
            for path in [target / 'container', target / 'systemd', target / 'tests']:
                self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            for path in (target / 'container').iterdir():
                self.assertEqual(path.stat().st_mode & 0o044, 0o044, path.name)
            self.assertEqual((target / '.secrets').stat().st_mode & 0o777, 0o700)
            self.assertEqual((target / '.secrets/compose.env').stat().st_mode & 0o777, 0o600)
            self.assertEqual((target / '.secrets/sandbox_ed25519.pub').stat().st_mode & 0o777, 0o644)
            # The kit's policy holds no secret: readable, writable by root only.
            self.assertEqual((target / 'hermes-config.yaml').stat().st_mode & 0o066, 0o044)

    def test_documentation_is_not_compared_and_comes_from_this_kit(self):
        target = self.restore(changes={'README.md': b'older notes\n'})
        self.assertEqual((target / 'README.md').read_bytes(), (recovery.KIT / 'README.md').read_bytes())

    def test_archived_code_or_policy_differing_from_this_kit_is_refused(self):
        hook = b'hooks:\n  on_start: [sh, -c, id]\n'
        cases = [{'changes': {'host-config.py': b'import os\nos.system("id")\n'}},
                 {'changes': {'compose.yaml': b'services: {}\n'}},
                 {'changes': {'hermes-config.yaml': (recovery.KIT / 'hermes-config.yaml').read_bytes() + hook}},
                 {'omit': ('hermes-config.yaml',)},
                 {'extra': {'container/extra.sh': b'#!/bin/sh\n'}},
                 {'omit': ('container/recovery-hermes.py',)}]
        for options in cases:
            with self.subTest(options=options), self.assertRaisesRegex(recovery.RecoveryError, 'different kit code'):
                self.restore(**options)

    def test_refusal_names_a_well_formed_kit_commit_only(self):
        commit = secrets.token_hex(20)
        for recorded, shown in ((commit, commit), ('\x1b]0;x\x07', 'unknown'), (None, 'unknown')):
            with self.subTest(recorded=recorded), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = kit_archive(root / 'config.tar', changes={'compose.yaml': b'services: {}\n'})
                with self.assertRaises(recovery.RecoveryError) as error:
                    recovery.restore_config(archive, root, recorded)
                self.assertIn('(kit commit ' + shown + ')', str(error.exception))

    def test_refusal_notes_a_backup_from_a_modified_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = kit_archive(root / 'config.tar', changes={'compose.yaml': b'services: {}\n'})
            with self.assertRaises(recovery.RecoveryError) as error:
                recovery.restore_config(archive, root, None, True)
            self.assertIn('(kit commit unknown, with local changes)', str(error.exception))

    def test_host_edited_units_are_not_compared_and_come_from_this_kit(self):
        unit = 'systemd/paperclip-hermes-backup.service'
        target = self.restore(changes={unit: b'[Service]\nExecStart=/bin/bash /srv/kit/backup.sh\n'})
        self.assertEqual((target / unit).read_bytes(), (recovery.KIT / unit).read_bytes())

    def test_nothing_written_when_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target'
            target.mkdir()
            archive = kit_archive(root / 'config.tar', changes={'Dockerfile.hermes': b'FROM scratch\n'})
            with self.assertRaises(recovery.RecoveryError):
                recovery.restore_config(archive, target)
            self.assertEqual(list(target.iterdir()), [])

    def test_missing_secret_is_refused(self):
        with self.assertRaisesRegex(recovery.RecoveryError, 'secrets'):
            self.restore(omit=('.secrets/github-token',))

    def test_validation_uses_this_kits_host_config_without_bytecode(self):
        self.assertTrue(recovery.sys.dont_write_bytecode)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            marker = target / 'archive-code-ran'
            (target / 'host-config.py').write_text(
                'from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("x")\n'
                'def validate_env():\n    pass\n')
            with self.assertRaises((SystemExit, recovery.RecoveryError, OSError)):
                recovery.validate_config(target)
            self.assertFalse(marker.exists())


@unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0 and Path('/root').is_dir(),
                     'requires root ownership checks on POSIX')
class RootPathTests(unittest.TestCase):
    def private_parent(self):
        directory = tempfile.TemporaryDirectory(dir='/root')
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def test_new_target_is_created_root_only(self):
        target = recovery.restore_target(self.private_parent() / 'restored')
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_existing_empty_private_target_is_accepted(self):
        target = self.private_parent() / 'restored'
        target.mkdir(mode=0o700)
        self.assertEqual(recovery.restore_target(target), target)

    def test_unsafe_targets_are_refused(self):
        parent = self.private_parent()
        nonempty = parent / 'nonempty'
        nonempty.mkdir(mode=0o700)
        (nonempty / 'file').write_text('x')
        shared = parent / 'shared'
        shared.mkdir(mode=0o700)
        os.chmod(shared, 0o755)
        link = parent / 'link'
        link.symlink_to(parent / 'elsewhere')
        with tempfile.TemporaryDirectory() as world:  # under /tmp, mode 1777 parent
            for target in (nonempty, shared, link, Path(world) / 'restored'):
                with self.subTest(target=target.name), self.assertRaises(recovery.RecoveryError):
                    recovery.restore_target(target)
        self.assertFalse((parent / 'elsewhere').exists())

    def test_lock_is_root_only_and_refuses_links_and_overlap(self):
        parent = self.private_parent()
        with patch.object(recovery, 'LOCK_DIR', parent / 'run'):
            with recovery.locked():
                self.assertEqual((parent / 'run').stat().st_mode & 0o777, 0o700)
                with self.assertRaisesRegex(recovery.RecoveryError, 'Another'):
                    with recovery.locked():
                        pass
            (parent / 'run/op.lock').unlink()
            (parent / 'run/op.lock').symlink_to(parent / 'victim')
            with self.assertRaises(OSError):
                with recovery.locked():
                    pass
            self.assertFalse((parent / 'victim').exists())


class RestoreGuardTests(unittest.TestCase):
    def test_fresh_project_check_includes_stopped_containers(self):
        def run(args, **kwargs):
            return 'stopped-container\n' if args[1] == 'container' and '--all' in args else ''
        args = SimpleNamespace(fenced=True, identity=Path('identity'), target=Path('target'), project='fixture')
        with patch.object(recovery, 'private_file'), patch.object(recovery, 'run', side_effect=run), \
             patch.object(recovery, 'restore_target') as target:
            with self.assertRaisesRegex(recovery.RecoveryError, 'fresh Compose project'):
                recovery.restore(args)
            target.assert_not_called()

    def test_failure_cleanup_removes_anonymous_volumes_and_continues(self):
        compose = MagicMock(side_effect=[recovery.RecoveryError('rm failed'), ''])
        with contextlib.redirect_stderr(io.StringIO()):
            recovery.abandon(compose)
        self.assertEqual(compose.call_args_list, [unittest.mock.call('rm', '--force', '--stop', '--volumes'),
                                                  unittest.mock.call('down')])

    def test_abandon_removes_one_off_containers_before_down(self):
        # compose rm and down skip 'run --rm' one-offs, so a one-off whose
        # client a signal killed would keep the network and volumes in use.
        commands = []
        def run(args, **kwargs):
            commands.append([str(arg) for arg in args])
            if args[:3] == ['docker', 'container', 'ls']:
                return 'oneoff-import\nservice-hermes\n'
            return ''
        with patch.object(recovery, 'run', side_effect=run):
            recovery.abandon(recovery.Compose(Path('/kit'), 'fixture'))
        self.assertEqual(commands[0], ['docker', 'container', 'ls', '--all', '--quiet', '--filter',
                                       'label=com.docker.compose.project=fixture'])
        self.assertEqual(commands[1], ['docker', 'rm', '--force', '--volumes', 'oneoff-import', 'service-hermes'])
        env_file = '.secrets/compose.env'
        actions = [command[command.index(env_file) + 5] if env_file in command else command[1]
                   for command in commands]
        self.assertEqual(actions, ['container', 'rm', 'rm', 'down'])

    def test_abandon_continues_when_the_container_removal_fails(self):
        compose = MagicMock(return_value='')
        compose.remove_containers.side_effect = recovery.RecoveryError('Command failed: docker (exit 1)')
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            recovery.abandon(compose)
        self.assertEqual(compose.call_args_list, [unittest.mock.call('rm', '--force', '--stop', '--volumes'),
                                                  unittest.mock.call('down')])
        self.assertIn('Cleanup step failed: docker rm', errors.getvalue())


class ComposeTests(unittest.TestCase):
    def test_every_compose_operation_uses_private_env_file(self):
        with patch.object(recovery, 'run', return_value='') as command:
            recovery.Compose(Path('/kit'), 'fixture')('ps')
        args = command.call_args.args[0]
        self.assertEqual(args[args.index('--env-file') + 1], '.secrets/compose.env')
        self.assertEqual(args[args.index('--project-name') + 1], 'fixture')

    def test_host_only_check_refuses_an_alternative_mounted_path(self):
        compose = recovery.Compose(Path('/kit'), 'fixture')
        with patch.object(compose, 'container', return_value='container'), \
             patch.object(recovery, 'run', return_value=json.dumps([{'Source': '/host/private'}])), \
             self.assertRaisesRegex(recovery.RecoveryError, 'container mount'):
            compose.require_host_only(Path('/host/private/backup.env'))

    def test_boot_health_wait_does_not_start_services(self):
        compose = MagicMock(return_value='\n'.join(('postgres', *recovery.SERVICES)))
        compose.container.return_value = 'container'
        compose.services.return_value = ['postgres', *recovery.SERVICES]
        with patch.object(recovery, 'run', return_value='healthy\n'), patch.object(recovery.time, 'sleep') as sleep:
            self.assertEqual(set(recovery.wait_stack(compose)), {'postgres', *recovery.SERVICES})
        self.assertEqual(compose.call_args_list, [unittest.mock.call('ps', '--services', '--status', 'running')])
        sleep.assert_not_called()

    def test_one_off_removal_filters_by_project_and_one_off_label(self):
        commands = []
        def run(args, **kwargs):
            commands.append(list(args))
            return 'oneoff-backup\n' if args[1] == 'container' else ''
        with patch.object(recovery, 'run', side_effect=run):
            recovery.Compose(Path('/kit'), 'fixture').remove_containers(one_off_only=True)
        self.assertEqual(commands, [
            ['docker', 'container', 'ls', '--all', '--quiet',
             '--filter', 'label=com.docker.compose.project=fixture',
             '--filter', 'label=com.docker.compose.oneoff=True'],
            ['docker', 'rm', '--force', '--volumes', 'oneoff-backup']])
        commands.clear()
        with patch.object(recovery, 'run', return_value='') as command:
            recovery.Compose(Path('/kit'), 'fixture').remove_containers()
        self.assertEqual(command.call_count, 1)  # nothing to remove: no docker rm

    def test_kit_modified_looks_only_at_the_kit(self):
        # Untracked files elsewhere in the clone (docs/logs) are not the kit.
        with patch.object(recovery, 'run', return_value='') as command:
            self.assertFalse(recovery.kit_modified())
        args = command.call_args.args[0]
        self.assertEqual(args[:4], ['git', '-C', recovery.KIT, 'status'])
        self.assertEqual(args[-2:], ['--', '.'])
        with patch.object(recovery, 'run', return_value='?? container/new.py\n'):
            self.assertTrue(recovery.kit_modified())

    def test_hermes_cli_bypasses_s6_and_uses_private_helper(self):
        with patch.object(recovery, 'run', return_value='') as command:
            recovery.Compose(Path('/kit'), 'fixture').hermes(Path('/stage'), 'import',
                '/recovery/hermes.zip', '--force', '--keep-host', '--messaging-off')
        args = command.call_args.args[0]
        self.assertIn('/kit/recovery-hermes.py', args)
        self.assertIn('--entrypoint', args)
        self.assertIn('--messaging-off', args)
        self.assertIn('--no-deps', args)


if __name__ == '__main__':
    unittest.main()


class RestoredProvenanceTests(unittest.TestCase):
    def test_restore_records_trusted_commit_and_detects_local_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = kit_archive(root / 'config.tar')
            target = root / 'restored'
            target.mkdir()
            trusted = recovery.kit_commit()
            recovery.restore_config(archive, target, 'f' * 40)
            with patch.object(recovery, 'KIT', target):
                self.assertEqual(recovery.kit_commit(), trusted)
                self.assertFalse(recovery.kit_modified())
                (target / 'compose.yaml').write_text('local edit')
                self.assertEqual(recovery.kit_commit(), 'unknown')
                self.assertTrue(recovery.kit_modified())
            self.assertEqual((target / '.kit-provenance.json').stat().st_mode & 0o777, 0o600)
