"""模型组切换、改名与保存恢复的真页面回归,自动进入 probes 层。"""

import shutil
import unittest
from pathlib import Path

from tests.headless import fixture_server, probe_env, requires_headless, run_probe

_PROBE = Path(__file__).with_name("stage_presets_probe.mjs")


@requires_headless
class StagePresetsProbeTests(unittest.TestCase):
    def test_switch_rename_and_recover_without_stale_save_callbacks(self):
        with fixture_server(script="serve_stage_presets.py") as base:
            result = run_probe([shutil.which("node"), str(_PROBE), str(base)],
                               timeout=180, env=probe_env())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
