# -*- coding: utf-8 -*-
"""绑了文档的收藏:**核验到提交整段待在换文件那条路也认的那把锁里。**

`doc_version` 只是一次**取样**。上一版把核验挪进了同一个请求、排在 INSERT 前面,
可两者之间仍然谁都能换掉那个文件 —— 而这个间隔不由我们决定:INSERT 要等别人的
写事务时,它由**对方**决定。审查实测核验与落库之间等了约 1 秒,期间另一路真走
`translate._save → sync.export` 把那一篇换成新译文,收藏照旧落成 `origin=doc`
返回 200,立刻回看 0 个高亮、提示「没找到这段收藏」。

所以这一份钉的是**临界区**,不是间隔长短:
1. 核验那一刻,改 vault 文件的那条路(它们全认 `kb/maintenance.py` 那把跨进程锁)
   拿不到锁;数据库那一头的写事务也已经在手上。
2. INSERT 等写锁的那一整段,文件照样换不进来。
3. 等不到那把锁就按 503 如实拒,**一个字节不写**;锁一放开同一条照旧存得下。
4. **反过来钉一边**:没有 path 的那几类(日报/问答/订阅原文)压根不绑文件,
   锁被别人攥着时它们照存。

观察手段只用两种,都不改被测语义:在另一条线程上**问**同一把锁和写事务忙不忙
(`maintenance_lock(wait=0)` 拿不到就是有人在里面),以及把 `_doc_version_now`
包一层**只记账、原样调真实实现**的壳。

摆盘(临时库 + 盘上那一篇 + 只挂收藏路由的 client)在 `tests/fav_snippet_fixture.py`,
存取那一头的用例在 `tests/test_fav_snippet_store.py`;硬约束和事故形态在
`docs/decisions/fav-snippets.md`,锁序那一条在 `docs/decisions/maintenance-lock.md`。

跑:  PYTHONPATH=<worktree 根> python -m unittest tests.test_fav_snippet_lock
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.maintenance import LockBusy, maintenance_lock           # noqa: E402
from kb.scanner import body_version                             # noqa: E402
from kb.web import favorites_api                                # noqa: E402

from tests.fav_snippet_fixture import (                         # noqa: E402
    DOC, REAL_CONNECT, ApiFixture)

#: 另一版正文(真去替换那个文件时写进去的)。里面**没有**用例收藏的那句话 ——
#: 换成功了的话,那条收藏点回去就是 0 个高亮。
OTHER_MD = """---
title: 活着的那篇
account: 某号
---

重译之后整段换了说法,原来那句话一个字都不在了。
"""

QUOTE = "文中一句"


class DocBoundSaveMutexTests(ApiFixture):

    def _busy(self) -> dict:
        """在另一条线程上问一句:那把维护锁、那个写事务,此刻忙不忙?

        只问不改:锁是 `wait=0` 试一下马上放,写事务是 `BEGIN IMMEDIATE` 成功就
        立刻回滚。线程另起是必须的 —— `maintenance_lock` 对**同一条线程**重入直接
        放行(flock 认的是 open file description),同线程问等于什么都没问。
        """
        out = {}

        def ask():
            try:
                with maintenance_lock(wait=0):
                    out["lock"] = "free"
            except LockBusy:
                out["lock"] = "busy"
            conn = sqlite3.connect(self.db, timeout=0.3)
            try:
                conn.execute("BEGIN IMMEDIATE")
                out["write"] = "free"
                conn.rollback()
            except sqlite3.OperationalError:
                out["write"] = "busy"
            finally:
                conn.close()

        t = threading.Thread(target=ask)
        t.start()
        t.join(10)
        return out

    def test_the_verification_happens_inside_the_mutex_and_the_write_transaction(self):
        """核验那一刻,两道带子都已经在手上。"""
        seen = {}
        real = favorites_api._doc_version_now

        def watched(path):
            seen.update(self._busy())      # 只记账
            return real(path)              # 原样调真实实现

        with mock.patch.object(favorites_api, "_doc_version_now", watched):
            r = self.snip(path=DOC, quote=QUOTE)
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual({"lock": "busy", "write": "busy"}, seen,
                         "核验是裸着跑的:这一刻换文件的那条路能进来")
        # 反过来钉一边:没有请求在里面时,这两句问出来的是"没人占着" ——
        # 否则上面那条断言可能只是在说"这台机器上这两样永远忙"。
        self.assertEqual({"lock": "free", "write": "free"}, self._busy())

    def test_a_swap_cannot_slip_in_while_the_insert_waits_for_the_write_lock(self):
        """审查那条复现:INSERT 在等别人的写事务,期间真去换那个文件。"""
        full = self.vault / DOC
        before = full.read_text(encoding="utf-8")
        out = {}

        def rival():
            conn = REAL_CONNECT(self.db)
            try:
                conn.execute("BEGIN IMMEDIATE")   # 先占住写锁,让那条 INSERT 等着
                out["holding"] = True
                holding.set()
                # 等到那条请求真的攥上了维护锁(问不到就是它在里面)
                for _ in range(300):
                    try:
                        with maintenance_lock(wait=0):
                            pass
                    except LockBusy:
                        out["entered"] = True
                        break
                    time.sleep(0.02)
                try:                              # 这就是导出那一步要做的事
                    with maintenance_lock(wait=0.5):
                        full.write_text(OTHER_MD, encoding="utf-8")
                        out["swap"] = "done"
                except LockBusy:
                    out["swap"] = "blocked"
                conn.rollback()                   # 放开写锁,让那条 INSERT 走完
            finally:
                conn.close()

        holding = threading.Event()
        t = threading.Thread(target=rival)
        t.start()
        self.assertTrue(holding.wait(10), "没能摆出'别人攥着写事务'这个局面")
        r = self.snip(path=DOC, quote=QUOTE)
        t.join(30)
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(out.get("entered"), "请求压根没进维护锁:临界区是空的")
        self.assertEqual("blocked", out.get("swap"))
        self.assertEqual(before, full.read_text(encoding="utf-8"))
        row = self.listed()["favorites"][0]
        self.assertEqual(DOC, row["path"])
        self.assertEqual(QUOTE, row["quote"])

    def test_the_stale_version_is_still_refused_once_the_swap_really_lands(self):
        """锁不是把文件冻住了:放开之后换得成,而那一版的收藏照旧按 409 拒。

        **这条是上面那条的另一半** —— 只有「换不进来」没有这一条的话,把
        `_bind_and_insert` 改成永远握着锁也能全绿。
        """
        stale = self.version
        with maintenance_lock(wait=5):
            (self.vault / DOC).write_text(OTHER_MD, encoding="utf-8")
        self.assertNotEqual(stale, body_version(OTHER_MD))
        r = self.snip(path=DOC, quote=QUOTE, doc_version=stale)
        self.assertEqual(409, r.status_code, r.text)
        self.assertEqual([], self.listed()["favorites"])
        # 用新那一版存就该成
        ok = self.snip(path=DOC, quote="重译之后整段换了说法",
                       doc_version=body_version(OTHER_MD))
        self.assertEqual(200, ok.status_code, ok.text)

    def test_a_bound_snippet_refuses_when_it_cannot_get_the_mutex(self):
        """等不到锁 → 503、一个字节不写;锁一放开同一条照旧存得下。"""
        release = threading.Event()
        holding = threading.Event()

        def holder():
            with maintenance_lock(wait=10):
                holding.set()
                release.wait(30)

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(holding.wait(10))
        try:
            with mock.patch.object(favorites_api, "_DOC_LOCK_WAIT", 0.2):
                r = self.snip(path=DOC, quote=QUOTE)
            self.assertEqual(503, r.status_code, r.text)
            self.assertIn("没法核对", r.json()["detail"])
            self.assertEqual([], self.listed()["favorites"])
            # **没有 path 的那几类不绑文件,锁被占着也该照存**(反过来钉一边:
            # 把整个端点圈进锁里就会在这儿红)。
            daily = self.snip(quote="日报一句", origin="daily",
                              origin_ref="2026-10-15", origin_title="日报 · 2026-10-15")
            self.assertEqual(200, daily.status_code, daily.text)
        finally:
            release.set()
            t.join(10)
        again = self.snip(path=DOC, quote=QUOTE)
        self.assertEqual(200, again.status_code, again.text)


if __name__ == "__main__":
    unittest.main()
