"""Windows onboarding contracts with synthetic files; never scans real processes."""

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from kb import wechat_runtime as runtime, wechat_setup as setup, wechat_windows as windows


class WindowsSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / 'xwechat_files/account/db_storage'
        self.root.mkdir(parents=True)
        for context in (
            patch.dict(os.environ, {'KB_WECHAT_RUNTIME': str(self.home / 'runtime'),
                                    'RION_WECHAT_READER_CONFIG': str(self.home / 'config/config.json'),
                                    'KB_WECHAT_KEYS_FILE': ''}),
            patch.object(Path, 'home', return_value=self.home),
            patch.object(windows, 'account_roots', return_value={'account': self.root}),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.events = []

    def progress(self, state, message, **extra):
        self.events.append({'state': state, 'message': message, **extra})

    def test_check_only_prepares_provider_and_waits_for_consent(self):
        with patch.object(windows, 'prepare_reader', return_value=['reader']), \
             patch.object(windows, 'prepare_provider', return_value=Path('provider')), \
             patch.object(runtime, 'json_call', return_value={'state': 'needs_access'}), \
             patch.object(runtime, 'run') as run:
            windows.connect(self.progress, '')
        state = self.events[-1]
        self.assertEqual('authorization_required', state['state'])
        self.assertEqual('windows', state['platform'])
        self.assertEqual('account', state['account_id'])
        self.assertTrue(state['confirmation_token'])
        self.assertIn('不会注入', state['message'])
        run.assert_not_called()

    def test_acquisition_imports_verifies_then_checks_actual_reader(self):
        def acquire(argv, **kwargs):
            Path(argv[-1]).write_text(json.dumps({'synthetic': {'enc_key': '0' * 64}}))
            return 0, 'never expose raw output'
        with patch.object(windows, 'prepare_reader', return_value=['reader']), \
             patch.object(windows, 'prepare_provider', return_value=Path('provider')), \
             patch.object(runtime, 'json_call', side_effect=[{'state': 'needs_access'}, {},
                                                           {'state': 'ready_to_configure'}, {}]) as call, \
             patch.object(runtime, 'run', side_effect=acquire) as run, \
             patch.object(setup, '_reader_ready', return_value=True) as ready:
            windows.connect(self.progress, 'account', acquire=True)
        self.assertEqual('ready', self.events[-1]['state'])
        ready.assert_called_once()
        self.assertIn('extract', run.call_args.args[0])
        self.assertNotIn('--decrypt', run.call_args.args[0])
        self.assertNotIn('capture-experimental', run.call_args.args[0])
        self.assertEqual(180, run.call_args.kwargs['timeout'])
        self.assertIn('import-access', call.call_args_list[1].args[0])
        self.assertIn('setup', call.call_args_list[-1].args[0])
        self.assertNotIn('raw output', json.dumps(self.events))

    def test_failed_acquisition_never_publishes_configuration_or_retries(self):
        with patch.object(windows, 'prepare_reader', return_value=['reader']), \
             patch.object(windows, 'prepare_provider', return_value=Path('provider')), \
             patch.object(runtime, 'json_call', return_value={'state': 'needs_access'}), \
             patch.object(runtime, 'run', return_value=(1, 'secret raw output')) as run:
            with self.assertRaises(runtime.SetupError) as error:
                windows.connect(self.progress, 'account', acquire=True)
        self.assertNotIn('secret', str(error.exception))
        self.assertFalse(setup._config().exists())
        run.assert_called_once()

    def test_existing_broken_configuration_is_preserved(self):
        config = setup._config()
        config.parent.mkdir()
        config.write_text('{}')
        with patch.object(windows, 'prepare_reader', return_value=['reader']), \
             patch.object(windows, 'prepare_provider') as provider, \
             patch.object(setup, '_reader_ready', return_value=False):
            windows.connect(self.progress, '', acquire=True)
        self.assertEqual('existing_configuration_requires_review', self.events[-1]['state'])
        self.assertEqual('{}', config.read_text())
        provider.assert_not_called()

    def test_existing_material_is_not_reacquired_when_verification_fails(self):
        keys = setup._config().parent / 'keys.json'
        keys.parent.mkdir()
        keys.write_text('{}')
        with patch.object(windows, 'prepare_reader', return_value=['reader']), \
             patch.object(windows, 'prepare_provider') as provider, \
             patch.object(runtime, 'json_call', return_value={'state': 'verification_failed'}):
            windows.connect(self.progress, 'account', acquire=True)
        self.assertEqual('verification_failed', self.events[-1]['state'])
        provider.assert_not_called()

    def test_missing_explicit_material_is_not_silently_replaced_by_acquisition(self):
        with patch.dict(os.environ, {'KB_WECHAT_KEYS_FILE': str(self.home / 'missing.json')}), \
             patch.object(windows, 'prepare_reader', return_value=['reader']), \
             patch.object(windows, 'prepare_provider') as provider, \
             patch.object(runtime, 'json_call', return_value={'state': 'needs_access'}):
            windows.connect(self.progress, '')
        self.assertNotEqual('authorization_required', self.events[-1]['state'])
        provider.assert_not_called()

    def test_partial_database_coverage_is_not_published_as_ready(self):
        with patch.object(windows, 'prepare_reader', return_value=['reader']), \
             patch.object(windows, 'prepare_provider') as provider, \
             patch.object(runtime, 'json_call', return_value={'state': 'partial'}) as call:
            windows.connect(self.progress, '')
        self.assertEqual('partial', self.events[-1]['state'])
        self.assertFalse(setup._config().exists())
        self.assertEqual(1, call.call_count)
        provider.assert_not_called()

    def test_ready_plan_still_requires_actual_collector_reader(self):
        with patch.object(windows, 'prepare_reader', return_value=['reader']), \
             patch.object(runtime, 'json_call', side_effect=[{'state': 'ready_to_configure'}, {}]), \
             patch.object(setup, '_reader_ready', return_value=False):
            with self.assertRaises(runtime.SetupError):
                windows.connect(self.progress, '')
        self.assertFalse(any(event['state'] == 'ready' for event in self.events))

    def test_missing_or_multiple_accounts_do_not_start_tools(self):
        for roots, expected in (({}, 'needs_database_location'),
                                ({'one': self.root, 'two': self.root.parent / 'other'}, 'account_selection_required')):
            with self.subTest(expected=expected), patch.object(windows, 'account_roots', return_value=roots), \
                 patch.object(windows, 'prepare_reader') as reader:
                windows.connect(self.progress, '')
            self.assertEqual(expected, self.events[-1]['state'])
            reader.assert_not_called()

    def test_changed_account_is_rejected_before_tool_preparation(self):
        with patch.object(windows, 'prepare_reader') as reader:
            with self.assertRaises(runtime.SetupError):
                windows.connect(self.progress, 'old-account', acquire=True)
        reader.assert_not_called()

    def test_windows_consent_is_bound_to_the_reviewed_provider_revision(self):
        runtime.private_dir(runtime.runtime_home())
        setup._write({'state': 'authorization_required', 'confirmation_token': 'test-token',
                      'expires_at': time.time() + 60, 'platform': 'windows',
                      'provider_revision': 'old-revision'})
        with patch.object(runtime.platform, 'system', return_value='Windows'), \
             patch.object(threading.Thread, 'start') as start:
            with self.assertRaisesRegex(runtime.SetupError, '已变化'):
                setup.start('acquire', confirmation_token='test-token', confirmed=True)
        start.assert_not_called()


class WindowsDiscoveryTests(unittest.TestCase):
    def test_document_and_ini_roots_are_deduplicated_and_account_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            root = home / 'Documents/xwechat_files/account/db_storage'
            root.mkdir(parents=True)
            config = home / 'appdata/Tencent/xwechat/config'
            config.mkdir(parents=True)
            (config / 'location.ini').write_text(str(home / 'Documents'), encoding='utf-8')
            with patch.object(Path, 'home', return_value=home), \
                 patch.dict(os.environ, {'APPDATA': str(home / 'appdata'), 'KB_WECHAT_DATABASE_ROOT': ''}):
                roots = windows.account_roots()
            self.assertIn(root, roots.values())
            self.assertEqual(1, list(roots.values()).count(root))
            self.assertTrue(all(len(identity) == 16 for identity in roots))

    def test_explicit_database_root_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.dict(os.environ, {'KB_WECHAT_DATABASE_ROOT': str(root)}):
                self.assertIn(root, windows.account_roots().values())


class WindowsRuntimeTests(unittest.TestCase):
    def test_windows_dependencies_are_pinned_with_hashes(self):
        lines = windows.REQUIREMENTS.splitlines()
        self.assertEqual(3, len(lines))
        self.assertTrue(all('==' in line and '--hash=sha256:' in line for line in lines))

    def test_unavailable_architecture_cannot_download_or_execute_tools(self):
        with patch.object(windows.platform, 'machine', return_value='ARM64'), \
             patch.object(runtime, 'download') as download:
            with self.assertRaises(runtime.SetupError):
                windows.prepare_reader(lambda *args: None)
        download.assert_not_called()

    def test_reader_self_test_must_explicitly_pass_before_becoming_available(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            python = source / 'venv/Scripts/python.exe'
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(windows.platform, 'machine', return_value='AMD64'), \
                 patch.object(windows, 'reader_paths', return_value=(source, python, source / 'reader_launcher.py')), \
                 patch.object(runtime, 'download'), \
                 patch.object(runtime, 'run', return_value=(0, '')), \
                 patch.object(runtime, 'json_call', return_value={'passed': False}):
                with self.assertRaisesRegex(runtime.SetupError, '自检未通过'):
                    windows.prepare_reader(lambda *args: None)

    def test_managed_reader_uses_python_argv_not_a_shell_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.dict(os.environ, {'KB_WECHAT_RUNTIME': str(root), 'RION_WECHAT_CLI_BIN': '',
                                         'RION_WECHAT_READER_BIN': '', 'CODEX_HOME': str(root / 'codex')}), \
                 patch.object(runtime.shutil, 'which', return_value=None):
                _, python, launcher = windows.reader_paths()
                python.parent.mkdir(parents=True)
                python.touch()
                launcher.touch()
                if os.name == 'nt':
                    self.assertEqual([str(python), str(launcher)], runtime.reader_command())

    @unittest.skipUnless(os.name == 'nt', 'Windows native ACL test')
    def test_runtime_directory_has_a_protected_windows_dacl(self):
        import ctypes
        from ctypes import wintypes
        from kb.wechat_windows_security import current_sid

        with tempfile.TemporaryDirectory() as directory:
            private = runtime.private_dir(Path(directory) / 'private')
            security = ctypes.WinDLL('advapi32', use_last_error=True)
            size = wintypes.DWORD()
            security.GetFileSecurityW(str(private), 4, None, 0, ctypes.byref(size))
            descriptor = ctypes.create_string_buffer(size.value)
            self.assertTrue(security.GetFileSecurityW(str(private), 4, descriptor, size, ctypes.byref(size)))
            text = wintypes.LPWSTR()
            convert = security.ConvertSecurityDescriptorToStringSecurityDescriptorW
            convert.argtypes = [wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
            self.assertTrue(convert(descriptor, 1, 4, ctypes.byref(text), None))
            try:
                self.assertTrue(text.value.startswith('D:P'))
                self.assertIn(current_sid(), text.value)
                self.assertNotIn(';;;WD)', text.value)
                self.assertNotIn(';;;BU)', text.value)
            finally:
                kernel = ctypes.WinDLL('kernel32')
                kernel.LocalFree.argtypes = [wintypes.LPVOID]
                kernel.LocalFree(ctypes.cast(text, wintypes.LPVOID))


if __name__ == '__main__':
    unittest.main()
