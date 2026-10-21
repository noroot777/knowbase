# -*- coding: utf-8 -*-
"""订阅正文补抓的写库准入(kb/feeds/fulltext.py + kb/feeds/grab_page.py 的 `admit`)。

被测的那件事:**「抓到了一页东西」不等于「拿到了这一篇的正文」。** 补抓这条路
写进去就是终态 —— `_save` 一落 `fulltext_at`,条目退出重试队列
(`fulltext.queued_sql` 的第二条),后面任何一轮同步都不会再碰它,而
`registry.put_items` 是 `ON CONFLICT DO NOTHING`。所以一张验证页、一道登录墙被
当成正文写进去之后**不可恢复**:检索搜到的是「Verify you are human」,提取打标
按它算摘要,日报素材里也是它。

**这一组是 2026-10-21 审查第 1 轮点出来的洞的回归**:关卡那六道判据当初只接在
手动收录那一头(`grab._grab_web`),补抓这一头直接拿 `readable.extract_or_reason`
的结果当正文,一道都不过。实测一张 **1453 字节、除了那行编排脚本什么都没有**的
Cloudflare 关卡页跑 `fetch_pending('rss')`:`done=1`、库里正文被换成
「Verify you are human…」、`fulltext_at` 写上、`pending` 归零 —— 而同一张页面在
手动收录那一侧被明确认了出来。

**断言一律先验后果**:库里那一行的正文/成功时间/重试次数、还在不在队列里、面板
明细说得出是哪一类、以及那段文字有没有一路进 vault 和模型请求 —— 只验「判据返回
了什么」的话,一个认出来了却照样覆盖正文的实现也能全绿。

**反过来钉一边**(`NotAGateTests`):准入那六道各有一整部误拒事故史(讲关卡的
正经文档、评论区挂验证码小组件的文章、`<template>` 里的密码框、页眉常驻登录框)。
在补抓这条**批量**链路上误拒一次就是几百条订阅内容集体按壳入库,所以该放行的
必须照旧补抓成功。

**存量那一半在 `RecheckTests`**:判据只管未来,已经写进去的那些只有
`scripts/feed_fulltext_recheck.py` 一条出路(面板那颗「重试失败项」是订阅级的,
不碰 `fulltext_at`)。那个脚本的射程里**还躺着几百条正常的壳**,所以它那两道判据
的射程不一样 —— 正反两侧都在这里钉着。

**按形状单独一个文件**:`tests/test_feed_fulltext.py` 已经 501 行、
`tests/test_fulltext_failures.py` 576 行,这一组挤进去会越过 700 行上限。脚手架
(`FullTextCase` / `SHELL` / `PAGE`、复查那一组的 `RecheckCase`)和页面夹具
(`page_html`)一律复用现成的 —— 各写一份的话两边的前提会各漂各的。
"""
from unittest.mock import Mock, patch

from kb import llm
from kb.collectors.feeds import FeedCollector
from kb.feeds import fulltext, readable, registry as reg, translate
from kb.feeds.fetching import Response
from tests.grab_fixtures import page_html
from tests.test_feed_fulltext import PAGE, SHELL, FullTextCase
from tests.test_fulltext_failures import RecheckCase

#: 某家关卡自己的**编排**组件(`page_gate.ASSETS` 里那一条)。通用的验证码小组件
#: 不算身份 —— 正常文章页的评论框就挂着它们(见 `RECAPTCHA`)。
ORCHESTRATE = ('<script src="https://t.test/cdn-cgi/challenge-platform/h/b'
               '/orchestrate/chl_page/v1"></script>')
#: ②**只靠组件认出来**的那一张:标题、正文都像一篇正常文章,唯一的身份是那行
#: 编排脚本。审查复现的那张 1453 字节的关卡页就是这个形状(不加任何脚本撑大)。
COMPONENT_ONLY = page_html(extra=ORCHESTRATE)
#: ③④**只会说那几句话**的那一张:地址干净、不挂任何组件、提取器交得出一大段。
#: 审查第二轮在手动收录那一侧实测的 1782 字验证页就是它(命中句占 67%)。
SAY_VERIFY = page_html(title='请完成验证后继续', body=[
    '请完成验证。Checking your browser before accessing the site. '
    '您需要启用 JavaScript 和 Cookie 后重试。在通过验证前文章内容不可见。' * 18])
#: ⑤一道**生效且拦在正文上**的登录墙:密码框长在 `<article>` 里。
LOGIN_FORM = '<form action="/login"><input type="password"></form>'
WALL_IN_ARTICLE = page_html().replace('</article>', LOGIN_FORM + '</article>')
#: ⑥**结构上认不出、内容自己露馅**的那一张:同一段铺了 7 遍。提取器交出一大段,
#: 份额判据不成立(它话很多)、措辞不在关卡表里、一个 `<form>` 都没有 ——
#: 按行去重之后连 400 的门槛都过不了(`readable.substance`)。
SAY_SIGN_IN = ('Sign in to continue reading. Enter the email address associated with your '
               'subscription. Members have access to the full archive and the weekly letter. ')
TEXT_WALL = page_html(title='Sign in to read Example Journal', body=[SAY_SIGN_IN] * 7)
#: 评论区那个通用验证码小组件。正常文章页上常态挂着,进判据表等于按「这站评论
#: 要过验证码」拒稿。
RECAPTCHA = '<script src="https://www.google.com/recaptcha/api.js" async defer></script>'


def resp(html: str, url: str = 'https://e.org/post') -> Response:
    """补抓那一下的响应。**`url` 是跟完重定向之后的最终地址** —— 路由那一道
    (`_GATE_ROUTES`)比的就是它,所以这一组要能自己指定。"""
    return Response(200, url, html.encode(), {'content-type': 'text/html; charset=utf-8'})


class AdmitCase(FullTextCase):
    """脚手架:摆一条壳条目,抓一轮,把库里那一行交回来。自己不带断言。"""

    def _round(self, html, url='https://e.org/post'):
        key = self.put()
        with patch.object(fulltext, 'request', Mock(return_value=resp(html, url))) as req:
            summary = fulltext.fetch_pending('rss')
        return {'key': key, 'summary': summary, 'row': self.row(key), 'req': req}

    def refused(self, html, url='https://e.org/post', says=''):
        """这一页被准入拦住了,而且**库里那一条一个字节都没动**。

        五件后果一起钉(缺一件就留下一个「认出来了却照样写坏」的实现空间):
        壳还在、`content_kind` 还是 `summary`、`fulltext_at` 没写、重试额度扣了
        一次、这一条还排在补抓队列里。再加一件:它没进翻译队列 —— 译一遍关卡页
        是真花钱的。
        """
        got = self._round(html, url)
        row, summary = got['row'], got['summary']
        self.assertEqual((0, 1), (summary['done'], summary['failed']))
        self.assertEqual(SHELL, row['body'])
        self.assertEqual('summary', row['content_kind'])
        self.assertEqual('', row['fulltext_at'])
        self.assertEqual(1, row['fulltext_tries'])
        self.assertEqual(1, fulltext.pending('rss'))
        self.assertEqual(0, translate.pending('rss'))
        if says:
            self.assertIn(says, row['fulltext_error'])
        return got

    def fetched(self, html, url='https://e.org/post'):
        """反过来这一页照旧补抓成功,**而且正文真的换了**。"""
        got = self._round(html, url)
        row, summary = got['row'], got['summary']
        self.assertEqual((1, 0, 0), (summary['done'], summary['failed'], summary['empty']))
        self.assertEqual('content', row['content_kind'])
        self.assertNotEqual('', row['fulltext_at'])
        self.assertNotEqual(SHELL, row['body'])
        self.assertEqual(0, fulltext.pending('rss'))      # 不该再排队重试
        return got


class AdmitTests(AdmitCase):
    """该拦的拦住:六道准入在补抓这条链路上也生效,而且拒绝时一个字节不改。"""

    def test_a_live_challenge_component_is_never_written_as_the_body(self):
        """**审查复现的那一张。** 唯一的身份是那行编排脚本 —— 标题和正文都像
        正常文章,所以这一道漏了就没有别的判据接得住它。
        """
        got = self.refused(COMPONENT_ONLY, says='验证')
        # 先钉住这张页面确实「提取得出一大段」:否则它会落到「没识别出正文」那一
        # 档,这条用例就测不到洞(基线正是靠这一段文字报的 `done=1`)。
        self.assertGreater(len(readable.extract(COMPONENT_ONLY)), readable.MIN_CHARS)
        self.assertEqual(1, len(got['summary']['errors']))

    def test_a_page_that_only_says_verify_is_refused(self):
        self.refused(SAY_VERIFY, says='验证')

    def test_a_login_wall_in_the_article_is_refused(self):
        self.refused(WALL_IN_ARTICLE, says='登录')

    def test_a_text_only_wall_is_refused_by_the_content_itself(self):
        """结构判据对它无从下手,分得开它的只有内容自己。"""
        # 两头都先钉住:提取器交得出一大段,而按行去重之后过不了那道 400。
        self.assertGreater(len(readable.extract(TEXT_WALL)), readable.MIN_CHARS)
        self.assertLess(readable.substance(readable.extract(TEXT_WALL)), readable.MIN_CHARS)
        self.refused(TEXT_WALL, says='正文')

    def test_a_challenge_route_is_refused_even_if_the_page_looks_fine(self):
        """路由那一道比的是**最终地址**,和提取器交得出什么无关。"""
        self.refused(page_html(), 'https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?x=1',
                     says='验证')

    def test_the_panel_says_which_class_it_was(self):
        """面板明细要说得出是哪一类 —— 只写一句「原文抓取失败」的话,用户点进去
        看到的是一条没有信息的记录,而这几类的下一步完全不同。

        `failure()` 那一份带着标题、原因、链接和「不再抓取」的句柄
        (`drop`),后者是面板上那颗按钮的入参。
        """
        got = self.refused(SAY_VERIFY)
        detail = got['summary']['errors'][0]
        self.assertEqual('Yes, and', detail['title'])
        self.assertIn('验证', detail['reason'])
        self.assertEqual('https://e.org/post', detail['url'])
        self.assertEqual('rss', detail['drop']['channel'])
        self.assertEqual([got['key']], got['summary']['failed_keys'])

    def test_the_queue_still_has_an_exit_after_max_tries(self):
        """**拒绝要留在原重试语义里**,两侧都钉:计次(不计次它就永远排在队首、
        每一轮都重抓同一张关卡页),而试满 `MAX_TRIES` 就离开队列按壳入库 ——
        一道验证页后面的链接不该让这条订阅的内容永远进不了知识库。
        """
        key = self.put()
        with patch.object(fulltext, 'request',
                          Mock(return_value=resp(COMPONENT_ONLY))) as req:
            for _ in range(fulltext.MAX_TRIES + 1):
                fulltext.fetch_pending('rss')
        row = self.row(key)
        self.assertEqual(fulltext.MAX_TRIES, row['fulltext_tries'])
        self.assertEqual(fulltext.MAX_TRIES, req.call_count)   # 出队之后不再敲对方
        self.assertEqual(0, fulltext.pending('rss'))
        self.assertEqual(SHELL, row['body'])
        self.assertEqual('', row['fulltext_at'])

    def test_the_gate_text_never_reaches_the_vault_or_the_model(self):
        """**最终后果**:整轮 materialize 走完,那段验证提示既不在 md 里、也没被
        拿去买一次翻译 —— 库里躺的是 feed 原本那份壳(正确的降级)。
        """
        key = self.put()
        chat = Mock(return_value='###TITLE###\n是的，而且\n###BODY###\n链接内容的译文。')
        with patch.object(fulltext, 'request', Mock(return_value=resp(COMPONENT_ONLY))), \
                patch.object(llm, 'chat', chat):
            for _ in range(fulltext.MAX_TRIES):
                FeedCollector('rss').materialize()
        md = (self.root / 'vault' / 'source' / 'rss' / (key + '.md')).read_text()
        asked = ' '.join(str(c) for c in chat.call_args_list)
        self.assertNotIn('Verify you are human', md)
        self.assertNotIn('Verify you are human', asked)
        # 库里躺的是那份壳:导出按 `content_kind='summary'` 写「以下为订阅源提供
        # 的摘要」那句注记,而买翻译时交给模型的也是壳(md 里是它的译文)。
        self.assertIn('订阅源提供的摘要', md)
        self.assertIn('HN Points', asked)


class NotAGateTests(AdmitCase):
    """反过来钉一边:该放行的照旧补抓成功。

    这一侧的代价和另一侧差着一个量级 —— 误拒一次是几百条订阅内容集体按壳入库,
    而且这条链路上**没有人在旁边看着**(手动收录那边用户能立刻点「重新抓一次」)。
    """

    def test_an_ordinary_article_is_still_fetched(self):
        got = self.fetched(PAGE)
        self.assertIn('The runtime schedules tools', got['row']['body'])

    def test_an_article_about_challenge_pages_is_still_fetched(self):
        """**页面上的文字不算页面身份,一个字都不算。** 一篇讲这类关卡的文档会把
        那段 `<script src=…>` 原样转义印在 `<pre>` 里,地址里还带着
        `cloudflare-challenges` 这一段(真实样本:developers.cloudflare.com 那篇
        1277 字的文档)—— 两样都不许算。
        """
        printed = page_html().replace(
            '</article>',
            f'<pre>{ORCHESTRATE.replace("<", "&lt;").replace(">", "&gt;")}</pre></article>')
        self.fetched(printed, 'https://developers.example.org/waf/reference/cloudflare-challenges/')

    def test_a_comment_captcha_widget_does_not_block_the_fetch(self):
        """通用验证码小组件是评论框的常态,不是这一页挂着关卡。"""
        self.fetched(page_html().replace('</article>', '</article>' + RECAPTCHA))

    def test_an_inert_login_demo_does_not_block_the_fetch(self):
        """**生效性和位置分开各判一次。** `<template>` 里的密码框是给 JS 以后
        克隆用的样板(一篇「手把手教你写登录表单」的教程),页眉里常驻的登录框
        不算拦在正文上 —— 两种都不是一道墙。
        """
        for name, html in (
                ('template', page_html().replace('</article>',
                                                 f'<template>{LOGIN_FORM}</template></article>')),
                ('页眉', page_html().replace('<nav>', f'<header class="site-header">{LOGIN_FORM}'
                                                      '</header><nav>'))):
            with self.subTest(form=name):
                self.setUp()                 # 每一档要一条干净的壳条目
                self.fetched(html)

    def test_a_quoted_gate_sentence_is_not_a_gate(self):
        """④ 按**份额**算、不设字数上限:一篇正常文章引一句那种提示照旧放行
        (手动收录那一侧实测:反复引那几句的正经文章也只到 50%)。
        """
        quoted = page_html(body=[
            '这是正文第一段，长度要过得了提取器那道门槛。' * 12,
            '第二段同样要够长，否则 readable 会判成"没找到正文"。' * 12,
            'The page said: verify you are human.'])
        self.fetched(quoted)


class RecheckTests(RecheckCase):
    """存量复查:已经被写死的那些要能换回壳,**而正常的壳一条都不许动**。

    射程两侧都钉。`_suspect` 的射程里躺着两类东西:补抓写进去的正文,和 feed
    原本那份壳(生产实测 2026-10-21:开了补抓的订阅名下 1021 条,352 条是壳)。
    「内容质量」那一道**壳天然过不了** —— 不分射程地扫一遍就是把那 352 条正常的
    壳判坏、换成按标题与链接重建的那一份、译文一并作废、再整批重导出一轮。
    """

    def test_a_gate_body_already_in_the_library_is_swapped_back_to_a_shell(self):
        """后果:那段验证提示没了,这一条回到补抓队列、额度重新给满。"""
        gate_body = readable.extract(SAY_VERIFY)
        key = self._already_wrong(gate_body)
        self.assertEqual(1, self._recheck('scan'))        # 只读那一趟先认得出
        self.assertEqual(gate_body, self.row(key)['body'])   # scan 一个字节不动
        self.assertEqual(0, self._recheck('run', '--yes'))
        row = self.row(key)
        self.assertNotIn('请完成验证', row['body'])
        self.assertEqual('summary', row['content_kind'])
        self.assertEqual(1, fulltext.pending('rss'))
        self.assertEqual((0, '', ''), (row['fulltext_tries'], row['fulltext_at'],
                                       row['translated_at']))

    def test_a_text_only_wall_already_in_the_library_is_swapped_back(self):
        """⑥ 那一档同样要收拾得掉:它在手动收录那一侧也是靠内容自己认出来的。"""
        key = self._already_wrong(readable.extract(TEXT_WALL))
        self.assertEqual(0, self._recheck('run', '--yes'))
        self.assertNotIn('Sign in to continue reading', self.row(key)['body'])
        self.assertEqual(1, fulltext.pending('rss'))

    def test_a_shell_waiting_for_its_body_is_never_touched(self):
        """**这一条是那 352 条的代表。** feed 原本那份壳(标题 + 域名 + HN 得分)
        说的东西本来就少,拿「内容质量」问它必然判坏 —— 而它正是补抓还没轮到的
        正常状态。选中它的后果不是「多抓一次」:壳会被换成按标题与链接重建的
        那一份、译文作废、整批重导出。
        """
        key = self.put()
        self.assertLess(readable.substance(SHELL), readable.MIN_CHARS)   # 它确实过不了
        self.assertEqual(0, self._recheck('scan'))        # 一条都不该选中
        self.assertEqual(0, self._recheck('run', '--yes'))
        row = self.row(key)
        self.assertEqual(SHELL, row['body'])
        self.assertEqual('summary', row['content_kind'])
        self.assertEqual(1, fulltext.pending('rss'))      # 照旧排在队列里等补抓

    def test_a_rebuilt_shell_is_never_judged_again(self):
        """**不许变成一个没有终点的循环。** 复查自己按标题与链接重建出来的那份壳
        同样说不出多少不重复的东西 —— 再判它一次坏,每跑一次就「收拾」同一条,
        译文作废和重导出跟着一轮一轮重做。判据里那道 `MIN_CHARS` 正是为它留的
        (成功时间人为填回去也一样,口径同 `tests/test_fulltext_failures.py` 那条
        「已经干净 ≠ 只是选不中」)。
        """
        key = self._already_wrong(readable.extract(SAY_VERIFY))
        self.assertEqual(0, self._recheck('run', '--yes'))
        with reg.db() as c:
            c.execute("UPDATE feed_items SET fulltext_at='2026-10-21T02:00:00+00:00'"
                      ' WHERE key=?', (key,))
        self.assertEqual(0, self._recheck('scan'))
        self.assertEqual('Yes, and\nhttps://e.org/post', self.row(key)['body'])

    def test_a_repetitive_feed_summary_is_not_this_scripts_business(self):
        """**「报过补抓成功」那道守卫自己的红。** feed 给的正文可以又长又重复
        (同一段铺满的摘要),而它从来没过补抓那道准入 —— 补抓这条路一个字都没
        写过它,换掉它就是拿掉用户订阅到的内容(口径同「没开补抓的订阅一个都
        不碰」)。
        """
        key = self.put(body=readable.extract(TEXT_WALL))
        self.assertEqual(0, self._recheck('scan'))
        self.assertEqual(0, self._recheck('run', '--yes'))
        self.assertIn('Sign in to continue reading', self.row(key)['body'])

    def test_a_real_fetched_article_is_never_touched(self):
        """反过来另一侧:补抓成功的真正文一个字节都不许动。"""
        key = self.put()
        with reg.db() as c:
            c.execute("UPDATE feed_items SET body=?,content_kind='content',"
                      "fulltext_at='2026-10-20T00:00:00+00:00' WHERE key=?",
                      (readable.extract(PAGE), key))
        self.assertEqual(0, self._recheck('scan'))
        self.assertIn('The runtime schedules tools', self.row(key)['body'])
