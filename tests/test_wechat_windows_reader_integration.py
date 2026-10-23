"""Pinned Windows Reader regression using only newly created encrypted databases.

Run with the prepared Windows runtime's Python and set
KB_WECHAT_TEST_WINDOWS_RUNTIME to its source directory. No downloads or acquisition.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from kb import wechat_runtime as runtime, wechat_setup as setup, wechat_windows as windows


@unittest.skipUnless(os.getenv('KB_WECHAT_TEST_WINDOWS_RUNTIME'), 'requires pinned Windows Reader and SQLCipher')
class WindowsEncryptedReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(os.environ['KB_WECHAT_TEST_WINDOWS_RUNTIME'])
        for name, digest in windows.READER_FILES.items():
            if hashlib.sha256((cls.source / name).read_bytes()).hexdigest() != digest:
                raise AssertionError('The integration fixture must use the pinned Windows Reader')
        from sqlcipher3 import dbapi2
        cls.driver = dbapi2

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = runtime.private_dir(Path(temporary.name).resolve())
        self.root = self.home / 'account/db_storage'
        self.root.mkdir(parents=True)
        self.reader = self.home / 'isolated-reader'
        self.reader.mkdir()
        for name in windows.READER_FILES:
            shutil.copyfile(self.source / name, self.reader / name)
        project = Path(__file__).resolve().parents[1]
        shutil.copyfile(project / 'kb/wechat_windows_reader.py', self.reader / 'reader_launcher.py')
        shutil.copyfile(project / 'kb/wechat_access_adapter.py', self.reader / 'wechat_access_adapter.py')
        self.command = [sys.executable, str(self.reader / 'reader_launcher.py')]
        self.config = self.home / 'configuration/config.json'
        self.keys = self.config.parent / 'keys.json'
        self.config.parent.mkdir()
        for context in (
            patch.dict(os.environ, {'KB_WECHAT_RUNTIME': str(self.home / 'runtime'),
                                    'RION_WECHAT_READER_CONFIG': str(self.config),
                                    'KB_WECHAT_KEYS_FILE': '', 'PYTHONUTF8': '1'}),
            patch.object(windows, 'account_roots', return_value={'synthetic-account': self.root}),
            patch.object(windows, 'prepare_reader', return_value=self.command),
        ):
            context.start()
            self.addCleanup(context.stop)
        os.environ.pop('RION_WECHAT_READER_KEYS', None)
        self.schemas = {
            'session.db': 'CREATE TABLE SessionTable (username TEXT)',
            'contact.db': 'CREATE TABLE contact (username TEXT)',
            'message.db': 'CREATE TABLE Name2Id (username TEXT); CREATE TABLE Msg_test (local_id INTEGER)',
            'aux.db': 'CREATE TABLE auxiliary_data (value TEXT)',
        }
        for name, schema in self.schemas.items():
            connection = self.driver.connect(str(self.root / name))
            try:
                connection.execute('PRAGMA key="x\'' + '11' * 32 + '\'"')
                connection.executescript(schema)
                connection.commit()
            finally:
                connection.close()
        self.events = []

    def progress(self, state, message, **extra):
        self.events.append({'state': state, 'message': message, **extra})

    def material(self, names, key='11'):
        return {'keys': {name: {'enc_key': key * 32, 'cipher_compatibility': 4} for name in names}}

    def write_material(self, path, material):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(material), encoding='utf-8')
        path.chmod(0o600)

    def plan(self):
        return runtime.json_call([*self.command, '--config', str(self.config), 'access-plan',
                                  '--database-root', str(self.root), '--keys-file', str(self.keys)])

    def test_completely_incompatible_bundle_is_rejected_without_installing_keys(self):
        bundle = self.home / 'wrong-bundle.json'
        self.write_material(bundle, self.material(self.schemas, key='22'))
        result = subprocess.run([*self.command, '--config', str(self.config), 'import-access',
                                 '--source', str(bundle), '--database-root', str(self.root),
                                 '--keys-file', str(self.keys)], capture_output=True,
                                text=True, encoding='utf-8', timeout=120)
        envelope = json.loads(result.stdout)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(envelope['ok'])
        self.assertEqual('access_bundle_not_compatible', envelope['error']['code'])
        self.assertFalse(self.keys.exists())
        self.assertFalse(self.config.exists())

    def test_wrong_keys_leave_every_database_unresolved(self):
        self.write_material(self.keys, self.material(self.schemas, key='22'))
        result = self.plan()
        self.assertEqual('verification_failed', result['state'])
        self.assertEqual(4, result['counts']['unresolved_databases'])
        self.assertFalse(self.config.exists())

    def test_readable_unknown_auxiliary_database_is_counted_as_verified(self):
        self.write_material(self.keys, self.material(self.schemas))
        result = self.plan()
        self.assertEqual('ready_to_configure', result['state'])
        self.assertEqual(0, result['counts']['unresolved_databases'])
        self.assertFalse(self.config.exists())

    def test_missing_auxiliary_key_is_not_counted_as_verified(self):
        self.write_material(self.keys, self.material(name for name in self.schemas if name != 'aux.db'))
        result = self.plan()
        self.assertEqual('partial', result['state'])
        self.assertEqual(1, result['counts']['unresolved_databases'])
        self.assertFalse(self.config.exists())

    def test_core_only_bundle_stays_partial_through_the_real_onboarding_boundary(self):
        bundle = windows.reader_paths()[0] / 'access-synthetic-account/all_keys.json'
        self.write_material(bundle, self.material(name for name in self.schemas if name != 'aux.db'))
        with patch.object(windows, 'prepare_provider') as provider:
            windows.connect(self.progress, 'synthetic-account')
        self.assertEqual('partial', self.events[-1]['state'])
        self.assertEqual(1, self.plan()['counts']['unresolved_databases'])
        self.assertTrue(self.keys.exists())
        self.assertTrue(bundle.exists())
        self.assertFalse(self.config.exists())
        self.assertIn(str(bundle), self.events[-1]['message'])
        self.assertFalse(any(event['state'] == 'ready' for event in self.events))
        provider.assert_not_called()

    def test_incompatible_bundle_stays_recoverable_through_the_real_onboarding_boundary(self):
        bundle = windows.reader_paths()[0] / 'access-synthetic-account/all_keys.json'
        self.write_material(bundle, self.material(self.schemas, key='22'))
        with patch.object(windows, 'prepare_provider') as provider:
            windows.connect(self.progress, 'synthetic-account')
        self.assertEqual('access_material_requires_review', self.events[-1]['state'])
        self.assertIn(str(bundle), self.events[-1]['message'])
        self.assertTrue(bundle.exists())
        self.assertFalse(self.keys.exists())
        self.assertFalse(self.config.exists())
        provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()
