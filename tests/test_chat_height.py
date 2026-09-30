"""问答面板的高度不再靠魔法数估算:首次配置横幅在与不在都得留在首屏内。

原来 `.chat-layout` 在三档断点上各写死一个 `calc(100dvh - 96/162/175px)`,
那几个数是照「页头上面还有多少东西」量出来的 —— `#wechat-welcome` 横幅
(88.6px + 24px 下边距)不在估算里,首次配置没做完时输入区整块掉出视口,
模型下拉和发送按钮都得往下滚才看得见。

驱动是 CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、
不连网、始终无头、临时 profile),口径同 tests/test_catgraph_browser.py。
页面由 tests/serve_workspace.py 原样发出工作树里的 kb/static,后端一律 503 ——
这里量的是 CSS 布局,不需要真数据。
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).resolve().parent / "chat_height_probe.mjs"
SERVER = Path(__file__).resolve().parent / "serve_workspace.py"


def _shell() -> Path | None:
    """playwright 缓存里的 chrome-headless-shell,版本号不写死。

    tests/test_catgraph_browser.py 那份钉的是 `chromium_headless_shell-1243`,
    缓存一升版整组测试就静默跳过、再也不跑(2026-09-30 审查提到的那条)。
    这里按 glob 取版本号最大的一份,一个都没有才跳过。
    """
    root = Path.home() / "Library/Caches/ms-playwright"
    found = sorted(root.glob("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"),
                   key=lambda p: int(re.search(r"-(\d+)", p.parts[-3]).group(1)))
    return found[-1] if found else None


SHELL = _shell()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(shutil.which("node"), "需要 node 驱动无头浏览器")
@unittest.skipUnless(SHELL is not None, "需要 playwright 缓存里的 chrome-headless-shell")
class ChatHeightTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.server = subprocess.Popen(
            [sys.executable, str(SERVER), "--port", str(cls.port)],
            cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{cls.port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/", timeout=1).read()
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        else:  # pragma: no cover - 起不来就没什么可测的
            cls.server.kill()
            raise AssertionError("预览服务没起来")
        run = subprocess.run(
            [shutil.which("node"), str(PROBE), base],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "HEADLESS_SHELL": str(SHELL)})
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

    def test_input_bar_stays_on_screen(self):
        self.assertTrue(self.results, "探针一条都没跑")
        for r in self.results:
            with self.subTest(check=r["name"]):
                self.assertTrue(r["ok"], r["detail"])
