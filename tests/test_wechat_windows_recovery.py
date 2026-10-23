"""Only explicit user recovery may replace retained Windows access material."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kb import wechat_runtime as runtime, wechat_setup as setup, wechat_windows as windows


class MaterialRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / 'account/db_storage'
        self.root.mkdir(parents=True)
        for context in (
            patch.dict(os.environ, {'KB_WECHAT_RUNTIME': str(self.home / 'runtime'),
                                    'RION_WECHAT_READER_CONFIG': str(self.home / 'config/config.json'),
                                    'KB_WECHAT_KEYS_FILE': ''}),
            patch.object(windows, 'account_roots', return_value={'account': self.root}),
            patch.object(windows, 'prepare_reader', return_value=['synthetic-reader']),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.bundle = windows.reader_paths()[0] / 'access-account/all_keys.json'
        self.keys = setup._config().parent / 'keys.json'
        self.events = []

    def progress(self, state, message, **extra):
        self.events.append({'state': state, 'message': message, **extra})

    def test_failed_or_timed_out_acquisition_removes_only_its_incomplete_bundle(self):
        for outcome in ('nonzero', 'timeout'):
            with self.subTest(outcome=outcome):
                def extract(argv, **kwargs):
                    self.bundle.write_text('incomplete', encoding='utf-8')
                    if outcome == 'timeout':
                        raise runtime.SetupError('工具运行超时')
                    return 1, 'secret subprocess output'

                with patch.object(windows, 'prepare_provider', return_value=Path('provider')), \
                     patch.object(runtime, 'json_call', return_value={'state': 'needs_access'}), \
                     patch.object(runtime, 'run', side_effect=extract) as run:
                    with self.assertRaises(runtime.SetupError):
                        windows.connect(self.progress, 'account', acquire=True)
                run.assert_called_once()
                self.assertFalse(self.bundle.exists())
                self.assertFalse(self.keys.exists())
                self.assertFalse(setup._config().exists())
                with patch.object(windows, 'prepare_provider', return_value=Path('provider')), \
                     patch.object(runtime, 'json_call', return_value={'state': 'needs_access'}), \
                     patch.object(runtime, 'run') as repeated:
                    windows.connect(self.progress, 'account')
                self.assertEqual('authorization_required', self.events[-1]['state'])
                repeated.assert_not_called()

    def test_partial_material_has_actionable_paths_and_can_be_manually_archived(self):
        self.bundle.parent.mkdir(parents=True)
        self.bundle.write_text('retained synthetic bundle', encoding='utf-8')

        def inspect(argv):
            if 'import-access' in argv:
                self.keys.parent.mkdir(parents=True)
                self.keys.write_text('retained synthetic keys', encoding='utf-8')
                return {}
            return {'state': 'partial'}

        with patch.object(runtime, 'json_call', side_effect=inspect) as call, \
             patch.object(windows, 'prepare_provider') as provider, \
             patch.object(runtime, 'run') as run:
            windows.connect(self.progress, 'account', acquire=True)
        state = self.events[-1]
        self.assertEqual('partial', state['state'])
        self.assertIn(str(self.keys), state['message'])
        self.assertIn(str(self.bundle), state['message'])
        self.assertIn('自行备份并移走', state['message'])
        self.assertIn('再次确认', state['message'])
        self.assertEqual('retained synthetic bundle', self.bundle.read_text(encoding='utf-8'))
        self.assertEqual('retained synthetic keys', self.keys.read_text(encoding='utf-8'))
        self.assertFalse(setup._config().exists())
        self.assertFalse(any('setup' in entry.args[0] for entry in call.call_args_list))
        provider.assert_not_called()
        run.assert_not_called()
        self.keys.rename(self.keys.with_suffix('.archived'))
        self.bundle.rename(self.bundle.with_suffix('.archived'))
        with patch.object(runtime, 'json_call', return_value={'state': 'needs_access'}), \
             patch.object(windows, 'prepare_provider', return_value=Path('provider')), \
             patch.object(runtime, 'run') as run:
            windows.connect(self.progress, 'account')
        self.assertEqual('authorization_required', self.events[-1]['state'])
        run.assert_not_called()

    def test_rejected_bundle_is_preserved_with_recovery_guidance_not_raw_errors(self):
        self.bundle.parent.mkdir(parents=True)
        self.bundle.write_text('invalid synthetic material', encoding='utf-8')
        with patch.object(runtime, 'json_call', side_effect=runtime.SetupError('secret')), \
             patch.object(windows, 'prepare_provider') as provider:
            windows.connect(self.progress, 'account')
        state = self.events[-1]
        self.assertEqual('access_material_requires_review', state['state'])
        self.assertIn(str(self.bundle), state['message'])
        self.assertNotIn('secret', state['message'])
        self.assertTrue(self.bundle.is_file())
        self.assertFalse(self.keys.exists())
        provider.assert_not_called()

    def test_missing_explicit_material_is_named_without_guessing_at_account_failure(self):
        with patch.dict(os.environ, {'KB_WECHAT_KEYS_FILE': str(self.home / 'absent.json')}), \
             patch.object(windows, 'prepare_reader') as reader, \
             patch.object(runtime, 'json_call') as call, \
             patch.object(windows, 'prepare_provider') as provider:
            windows.connect(self.progress, 'account')
        state = self.events[-1]
        self.assertEqual('access_material_missing', state['state'])
        self.assertIn('KB_WECHAT_KEYS_FILE', state['message'])
        self.assertIn('不存在', state['message'])
        self.assertNotIn('已有材料', state['message'])
        call.assert_not_called()
        reader.assert_not_called()
        provider.assert_not_called()

    def test_explicit_external_material_is_never_deleted_and_explains_the_override(self):
        external = self.home / 'external.json'
        external.write_text('retained', encoding='utf-8')
        with patch.dict(os.environ, {'KB_WECHAT_KEYS_FILE': str(external)}), \
             patch.object(runtime, 'json_call', side_effect=runtime.SetupError('failed')):
            windows.connect(self.progress, 'account')
        self.assertEqual('retained', external.read_text(encoding='utf-8'))
        self.assertIn('取消 KB_WECHAT_KEYS_FILE', self.events[-1]['message'])
        self.assertIn('外部材料不会自动删除', self.events[-1]['message'])

    def test_plan_states_keep_their_own_meaning_without_claiming_existing_material(self):
        for state in ('invalid_access_material', 'configuration_check_failed', 'database_missing',
                      'account_selection_required', 'future_reader_state'):
            with self.subTest(state=state), \
                 patch.object(runtime, 'json_call', return_value={'state': state}), \
                 patch.object(windows, 'prepare_provider') as provider:
                windows.connect(self.progress, 'account')
            self.assertEqual(state if state in setup.MESSAGES else 'blocked', self.events[-1]['state'])
            self.assertNotIn('已有材料', self.events[-1]['message'])
            provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()
