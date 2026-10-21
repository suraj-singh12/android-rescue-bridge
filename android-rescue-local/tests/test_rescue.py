"""Synthetic regression tests; never start ADB or access a real phone."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('rescue', Path(__file__).parents[1] / 'android_rescue.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class FakeAdb:
    serial = 'synthetic'
    prefix = ['unused-adb']

    def __init__(self, *args):
        self.files = {'/sdcard/A.jpg': b'first', '/sdcard/a.jpg': b'second'}
        self.fail_batch = False
        self.copies = 0

    def supports_find_print0(self, root):
        return True

    def iter_find(self, root, kind, sink):
        yield from (self.files if kind == 'f' else [root])

    def pull_many(self, remotes, dest, timeout):
        if self.fail_batch:
            return False, 'batch failure'
        for remote in remotes:
            self.pull_one(remote, dest / Path(remote).name, timeout)
        return True, ''

    def pull_one(self, remote, local, timeout):
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self.files[remote])
        self.copies += 1
        return True, ''

    def shell_text(self, *args, **kwargs):
        return 'package:com.test.debug'

    def run(self, *args, **kwargs):
        return subprocess.CompletedProcess([], 0, '/data/user/0/com.test.debug', '')


class RescueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name) / 'case'
        self.log = r.Logger(self.case / 'logs/acquisition.log')
        self.addCleanup(self.log.close)
        self.db = r.StateDB(self.case / '.state/rescue.sqlite3')
        self.addCleanup(self.db.close)
        self.root = {'label': 'primary', 'remote': '/sdcard'}
        self.adb = FakeAdb()
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def copy(self, rows):
        return r.acquire_storage_rows(self.adb, self.db, self.case, self.root, rows, 3, self.log)

    def test_copy_selection_collisions_retry_and_verified_resume(self):
        rows = r.queue_extension_category(self.adb, self.db, [self.root], r.IMAGE_EXTENSIONS,
                                          self.log, 'photos', {})['primary']
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][1].lower(), rows[1][1].lower())
        self.adb.fail_batch = True
        self.assertEqual(self.copy(rows)['copied'], 2)
        for remote, local, _ in rows:
            self.assertEqual((self.case / local).read_bytes(), self.adb.files[remote])
        before = self.adb.copies
        self.assertEqual(self.copy(rows)['skipped'], 2)
        self.assertEqual(before, self.adb.copies)
        # Same-size corruption must be detected, not just truncation.
        local = self.case / rows[0][1]
        local.write_bytes(b'x' * local.stat().st_size)
        self.assertEqual(self.copy(rows)['copied'], 1)
        self.assertEqual(local.read_bytes(), self.adb.files[rows[0][0]])
        local.unlink()
        self.assertEqual(self.copy(rows)['copied'], 1)

    def test_legacy_database_done_row_without_hash_is_recopied(self):
        row = r.register_storage_file(self.db, self.root, '/sdcard/A.jpg')
        local = self.case / row[1]
        local.parent.mkdir(parents=True)
        local.write_bytes(b'old')
        self.db.conn.execute("UPDATE storage_files SET status='done'")
        self.assertEqual(self.copy([row])['copied'], 1)
        self.assertTrue(self.db.verified_file(row[1]))

    def test_archive_interrupt_cannot_be_resumed_as_success(self):
        def interrupted(cmd, dest, **kwargs):
            dest.write_bytes(b'incomplete archive')
            raise KeyboardInterrupt
        with patch.object(r, 'run_stream_to_file_with_progress', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                r.archive_debuggable_private_data(self.adb, self.case, 3, self.log)
        target = self.case / 'apps/debuggable_private/com.test.debug.tar'
        self.assertFalse(target.exists())
        target.write_bytes(b'legacy corrupt archive')
        def complete(cmd, dest, **kwargs):
            with tarfile.open(dest, 'w') as tf:
                data = b'valid payload'
                member = tarfile.TarInfo('data.txt'); member.size = len(data)
                tf.addfile(member, io.BytesIO(data))
            return 0, ''
        with patch.object(r, 'run_stream_to_file_with_progress', side_effect=complete) as stream:
            r.archive_debuggable_private_data(self.adb, self.case, 3, self.log)
            self.assertEqual(stream.call_count, 1)
            r.archive_debuggable_private_data(self.adb, self.case, 3, self.log)
            self.assertEqual(stream.call_count, 1)
        with tarfile.open(target) as tf:
            self.assertEqual(tf.extractfile('data.txt').read(), b'valid payload')

    def test_truncated_archive_payload_is_rejected(self):
        def truncated(cmd, dest, **kwargs):
            member = tarfile.TarInfo('large'); member.size = 4096
            dest.write_bytes(member.tobuf() + b'tiny')
            return 0, ''
        with patch.object(r, 'run_stream_to_file_with_progress', side_effect=truncated):
            r.archive_debuggable_private_data(self.adb, self.case, 3, self.log)
        info = json.loads((self.case / 'apps/debuggable_private/status.json').read_text())
        self.assertFalse(info['com.test.debug']['archived'])
        self.assertTrue(self.log.issues)

    def test_apk_resume_detects_corruption(self):
        remote = '/sdcard/A.jpg'
        rel = 'apps/apks/com.test/base.apk'
        self.db.add_artifact('apk', 'com.test', remote, rel)
        local = self.case / rel
        local.parent.mkdir(parents=True)
        local.write_bytes(b'original')
        self.db.set_artifact('apk', 'com.test', remote, 'done')
        local.write_bytes(b'corrupt!')
        self.assertFalse(self.db.verified_file(rel))
        with patch.object(r, 'get_packages', return_value=[]):
            r.acquire_apks(self.adb, self.db, self.case, 'all', 3, self.log)
        self.assertEqual(local.read_bytes(), self.adb.files[remote])
        self.assertTrue(self.db.verified_file(rel))

    def test_provider_denial_is_in_failure_report(self):
        denied = subprocess.CompletedProcess([], 1, '', 'Permission denial')
        with patch.object(self.adb, 'shell', return_value=denied, create=True):
            status = r.export_content_providers(self.adb, self.case, self.log)
        self.assertTrue(all(not item['ok'] for item in status.values()))
        self.assertEqual(r.write_failures_report(self.db, self.case, self.log), 5)

    def run_main(self, denied=False, no_hash=False):
        case = Path(self.tmp.name) / 'main-case'
        class Denied(FakeAdb):
            def iter_find(self, root, kind, sink):
                sink(1, 'connection lost')
                yield from []
        argv = ['rescue', '--authorized', '--mode', 'auto', '--output', self.tmp.name,
                '--case-name', 'main-case', '--apk-mode', 'none', '--no-debuggable-data',
                '--no-providers', '--no-bugreport']
        if no_hash:
            argv.append('--no-hash')
        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with contextlib.ExitStack() as stack:
            for name, value in [('Adb', Denied if denied else FakeAdb),
                                ('require_program', lambda x: 'unused'),
                                ('adb_global', lambda *a, **k: None),
                                ('choose_device', lambda *a: 'synthetic'),
                                ('case_identity', lambda *a: {'model': 'synthetic'}),
                                ('collect_metadata', lambda *a: None),
                                ('discover_storage_roots', lambda *a: [self.root])]:
                stack.enter_context(patch.object(r, name, value))
            stack.enter_context(patch.object(sys, 'argv', argv))
            try:
                rc = r.main()
            finally:
                for sig, handler in saved.items():
                    signal.signal(sig, handler)
        return rc, case

    def test_main_failed_scan_is_not_clean_success(self):
        rc, case = self.run_main(denied=True)
        meta = json.loads((case / 'metadata/acquisition.json').read_text())
        self.assertEqual(rc, 2)
        self.assertEqual(meta['status'], 'complete_with_errors')
        self.assertGreater(meta['failure_count'], 0)
        self.assertIn('connection lost', (case / 'reports/failures.csv').read_text())

    def test_final_manifest_matches_every_published_file(self):
        rc, case = self.run_main()
        self.assertEqual(rc, 0)
        sums = (case / 'manifests/SHA256SUMS.txt').read_text().splitlines()
        self.assertTrue(any(line.endswith('metadata/acquisition.json') for line in sums))
        for line in sums:
            digest, name = line.split('  ', 1)
            self.assertEqual(digest, hashlib.sha256((case / name).read_bytes()).hexdigest())

    def test_no_hash_keeps_resume_integrity_without_manifests(self):
        rc, case = self.run_main(no_hash=True)
        self.assertEqual(rc, 0)
        self.assertFalse((case / 'manifests/SHA256SUMS.txt').exists())
        db = r.StateDB(case / '.state/rescue.sqlite3')
        try:
            self.assertTrue(all(db.verified_file(row[1]) for row in db.storage_rows('primary')))
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
