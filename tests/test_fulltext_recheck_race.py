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

**第 4 轮审查紧接着抓到了同一个窗口的另一个写入方向**(`StaleTranslationTests`):
翻译请求先基于旧正文发出,复查在回包返回之前把正文换掉并作废译文,而
`translate._save` 当时只按 `key` 无条件写 —— 旧回包把「版权所有,网站使用条款
和隐私声明。」重新写了回去,`translated_at` 跟着重新设上,于是这一条**再也不进
翻译队列**:正文是正确的标题与链接,中文却是那段版权声明,再同步三轮也清不掉
(三个隔离库稳定复现)。两个方向的判据是同一条规矩,所以摆在一个文件里。

**按形状单独一个文件**:`tests/test_fulltext_failures.py` 已经 576 行,这一组
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
from kb.feeds import fulltext, readable, registry as reg, sync, translate
from tests.test_feed_fulltext import PAGE, SHELL, page
from tests.test_fulltext_failures import LEGAL_TEXT, RecheckCase

# 补抓成功之后库里那份正文的特征句(来自 `PAGE` 的 `<article>`)。
ARTICLE_MARK = 'The runtime schedules tools'
# 另一条路写进来的正文:`_save` 之外的写入方也算"内容已经换过"。
OTHER_BODY = 'A completely different article body, long enough to count. ' * 12
# 旧请求那份作废译文的特征句。逐字取自审查报告里那张表的第二行。
STALE_ZH = '版权所有，网站使用条款和隐私声明。'
# 复查写回之后 `body` 里该出现的东西(`_rebuild` 那个"标题与链接"兜底形态)。
SHELL_MARK = 'https://e.org/post'


def plan_now(**kw):
    """现在就算一份复查计划:真 `_suspect` + 真 `_plan`,不打桩。

    两个类共用(复查 vs 补抓、复查 vs 翻译),所以搁在模块级 —— 各写一份的话
    两组的前提会各漂各的,而它们钉的恰恰是同一条判据的两个方向。
    """
    import scripts.feed_fulltext_recheck as mod
    return mod, mod._plan(mod._suspect(**kw), [])


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
        mod, plan = plan_now()
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
        mod, plan = plan_now()
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
        mod, plan = plan_now()
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
                mod, plan = plan_now()
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


class StaleTranslationTests(RecheckCase):
    """翻译请求发出之后、回包返回之前,复查把这一条的正文换掉并作废了译文。

    **断言一律先验后果** —— 库里那份译文、`translated_at`、导出的 md 是什么;
    只验「写回返回 True 还是 False」的话,一个认出来了却照样写的实现也能全绿。
    """

    def _waiting_for_translation(self, body=LEGAL_TEXT, url=None):
        """审查那条复现的初始形态:此前误抓的样板文字条目,补抓成功时间留着
        (所以它不在补抓队列里)、译文清空(所以它进得了翻译队列)。"""
        key = self._already_wrong(body, url=url)
        with reg.db() as c:
            c.execute("UPDATE feed_items SET title_zh='',body_zh='',translated_at='',"
                      "lang='en' WHERE key=?", (key,))
        return key

    def _translate_with_recheck_in_the_gap(self, zh=STALE_ZH, boom=None):
        """跑一轮**真** `translate_pending`,在回包返回之前插一趟**真**复查。

        **注入点选在模型回包那一刻,因为真实的窗口就在那儿**:`translate_pending`
        已经把条目快照读进内存、连接也关了,而模型请求要几十秒到几分钟 —— 换正
        文的那两个写入方(一次成功补抓、一趟存量复查)在这期间完全可能跑完。
        `translate_pending`、`translate_item`、`_save`、`cmd_run` 全是真的,只有
        "复查什么时候跑完"被排定了。
        """
        asked = []

        def chat(prompt, *a, **kw):
            asked.append(prompt)
            if len(asked) == 1:                 # 这一条的请求已经发出去了
                self._recheck('run', '--yes')   # 真复查:正文换成壳、译文作废
                if boom:
                    raise boom
            return f'###TITLE###\n版权标题\n###BODY###\n{zh}'

        with patch.object(llm, 'chat', chat):
            return translate.translate_pending('rss'), asked

    def test_a_stale_translation_is_not_written_back(self):
        """**审查那条拦验收证据的反面。**

        修前(三个隔离库各一次):`body` 已经是正确的标题与链接,而 `body_zh`
        重新变成那段版权声明、`translated_at` 也重新设上。
        """
        key = self._waiting_for_translation()
        summary, asked = self._translate_with_recheck_in_the_gap()
        self.assertIn('Rights Reserved', asked[0])      # 它真的在译那段版权文字
        row = self.row(key)
        # ① 那份作废的译文一个字都没进库
        self.assertEqual('', row['body_zh'])
        self.assertEqual('', row['title_zh'])
        # ② 成功标记没写上 —— 写上了这一条就**再也不进翻译队列**
        self.assertEqual('', row['translated_at'])
        # ③ 复查那次写回原样有效:正文是壳,不是版权声明
        self.assertIn(SHELL_MARK, row['body'])
        self.assertNotIn('Rights Reserved', row['body'])
        self.assertEqual('summary', row['content_kind'])
        self.assertEqual(0, summary['done'])

    def test_the_vault_never_gets_the_stale_translation(self):
        """审查那张表的第三行:后续三轮补抓失败之后,导出的 md 里还留着它。

        正文换过之后这一条先回补抓队列,三轮试满才离开 —— 那时按**新**正文译
        一次。整条链上任何一处都不该再出现那段版权译文。
        """
        key = self._waiting_for_translation()
        self._translate_with_recheck_in_the_gap()
        _, asked, md = self._three_failed_rounds()
        row = self.row(key)
        self.assertNotIn(STALE_ZH, md)
        self.assertNotIn(STALE_ZH, row['body_zh'])
        self.assertNotIn('Rights Reserved', md)
        self.assertNotIn('Rights Reserved', asked)      # 也没再买一次那段文字
        self.assertEqual(3, row['fulltext_tries'])      # 降级出口照旧走到了

    def test_a_stale_failure_is_not_charged_to_the_new_content(self):
        """失败那一侧同理:旧正文的毛病不该记在换过来的新内容头上。

        记上去就是拿别人的错误推进这一条的重试计数 —— `MAX_TRIES` 到顶就按原文
        导出,等于让新正文少几次翻译机会。
        """
        key = self._waiting_for_translation()
        summary, _ = self._translate_with_recheck_in_the_gap(
            boom=RuntimeError('upstream 502'))
        row = self.row(key)
        self.assertEqual(0, row['translate_tries'])
        self.assertEqual('', row['translate_error'])
        self.assertEqual(0, summary['failed'])
        self.assertEqual([], summary['failed_keys'])
        self.assertEqual(1, summary['stale'])

    def test_the_discarded_result_is_counted_as_stale(self):
        """统计按**实际写回结果**(报告点名的那半句)。

        按"调了几次模型"报的话这一轮会说「已翻译 1 条」,而库里一个字都没改。
        既不算成功也不算失败:什么都没坏,只是这份译文不是当前内容的译文。
        """
        self._waiting_for_translation()
        summary, _ = self._translate_with_recheck_in_the_gap()
        self.assertEqual({'done': 0, 'failed': 0, 'stale': 1},
                         {k: summary[k] for k in ('done', 'failed', 'stale')})
        self.assertEqual([], summary['errors'])
        note = translate.summarize(summary)
        self.assertIn('旧译文作废', note)
        self.assertNotIn('翻译失败', note)              # 不判红:这不是故障

    def test_the_partial_path_is_guarded_too(self):
        """半成品那条路(只补一行标题)有独立射程:它交回去的 `body_zh` 压根不
        是这次模型给的,而是**请求发出时**库里那份旧正文的译文。

        不比标题之外那一半的话,这条路会把旧正文的译文重新写回新正文头上。
        """
        key = self._waiting_for_translation()
        with reg.db() as c:                     # 摆成「正文有译文、缺标题」
            c.execute('UPDATE feed_items SET body_zh=?,translated_at=? WHERE key=?',
                      (STALE_ZH, '2026-10-09T01:00:00+00:00', key))
        asked = []

        def chat(prompt, *a, **kw):
            asked.append(prompt)
            self._recheck('run', '--yes')
            return '版权标题'

        with patch.object(llm, 'chat', chat):
            summary = translate.translate_pending('rss')
        row = self.row(key)
        self.assertEqual(1, len(asked))                 # 真走了"只补标题"那条路
        self.assertNotIn('Rights Reserved', asked[0])
        self.assertEqual('', row['body_zh'])
        self.assertEqual('', row['title_zh'])
        self.assertEqual('', row['translated_at'])
        self.assertEqual(1, summary['stale'])

    def test_the_guard_holds_across_connections(self):
        """直测:请求发出之后,**另一条独立连接**把正文换掉并提交。

        两条连接(`reg.db()` 每次新开一条,sqlite3 不共享 cache)就足以证明这道
        闸不靠任何进程内状态 —— 判据待在写语句里,和那次写入同生共死。
        """
        key = self._waiting_for_translation()
        seen = translate.input_version(self.row(key))
        with reg.db() as c:
            c.execute("UPDATE feed_items SET body=?,content_kind='summary' WHERE key=?",
                      (SHELL, key))
        self.assertFalse(translate._save(key, '版权标题', STALE_ZH, seen))
        self.assertFalse(translate._fail(key, RuntimeError('502'), seen))
        row = self.row(key)
        self.assertEqual('', row['body_zh'])
        self.assertEqual('', row['translated_at'])
        self.assertEqual(0, row['translate_tries'])
        self.assertEqual('', row['translate_error'])

    def test_the_title_is_part_of_the_request_too(self):
        """**这一道是判据自己的契约**:请求输入是 (标题, 正文) 两项,判据就该覆盖
        两项,而不是"今天恰好只有正文会变"。

        如实记一笔:只比正文的实现在这套用例之外是全绿的 —— 现有的两个换内容的
        写入方(`fulltext._save`、复查的 `_downgrade`)都必然连正文一起换,所以
        正文那一半今天就够了。留着标题这一半的理由是**构造上的**:半成品那条路
        (见 `PARTIAL_SQL`)的请求输入**只有**标题,它交回来的正文译文压根不是这次
        模型给的 —— 哪天多一条只改标题的路,少这一半就是把旧标题的译文写给新标题。
        """
        key = self._waiting_for_translation()
        seen = translate.input_version(self.row(key))
        with reg.db() as c:
            c.execute('UPDATE feed_items SET title=? WHERE key=?', ('Another title', key))
        self.assertFalse(translate._save(key, '版权标题', STALE_ZH, seen))
        self.assertFalse(translate._fail(key, RuntimeError('502'), seen))
        row = self.row(key)
        self.assertEqual('', row['body_zh'])
        self.assertEqual('', row['translated_at'])
        self.assertEqual(0, row['translate_tries'])

    def test_without_a_race_the_translation_still_lands(self):
        """反过来钉一边:没有交错时译文照旧写得进去,别把正常翻译也拦了。"""
        key = self._waiting_for_translation()
        seen = translate.input_version(self.row(key))
        self.assertTrue(translate._save(key, '版权标题', STALE_ZH, seen))
        row = self.row(key)
        self.assertEqual(STALE_ZH, row['body_zh'])
        self.assertEqual('版权标题', row['title_zh'])
        self.assertNotEqual('', row['translated_at'])
        self.assertEqual('', row['exported_hash'])      # 重导出的闸门开了

    def test_two_real_threads_end_up_consistent(self):
        """**真的两个执行体**(各一条连接),不人为排序地抢同一条记录。

        两种交错的末态逐字相同 —— 壳 + 空译文:`_save` 先提交则复查紧接着把译
        文清掉(它的判据是 `body`,而翻译不改 body),复查先提交则 `_save` 的判据
        不再成立、0 行。**不许出现「壳 + 版权译文」那种半状态**,口径同
        `sync._claim_write` 那条「两种结局的末态都是对的」。8 轮各一次。
        """
        for i in range(8):
            with self.subTest(round=i):
                key = self._waiting_for_translation(url=f'https://e.org/t{i}')
                seen = translate.input_version(self.row(key))
                mod, plan = plan_now()
                mine = [r for r in plan if r['key'] == key]
                self.assertEqual(1, len(mine))
                gate = threading.Barrier(2)

                def downgrade():
                    gate.wait()
                    mod._downgrade(mine)

                def save():
                    gate.wait()
                    translate._save(key, '版权标题', STALE_ZH, seen)

                threads = [threading.Thread(target=downgrade),
                           threading.Thread(target=save)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                row = self.row(key)
                self.assertEqual('', row['body_zh'])
                self.assertEqual('', row['translated_at'])
                self.assertNotIn('Rights Reserved', row['body'])
                self.assertEqual('summary', row['content_kind'])


if __name__ == '__main__':
    unittest.main()
