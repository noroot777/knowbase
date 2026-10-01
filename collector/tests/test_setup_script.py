"""setup.sh 把 data/ 和 .venv/ 链到仓库外那份真实目录上。

钉住两件事。

一是「替换」:`ln -sfn A link` 在 link 已经是真目录时会把链接建**进**那个目录
(link/A),而不是替换它——而库是照 `DATA_DIR = collector/data` 建的,于是采集器
拿一个空库跑,`cli status` 报 0 个号而不是报错。非空目录不许静默替换。

二是「别换掉人家指定的数据位置」:这个脚本原来读 GZH_DATA_HOME / GZH_VENV_HOME,
改名成 KB_COLLECTOR_* 之后旧名字必须继续认。用旧名字显式隔离过数据目录的人,
升级后看到的订阅和文章会变成另一批(或者看起来全没了),而脚本照旧返回 0。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "setup.sh"


class _Base(unittest.TestCase):
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

    def run_setup(self, **overrides):
        """跑一遍脚本。overrides 里给 None 表示把那个变量从环境里摘掉。"""
        env = {**os.environ, "KB_COLLECTOR_DATA_PATH": str(self.data),
               "KB_COLLECTOR_VENV_PATH": str(self.venv)}
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return subprocess.run(["./setup.sh"], cwd=self.here, env=env,
                              capture_output=True, text=True)

    def assert_linked(self):
        link = self.here / "data"
        self.assertTrue(link.is_symlink(), f"data 不是符号链接:{link}")
        self.assertEqual(self.data, link.resolve())
        self.assertFalse((link / "real-data").exists(), "链接被建进了目录里")
        self.assertEqual(self.venv, (self.here / ".venv").resolve())


class LinkingTheTwoDirectories(_Base):
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


class TheOldVariableNamesStillWork(_Base):
    """只设旧名字时,链接必须落在旧名字指的那个目录上。

    这是审查复现出来的那条路:用户早就用 GZH_DATA_HOME 把自己的库隔离开了,
    新脚本只认 KB_COLLECTOR_DATA_PATH,于是无视这个选择、把链接改到默认位置
    或者存量回退那个原项目目录,还返回 0 报成功。
    """

    def only_legacy(self, **overrides):
        return self.run_setup(KB_COLLECTOR_DATA_PATH=None,
                              KB_COLLECTOR_VENV_PATH=None,
                              GZH_DATA_HOME=self.data,
                              GZH_VENV_HOME=self.venv, **overrides)

    def test_a_custom_directory_named_by_the_old_variable_is_honoured(self):
        result = self.only_legacy()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_linked()
        self.assertIn("GZH_DATA_HOME", result.stderr)   # 换了名字要说一声

    def test_the_old_variable_beats_the_legacy_location_fallback(self):
        """旧变量是显式选择,不该被"原位置还有东西"那条兜底顶掉。

        兜底的本意是「谁都没指定」时别把人家原来的库丢下;用旧名字指过的人
        恰恰是指定了。两者撞上时按用户说的来。
        """
        legacy = Path(self.tmp.name).resolve() / "fake-home" / "code" / "gongzhonghao"
        (legacy / "WeChatOA_Aggregation" / "data").mkdir(parents=True)
        (legacy / "WeChatOA_Aggregation" / "data" / "wechatoa.db").write_bytes(b"old project")
        self.assertEqual(0, self.only_legacy(HOME=legacy.parents[1]).returncode)
        self.assert_linked()

    def test_the_new_name_wins_when_both_are_set(self):
        other = Path(self.tmp.name).resolve() / "other-data"
        other.mkdir()
        result = self.run_setup(GZH_DATA_HOME=other, GZH_VENV_HOME=self.venv)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_linked()                             # 新名字那个
        self.assertIn("都设了", result.stderr)           # 但别闷着
