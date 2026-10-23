import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.crawler import local_wechat as local


@unittest.skipUnless(os.name == 'nt', 'Windows native command selection')
class WindowsReaderTests(unittest.TestCase):
    def test_managed_reader_uses_native_python_without_a_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            reader = root / 'windows-reader'
            python, launcher = reader / 'venv/Scripts/python.exe', reader / 'reader_launcher.py'
            python.parent.mkdir(parents=True)
            python.touch()
            launcher.touch()
            response = Mock(returncode=0, stdout='{"ok":true,"data":{"status":{}}}')
            with patch.dict(os.environ, {'KB_WECHAT_RUNTIME': str(root)}), \
                 patch.object(local, 'reader_command', side_effect=local.LocalReaderError('not installed')), \
                 patch.object(local.subprocess, 'run', return_value=response) as run:
                local.LocalReader().call('doctor')
            self.assertEqual([str(python), str(launcher), 'doctor'], run.call_args.args[0])
            self.assertEqual('utf-8', run.call_args.kwargs['encoding'])
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_explicit_reader_keeps_priority_over_managed_runtime(self):
        with patch.object(local, 'reader_command', return_value='custom-reader.exe'):
            self.assertEqual(['custom-reader.exe'], local.reader_argv())
