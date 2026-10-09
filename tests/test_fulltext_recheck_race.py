# -*- coding: utf-8 -*-
"""存量复查的写回和补抓落库抢同一条记录(scripts/feed_fulltext_recheck.py)。

2026-10-09 第 3 轮自由工作流审查抓到的:复查**先读**出那份已知错误的正文,
随后只按 `key` 无条件 UPDATE —— 而 `_suspect` 的读事务早就结束了,`_plan` 还
要去翻备份库。同步在这段窗口里把这一条补抓成功、翻译好、导出到 vault 之后,
这份旧计划照样把它盖回去:914 字完整正文连同中文译文和补抓成功时间一起变回
29 字的标题加链接,后面三轮再抓不到就永久停在壳上(审查双线程实测,独立复现
三次)。

**这一组钉的是「扫描读到的那一份」和「写回那一刻库里的那一份」之间的关系**,
判据在 `_downgrade` 那条 UPDATE 自己的 WHERE 里(`AND body=?`)。所以断言
**一律先验后果** —— 那份完整正文、译文、成功标记、已经导出的 md 还在不在;
只验「跳没跳过」的话,一个认出来了却照样覆盖的实现也能全绿。

**按形状单独一个文件**:`tests/test_fulltext_failures.py` 已经 572 行,这一组
连同它的交错摆盘挤进去会越过 700 行上限。摆盘(`RecheckCase`)复用那一份 ——
各写一份的话两边的前提会各漂各的。
"""
import contextlib
import io
import threading
import unittest
from unittest.mock import Mock, patch

from kb import llm, pipeline_state
from kb.collectors.feeds import FeedCollector
from kb.feeds import fulltext, readable, registry as reg, sync
from tests.test_feed_fulltext import PAGE, page
from tests.test_fulltext_failures import LEGAL_TEXT, RecheckCase

# 补抓成功之后库里那份正文的特征句(来自 `PAGE` 的 `<article>`)。
ARTICLE_MARK = 'The runtime schedules tools'
# 另一条路写进来的正文:`_save` 之外的写入方也算"内容已经换过"。
OTHER_BODY = 'A completely different article body, long enough to count. ' * 12


class RecheckRaceTests(RecheckCase):
    """复查扫描之后、写回之前,同步把这一条补抓成功了。"""

    def _waiting_for_recheck(self, body=LEGAL_TEXT, url=None):
        """审查那条复现的初始形态:旧版修复留下的「body 还是样板文字、却已经
        回到补抓队列」。第 2 轮确认过它就是生产库此刻的样子。"""
        key = self._already_wrong(body, url=url)
        with reg.db() as c:
            c.execute("UPDATE feed_items SET fulltext_at='',fulltext_tries=0,"
                      "body_zh='',title_zh='',translated_at='' WHERE key=?", (key,))
        return key

    def _sync_succeeds(self):
        """跑一轮**真** materialize:补抓拿到正常文章页,翻译、导出全走真逻辑。"""
        with patch.object(fulltext, 'request', Mock(return_value=page(PAGE))), \
                patch.object(sync, '_stage', lambda *a, **k: None), \
                patch.object(llm, 'chat', Mock(
                    return_value='###TITLE###\n标题译文\n###BODY###\n正文译文')), \
                patch.dict(pipeline_state._state, stop_requested=False):
            FeedCollector('rss').materialize()

    def _run_with_sync_in_the_gap(self, *argv):
        """真 `cmd_run`,但在 `_plan` 交出计划之后、写回之前插一轮成功同步。

        **注入点选在 `_plan` 之后,因为真实的窗口就在那儿**:`_suspect` 的读
        事务已经结束,`_plan` 还要逐条去翻备份库(I/O),写回是另一个事务。
        `_suspect`、`_plan` 和写回全是真的,只有"同步什么时候跑完"被排定了。
        """
        import scripts.feed_fulltext_recheck as mod
        real = mod._plan

        def planned(rows, backups):
            plan = real(rows, backups)
            self._sync_succeeds()
            return plan

        buf = io.StringIO()
        with patch.object(mod, '_plan', planned), \
                patch.object(mod.sys, 'argv', ['feed_fulltext_recheck.py', *argv]), \
                contextlib.redirect_stdout(buf):
            return mod.main(), buf.getvalue()

    def _vault(self):
        return '\n'.join(f.read_text() for f in (self.root / 'vault').rglob('*.md'))

    def _plan_now(self, **kw):
        import scripts.feed_fulltext_recheck as mod
        return mod, mod._plan(mod._suspect(**kw), [])

    def test_a_body_fetched_in_the_gap_is_not_overwritten(self):
        """**审查那条拦验收证据的反面。**

        修前(三次独立复现):写回之后 `body` 只剩 29 字标题加链接、
        `content_kind` 退回 `summary`、`fulltext_at` 和 `body_zh` 都被清空,
        导出的 md 也失去了刚拿到的正文和译文。
        """
        key = self._waiting_for_recheck()
        code, _ = self._run_with_sync_in_the_gap('run', '--yes')
        row = self.row(key)
        # ① 那份完整正文一个字都没动
        self.assertIn(ARTICLE_MARK, row['body'])
        self.assertEqual('content', row['content_kind'])
        self.assertNotIn('Rights Reserved', row['body'])     # 样板文字也没回来
        # ② 成功标记和译文还在:它不该重新排进补抓队列
        self.assertNotEqual('', row['fulltext_at'])
        self.assertEqual('正文译文', row['body_zh'])
        self.assertEqual(0, fulltext.pending('rss'))
        # ③ 已经导出的那篇 md 还是译文,不是壳
        self.assertIn('正文译文', self._vault())
        self.assertEqual(0, code)

    def test_the_rescued_item_does_not_fall_back_to_the_shell_later(self):
        """审查那张表的第三行:被覆盖之后再失败三轮就永久停在壳上。

        正文保住了,这一条压根不在补抓队列里 —— 后面原文怎么坏都影响不到它。
        """
        key = self._waiting_for_recheck()
        self._run_with_sync_in_the_gap('run', '--yes')
        self._three_failed_rounds()                 # 原文此后一直回版权声明页
        row = self.row(key)
        self.assertIn(ARTICLE_MARK, row['body'])
        self.assertEqual('content', row['content_kind'])
        self.assertIn('正文译文', self._vault())
        self.assertEqual(0, row['fulltext_tries'])  # 一次重试都没被耗掉

    def test_a_mixed_batch_cuts_only_the_one_that_moved_on(self):
        """同一批里一条被同步修好、一条还是样板文字:各归各位。

        跳过一条不许把另一条连坐 —— 逐条判、逐条统计。
        """
        rescued = self._waiting_for_recheck()       # 在队列里,同步会修好它
        stale = self._already_wrong(LEGAL_TEXT, url='https://e.org/stale')
        code, out = self._run_with_sync_in_the_gap('run', '--yes')
        self.assertIn(ARTICLE_MARK, self.row(rescued)['body'])
        self.assertEqual('', readable.boilerplate_reason(self.row(stale)['body']))
        self.assertEqual('summary', self.row(stale)['content_kind'])
        self.assertIn('已收拾 1 条', out)
        self.assertEqual(0, code)

    def test_the_skip_is_counted_not_swallowed(self):
        """报告点名的那半句:成功/跳过数量按**实际更新结果**统计。

        按计划条数报的话,这一趟会说「已收拾 1 条」而库里一个字节都没改 ——
        那比不报还糟:人看完就以为这条已经处理过了。
        """
        self._waiting_for_recheck()
        code, out = self._run_with_sync_in_the_gap('run', '--yes')
        self.assertEqual(0, code)
        self.assertIn('已收拾 0 条', out)
        self.assertIn('内容已经换过', out)       # 跳过了几条、是哪几条都说出来
        self.assertIn('Yes, and', out)

    def test_the_guard_holds_across_connections(self):
        """直测:计划算好之后,**另一条独立连接**把正文换掉并提交。

        两条连接(`reg.db()` 每次新开一条,sqlite3 不共享 cache)就足以证明
        这道闸不靠任何进程内状态 —— 判据待在写语句里,和那次写入同生共死。
        """
        key = self._waiting_for_recheck()
        mod, plan = self._plan_now()
        self.assertEqual(1, len(plan))
        with reg.db() as c:
            c.execute("UPDATE feed_items SET body=?,content_kind='content' WHERE key=?",
                      (OTHER_BODY, key))
        cut, moved = mod._downgrade(plan)
        self.assertEqual([], [r['key'] for r in cut])
        self.assertEqual([key], [r['key'] for r in moved])
        self.assertEqual(OTHER_BODY, self.row(key)['body'])
        self.assertEqual('content', self.row(key)['content_kind'])

    def test_without_a_race_everything_is_still_cut(self):
        """反过来钉一边:没有交错时照旧全部收拾掉,别把正常降级也拦了。"""
        key = self._waiting_for_recheck()
        mod, plan = self._plan_now()
        cut, moved = mod._downgrade(plan)
        self.assertEqual([key], [r['key'] for r in cut])
        self.assertEqual([], moved)
        self.assertEqual('', readable.boilerplate_reason(self.row(key)['body']))
        self.assertEqual('summary', self.row(key)['content_kind'])

    def test_a_later_fetch_still_lands_after_a_downgrade(self):
        """另一种次序:复查先写回,补抓随后才成功 —— 那就等于"用户在复查之后
        才抓到正文",照旧要落得进去。两种交错的末态都是那份完整正文。
        """
        key = self._waiting_for_recheck()
        mod, plan = self._plan_now()
        cut, _ = mod._downgrade(plan)
        self.assertEqual([key], [r['key'] for r in cut])
        self.assertEqual(1, fulltext.pending('rss'))        # 回到队列里了
        self._sync_succeeds()
        row = self.row(key)
        self.assertIn(ARTICLE_MARK, row['body'])
        self.assertEqual('content', row['content_kind'])
        self.assertNotEqual('', row['fulltext_at'])

    def test_two_real_threads_end_up_consistent(self):
        """**真的两个执行体**(各一条连接),不人为排序地抢同一条记录。

        两种交错都通向同一个末态 —— 那份完整正文:`_save` 先提交则判据看到
        正文已经换了、跳过;`_downgrade` 先提交则 `_save` 紧接着把正文写上去。
        **不许出现「壳 + 补抓成功时间」或者「壳 + 译文」那种半状态**,口径同
        `sync._claim_write` 那条「两种结局的末态都是对的」。8 轮各一次。
        """
        article = ARTICLE_MARK + ' and keeps the transcript for later replay. ' * 20
        for i in range(8):
            with self.subTest(round=i):
                key = self._waiting_for_recheck(url=f'https://e.org/race{i}')
                mod, plan = self._plan_now()
                mine = [r for r in plan if r['key'] == key]
                self.assertEqual(1, len(mine))
                gate = threading.Barrier(2)

                def downgrade():
                    gate.wait()
                    mod._downgrade(mine)

                def save():
                    gate.wait()
                    fulltext._save(key, article)

                threads = [threading.Thread(target=downgrade), threading.Thread(target=save)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                row = self.row(key)
                self.assertIn(ARTICLE_MARK, row['body'])
                self.assertEqual('content', row['content_kind'])
                self.assertNotEqual('', row['fulltext_at'])


if __name__ == '__main__':
    unittest.main()
