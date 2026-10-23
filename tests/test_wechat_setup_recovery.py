"""State recovery must distinguish an absent job from an unreadable status."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kb import wechat_runtime as runtime, wechat_setup as setup


TERMINAL_RESULTS = {
    'ready': {'ready': True},
    'authorization_required': {
        'confirmation_token': 'synthetic-consent', 'expires_at': 9999999999,
        'account_id': 'synthetic-account', 'provider_revision': 'synthetic-revision',
        'platform': runtime.platform.system().lower(),
    },
    'partial': {'unresolved': 2},
    'failed': {'ready': False},
    'account_selection_required': {'accounts': [{'id': 'synthetic-account', 'label': '本人账号'}]},
    'blocked': {'ready': False},
}


class StatusRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {'KB_WECHAT_RUNTIME': str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_only_an_absent_file_means_unchecked(self):
        self.assertEqual('unchecked', setup.snapshot()['state'])
        for content in ('not-json', '[]', 'null', '{}', '{"state": null}',
                        '{"state": "checking", "running": "false"}'):
            with self.subTest(content=content):
                setup._state_file().write_text(content, encoding='utf-8')
                state = setup.snapshot()
                self.assertEqual('status_unavailable', state['state'])
                self.assertIsNone(state['running'])
                self.assertTrue(state['retryable'])
                self.assertFalse(state['can_connect'])

    def test_transient_read_denial_recovers_without_claiming_idle(self):
        setup._write({'state': 'ready', 'running': False, 'ready': True})
        read_text = Path.read_text
        attempts = []

        def transient_read(path, *args, **kwargs):
            attempts.append(path)
            if len(attempts) < 3:
                raise PermissionError('private path must not be published')
            return read_text(path, *args, **kwargs)

        with patch.object(Path, 'read_text', transient_read), patch.object(setup.time, 'sleep'):
            state = setup.snapshot()
        self.assertTrue(state['ready'])
        self.assertEqual(3, len(attempts))

    def test_permanent_read_denial_is_bounded_and_reported_as_unknown(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError('secret')) as read, \
             patch.object(setup.time, 'sleep') as sleep:
            state = setup.snapshot()
        self.assertEqual(6, read.call_count)
        self.assertEqual(5, sleep.call_count)
        self.assertEqual('status_unavailable', state['state'])
        self.assertIsNone(state['running'])
        self.assertIn('无法读取', state['message'])
        self.assertNotIn('secret', json.dumps(state))

    def test_other_read_io_failure_is_not_unchecked(self):
        with patch.object(Path, 'read_text', side_effect=OSError('secret')):
            state = setup.snapshot()
        self.assertEqual('status_unavailable', state['state'])
        self.assertIsNone(state['running'])

    def test_preparing_the_lock_is_not_contention(self):
        setup._write({'state': 'checking', 'running': True})
        for error in (runtime.SetupError('接入目录不能是符号链接'), PermissionError('secret')):
            with self.subTest(error=type(error).__name__), \
                 patch.object(runtime, 'private_dir', side_effect=error):
                state = setup.snapshot()
            self.assertEqual('status_unavailable', state['state'])
            self.assertIsNone(state['running'])
            self.assertFalse(state['can_connect'])
            self.assertNotIn('secret', json.dumps(state))

    @unittest.skipUnless(os.name == 'nt', 'Windows directory protection failures')
    def test_acl_failure_and_sid_timeout_cannot_be_reported_as_a_running_job(self):
        setup._write({'state': 'checking', 'running': True})
        for error in (OSError('private path'), subprocess.TimeoutExpired('whoami', 10)):
            with self.subTest(error=type(error).__name__), \
                 patch('kb.wechat_windows_security.restrict', side_effect=error):
                state = setup.snapshot()
                with self.assertRaises(runtime.SetupError):
                    setup._lock()
            self.assertEqual('status_unavailable', state['state'])
            self.assertIsNone(state['running'])
            self.assertIn('无法保护 Windows', state['message'])
            self.assertNotIn('private path', json.dumps(state))
        self.assertEqual('interrupted', setup.snapshot()['state'])

    def test_actual_busy_lock_is_running_and_becomes_interrupted_when_released(self):
        setup._write({'state': 'acquiring', 'running': True})
        descriptor = setup._lock()
        try:
            self.assertTrue(setup.snapshot()['running'])
        finally:
            os.close(descriptor)
        self.assertEqual('interrupted', setup.snapshot()['state'])

    def test_terminal_state_written_before_lock_acquisition_is_preserved(self):
        lock = setup._lock
        for terminal, fields in TERMINAL_RESULTS.items():
            for running in (False, True):
                with self.subTest(terminal=terminal, running=running):
                    setup._write({'state': 'verifying', 'running': True})

                    def complete_then_lock():
                        setup._write({'state': terminal, 'running': running, **fields})
                        return lock()

                    with patch.object(setup, '_lock', side_effect=complete_then_lock):
                        state = setup.snapshot()
                    self.assertEqual(terminal, state['state'])
                    self.assertFalse(state['running'])
                    for field, value in fields.items():
                        self.assertEqual(value, state[field])

    def test_worker_publishes_complete_terminals_without_readback(self):
        read_text = Path.read_text
        for terminal, fields in TERMINAL_RESULTS.items():
            with self.subTest(terminal=terminal):
                published = []

                def connect(progress, *args):
                    progress(terminal, '合成终态', **fields)
                    published.append(json.loads(read_text(setup._state_file(), encoding='utf-8')))

                descriptor = setup._lock()
                with patch.object(setup, '_connect', side_effect=connect), \
                     patch.object(Path, 'read_text', side_effect=PermissionError('synthetic denial')) as read, \
                     patch.object(setup.time, 'sleep'):
                    setup._work(descriptor, '', False)
                read.assert_not_called()
                persisted = setup._read()
                self.assertEqual(published, [persisted])
                self.assertEqual(terminal, persisted['state'])
                self.assertFalse(persisted['running'])
                self.assertEqual('合成终态', persisted['message'])
                for field, value in fields.items():
                    self.assertEqual(value, persisted[field])
                observed = setup.snapshot()
                self.assertEqual(terminal, observed['state'])
                self.assertFalse(observed['running'])
                os.close(setup._lock())

    def test_legacy_terminals_ignore_stale_running_marker_without_locking(self):
        for terminal, fields in TERMINAL_RESULTS.items():
            with self.subTest(terminal=terminal):
                setup._write({'state': terminal, 'running': True, **fields})
                with patch.object(setup, '_lock', side_effect=AssertionError('terminal must not recover')):
                    state = setup.snapshot()
                self.assertEqual(terminal, state['state'])
                self.assertFalse(state['running'])
                for field, value in fields.items():
                    self.assertEqual(value, state[field])

    def test_active_publications_are_running_and_recover_only_after_unlock(self):
        for stage in sorted(setup._ACTIVE_STATES):
            with self.subTest(stage=stage):
                observed = []

                def connect(progress, *args):
                    progress(stage, '合成进行中状态')
                    observed.append(setup.snapshot())

                descriptor = setup._lock()
                with patch.object(setup, '_connect', side_effect=connect) as connect_call:
                    setup._work(descriptor, '', False)
                connect_call.assert_called_once()
                self.assertEqual(stage, observed[0]['state'])
                self.assertTrue(observed[0]['running'])
                self.assertEqual('interrupted', setup.snapshot()['state'])
                self.assertFalse(setup.snapshot()['running'])

    def test_worker_errors_publish_finished_failure_and_preserve_safe_reason(self):
        for error in (runtime.SetupError('合成可公开原因'), ValueError('synthetic secret')):
            with self.subTest(error=type(error).__name__):
                descriptor = setup._lock()
                with patch.object(setup, '_connect', side_effect=error), \
                     patch.object(setup, '_read', side_effect=AssertionError('no final read')):
                    setup._work(descriptor, '', False)
                state = setup.snapshot()
                self.assertEqual('failed', state['state'])
                self.assertFalse(state['running'])
                self.assertNotIn('synthetic secret', json.dumps(state))
                if isinstance(error, runtime.SetupError):
                    self.assertEqual(str(error), state['message'])
                os.close(setup._lock())

    def test_backup_progress_from_the_real_producer_recovers_after_interruption(self):
        root = self.home / 'empty-database-root'
        root.mkdir()

        def progress(state, message):
            setup._write({'state': state, 'message': message, 'running': False})

        setup._backup(root, progress)
        self.assertEqual('backing_up', setup._read()['state'])
        self.assertEqual('interrupted', setup.snapshot()['state'])

    def test_normal_terminal_states_are_not_rewritten_as_interrupted(self):
        for terminal in ('ready', 'failed', 'partial', 'authorization_required',
                         'account_selection_required', 'interrupted'):
            with self.subTest(terminal=terminal):
                setup._write({'state': terminal, 'running': False})
                self.assertEqual(terminal, setup.snapshot()['state'])


if __name__ == '__main__':
    unittest.main()
