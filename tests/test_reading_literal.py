# -*- coding: utf-8 -*-
"""同一篇文章的两个阅读入口必须读到同样的内容。

订阅正文按契约已经是纯文本(`kb/feeds/fetching.py` 把标签剥干净了),所以正文里
剩下的 `List<T>`、`<script>…</script>` 是文章作者写的示例文字。订阅面板的展开阅读
用 textContent 显示,看得见;知识库阅读页(`kb/web/pages.py` 的 `/doc/…`)把 md 交给
marked,原来会把这些字当原始 HTML —— `Use List<T>` 显示成 `Use List`、整段只有一个
脚本示例的那篇正文一片空白(2026-10-08 第 3 轮审查实测)。

正文嵌进页面内联 `<script>` 的那一层还有一处边界:`<!--` 之后再出现 `<script` 会
把分词器推进「双重转义」,页面末尾那个 `</script>` 不再结束脚本元素 —— 整段脚本一行
都没执行,阅读页只剩标题,连后面那个普通段落一起没了;同一个开头放进 markdown 代码
围栏里照样复现,因为故障发生在 markdown 解析**之前**(第 4 轮审查实测)。

判据不能只是「md 文件里能找到这串字符」:那一关原来就是过的,丢字发生在渲染。
所以这条回归真起一个沙箱 kb-web、真用无头浏览器打开两个入口比内容;顺便钉住反向
——转义原始 HTML 之后正常的 markdown 照旧渲染成元素,不许退化成纯文字。

驱动是 CDP 直连 chrome-headless-shell(不装 playwright、不连网、始终无头、临时
profile);怎么找那个二进制的口径在 tests/headless.py,几个浏览器回归共用。
数据由 tests/serve_reading.py 真走 put_items → export → index 摆出来;那个沙箱
连同它的临时库由 tests.headless.fixture_server 起和收(所有权见 tests/fixture_root.py)。
"""

import contextlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).resolve().parent / "reading_probe.mjs"

sys.path.insert(0, str(REPO))

from tests.headless import (  # noqa: E402
    fixture_server, probe_env, requires_headless, run_probe)


@requires_headless
class ReadingEntriesAgreeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 沙箱服务、端口、那份临时库全部交给 fixture_server:它建根、用
        # KB_FIXTURE_ROOT 传下去、在自己的 finally 里删。这里自己 Popen +
        # terminate 的旧写法把库的所有权留在一个**收不到清理机会**的进程手上
        # (SIGTERM 之后它那边的 with 一行都不跑,盘上留下 kb-reading-qa-*)。
        cls.stack = contextlib.ExitStack()
        # **建完立刻登记**,别靠 try/except 一处处补:`addClassCleanup` 在 setUpClass
        # 抛异常时也会跑(那时 unittest 不调 tearDownClass,只跑 doClassCleanups),
        # 于是"起服务 → 跑探针 → 解析回包"整段都在同一个清理域里。原来只有前两步
        # 包在 try 里,**回包非空但不是合法 JSON** 时 `json.loads` 在域外抛 ——
        # 服务和那份临时库都留着(第 6 轮审查按代码指出)。
        cls.addClassCleanup(cls.stack.close)
        # manifest 不放沙箱根里:探针读它,而它的生死应当跟这条用例一致
        cls.tmp = cls.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="kb-reading-manifest-"))
        manifest = Path(cls.tmp) / "docs.json"
        # 摆盘(导出 + 建索引)在起服务之前跑完,所以要等得比别的沙箱久
        base = cls.stack.enter_context(fixture_server(
            "--manifest", str(manifest), script="serve_reading.py", timeout=150))
        run = run_probe([shutil.which("node"), str(PROBE), base, str(manifest)],
                        timeout=300, env=probe_env())
        cls.stderr = run.stderr
        line = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else ""
        if not line:
            raise AssertionError(run.stderr)
        cls.results = json.loads(line)

    def test_both_reading_entries_show_the_same_text(self):
        self.assertTrue(self.results, "探针一条都没跑")
        for r in self.results:
            with self.subTest(check=r["name"]):
                self.assertTrue(r["ok"], r["detail"])


class EmbeddedJsonTests(unittest.TestCase):
    """内联脚本里的 JSON 不许留裸 `<`(便宜的早期信号,行为由上面那条浏览器回归钉)。"""

    def test_no_raw_angle_bracket_survives_serialization(self):
        from kb.web.pages import _script_json
        for body in ['示例开头写 <!-- <script>。\n\n这里是普通的第二段。',
                     '<!--\n<script>',
                     '收尾写 </script> 的那种',
                     '新 API 使用 List<T>。',
                     'source/dr-gzh/<odd>.md']:
            with self.subTest(body=body):
                out = _script_json(body)
                self.assertNotIn('<', out)
                self.assertEqual(json.loads(out), body)
