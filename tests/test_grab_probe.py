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
            texts = [f.read_text() for f in files]
            # **重复投递只许有一篇**(探针第 4 步投了两次同一条链接):数的是
            # 「带着那条 url 的 md 有几份」,而不是目录里一共几个文件 —— 探针后面
            # 那条慢回包用的是另一个地址,它落不落盘取决于最后那一轮排没排上。
            self.assertEqual(
                sum('url: https://example.org/posts/manual-one' in t for t in texts), 1,
                f'带着那条地址的 md 应当只有一份，实际 {files}')
            one = next(t for t in texts if 'url: https://example.org/posts/manual-one' in t)
            self.assertIn('把链接存进知识库', one)
            for text in texts:
                self.assertIn('source: inbox', text)


if __name__ == '__main__':
    unittest.main()
