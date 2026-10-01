"""setup.sh 把 data/ 和 .venv/ 链到仓库外那份真实目录上。

钉住的是「替换」这件事:`ln -sfn A link` 在 link 已经是真目录时会把链接建**进**
那个目录(link/A),而不是替换它——而库是照 `DATA_DIR = collector/data` 建的,
于是采集器拿一个空库跑,`cli status` 报 0 个号而不是报错。非空目录不许静默替换。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "setup.sh"


class SetupScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # macOS 的 /var 是 /private/var 的软链接,resolve() 会消解它 —— 两边口径得一致
        root = Path(self.tmp.name).resolve()
        # 把脚本单独拷出来跑:它只用 $0 定位自己,不需要整棵源码树
        self.here = root / "collector"
        self.here.mkdir()
        shutil.copy2(SETUP, self.here / "setup.sh")
        self.data = root / "real-data"
        self.data.mkdir()
        (self.data / "wechatoa.db").write_bytes(b"real")
        # venv 用桩顶住,免得真去跑 uv:脚本只查 bin/python 可执行、再拿它自检一次
        self.venv = root / "real-venv"
        (self.venv / "bin").mkdir(parents=True)
        python = self.venv / "bin" / "python"
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def run_setup(self):
        env = {**os.environ, "KB_COLLECTOR_DATA_PATH": str(self.data),
               "KB_COLLECTOR_VENV_PATH": str(self.venv)}
        return subprocess.run(["./setup.sh"], cwd=self.here, env=env,
                              capture_output=True, text=True)

    def assert_linked(self):
        link = self.here / "data"
        self.assertTrue(link.is_symlink(), f"data 不是符号链接:{link}")
        self.assertEqual(self.data, link.resolve())
        self.assertFalse((link / "real-data").exists(), "链接被建进了目录里")
        self.assertEqual(self.venv, (self.here / ".venv").resolve())

    def test_a_fresh_checkout_gets_both_links(self):
        self.assertEqual(0, self.run_setup().returncode)
        self.assert_linked()

    def test_running_twice_changes_nothing(self):
        self.run_setup()
        self.assertEqual(0, self.run_setup().returncode)
        self.assert_linked()

    def test_an_empty_placeholder_directory_is_replaced_not_nested_into(self):
        """git / 上一次误跑留下的空 data/ 要被收掉。

        不收的话链接变成 data/real-data,而 `DATA_DIR` 指的是 data 本身——
        采集器在那个空目录里新建一个库,一篇文章都没有却报成功。
        """
        (self.here / "data").mkdir()
        (self.here / ".venv").mkdir()
        self.assertEqual(0, self.run_setup().returncode)
        self.assert_linked()

    def test_a_directory_with_something_in_it_stops_the_script(self):
        """里面有东西就不知道那是什么了,停下来让人看一眼,别静默覆盖。"""
        (self.here / "data").mkdir()
        (self.here / "data" / "wechatoa.db").write_bytes(b"someone else's db")
        result = self.run_setup()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("非空目录", result.stderr)
        self.assertFalse((self.here / "data").is_symlink())
        self.assertEqual(b"someone else's db",
                         (self.here / "data" / "wechatoa.db").read_bytes())

    def test_a_plain_file_in_the_way_is_not_clobbered(self):
        (self.here / "data").write_text("not a directory")
        result = self.run_setup()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("not a directory", (self.here / "data").read_text())
