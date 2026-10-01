# -*- coding: utf-8 -*-
"""找无头壳的那份口径:不许锁死平台、不许锁死 playwright 构建号。

三个浏览器回归原来各写死一份
`~/Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell`。
后果不是"报错",是**静默跳过**:别的机器(linux、x64、或者 playwright 升过版)上
`skipUnless` 不声不响地放过整组,贡献者改了前端得不到任何反馈,也看不出为什么。
所以这里钉的不只是"能找到",还有"找不到时说得清怎么补"。
"""

from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests import headless


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class HeadlessShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # 干净环境:既没有 HEADLESS_SHELL,也不让它捡到 PATH 上和真 HOME 里的
        env = patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(self.root / "pw")},
                         clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HEADLESS_SHELL", None)
        home = patch.object(Path, "home", staticmethod(lambda: self.root / "home"))
        home.start()
        self.addCleanup(home.stop)
        which = patch.object(headless.shutil, "which", return_value=None)
        which.start()
        self.addCleanup(which.stop)

    def test_env_var_wins(self):
        """自己装的 / CI 下的那一份:HEADLESS_SHELL 说了算,不去探缓存。"""
        mine = _exe(self.root / "mine/chrome-headless-shell")
        _exe(self.root / "pw/chromium_headless_shell-9999/x/chrome-headless-shell")
        os.environ["HEADLESS_SHELL"] = str(mine)
        self.assertEqual(mine, headless.headless_shell())

    def test_env_var_pointing_nowhere_is_not_silently_ignored(self):
        """配错了就当没有(随后 skip 会把怎么装说清楚),不要偷偷回落到别的壳。"""
        _exe(self.root / "pw/chromium_headless_shell-1/x/chrome-headless-shell")
        os.environ["HEADLESS_SHELL"] = str(self.root / "nope")
        self.assertIsNone(headless.headless_shell())

    def test_platform_dir_is_not_hardcoded(self):
        """平台目录名不写死:linux / x64 / Windows 的那几种写法都得认。"""
        for plat, name in [("chrome-headless-shell-linux", "chrome-headless-shell"),
                           ("chrome-headless-shell-mac-x64", "chrome-headless-shell"),
                           ("chrome-headless-shell-win64", "chrome-headless-shell.exe")]:
            with self.subTest(plat=plat), TemporaryDirectory() as box:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = box
                want = _exe(Path(box) / f"chromium_headless_shell-1200/{plat}/{name}")
                self.assertEqual(want, headless.headless_shell())

    def test_build_number_is_not_hardcoded_and_newest_wins(self):
        """构建号不写死,升版之后照样找得到;多份并存取最新那一个。"""
        pw = self.root / "pw"
        _exe(pw / "chromium_headless_shell-998/chrome-headless-shell-linux/chrome-headless-shell")
        newest = _exe(pw / "chromium_headless_shell-1243/chrome-headless-shell-linux/chrome-headless-shell")
        _exe(pw / "chromium_headless_shell-1100/chrome-headless-shell-linux/chrome-headless-shell")
        self.assertEqual(newest, headless.headless_shell())
        # 字典序会把 998 排在 1243 后面 —— 必须按数字比
        self.assertGreater(headless._build_number(newest), headless._build_number(
            pw / "chromium_headless_shell-998/chrome-headless-shell-linux/chrome-headless-shell"))

    def test_nothing_found_returns_none(self):
        self.assertIsNone(headless.headless_shell())

    def test_skip_message_says_how_to_install(self):
        """静默跳过是上面那个问题的根:skip 理由里必须有可执行的下一步。"""
        with patch.object(headless, "SHELL", None), patch.object(headless, "NODE", "/usr/bin/node"):
            @headless.requires_headless
            class Dummy(unittest.TestCase):
                def test_nothing(self):   # pragma: no cover - 只看它被 skip
                    pass

            reason = getattr(Dummy, "__unittest_skip_why__", "")
        self.assertTrue(getattr(Dummy, "__unittest_skip__", False))
        self.assertIn("chrome-headless-shell", reason)
        self.assertIn("playwright install", reason)
        self.assertIn("HEADLESS_SHELL", reason)

    def test_missing_node_is_named_too(self):
        """缺 node 也要说出来,不然只说缺浏览器会让人装错东西。"""
        with patch.object(headless, "SHELL", Path("/x/shell")), patch.object(headless, "NODE", None):
            @headless.requires_headless
            class Dummy(unittest.TestCase):
                def test_nothing(self):   # pragma: no cover
                    pass
        self.assertIn("node", getattr(Dummy, "__unittest_skip_why__", ""))

    def test_the_three_browser_regressions_share_this_one_copy(self):
        """口径只写一份:三处都不许再自己探测。"""
        for name in ["test_catgraph_browser.py", "test_chat_height.py", "test_retired_channel.py"]:
            src = (Path(__file__).resolve().parent / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIn("from tests.headless import", src)
                self.assertNotIn("ms-playwright", src)
                self.assertNotIn("chromium_headless_shell-", src)


if __name__ == "__main__":
    unittest.main()
