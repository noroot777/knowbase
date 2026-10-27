# -*- coding: utf-8 -*-
"""「问题与待办」面板在真实 kb-web 里的浏览器回归。

探针本体在 `tests/intake_diag_probe.mjs`(里面写着它验的那几件事)。这边只做
两件事:把沙箱服务的生命周期交给 `fixture_server`,把「一条都不许红」钉死。

**条数下界要钉**(`assertGreaterEqual`):探针里 `check()` 是逐条追加的,若某一
节因为页面结构变了而整段没跑到,`bad` 仍然是 0 —— 只看 `ok` 会把「这一节压根
没验」读成绿。
"""
import json
import unittest

from tests.headless import NODE, fixture_server, probe_env, requires_headless, run_probe


@requires_headless
class IntakeDiagBrowserTests(unittest.TestCase):
    def test_panel_answers_the_three_questions_without_borrowing_the_sync_center_wording(self):
        with fixture_server(script='serve_intake_diag.py') as base:
            result = run_probe([NODE, 'tests/intake_diag_probe.mjs', base],
                               timeout=300, env=probe_env())
        self.assertEqual(0, result.returncode, result.stderr[-4000:] + result.stdout[-4000:])
        results = json.loads(result.stdout.strip().splitlines()[-1])
        failed = [r for r in results if not r['ok']]
        self.assertEqual([], [f"{r['name']} — {r.get('detail', '')}" for r in failed])
        self.assertGreaterEqual(len(results), 60)


if __name__ == '__main__':
    unittest.main()
