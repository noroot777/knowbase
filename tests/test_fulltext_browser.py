# -*- coding: utf-8 -*-
"""正文补抓(kb/feeds/fulltext.py)那一档在真页面上的回归。

后端那一侧由 tests/test_feed_fulltext.py 钉住(队列口径、两道闸门、落库)。
这里验的是它在界面上留不留得住:开关在编辑弹窗里存得住、保存时真的发出去、
订阅行上标得出来、待补抓的条目换掉的是**正文**而不是标题、也不给「展开阅读」,
以及别人家的条目一条都不受影响。

另一个订阅级开关「提取知识标签」(`subscriptions.extract`)也在这里一起验 ——
它和补抓挤在同一个弹窗、同一次 PATCH 里,「改 A 把 B 静默重置」这种缺陷只有
两个开关摆进同一条用例才看得见。它的后端口径在 tests/test_extract_optin.py。

判据不能只看后端返回的那个 `fetching` 字段 —— 它原来就是对的,丢的是显示:
壳条目照着正文显示出来的话用户看到的就是「标题(域名)↑ 101 HN Points」,
而那正是他说「都像个跳转似的」的那个东西。

驱动是 CDP 直连 chrome-headless-shell(不装 playwright、不连网、始终无头、
临时 profile),找二进制的口径在 tests/headless.py。沙箱复用 daily_sandbox
那一套,再由 fulltext_sandbox 加一条开了补抓的订阅 —— **不往 daily_sandbox
里加东西**:它是 daily_probe / subs_probe 的共同依赖,那两个探针有好几条
检查认的是"列表第一行"。
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
PROBE = HERE / "fulltext_probe.mjs"

sys.path.insert(0, str(REPO))

from tests.headless import NODE, probe_env, requires_headless, run_probe  # noqa: E402


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
class FullTextPanelTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="kb-fulltext-sandbox-")
        base_dir = Path(cls.tmp.name)
        cls.stub_port, cls.web_port = _free_port(), _free_port()
        # 模型指到本地桩:摆盘那一步不连外网、不花钱(这条回归压根不调模型,
        # 但沙箱的 secrets.json 要有一份合法配置,否则面板顶栏会报没配 key)。
        cls.stub = subprocess.Popen(
            [sys.executable, str(HERE / "llm_stub.py"), "--port", str(cls.stub_port)],
            cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for script, args in ((HERE / "daily_sandbox.py", [str(base_dir), str(cls.stub_port)]),
                             (HERE / "fulltext_sandbox.py", [str(base_dir)])):
            done = subprocess.run([sys.executable, str(script), *args], cwd=str(REPO),
                                  capture_output=True, text=True, timeout=600)
            if done.returncode:
                cls.stub.kill()
                raise AssertionError(f"摆盘失败 {script.name}:{done.stderr[-2000:]}")
            tail = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
        # fulltext_sandbox 最后一行打印 `agg_id=.. plain_id=..`
        ids = dict(part.split("=") for part in tail.split())
        env = {**os.environ, "KB_DATA_DIR": str(base_dir / "data"),
               "KB_VAULT_ROOT": str(base_dir / "vault")}
        cls.web = subprocess.Popen(
            ["uv", "run", "--offline", "uvicorn", "kb.server:app", "--port", str(cls.web_port)],
            cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{cls.web_port}"
        if not _wait(url + "/"):
            cls.web.kill(); cls.stub.kill()
            raise AssertionError("沙箱 kb-web 没起来")
        run = run_probe(
            [str(NODE), str(PROBE), url, ids["agg_id"], ids["plain_id"]],
            timeout=300, env=probe_env(), cwd=str(REPO))
        cls.stderr = run.stderr
        line = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else ""
        if not line:
            cls.web.kill(); cls.stub.kill()
            raise AssertionError(run.stderr[-4000:])
        cls.results = json.loads(line)

    @classmethod
    def tearDownClass(cls):
        for proc in (cls.web, cls.stub):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:   # pragma: no cover
                proc.kill()
        cls.tmp.cleanup()

    def test_the_panel_tells_the_fetching_items_apart(self):
        self.assertTrue(self.results, "探针一条都没跑")
        for r in self.results:
            with self.subTest(check=r["name"]):
                self.assertTrue(r["ok"], r["detail"])
