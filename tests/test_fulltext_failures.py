# -*- coding: utf-8 -*-
"""补抓没能拿到正文时,系统该怎么表现(kb/feeds/readable.py + sync.py)。

两条都是 2026-10-09 自由工作流审查在**生产库里**抓到的,形状相反:
①**拿到了垃圾却报成功** —— 版权声明、作者简介被当成文章正文覆盖掉原内容,
  `fulltext_at` 落下、条目离开重试队列,接着进翻译,页面上标着「已译为中文」。
  这一档**不可恢复**:后面任何一轮同步都不会再碰它。
②**压根没拿到却报绿** —— 原文回 403 时补抓摘要只进运行日志,导出收尾把阶段
  刷成 ok、failures 空,而那篇新文章一个字都没进知识库。

断言一律**先验后果**(正文有没有被覆盖、成功时间有没有落下、有没有进翻译、
阶段是不是红、明细里有没有原因),再验判据返回了什么 —— 只验判据的话,一个
认出来了却照样覆盖正文的实现也能全绿。

**按形状单独一个文件**:`tests/test_feed_fulltext.py` 已经 495 行,这一组连同
那两份真实样本的形态挤进去会越过 700 行上限。脚手架(`FullTextCase` / `page` /
`SHELL` / `PAGE`)复用那一份 —— 各写一份的话两边的前提会各漂各的。
"""
import contextlib
import inspect
import io
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from kb import llm, pipeline_state
from kb.collectors.feeds import FeedCollector
from kb.feeds import fulltext, readable, registry as reg, sync, translate
from kb.feeds.fetching import FeedError
from tests.test_feed_fulltext import PAGE, SHELL, FullTextCase, page

# 审查在生产库里抓到的那两条成品的真实形态(取自证据目录的
# `fortune-extracted.txt` / `stripe-extracted.txt`)。
LEGAL_TEXT = (
    '© 2026 Fortune Media IP Limited. All Rights Reserved. Use of this site constitutes '
    'acceptance of our Terms of Use and Privacy Policy | CA Notice at Collection and '
    'Privacy Notice | Do Not Sell/Share My Personal Information\n'
    'FORTUNE is a trademark of Fortune Media IP Limited, registered in the U.S. and other '
    'countries. FORTUNE may receive compensation for some links to products and services '
    'on this website. Offers may be subject to change without notice.')
BYLINE_TEXT = (
    'About the authors\n\n/About the authors\n\nAnna Mason\n\nAnna Mason is a technical '
    "writer at Stripe working across the engineering organization.\n\nHow API changes flow "
    "into Stripe's developer products\n\nFormatting an entire 25 million line codebase "
    'overnight: the rubyfmt story\n\nSharadh Krishnamurthy\n\nSharadh Krishnamurthy is the '
    'Engineering Manager of the Agent Foundation team at Stripe.\n\nAnupam Upadhyay\n\n'
    'Anupam Upadhyay is a Software Engineer on the AI Platform team at Stripe.')

# Fortune 那页的形态:文章正文只在 <script> 的 flight payload 里、DOM 里压根没有,
# 于是那个 React slot 成了唯一候选。**它没有 class、id 是 slot 编号**,所以
# `_NOISE` 那道判据对它完全是瞎的 —— 这是内容判据非有不可的依据。
LEGAL_PAGE = ('<html><head><title>LeCun</title></head><body>'
              '<div id="S:0">Latest in AI Most Popular</div>'
              f'<div id="S:1"><p>{LEGAL_TEXT}</p></div>'
              '<script>self.__next_f.push([1,"the real article lives in here"])</script>'
              '</body></html>')
# Stripe 那页的形态:class 自己报了身份,只是写成了 CSS Modules 的驼峰。
BYLINE_PAGE = ('<html><head><title>Knowledge AI</title></head><body><main>'
               "<section><h1>Meet Stripe's Knowledge AI Platform</h1></section>"
               '<div class="BlogPost-module__ifVEqa__aboutAuthor"><h2>About the authors</h2>'
               + ''.join(f'<p>{p}</p>' for p in BYLINE_TEXT.split('\n\n')[2:])
               + '</div></main></body></html>')


def tag(html, **find):
    return BeautifulSoup(html, 'html.parser').find(**find)


class BoilerplateJudgementTests(unittest.TestCase):
    """三道判据各自的射程。各管一类,少哪一道就漏哪一类。"""

    def test_a_page_of_only_legal_text_is_not_an_article(self):
        self.assertEqual('', readable.extract(LEGAL_PAGE))
        self.assertIn('版权', readable.boilerplate_reason(LEGAL_TEXT))

    def test_a_page_of_only_author_bios_is_not_an_article(self):
        self.assertEqual('', readable.extract(BYLINE_PAGE))
        self.assertIn('作者简介', readable.boilerplate_reason(BYLINE_TEXT))

    def test_the_class_judgement_is_blind_to_the_legal_page(self):
        """Fortune 那块没有 class、id 是 React slot 编号 —— 内容判据非有不可。"""
        self.assertFalse(readable._noisy(tag(LEGAL_PAGE, id='S:1')))

    def test_the_content_judgement_is_blind_to_the_bio_container(self):
        """反过来:作者简介那页赢出来的是**容器**,它开头是 `<h2>` 那行抬头,
        而真实页面上抬头常常不在文本最前面 —— class 那道因此非有不可。"""
        self.assertTrue(readable._noisy(
            tag(BYLINE_PAGE, name='div', class_='BlogPost-module__ifVEqa__aboutAuthor')))

    def test_camel_case_class_names_are_recognised(self):
        """CSS Modules / styled-components 的驼峰写法下,整张名单原来全部失效。"""
        for name in ('siteFooter', 'relatedPosts', 'shareBar', 'navMenu', 'sidebarWrap',
                     'aboutAuthor', 'BlogPost-module__xY__aboutAuthor'):
            with self.subTest(name=name):
                self.assertTrue(readable._noisy(tag(f'<div class="{name}">x</div>', name='div')))

    def test_the_reason_travels_with_the_empty_body(self):
        """面板上要说得出是哪一类(口径同 `fetch_one` 对 `html_problem` 那段)。"""
        body, why = readable.extract_or_reason(LEGAL_PAGE)
        self.assertEqual('', body)
        self.assertIn('版权', why)
        # 别的读不懂没有判据可言,两个都是空串 —— 调用方据此退回通用文案。
        self.assertEqual(('', ''), readable.extract_or_reason('<html><body><p>Short.</p></body></html>'))

    def test_an_ordinary_article_is_left_alone(self):
        """反过来钉一边:正常文章照旧提得出来,判据一个字都不许多。"""
        text = readable.extract(PAGE)
        self.assertIn('The runtime schedules tools for coding agents', text)
        self.assertEqual('', readable.boilerplate_reason(text))

    def test_a_long_article_with_a_copyright_footer_is_still_an_article(self):
        """判据是**占比**不是命中次数:长文章末尾挂一段版权声明是常态。"""
        article = ('The runtime replays every tool call from one deterministic log. ' * 40
                   + '\n' + LEGAL_TEXT)
        self.assertEqual('', readable.boilerplate_reason(article))

    def test_a_long_article_ending_in_author_bios_is_still_an_article(self):
        """作者简介那道只看开头,理由同上。"""
        article = ('The scheduler keeps the transcript so a crashed run resumes. ' * 40
                   + '\n\n' + BYLINE_TEXT)
        self.assertEqual('', readable.boilerplate_reason(article))


class BoilerplateOutcomeTests(FullTextCase):
    """后果先断言:这两类输入不许覆盖旧正文、不许写成功时间、不许进翻译。"""

    def _round(self, html):
        key = self.put()
        with patch.object(fulltext, 'request', Mock(return_value=page(html))), \
                patch.dict(pipeline_state._state, stop_requested=False):
            summary = fulltext.fetch_pending('rss')
        return key, summary, self.row(key)

    def test_the_legal_page_leaves_the_shell_untouched(self):
        key, summary, row = self._round(LEGAL_PAGE)
        self.assertEqual(SHELL, row['body'])              # 旧正文一个字没动
        self.assertEqual('', row['fulltext_at'])          # 没写成功时间
        self.assertEqual('summary', row['content_kind'])
        self.assertEqual('', row['translated_at'])
        self.assertEqual(0, summary['done'])
        self.assertEqual(1, summary['empty'])
        self.assertIn('版权', summary['errors'][0]['reason'])
        # 还在队列里,下一轮会再试 —— 而不是永久退出。
        self.assertEqual(1, fulltext.pending('rss'))
        self.assertEqual(1, row['fulltext_tries'])

    def test_the_bio_page_leaves_the_shell_untouched(self):
        key, summary, row = self._round(BYLINE_PAGE)
        self.assertEqual(SHELL, row['body'])
        self.assertEqual('', row['fulltext_at'])
        self.assertEqual(0, summary['done'])
        self.assertEqual(1, summary['empty'])
        self.assertEqual(1, fulltext.pending('rss'))

    def test_neither_reaches_the_translator(self):
        for html, label in ((LEGAL_PAGE, '版权'), (BYLINE_PAGE, '作者简介')):
            with self.subTest(label=label):
                self._round(html)
                self.assertEqual(0, translate.pending('rss'))

    def test_a_real_article_still_succeeds(self):
        """反过来钉一边:正常页面照旧补抓成功并进翻译队列。"""
        key, summary, row = self._round(PAGE)
        self.assertEqual(1, summary['done'])
        self.assertIn('The runtime schedules tools', row['body'])
        self.assertNotEqual('', row['fulltext_at'])
        self.assertEqual(1, translate.pending('rss'))


class StaleRecheckTests(FullTextCase):
    """存量复查:已经错误出队的那些要能放回队列(报告点名「不能只修新条目」)。"""

    def _recheck(self, *argv):
        """跑一趟存量复查脚本。它那几行给人看的提示吞掉,不然测试输出里全是它。"""
        import scripts.feed_fulltext_recheck as mod
        with patch.object(mod.sys, 'argv', ['feed_fulltext_recheck.py', *argv]), \
                contextlib.redirect_stdout(io.StringIO()):
            return mod.main()

    def _already_wrong(self, body):
        """摆一条「补抓报过成功、正文其实是样板文字」的存量条目 —— 生产库里
        那两条就是这个形态:`fulltext_at` 和 `translated_at` 都非空、已导出。"""
        key = self.put()
        with reg.db() as c:
            c.execute("UPDATE feed_items SET body=?,content_kind='content',"
                      "fulltext_at='2026-10-09T00:00:00+00:00',title_zh='版权声明',"
                      "body_zh='译文',translated_at='2026-10-09T01:00:00+00:00',"
                      'exported_hash=content_hash WHERE key=?', (body, key))
        return key

    def test_scan_finds_them_and_changes_nothing(self):
        key = self._already_wrong(LEGAL_TEXT)
        self.assertEqual(1, self._recheck('scan'))
        self.assertNotEqual('', self.row(key)['fulltext_at'])   # 只读,一个字节没动
        self.assertEqual(0, fulltext.pending('rss'))

    def test_run_puts_them_back_in_the_queue(self):
        key = self._already_wrong(LEGAL_TEXT)
        self.assertEqual(0, self._recheck('run', '--yes'))
        row = self.row(key)
        self.assertEqual(1, fulltext.pending('rss'))      # 后果:真的回到队列里了
        self.assertEqual('', row['fulltext_at'])
        self.assertEqual(0, row['fulltext_tries'])        # 额度重新给满
        self.assertEqual('', row['translated_at'])        # 那份译文译的是样板文字
        self.assertEqual('', row['body_zh'])
        self.assertEqual(1, row['extract_stale'])         # 旧摘要等着作废

    def test_the_bio_tier_is_found_too(self):
        self._already_wrong(BYLINE_TEXT)
        self.assertEqual(0, self._recheck('run', '--yes'))
        self.assertEqual(1, fulltext.pending('rss'))

    def test_a_correctly_fetched_item_is_left_alone(self):
        """反过来钉一边:正常补抓成品不许被放回队列、译文不许被清掉。"""
        key = self._already_wrong('The runtime replays every tool call from one log. ' * 12)
        self.assertEqual(0, self._recheck('run', '--yes'))
        self.assertEqual(0, fulltext.pending('rss'))
        self.assertNotEqual('', self.row(key)['fulltext_at'])
        self.assertEqual('译文', self.row(key)['body_zh'])

    def test_without_yes_nothing_changes(self):
        key = self._already_wrong(LEGAL_TEXT)
        self.assertEqual(1, self._recheck('run'))
        self.assertNotEqual('', self.row(key)['fulltext_at'])
        self.assertEqual(0, fulltext.pending('rss'))

    def test_the_judgement_is_the_same_one_the_fetcher_uses(self):
        """口径只有一份:两处各判一遍的话,放回队列的和以后会被挡掉的不是同一批。"""
        import scripts.feed_fulltext_recheck as mod
        self.assertIn('readable.boilerplate_reason', inspect.getsource(mod))


class FetchFailureStageTests(FullTextCase):
    """补抓失败要进导出的最终状态。报告证据:阶段 ok、failures 空,而文章没入库。

    四类分开说(报告点名「不要把积压、当前故障和正常降级合成一个状态」):
    本轮失败 / 本轮还没轮到 / 此前失败等着重试 / 试满次数正常降级。
    """

    def _materialize(self, side_effect=None, result=None):
        stages = []
        chat = Mock(return_value='###TITLE###\n是的，而且\n###BODY###\n译文')
        req = Mock(side_effect=side_effect) if side_effect else Mock(return_value=result)
        with patch.object(fulltext, 'request', req), \
                patch.object(sync, '_stage', lambda *a, **k: stages.append((a, k))), \
                patch.object(llm, 'chat', chat), \
                patch.dict(pipeline_state._state, stop_requested=False):
            FeedCollector('rss').materialize()
        final = stages[-1]
        return final[0][1], final[0][2], final[1].get('failures') or []

    def _mark(self, key, **cols):
        sets = ','.join(f'{c}=?' for c in cols)
        with reg.db() as c:
            c.execute(f'UPDATE feed_items SET {sets} WHERE key=?', (*cols.values(), key))

    def test_a_first_http_failure_turns_the_stage_red(self):
        key = self.put()
        status, note, failures = self._materialize(side_effect=FeedError('HTTP 403'))
        self.assertEqual('error', status)                     # 报告证据的反面
        self.assertIn('本轮 1 条原文抓取失败', note)
        self.assertIn('暂未入库', note)
        self.assertEqual(['HTTP 403'], [f['reason'] for f in failures])
        self.assertEqual('', self.row(key)['exported_hash'])  # 确实没入库
        self.assertEqual([], list((self.root / 'vault').rglob('*.md')))

    def test_a_timeout_reads_the_same_way(self):
        self.put()
        status, _, failures = self._materialize(side_effect=FeedError('读取超时'))
        self.assertEqual('error', status)
        self.assertIn('读取超时', failures[0]['reason'])

    def test_an_unreadable_page_turns_the_stage_red_too(self):
        """「页面抓到了、认不出正文」同样是本轮试过并失败,口径一致。"""
        self.put()
        status, note, failures = self._materialize(result=page(LEGAL_PAGE))
        self.assertEqual('error', status)
        self.assertIn('本轮 1 条原文抓取失败', note)
        self.assertIn('版权', failures[0]['reason'])

    def test_an_item_not_yet_reached_is_plain_backlog(self):
        """本轮还没轮到的那些只是积压:不判红、不说成故障。"""
        for i in range(3):
            self.put(url=f'https://e.org/p{i}', title=f'Post {i}')
        with patch.object(fulltext, 'MAX_ITEMS', 1):
            status, note, failures = self._materialize(result=page(PAGE))
        self.assertEqual('ok', status)
        self.assertIn('待补抓原文正文', note)
        self.assertNotIn('失败', note)
        self.assertEqual([], failures)

    def test_an_earlier_failure_waiting_for_its_turn_is_not_red(self):
        """此前失败、这一轮没碰它的:如实列出来,但不判红 —— 这一轮什么都没坏。"""
        old = self.put(url='https://e.org/old', title='Old')
        self._mark(old, fulltext_error='HTTP 500', fulltext_tries=1)
        self.put(url='https://e.org/new', title='New')
        # 失败过的优先,所以要把预算卡在 1 条之外才能造出「这一轮没碰它」。
        with patch.object(fulltext, 'MAX_ITEMS', 1), \
                patch.object(fulltext, 'ROUND_SECONDS', 0):
            status, note, failures = self._materialize(result=page(PAGE))
        self.assertEqual('ok', status)
        self.assertIn('此前原文抓取失败', note)
        self.assertIn('排队重试', note)
        self.assertNotIn('本轮', note)
        self.assertEqual([], failures)

    def test_the_third_failure_falls_through_as_designed(self):
        """试满 MAX_TRIES 就按壳入库 —— 那是设计好的降级出口,不判红。"""
        key = self.put()
        self._mark(key, fulltext_tries=fulltext.MAX_TRIES, fulltext_error='HTTP 403')
        status, note, failures = self._materialize(side_effect=FeedError('HTTP 403'))
        self.assertEqual('ok', status)
        self.assertNotIn('原文抓取失败', note)
        self.assertEqual([], failures)
        self.assertNotEqual('', self.row(key)['exported_hash'])   # 按壳入库了
        self.assertTrue(list((self.root / 'vault').rglob('*.md')))

    def test_a_shipped_item_failing_now_says_the_library_still_has_the_link(self):
        """已入库的旧链接:读得到,可库里躺的是那个几十字的壳 —— 照样判红,
        只是文案不同(说"暂未入库"对它就是谎报,它此刻读得到)。"""
        key = self.put()
        self._mark(key, exported_hash=self.row(key)['content_hash'],
                   path='source/rss/' + key + '.md')
        status, note, failures = self._materialize(side_effect=FeedError('HTTP 403'))
        self.assertEqual('error', status)
        self.assertIn('本轮 1 条原文抓取失败', note)
        self.assertIn('库里仍是链接内容', note)
        self.assertNotIn('暂未入库', note)
        self.assertEqual(['HTTP 403'], [f['reason'] for f in failures])

    def test_a_shipped_item_that_failed_earlier_is_not_red(self):
        """已入库 × 此前失败:同样只是排队重试。两个维度各回答一件事。"""
        old = self.put(url='https://e.org/old', title='Old')
        self._mark(old, fulltext_error='HTTP 500', fulltext_tries=1,
                   exported_hash=self.row(old)['content_hash'])
        self.put(url='https://e.org/new', title='New')
        with patch.object(fulltext, 'MAX_ITEMS', 1), \
                patch.object(fulltext, 'ROUND_SECONDS', 0):
            status, note, _ = self._materialize(result=page(PAGE))
        self.assertEqual('ok', status)
        self.assertIn('此前原文抓取失败', note)
        self.assertIn('库里仍是链接内容', note)

    def test_a_clean_round_stays_green(self):
        """反过来钉一边:都抓成了就别报红,也别留下提示。"""
        self.put()
        status, note, failures = self._materialize(result=page(PAGE))
        self.assertEqual('ok', status)
        self.assertNotIn('原文抓取失败', note)
        self.assertNotIn('待补抓', note)
        self.assertEqual([], failures)

    def test_export_on_its_own_blames_nothing_on_this_round(self):
        """`export` 被单独调用(这一轮压根没跑补抓)时没有"本轮新失败"可言。"""
        key = self.put()
        self._mark(key, fulltext_error='HTTP 500', fulltext_tries=1)
        stages = []
        with patch.object(sync, '_stage', lambda *a, **k: stages.append((a, k))):
            sync.export('rss')
        self.assertEqual('ok', stages[-1][0][1])
        self.assertIn('此前原文抓取失败', stages[-1][0][2])
        self.assertNotIn('本轮', stages[-1][0][2])


if __name__ == '__main__':
    unittest.main()
