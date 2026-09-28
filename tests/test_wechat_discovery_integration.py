"""Optional real SQLCipher regression against the downloaded, pinned Reader.

Run with the isolated runtime's Python and KB_WECHAT_TEST_RUNTIME pointing at
the directory containing rion_wechat_reader.py. Uses only synthetic databases.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from kb.wechat_access_adapter import install_discovery_fix


@unittest.skipUnless(os.getenv('KB_WECHAT_TEST_RUNTIME'), 'requires prepared isolated Reader runtime')
class EncryptedDiscoveryTests(unittest.TestCase):
    def test_auxiliary_database_is_counted_only_when_actually_readable(self):
        sys.path.insert(0, os.environ['KB_WECHAT_TEST_RUNTIME'])
        import rion_wechat_reader as reader
        from sqlcipher3 import dbapi2
        original = reader.inspect_authorized_database
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                key = '11' * 32  # Synthetic raw key, never user material.
                schemas = {
                    'session.db': 'CREATE TABLE SessionTable (username TEXT)',
                    'contact.db': 'CREATE TABLE contact (username TEXT)',
                    'message.db': 'CREATE TABLE Name2Id (username TEXT); CREATE TABLE Msg_test (local_id INTEGER)',
                    'aux.db': 'CREATE TABLE auxiliary_cache (value TEXT)',
                }
                for filename, sql in schemas.items():
                    conn = dbapi2.connect(root / filename)
                    conn.execute(f'PRAGMA key="x\'{key}\'"')
                    conn.executescript(sql); conn.commit(); conn.close()
                keys = root / 'keys.json'
                keys.write_text(json.dumps({'keys': {name: {'enc_key': key, 'cipher_compatibility': 4}
                                                     for name in schemas}}))
                keys.chmod(0o600)
                config = root / 'not-published.json'
                before = reader.access_plan(config, root, keys)
                self.assertEqual('partial', before['state'])
                install_discovery_fix(reader)
                after = reader.access_plan(config, root, keys)
                self.assertEqual('ready_to_configure', after['state'])
                self.assertEqual(0, after['counts']['unresolved_databases'])
                self.assertFalse(config.exists())
                (root / 'aux.db').write_bytes(b'not a readable database')
                failed = reader.access_plan(config, root, keys)
                self.assertEqual('partial', failed['state'])
                self.assertEqual(1, failed['counts']['unresolved_databases'])
        finally:
            reader.inspect_authorized_database = original
            sys.path.pop(0)


if __name__ == '__main__':
    unittest.main()
