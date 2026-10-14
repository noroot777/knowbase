# -*- coding: utf-8 -*-
"""「md 那行 flag 还没对齐」这条待办:它什么时候进库、什么时候出库。

被测的那件事:软广的一次交付跨两个存储 —— 判定进 kb.db 的事务,md frontmatter
那行进 vault 文件,**没有一个事务能同时管住两者**。中间断掉的末态是库里 none、
md 里还挂着 flag,而这一篇按 `promo_level <> ''` 从此掉出所有默认候选:没有
一条持久化待办,谁也不会再回来看它一眼,而面板上一切正常。

所以待办的时机是**判定提交前的协议**,不是失败之后的记账:
  * 落 —— 和判定同一个事务(`save_promo`),要么两样一起进库、要么一起回滚;
  * 清 —— md 那行写回**并回读确认**之后(`align_promo_tag`)。

挂在 `except` 上的那一版只在「我们还活着、还能执行一条 SQL」时才落得下来,而
头号时序恰恰是进程在中间没了。这里用真实的 `SIGKILL` 钉它:子进程跑到判定已
提交、md 还没写的那一瞬间给自己 `-9`,父进程检查末态。

硬约束在 `docs/decisions/promo.md` 第一节。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from kb import extract, store                                 # noqa: E402
from tests.test_promo_delivery import OLD_BODY, PATH, _Vault   # noqa: E402

# 子进程:跑一次真实交付,在「判定已提交、md 那行还没写」那一瞬间给自己 -9。
# 注入点挑 `sync_promo_tag` 是因为它就是回写那一步的入口 —— 判定此刻已经
# commit 过了(`commit_promo` 的事务在它之前收尾),md 一个字都还没动。
_KILL_CHILD = r'''
import os, signal, sys
sys.path.insert(0, {root!r})
sys.path.insert(0, {root!r} + "/scripts")
from pathlib import Path
from unittest import mock
from kb import extract, extract_promo, maintenance, store

root = Path({tmp!r})
with mock.patch.object(extract, "VAULT_ROOT", root / "vault"), \
     mock.patch.object(maintenance, "DATA_DIR", root):
    conn = store.connect(root / "kb.db")
    seen = extract.model_input(conn, {path!r})

    def die(*a, **kw):
        sys.stdout.write("about-to-write-md\n")
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)

    with mock.patch.object(extract_promo, "sync_promo_tag", die):
        extract_promo.commit_promo(conn, {path!r},
                                   {{"level": "none", "reason": "正常资讯"}},
                                   seen["version"], seen["seen"])
'''


class KilledMidDeliveryTest(_Vault):
    """判定提交之后、md 那行写回之前进程被 SIGKILL —— 待办必须留在库里。"""

    def _deliver_then_die(self):
        """真起一个子进程交付,让它在写 md 之前被 -9。返回它的 CompletedProcess。"""
        root = Path(__file__).resolve().parent.parent
        code = _KILL_CHILD.format(root=str(root), tmp=str(self.root), path=PATH)
        return subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=120)

    def test_a_killed_process_leaves_a_repairable_todo(self):
        """-9 之后:判定在库里、md 那行还挂着、**待办也在**,sync-tags 修得回来。

        这是整条兜底的后果式断言 —— 不验「标志打上没有」,验「下一轮还补不补得
        回来」。待办只挂在 `except` 上的那一版在这里红:`promo_tag_pending` 是
        0,`promo_tag_pending_docs` 是空的,`sync-tags` 报「待对齐 0 篇」,而库里
        是 none、md 里挂着 flag —— 两处永久不一致,没有任何待办指向它。
        """
        self.seed("hard", OLD_BODY, flagged=True)
        self.conn.commit()
        self.conn.close()                     # 子进程要独占写这个库
        proc = self._deliver_then_die()
        self.conn = store.connect(self.db)
        self.assertIn("about-to-write-md", proc.stdout,
                      f"子进程没跑到注入点:{proc.stderr[-800:]}")
        self.assertEqual(-9, proc.returncode,
                         f"子进程不是被 SIGKILL 结束的:{proc.stderr[-800:]}")
        # 末态:判定落了、md 那行还在(回写压根没发生)
        self.assertEqual("none", self.level(), "判定没落库 —— 夹具没摆成要测的那一档")
        self.assertTrue(self.md_flag(), "md 那行已经摘掉了 —— 注入点挑错了")
        # 要钉的那一条:待办在库里,而且修复那条路看得见它
        self.assertTrue(self.pending(), "进程被杀之后没有留下待办")
        self.assertEqual([PATH], extract.promo_tag_pending_docs(self.conn))
        # 后果:不花钱地修得回来,末态三处一致
        with self.env() as out:
            self.assertEqual(0, self.pb.cmd_sync_tags(
                SimpleNamespace(account=None)))
        self.assertIn("待对齐 1 篇", out.getvalue())
        self.assertFalse(self.md_flag(), "sync-tags 没把那行摘掉")
        self.assertFalse(self.db_flag())
        self.assertFalse(self.pending(), "修完了待办还挂着")

    def test_a_normal_delivery_leaves_nothing_behind(self):
        """反过来钉一边:没被打断的那一次,待办不许留在库里。

        不钉的话「永远留一条待办」也是绿的 —— 那会让 `sync-tags` 每次把整库
        重走一遍,而真正被打断的那几篇淹在里面看不出来。
        """
        self.seed("hard", OLD_BODY, flagged=True)
        with self.env(judge=lambda *a, **kw: {"level": "none", "reason": "正常资讯"}):
            self.assertEqual(0, self.pb.cmd_run(self.args()))
        self.assertEqual("none", self.level())
        self.assertFalse(self.md_flag())
        self.assertFalse(self.pending(), "交付成功了还留着待办")


# 同一件事的另一条协调入口:提取那三条路都经 `commit_extraction`。注入点挪到
# 它调回写那一下 —— 判定同样已经 commit 过了,md 一个字都还没动。
_KILL_EXTRACT_CHILD = r'''
import os, signal, sys
sys.path.insert(0, {root!r})
sys.path.insert(0, {root!r} + "/scripts")
from pathlib import Path
from unittest import mock
from kb import extract, maintenance, store

root = Path({tmp!r})
with mock.patch.object(extract, "VAULT_ROOT", root / "vault"), \
     mock.patch.object(maintenance, "DATA_DIR", root):
    conn = store.connect(root / "kb.db")
    seen = extract.model_input(conn, {path!r})
    data = {{"entities": [], "topics": [], "summary": "s",
            "promo": {{"level": "none", "reason": "正常资讯"}}}}

    def die(*a, **kw):
        sys.stdout.write("about-to-write-md\n")
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)

    with mock.patch.object(extract, "writeback_extraction", die):
        extract.commit_extraction(conn, {path!r}, data, seen["seen"])
'''


class KilledMidExtractionTest(_Vault):
    """提取那条入口被 SIGKILL 打断,待办同样要留在库里。

    **不是上面那条的复制**:两条入口各自开自己的事务、各自在不同的位置调回写,
    所以「待办进了判定那个事务」这件事要在两条路上各钉一次 —— 只钉补判那条的
    话,提取那三条路(管道、`kb extract --writeback`、并行脚本)是裸的。
    """

    def test_a_killed_extraction_also_leaves_a_repairable_todo(self):
        self.seed("hard", OLD_BODY, flagged=True)
        self.conn.commit()
        self.conn.close()
        root = Path(__file__).resolve().parent.parent
        code = _KILL_EXTRACT_CHILD.format(root=str(root), tmp=str(self.root),
                                          path=PATH)
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=120)
        self.conn = store.connect(self.db)
        self.assertIn("about-to-write-md", proc.stdout,
                      f"子进程没跑到注入点:{proc.stderr[-800:]}")
        self.assertEqual(-9, proc.returncode,
                         f"子进程不是被 SIGKILL 结束的:{proc.stderr[-800:]}")
        self.assertEqual("none", self.level(), "判定没落库 —— 夹具没摆成要测的那一档")
        self.assertTrue(self.md_flag(), "md 那行已经摘掉了 —— 注入点挑错了")
        self.assertTrue(self.pending(), "提取那条入口被杀之后没有留下待办")
        with self.env() as out:
            self.assertEqual(0, self.pb.cmd_sync_tags(
                SimpleNamespace(account=None)))
        self.assertIn("待对齐 1 篇", out.getvalue())
        self.assertFalse(self.md_flag(), "sync-tags 没把那行摘掉")
        self.assertFalse(self.pending())


class OneTransactionTest(_Vault):
    """判定和待办在**同一个事务**里:DB 那一侧写不下去就两样一起回滚。"""

    def test_a_failed_todo_write_rolls_the_verdict_back_too(self):
        """落待办那一句炸了 —— 判定也不许留在库里。

        两样分开提交的话,这里的末态是「判定 none 落了库、待办没落」,也就是
        `SIGKILL` 那一条要防的同一个坑,只不过起因换成了一次 DB 错误。
        """
        import sqlite3
        from unittest import mock

        from kb import extract_promo
        v = self.seed("hard", OLD_BODY, flagged=True)
        # 打在 `extract_promo` 上而不是 `extract` 上:`save_promo` 调的是它自己
        # 模块里那个全局名字,`kb.extract` 那边只是原样再导出的一份引用。
        with self.env(), \
             mock.patch.object(extract_promo, "mark_promo_tag_pending",
                               side_effect=sqlite3.OperationalError("disk I/O error")), \
             self.assertRaises(sqlite3.OperationalError):
            extract.commit_promo(self.conn, PATH,
                                 {"level": "none", "reason": "正常资讯"},
                                 v, extract.model_input(self.conn, PATH)["seen"])
        self.assertEqual("hard", self.level(), "待办写失败了,判定却留在了库里")
        self.assertTrue(self.db_flag(), "doc_tags 那行被改掉了")
        self.assertTrue(self.md_flag())

    def test_a_verdict_without_writeback_still_books_the_todo(self):
        """`kb extract` 不带 `--writeback`:只落库不碰文件,待办照样要留。

        那条路的 md 那行确实还欠着一次对齐。不留待办的话它和「已经对齐了」
        长得一模一样,而 `sync-tags` 是唯一会回来看它的人。
        """
        self.seed("", OLD_BODY, flagged=False)
        data = {"entities": [], "topics": [], "summary": "s",
                "promo": {"level": "hard", "reason": "文末在卖代充值"}}
        seen = extract.model_input(self.conn, PATH)["seen"]
        with self.env():
            self.assertFalse(extract.commit_extraction(
                self.conn, PATH, data, seen, writeback=False))
        self.assertEqual("hard", self.level())
        self.assertFalse(self.md_flag(), "writeback=False 居然碰了文件")
        self.assertTrue(self.pending(), "只落库那条路没留下待办")


if __name__ == "__main__":
    unittest.main()
