"""Onboarding contracts; never acquires keys or opens real user databases."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import wechat_access_adapter as adapter, wechat_runtime as runtime, wechat_setup as setup
from kb.web import wechat_api


@unittest.skipUnless(shutil.which('node'), '需要 node 执行接入提示词回归')
class AgentPromptTests(unittest.TestCase):
    def test_frontend_prompt_contract(self):
        probe = Path(__file__).with_name('wechat_agent_test.mjs')
        result = subprocess.run([shutil.which('node'), '--test', str(probe)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.runtime = self.home / 'runtime'
        self.env = patch.dict(os.environ, {'KB_WECHAT_RUNTIME': str(self.runtime)}, clear=False)
        self.env.start()
        self.home_patch = patch.object(Path, 'home', return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.events = []

    def progress(self, state, message, **extra):
        self.events.append({'state': state, 'message': message, **extra})

    def account(self, name='account-1'):
        root = self.home / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files' / name / 'db_storage'
        root.mkdir(parents=True)
        return root

    def test_existing_ready_reused_without_install_acquire_or_backup(self):
        with patch.object(setup, '_reader_ready', return_value=True), \
             patch.object(runtime, 'prepare_reader') as reader, \
             patch.object(runtime, 'prepare_provider') as provider, \
             patch.object(setup, '_backup') as backup:
            setup._connect(self.progress, '')
        self.assertEqual('ready', self.events[-1]['state'])
        reader.assert_not_called(); provider.assert_not_called(); backup.assert_not_called()

    def test_no_database_does_not_install_or_acquire(self):
        with patch.object(setup, '_reader_ready', return_value=False), \
             patch.object(runtime, 'prepare_reader') as reader:
            setup._connect(self.progress, '')
        self.assertEqual('needs_database_location', self.events[-1]['state'])
        reader.assert_not_called()

    def test_multiple_accounts_require_explicit_selection(self):
        self.account('one'); self.account('two')
        with patch.object(setup, '_reader_ready', return_value=False), \
             patch.object(runtime, 'prepare_reader') as reader:
            setup._connect(self.progress, '')
        self.assertEqual('account_selection_required', self.events[-1]['state'])
        self.assertEqual(2, len(self.events[-1]['accounts']))
        self.assertNotIn(str(self.home), json.dumps(self.events))
        reader.assert_not_called()

    def test_missing_key_prepares_then_waits_for_consent(self):
        self.account()
        with patch.object(setup, '_reader_ready', return_value=False), \
             patch.object(runtime, 'prepare_reader', return_value=['helper']), \
             patch.object(runtime, 'prepare_provider', return_value=(Path('/provider'), 'digest')), \
             patch.object(runtime, 'json_call', side_effect=[{'state': 'provider_required'}, {'state': 'authorization_required'}]) as call:
            setup._connect(self.progress, '')
        self.assertEqual('authorization_required', self.events[-1]['state'])
        self.assertTrue(self.events[-1]['confirmation_token'])
        self.assertFalse(any('--apply' in c.args[0] for c in call.call_args_list))

    def test_existing_broken_config_is_not_replaced_or_reacquired(self):
        self.account()
        config = setup._config(); config.parent.mkdir(parents=True); config.write_text('{}')
        with patch.object(setup, '_reader_ready', return_value=False), \
             patch.object(runtime, 'prepare_reader', return_value=['helper']), \
             patch.object(runtime, 'prepare_provider') as provider, \
             patch.object(runtime, 'json_call', return_value={'state': 'existing_configuration_requires_review'}) as call:
            setup._connect(self.progress, '')
        provider.assert_not_called()
        self.assertNotIn('--database-root', call.call_args.args[0])
        self.assertEqual('{}', config.read_text())

    def test_existing_wxkey_material_is_verified_and_configured_without_provider(self):
        self.account()
        source = self.home / '.config/wxcli/config.json'
        source.parent.mkdir(parents=True); source.write_text('{}')
        with patch.object(setup, '_reader_ready', side_effect=[False, True]), \
             patch.object(runtime, 'prepare_reader', return_value=['helper']), \
             patch.object(runtime, 'prepare_provider') as provider, \
             patch.object(runtime, 'json_call', side_effect=[{'state': 'ready_to_configure'}, {'state': 'ready'}]) as call:
            setup._connect(self.progress, '')
        self.assertIn('--source', call.call_args.args[0])
        self.assertIn('--apply', call.call_args.args[0])
        provider.assert_not_called()
        self.assertEqual('ready', self.events[-1]['state'])

    def test_acquisition_is_followed_by_actual_reader_verification(self):
        self.account()
        with patch.object(setup, '_reader_ready', side_effect=[False, True]), \
             patch.object(runtime, 'prepare_reader', return_value=['helper']), \
             patch.object(runtime, 'prepare_provider', return_value=(Path('/provider'), 'digest')), \
             patch.object(setup, '_backup') as backup, \
             patch.object(runtime, 'json_call', side_effect=[{'state': 'provider_required'}, {'state': 'authorization_required'}, {'state': 'ready'}]) as call:
            setup._connect(self.progress, '', acquire=True)
        self.assertIn('--confirm-side-effects', call.call_args.args[0])
        self.assertEqual(840, call.call_args.kwargs['timeout'])
        backup.assert_called_once()
        self.assertEqual('ready', self.events[-1]['state'])

    def test_recovery_lock_prevents_repeated_acquisition(self):
        lock = setup._recovery_lock(); lock.parent.mkdir(parents=True); lock.touch()
        with patch.object(setup, '_reader_ready', return_value=False), \
             patch.object(runtime, 'prepare_reader') as reader:
            with self.assertRaises(runtime.SetupError):
                setup._connect(self.progress, '', acquire=True)
        reader.assert_not_called()
        self.assertTrue(lock.exists())

    def test_invalid_expired_or_unconfirmed_tokens_rejected(self):
        runtime.private_dir(self.runtime)
        for supplied, confirmed, expiry in [('wrong', True, time.time()+30), ('token', False, time.time()+30), ('token', True, 0)]:
            setup._write({'state': 'authorization_required', 'confirmation_token': 'token', 'expires_at': expiry})
            with patch.object(threading.Thread, 'start') as start:
                with self.assertRaises(runtime.SetupError):
                    setup.start('acquire', confirmation_token=supplied, confirmed=confirmed)
                start.assert_not_called()

    def test_single_background_job_and_refresh_survive_duplicate_click(self):
        entered, release = threading.Event(), threading.Event()
        def connect(progress, *args):
            entered.set(); release.wait(3); progress('ready', 'verified', ready=True)
        with patch.object(setup, '_connect', side_effect=connect):
            setup.start(); self.assertTrue(entered.wait(1))
            try:
                with self.assertRaises(runtime.SetupError):
                    setup.start()
                self.assertTrue(setup.snapshot()['running'])
            finally:
                release.set()
            for _ in range(100):
                if not setup._read()['running']:
                    break
                time.sleep(.01)
        self.assertTrue(setup.snapshot()['ready'])
        self.assertEqual(0o600, setup._state_file().stat().st_mode & 0o777)

    def test_crashed_job_shows_interrupted_without_auto_restart(self):
        runtime.private_dir(self.runtime)
        setup._write({'state': 'acquiring', 'running': True})
        self.assertEqual('interrupted', setup.snapshot()['state'])

    def test_raw_exceptions_never_enter_public_status(self):
        fd = setup._lock()
        with patch.object(setup, '_connect', side_effect=ValueError('secret_key_private_path')):
            setup._work(fd, '', False)
        result = setup.snapshot()
        self.assertEqual('failed', result['state'])
        self.assertNotIn('secret_key', json.dumps(result))

    def test_download_mismatch_cannot_publish_code(self):
        response = Mock(); response.read.return_value = b'wrong-source'
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        target = self.home / 'reader.py'
        with patch.object(runtime, 'urlopen', return_value=response):
            with self.assertRaises(runtime.SetupError):
                runtime.download('https://example.com/code', target, '0'*64)
        self.assertFalse(target.exists())

    def test_subprocess_timeout_is_redacted(self):
        import sys
        with self.assertRaises(runtime.SetupError) as error:
            runtime.run([sys.executable, '-c', 'import time; print("secret_key",flush=True); time.sleep(5)'], timeout=.05)
        self.assertNotIn('secret_key', str(error.exception))


class DiscoveryFixTests(unittest.TestCase):
    def test_decryptable_auxiliary_is_verified_without_becoming_a_core_db(self):
        class ReaderError(Exception): pass
        reader = SimpleNamespace(ReaderError=ReaderError, classify_database_tables=lambda _: [])
        adapter.install_discovery_fix(reader)
        @contextmanager
        def connect(path):
            yield SimpleNamespace(execute=lambda _: [('unknown_auxiliary',)])
        kinds = reader.inspect_authorized_database(SimpleNamespace(connect=connect), Path('aux.db'))
        self.assertTrue(bool(kinds))
        self.assertEqual([], list(kinds))
        @contextmanager
        def unreadable(path):
            raise ReaderError('invalid key')
            yield
        self.assertFalse(reader.inspect_authorized_database(SimpleNamespace(connect=unreadable), Path('bad.db')))


class APITests(unittest.TestCase):
    def setUp(self):
        app = FastAPI(); app.include_router(wechat_api.router)
        self.client = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 54321))
        self.headers = {'X-KB-Wechat-Action': '1', 'Origin': 'http://127.0.0.1'}

    def test_local_click_starts_background_job(self):
        with patch.object(setup, 'start', return_value={'running': True}) as start:
            response = self.client.post('/api/wechat/setup', json={}, headers=self.headers)
        self.assertEqual(202, response.status_code); start.assert_called_once()

    def test_cross_origin_rebinding_and_unmarked_posts_rejected(self):
        for headers in ({}, {**self.headers, 'Origin': 'https://evil.example'},
                        {**self.headers, 'Host': 'evil.example'},
                        {**self.headers, 'Sec-Fetch-Site': 'cross-site'}):
            with patch.object(setup, 'start') as start:
                self.assertEqual(403, self.client.post('/api/wechat/setup', json={}, headers=headers).status_code)
                start.assert_not_called()

    def test_remote_client_cannot_read_status_or_start(self):
        app = FastAPI(); app.include_router(wechat_api.router)
        client = TestClient(app, base_url='http://127.0.0.1', client=('192.168.1.2', 54321))
        self.assertEqual(403, client.get('/api/wechat/setup').status_code)
        self.assertEqual(403, client.post('/api/wechat/setup', json={}, headers=self.headers).status_code)

    def test_status_read_does_not_start_acquisition(self):
        with patch.object(setup, 'snapshot', return_value={'state': 'unchecked'}), \
             patch.object(setup, 'start') as start:
            response = self.client.get('/api/wechat/setup')
        self.assertEqual(200, response.status_code); start.assert_not_called()
        self.assertNotIn('keys', response.json())


if __name__ == '__main__':
    unittest.main()
