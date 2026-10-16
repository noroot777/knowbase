# -*- coding: utf-8 -*-
"""手动收录的界面贯通:粘一条链接 → 列表里出现 → vault 里真有那一篇。

浏览器那一半的断言在 `tests/grab_probe.mjs`;这里多钉**盘上的后果** ——
界面说「正在写入知识库」而 vault 里什么都没有,是这条链最难看出来的一种谎报。
"""
import json
import shutil
import unittest
from pathlib import Path

from tests.headless import fixture_server, probe_env, requires_headless, run_probe

ROOT = Path(__file__).resolve().parents[1]


@requires_headless
class GrabProbeTests(unittest.TestCase):
    def test_grabbing_a_link_from_the_panel_lands_in_the_vault(self):
        # 沙箱和它的临时库都归 fixture_server 管(见那边的说明)。
        with fixture_server('--grab-fixture') as base:
            result = run_probe([shutil.which('node'), 'tests/grab_probe.mjs', base],
                               timeout=240, env=probe_env(), cwd=ROOT)
            self.assertTrue(result.stdout.strip(), result.stderr)
            checks = json.loads(result.stdout.strip().splitlines()[-1])
            # **先逐条断言,再断数量**:倒过来写的话「探针半路抛了」只报一句
            # 「2 not greater than 10」,而真因就在那两条的 detail 里。
            for check in checks:
                with self.subTest(check=check['name']):
                    self.assertTrue(check['ok'], check['detail'])
            self.assertGreater(len(checks), 10, checks)
            self.assertEqual(result.returncode, 0, result.stderr)
            # **盘上的后果**:导出阶段真把它写成了 vault 里那一篇。
            root = Path(base.root) / 'vault' / 'source' / 'inbox'
            files = sorted(root.glob('*.md')) if root.exists() else []
            self.assertEqual(len(files), 1, f'vault 里应当只有那一篇，实际 {files}')
            text = files[0].read_text()
            self.assertIn('把链接存进知识库', text)
            self.assertIn('source: inbox', text)
            self.assertIn('url: https://example.org/posts/manual-one', text)


if __name__ == '__main__':
    unittest.main()
