"""阅读器在慢图与错误页中的真实焦点、点击和 Escape 契约。"""

import json
from pathlib import Path
import unittest

from tests.headless import NODE, fixture_server, probe_env, requires_headless, run_probe


@requires_headless
class ReaderCloseBrowserTests(unittest.TestCase):
    def test_slow_links_and_error_page_focus(self):
        with fixture_server(script="serve_reader_close.py") as base:
            result = run_probe(
                [NODE, str(Path(__file__).with_name("reader_close_probe.mjs")), base],
                env=probe_env(), timeout=90,
            )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([], report["errors"])
        self.assertGreaterEqual(len(report["checks"]), 10)
        self.assertEqual([], [check for check in report["checks"] if not check["ok"]], report)


if __name__ == "__main__":
    unittest.main()
