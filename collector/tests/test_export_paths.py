"""Collector exports must land in the same vault mounted by Knowbase."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ExportPathsTests(unittest.TestCase):
    def test_export_honors_mounted_vault(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = str(Path(folder) / 'mounted-vault')
            result = subprocess.run(
                [sys.executable, '-c',
                 'import json; from scripts.export_obsidian import OBS_ROOT, DEFAULT_OUT, DEFAULT_DEDUP_ROOTS; '
                 'print(json.dumps([str(p) for p in [OBS_ROOT, DEFAULT_OUT, *DEFAULT_DEDUP_ROOTS]]))'],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, 'KB_VAULT_ROOT': vault}, capture_output=True, text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([vault + '/source', vault + '/source/dr-gzh',
                              vault + '/source/gzh', vault + '/source/dr-gzh'], json.loads(result.stdout))


if __name__ == '__main__':
    unittest.main()
