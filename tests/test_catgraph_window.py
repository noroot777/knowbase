# -*- coding: utf-8 -*-
"""分类图谱页面的「近 30/90 天」截止日:必须全程按 UTC 日历算。

`meta.built` 是后端 `date.today()` 落下来的一个**不带时区的日历日**,页脚那句
「近 30 天有提及 N」也是后端按 `date.today() - timedelta(days=30)` 数出来的。
前端筛选要是按浏览器本地时区解析这个日期再 `toISOString()`,在 UTC+8 下截止日
会退回前一天,窗口凭空多含一天——项目图谱实测多出 21 个,同一张页面自己跟自己
对不上(2026-09-22 第 10 轮审查发现)。所有 UTC 正偏移时区都会踩。

所以直接把 `catgraph.js` 里那段真代码抠出来丢给 node,在几个时区下跑,和
Python 那一侧的日历减法逐个比对——改回本地时区解析就会红。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSET = REPO / "scripts" / "wiki" / "assets" / "catgraph.js"

# 时区挑的是三类边界:零偏移、正偏移(用户自己这个)、负偏移,外加最大的 +14
ZONES = ["UTC", "Asia/Shanghai", "America/New_York", "Pacific/Kiritimati"]
# 日期挑的是跨月/跨年/闰月/夏令时切换那几天
BUILTS = ["2026-09-22", "2026-01-01", "2026-03-08", "2026-11-01", "2024-03-01"]


def _snippet() -> str:
    """从真实资源文件里抠出 today/cutoff 那一段(测的是线上那份,不是副本)。"""
    src = ASSET.read_text(encoding="utf-8")
    i = src.index("const today = ")
    j = src.index("\n}", src.index("function cutoff", i)) + 2
    return src[i:j]


@unittest.skipUnless(shutil.which("node"), "需要 node 跑页面脚本")
class WindowCutoffTest(unittest.TestCase):

    def _cutoffs(self, built: str, tz: str) -> dict:
        js = (f'const DATA = {{meta: {{built: {built!r}}}}};\n'
              f'{_snippet()}\n'
              'console.log(JSON.stringify({d30: cutoff(30), d90: cutoff(90)}));\n')
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                             env={**os.environ, "TZ": tz}, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_matches_backend_calendar_math(self):
        for built in BUILTS:
            d0 = date.fromisoformat(built)
            want = {"d30": (d0 - timedelta(days=30)).isoformat(),
                    "d90": (d0 - timedelta(days=90)).isoformat()}
            for tz in ZONES:
                with self.subTest(built=built, tz=tz):
                    self.assertEqual(self._cutoffs(built, tz), want)

    def test_shanghai_reproduces_the_reported_case(self):
        """报告那一例:built=2026-09-22,30 天档正确截止日是 08-23 不是 08-22。"""
        got = self._cutoffs("2026-09-22", "Asia/Shanghai")
        self.assertEqual(got["d30"], "2026-08-23")
        self.assertEqual(got["d90"], "2026-06-24")


if __name__ == "__main__":
    unittest.main()
