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
import signal
import subprocess
import sys
import tempfile
import time
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

    def _outer(self, book: Path) -> subprocess.Popen:
        """起一个**外层**:它自己调那个助手,内层起好夹具后把几个号写进账本再睡。

        账本里按序是:端口、内层运行器的 pid、中间进程的 pid、夹具的 pid。写之前先落
        `.part` 再改名,所以读到的一定是整份。
        """
        inner = "\n".join([
            "import os, sys, time, unittest",
            f"sys.path.insert(0, {str(ROOT)!r})",
            "from tests.fixture_life_cases import 起与收",
            "class 一次(起与收):",
            "    def runTest(self):",
            "        port, middle, kid = self._spawn_via_middle(idle='0', owned=False)",
            f"        open({str(book)!r} + '.part', 'w').write("
            "'%d %d %d %d' % (port, os.getpid(), middle.pid, kid))",
            f"        os.replace({str(book)!r} + '.part', {str(book)!r})",
            "        time.sleep(600)",      # 睡着:它再也走不到自己的清理
            "case = 一次()",
            "case.run(unittest.TestResult())",
        ])
        outer = "\n".join([
            "import sys, unittest",
            f"sys.path.insert(0, {str(ROOT)!r})",
            "from tests.fixture_life_cases import 起与收",
            "class 外层(起与收):",
            "    def runTest(self):",
            f"        self._run_nested([sys.executable, '-c', {inner!r}], timeout=300)",
            "case = 外层()",
            "case.run(unittest.TestResult())",
        ])
        # stderr 落文件而不是 PIPE:没人读的管道既会攒出 ResourceWarning,也可能堵住它
        with open(book.parent / "outer.err", "w") as ef:
            proc = subprocess.Popen([sys.executable, "-c", outer], cwd=ROOT,
                                    stdout=subprocess.DEVNULL, stderr=ef,
                                    start_new_session=True)
        # 它自己是一个新组的组首 → 兜底也走那条有归属证明的路
        self.addCleanup(self._sweep_session, proc)
        return proc

    def _wait_book(self, book: Path, timeout: float) -> tuple[int, int, int, int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if book.exists():
                got = [int(x) for x in book.read_text().split()]
                self.assertEqual(4, len(got), f"账本不完整:{got!r}")
                return tuple(got)                              # type: ignore[return-value]
            time.sleep(0.1)
        self.fail("内层没能在限时内起好夹具,这条用例没验到东西")

    def test_外层被取消时整棵树也得收掉(self):
        """**`unittest` 遇到 `KeyboardInterrupt` 不跑 `doCleanups()`。**

        它在 `_Outcome.testPartExecutor` 里把这个异常一路往上抛,`run()` 里那行
        `self.doCleanups()` 压根不执行 —— 所以「进程一建出来就 `addCleanup`」在这条终态上
        一个字都不算。而内层自己在独立会话里,不跟着外层所在的终端进程组收信号。实测:
        外层 rc=-2、日志是 KeyboardInterrupt,内层运行器、中间进程、夹具三个全活着,页面
        200。

        所以收场那一下必须在那个助手**自己的 `finally`** 里。这里断言的就是这条路的后果。
        """
        work = Path(tempfile.mkdtemp(prefix="nested-cancel-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        proc = self._outer(work / "book")
        port, _inner, middle, kid = self._wait_book(work / "book", 90)
        proc.send_signal(signal.SIGINT)
        rc = proc.wait(timeout=90)
        said = (work / "outer.err").read_text(errors="replace")
        # 前提确认:走的真是「被取消」那条终态,不是它自己正常跑完了
        self.assertNotEqual(0, rc, f"外层该是被取消掉的:{said[-400:]!r}")
        self.assertIn("KeyboardInterrupt", said, f"没走到那条终态:{said[-400:]!r}")
        self.assertTrue(self._wait_dead(kid, 15), f"夹具 {kid} 还在后台")
        self.assertTrue(self._wait_dead(middle, 15), f"中间进程 {middle} 还在后台")
        self.assertFalse(self._port_open(port), "页面不该还开着")

    def test_内层先退掉也不许把它的后代留下(self):
        """**拿到内层的退出码,不等于可以放手。**

        上一版把内层直接起成组首,于是「等它退出」这件事本身就把尸收了 —— 那一刻 pgid 的
        归属证明消失,而后代可能还活着:内层被外部杀掉时它压根没跑自己的清理。实测外层
        一切正常、清理全跑完,中间进程和夹具照旧活着、页面 200。

        这里定点制造那条路:等内层起好夹具,**从外面**把内层运行器 SIGKILL 掉,然后等外层
        自己走完。断言的是它的后代都没了 —— 判据不是「外层红没红」。
        """
        work = Path(tempfile.mkdtemp(prefix="nested-innerkill-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        proc = self._outer(work / "book")
        port, inner, middle, kid = self._wait_book(work / "book", 90)
        os.kill(inner, signal.SIGKILL)       # 它一条自己的清理都跑不了
        proc.wait(timeout=90)
        self.assertTrue(self._wait_dead(inner, 15),
                        "内层没被杀掉,这条用例没验到东西")
        self.assertTrue(self._wait_dead(kid, 15), f"夹具 {kid} 还在后台")
        self.assertTrue(self._wait_dead(middle, 15), f"中间进程 {middle} 还在后台")
        self.assertFalse(self._port_open(port), "页面不该还开着")

    def test_按组收的时候不许碰别人那一组(self):
        """**反过来钉一边:该收的收掉,不该碰的一个字都不许碰。**

        按组动手最怕的就是打到别人那一轮的验证上。这里让一个无关的夹具跟它并存 —— 那个
        夹具在**我们自己**的进程组里,不在锚点那一组 —— 然后真触发一次按组回收,断言它
        照旧活着、页面照旧开着。
        """
        port, bystander = self._spawn(idle="0")
        self.assertTrue(self._port_open(port), "旁边那个夹具没起来,这条用例没验到东西")
        done = self._run_nested(
            [sys.executable, "-c", "import time; time.sleep(600)"], timeout=3)
        self.assertIsNone(done.returncode, "这条用例要的就是走一遍按组回收")
        self.assertIsNone(bystander.poll(), "别人那一组的夹具不许被收掉")
        self.assertTrue(self._port_open(port), "它的页面该照旧开着")

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
