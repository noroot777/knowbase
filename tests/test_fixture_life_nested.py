# -*- coding: utf-8 -*-
"""**防线还没生效的那段窗口里,谁来兜底。**

两条用例各打这段窗口的一头,共用的立意是:夹具的三道防线(owner、空闲、收场)都要先
装上才算数,**而装上之前、以及装它的那个进程自己被掐掉之后,后台不许留下在服务的东西**。

- 一头是**外层**:内层那一轮 unittest 自己还会起中间进程和真夹具,它被外层 deadline 掐掉
  时一条 `addCleanup` 都跑不了,所以整棵树的最终所有权必须在外层(`_run_nested`)。
- 另一头是**里层**:摆盘那道输出边界**建立的过程本身**不许依赖旧输出写得动,否则三道防线
  一道都装不上,而这一刻没有任何更晚的硬退出能打断它。

硬约束与事故形态在 `docs/decisions/fixture-life.md`。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import fixture_life
from tests.fixture_life_cases import 起与收
from tests.fixture_life_probe import _free_port, _wait_listening, ROOT


class 没装上防线的那段窗口(起与收):

    def test_内层那一轮被掐掉时整棵树都得收掉(self):
        """**外层 deadline 到了,内层登记的回收一行都不会跑 —— 所以得外层来收。**

        `subprocess.run(..., timeout=...)` 只 SIGKILL 直接那个子进程。被强杀的测试运行器
        跑不了自己的 `addCleanup`,它起的中间进程和夹具就都没人收;更糟的是夹具的 owner
        写端攥在**活着的**中间进程手里,EOF 那道防线也不会响,而这里起的正是 `idle='0'`
        那一档 —— 空闲那道也关着。三道防线同时失效,留下的是一个永久在服务的夹具。

        这里确定性地制造那个局面:内层起好夹具、把三个号写进一本账,然后睡到外层 deadline。
        断言的是**后果** —— 那两个进程都没了、端口也不响应了,而且那份根不留在盘上。
        """
        box = tempfile.mkdtemp(prefix="nested-outer-")
        self.addCleanup(shutil.rmtree, box, ignore_errors=True)
        ledger = Path(tempfile.mkdtemp(prefix="nested-ledger-"))
        self.addCleanup(shutil.rmtree, ledger, ignore_errors=True)
        book = ledger / "pids"
        code = "\n".join([
            "import sys, time, unittest",
            f"sys.path.insert(0, {str(ROOT)!r})",
            "from tests.fixture_life_cases import 起与收",
            "class 一次(起与收):",
            "    def runTest(self):",
            "        port, middle, kid = self._spawn_via_middle(idle='0', owned=False)",
            f"        open({str(book)!r}, 'w').write("
            "'%d %d %d' % (port, middle.pid, kid))",
            "        time.sleep(600)",      # 卡住:它再也走不到自己的清理
            "case = 一次()",
            "case.run(unittest.TestResult())",
        ])
        done = self._run_nested([sys.executable, "-c", code], timeout=25,
                                env={**os.environ, "TMPDIR": box})
        self.assertIsNone(done.returncode,
                          f"这条用例要的就是外层超时:{done.stdout[-300:]!r}")
        self.assertTrue(book.exists(),
                        "内层还没起好夹具就被掐了,这条用例没验到东西")
        port, middle, kid = (int(x) for x in book.read_text().split())
        self.assertTrue(self._wait_dead(kid, 10), f"夹具 {kid} 还在后台")
        self.assertTrue(self._wait_dead(middle, 10), f"中间进程 {middle} 还在后台")
        self.assertFalse(self._port_open(port), "页面不该还开着")
        self.assertEqual([], os.listdir(box),
                         "内层被掐掉之后那份根也不该留在盘上")

    def test_边界建起来之前不许倒那点缓冲(self):
        """**立那道边界的过程本身,也不许依赖旧输出写得动。**

        摆盘边界原先是「先把 Python 层缓冲倒给真 fd、再换号」。真管道满了又没人消费、而
        缓冲里确实有东西(包装程序在 import 入口之前打过一段不带换行的字)时,那次同步
        `flush()` 就永久停在那儿 —— `suppress(Exception)` 接得住异常,接不住阻塞。此刻
        owner 和空闲两道看门狗都还没装(这个模块是 `fixture_life` 在自己模块体里 import
        进来的),没有任何更晚的硬退出能打断它:进程活着、首页不监听、沙箱根还在。

        现有那两条满管道用例验不到这里:它们没有预先往缓冲里写东西,所以倒缓冲那一下是
        空操作。这里两条通道各补一次,断言的是后果 —— **照旧起得来服务,随后按空闲自己
        退掉**。
        """
        for fd, writer in ((1, "sys.stdout"), (2, "sys.stderr")):
            with self.subTest(channel=fd):
                port = _free_port()
                _read_fd, write_fd = self._plug()
                wrapper = "\n".join([
                    "import runpy, sys",
                    # **不带换行**:行缓冲下它就留在 Python 层,等着谁去 flush
                    f"{writer}.write('摆盘前打的一段字')",
                    "sys.argv = sys.argv[1:]",
                    "runpy.run_path(sys.argv[0], run_name='__main__')",
                ])
                env = {**os.environ, fixture_life.IDLE_ENV: "1"}
                env.pop(fixture_life.OWNER_ENV, None)
                env.pop("PYTHONUNBUFFERED", None)   # 无缓冲就留不住那段字了
                proc = subprocess.Popen(
                    [sys.executable, "-c", wrapper,
                     "tests/serve_workspace.py", "--port", str(port)],
                    cwd=ROOT, env=env,
                    stdout=write_fd if fd == 1 else subprocess.DEVNULL,
                    stderr=write_fd if fd == 2 else subprocess.DEVNULL)
                self.addCleanup(self._reap, proc)
                # 那根管子**一直不消费**:边界立对了的话,它照旧起得来
                _wait_listening(port, proc, timeout=60)
                self.assertEqual(proc.wait(timeout=60), 0,
                                 f"{fd} 号堵着也该按空闲正常退掉")
                self.assertFalse(self._port_open(port), "退了就不该还监听")


if __name__ == "__main__":
    unittest.main()
