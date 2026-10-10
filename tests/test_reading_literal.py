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
数据由 tests/serve_reading.py 真走 put_items → export → index 摆出来。
"""

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).resolve().parent / "reading_probe.mjs"
SERVER = Path(__file__).resolve().parent / "serve_reading.py"

sys.path.insert(0, str(REPO))

from tests.headless import probe_env, requires_headless, run_probe  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@requires_headless
class ReadingEntriesAgreeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.tmp = tempfile.TemporaryDirectory(prefix="kb-reading-manifest-")
        manifest = Path(cls.tmp.name) / "docs.json"
        cls.server = subprocess.Popen(
            [sys.executable, str(SERVER), "--port", str(cls.port),
             "--manifest", str(manifest)],
            cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{cls.port}"
        # 摆盘(导出 + 建索引)在起服务之前跑完,所以这里要等得比别的探针久一点
        for _ in range(600):
            try:
                urllib.request.urlopen(base + "/", timeout=1).read()
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        else:  # pragma: no cover - 起不来就没什么可测的
            cls.server.kill()
            raise AssertionError("沙箱 kb-web 没起来")
        run = run_probe([shutil.which("node"), str(PROBE), base, str(manifest)],
                        timeout=300, env=probe_env())
        cls.stderr = run.stderr
        line = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else ""
        if not line:
            cls.server.kill()
            raise AssertionError(run.stderr)
        cls.results = json.loads(line)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.tmp.cleanup()

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
