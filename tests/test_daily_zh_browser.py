# -*- coding: utf-8 -*-
"""日报正文里的本地译文入口(kb/static/js/daily_zh.js + kb/daily_trans.py)在真
页面上的回归。

后端那一侧由 tests/test_daily_trans.py 钉住(判据是 frontmatter 的
`translated_from`、衍生条目不冒充、读不出来当没有)。这里验的是它在界面上的样子:
是译文的那一条多一个入口、不是译文的那一条一个字都不多、点开从**左边**滑出
阅读器读 vault 里那一篇,以及别处打开的阅读器照旧从右边出来。

**另有一组是冲着「收藏此段」去的**:日报正文同时是片段收藏的采集面,片段的回跳
锚点按正文的**文字坐标系**算(kb/static/js/snip_text.js)—— 往正文里插一个真字符,
**已经收下的片段**就整体错位、回跳落空。所以那个「译」字只能由 CSS 伪元素画,
探针逐条钉住它既不在 `textContent` 里、也不在屏幕选区里。

驱动是 CDP 直连 chrome-headless-shell(不装 playwright、不连网、始终无头、临时
profile),找二进制的口径在 tests/headless.py。沙箱复用 daily_sandbox 那一套,
再由 daily_zh_sandbox 加译文那一档 —— **不往 daily_sandbox 里加东西**:它是
daily_probe / subs_probe 的共同依赖,那两个探针有好几条检查认的是"素材清单
第 N 条""条目列表第一行"。
"""

import json
import os
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
HERE = Path(__file__).resolve().parent
PROBE = HERE / "daily_zh_probe.mjs"

sys.path.insert(0, str(REPO))

from tests.daily_zh_sandbox import ZH_MARK, ZH_URL  # noqa: E402
from tests.headless import NODE, probe_env, reap, requires_headless, run_probe  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, tries: int = 600) -> bool:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


@requires_headless
class DailyTranslationEntryTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="kb-daily-zh-sandbox-")
        # 建完/起完**立刻**登记(LIFO:先收进程、再删目录):后面任意一步抛出来
        # unittest 都不调 tearDownClass,服务和沙箱就全留在后台了。
        cls.addClassCleanup(cls.tmp.cleanup)
        base_dir = Path(cls.tmp.name)
        cls.stub_port, cls.web_port = _free_port(), _free_port()
        # 这条回归一次模型都不调,但沙箱的 secrets.json 要有一份合法配置,
        # 否则面板顶栏会挂一句"没配 key"。桩起着就不连外网、不花钱。
        cls.stub = subprocess.Popen(
            [sys.executable, str(HERE / "llm_stub.py"), "--port", str(cls.stub_port)],
            cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(reap, cls.stub)
        tail = ""
        for script, args in ((HERE / "daily_sandbox.py", [str(base_dir), str(cls.stub_port)]),
                             (HERE / "daily_zh_sandbox.py", [str(base_dir)])):
            done = subprocess.run([sys.executable, str(script), *args], cwd=str(REPO),
                                  capture_output=True, text=True, timeout=600)
            if done.returncode:
                raise AssertionError(f"摆盘失败 {script.name}:{done.stderr[-2000:]}")
            tail = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
        # daily_zh_sandbox 最后一行打印 `report=<日期> zh_url=<地址>`
        info = dict(part.split("=", 1) for part in tail.split())
        env = {**os.environ, "KB_DATA_DIR": str(base_dir / "data"),
               "KB_VAULT_ROOT": str(base_dir / "vault")}
        cls.web = subprocess.Popen(
            ["uv", "run", "--offline", "uvicorn", "kb.server:app", "--port", str(cls.web_port)],
            cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(reap, cls.web)
        url = f"http://127.0.0.1:{cls.web_port}"
        if not _wait(url + "/"):
            raise AssertionError("沙箱 kb-web 没起来")
        run = run_probe([str(NODE), str(PROBE), url, info["report"], ZH_URL, ZH_MARK],
                        timeout=300, env=probe_env(), cwd=str(REPO))
        cls.stderr = run.stderr
        line = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else ""
        if not line:
            raise AssertionError(run.stderr[-4000:])
        cls.results = json.loads(line)

    def test_the_report_offers_the_local_translation(self):
        self.assertTrue(self.results, "探针一条都没跑")
        for r in self.results:
            with self.subTest(check=r["name"]):
                self.assertTrue(r["ok"], r["detail"])
