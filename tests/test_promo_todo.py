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

from kb import extract, scanner, store                        # noqa: E402
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


class ReusedVerdictTest(_Vault):
    """这次没拿到新结论、继续沿用库里那份判定 —— 待办照样要留。

    是漏得最深的那一档:`fulltext.retire_stale` 把补抓过正文的文章排回提取时
    **明确保留**原来的 promo,于是一篇已经判 hard 的广告带着结论重新排队;模型
    这一回没给 promo,`normalize_promo` 填一个带 `fallback` 的 none,而
    `save_promo` 不许兜底的 none 盖掉已有结论 —— 它返回 False。

    返回 False 不代表「没事要做」:库里那份 hard 还在,回写那一步照旧会按它去
    对齐 md 那行。把待办挂在「这次写进去了」上面,这一档就是裸的 —— 实测末态是
    库里 hard + md 里没那行 + 待办 0,`sync-tags` 报「待对齐 0 篇」,而
    `extracted_at` 已经非空,这一篇掉出所有候选:不花钱那条修复路和默认重提都
    不会再选中它。
    """

    def _fallback_data(self) -> dict:
        """走真的 `normalize_promo`,不自己编 fallback 长什么样。"""
        from kb.extract_llm import normalize_promo
        return {"entities": [], "topics": [], "summary": "新版普通教程",
                "promo": normalize_promo(None)}

    def _user_edited_it(self):
        """用户更新了文章、顺手把那行 flag 删了,索引也跟上了。"""
        self.md.write_text(self.md_text(OLD_BODY, flagged=False),
                           encoding="utf-8")
        from kb.chunker import split_chunks
        from kb.config import Source
        from unittest import mock as _m
        with _m.patch.object(scanner, "VAULT_ROOT", self.vault):
            doc = scanner._doc_from_file(
                self.md, Source(name="dr-gzh", root=self.vault / "source/dr-gzh"))
        store.upsert_document(self.conn, doc, split_chunks(doc.body), "fresh")
        self.conn.execute("DELETE FROM doc_tags WHERE path=? AND tag=?",
                          (PATH, extract.PROMO_TAG))
        self.conn.commit()

    def test_a_reused_verdict_whose_writeback_fails_is_still_repairable(self):
        """回写撞上一次真实 `OSError`,待办要在、`sync-tags` 要修得回来。

        注入的是**目录只读**:tag 回写走原子替换,文件自己那个 0444 拦不住
        `os.replace`(`mv` 覆盖只读文件本来就成功)。目录只读让临时文件压根建
        不起来,和磁盘满、vault 挂成只读是同一档。
        """
        self.seed("hard", OLD_BODY, flagged=True)
        self._user_edited_it()
        seen = extract.model_input(self.conn, PATH)["seen"]
        d = self.md.parent
        d.chmod(0o555)
        self.addCleanup(lambda: d.exists() and d.chmod(0o755))
        try:
            with self.env(), self.assertRaises(OSError):
                extract.commit_extraction(self.conn, PATH,
                                          self._fallback_data(), seen)
        finally:
            d.chmod(0o755)
        # 夹具摆对了吗:兜底的 none 没盖掉那份 hard,md 那行确实缺着
        self.assertEqual("hard", self.level(), "兜底的 none 把 hard 盖掉了")
        self.assertFalse(self.md_flag(), "夹具没摆成「md 那行缺着」")
        # 要钉的那一条:待办在,而且不花钱那条路修得回来
        self.assertTrue(self.pending(), "沿用旧判定那条路没留下待办")
        self.assertEqual([PATH], extract.promo_tag_pending_docs(self.conn))
        with self.env() as out:
            self.assertEqual(0, self.pb.cmd_sync_tags(
                SimpleNamespace(account=None)))
        self.assertIn("待对齐 1 篇", out.getvalue())
        self.assertTrue(self.md_flag(), "sync-tags 没把那行补回来")
        self.assertFalse(self.pending(), "修完了待办还挂着")

    def test_a_reused_verdict_that_writes_fine_leaves_nothing(self):
        """反过来钉一边:同一条路没出故障时,待办不许留下。

        不钉的话「一律留一条待办」也全绿 —— 那会让 `sync-tags` 每轮把整库重走
        一遍,真被打断的那几篇淹在里面。
        """
        self.seed("hard", OLD_BODY, flagged=True)
        self._user_edited_it()
        seen = extract.model_input(self.conn, PATH)["seen"]
        with self.env():
            self.assertTrue(extract.commit_extraction(
                self.conn, PATH, self._fallback_data(), seen))
        self.assertEqual("hard", self.level())
        self.assertTrue(self.md_flag(), "回写没把那行补回来")
        self.assertFalse(self.pending(), "交付成功了还留着待办")

    def test_a_pretagged_extraction_books_nothing(self):
        """另一边:产物里压根没有 promo 字段的那条路,不该替它登记一次对齐。

        pre_tagged 来源走 `parse_pretagged`,结果里没有 promo —— 回写那一步也
        因此不碰 md 那行(`writeback_extraction` 的 `has_promo` 为假)。所以条件
        是「这次会去对齐 **∧** 库里有明确判定」,不是光看库里有没有判定:写宽成
        后者的话这里全绿,而没有人会回来清这条待办,`sync-tags` 每轮都把这些
        文章重走一遍。
        """
        self.seed("hard", OLD_BODY, flagged=True)
        seen = extract.model_input(self.conn, PATH)["seen"]
        data = {"entities": [], "topics": [], "summary": "s"}   # 没有 promo
        with self.env():
            extract.commit_extraction(self.conn, PATH, data, seen,
                                      writeback=False)
        self.assertEqual("hard", self.level(), "判定被这条路动过了")
        self.assertFalse(self.pending(), "不碰 md 那行的路留下了待办")


if __name__ == "__main__":
    unittest.main()
