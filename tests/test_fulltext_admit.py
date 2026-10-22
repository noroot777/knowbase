# -*- coding: utf-8 -*-
"""订阅正文补抓的写库准入(kb/feeds/fulltext.py + kb/feeds/grab_page.py 的 `admit`)。

被测的那件事:**「抓到了一页东西」不等于「拿到了这一篇的正文」。** 补抓这条路
写进去就是终态 —— `_save` 一落 `fulltext_at`,条目退出重试队列
(`fulltext.queued_sql` 的第二条),后面任何一轮同步都不会再碰它,而
`registry.put_items` 是 `ON CONFLICT DO NOTHING`。所以一张验证页、一道登录墙被
当成正文写进去之后**不可恢复**:检索搜到的是「Verify you are human」,提取打标
按它算摘要,日报素材里也是它。

**这一组是 2026-10-21 审查第 1 轮点出来的洞的回归**:关卡那几道判据当初只接在
手动收录那一头(`grab._grab_web`),补抓这一头直接拿 `readable.extract_or_reason`
的结果当正文,一道都不过。实测一张 **1453 字节、除了那行编排脚本什么都没有**的
Cloudflare 关卡页跑 `fetch_pending('rss')`:`done=1`、库里正文被换成
「Verify you are human…」、`fulltext_at` 写上、`pending` 归零 —— 而同一张页面在
手动收录那一侧被明确认了出来。

**断言一律先验后果**:库里那一行的正文/成功时间/重试次数、还在不在队列里、面板
明细说得出是哪一类、以及那段文字有没有一路进 vault 和模型请求 —— 只验「判据返回
了什么」的话,一个认出来了却照样覆盖正文的实现也能全绿。

**反过来钉一边**(`NotAGateTests`):准入那八道各有一整部误拒事故史(讲关卡的
正经文档、评论区挂验证码小组件的文章、`<template>` 里的密码框、页眉常驻登录框)。
在补抓这条**批量**链路上误拒一次就是几百条订阅内容集体按壳入库,所以该放行的
必须照旧补抓成功。

**存量那一半在 `RecheckTests`**:判据只管未来,已经写进去的那些只有
`scripts/feed_fulltext_recheck.py` 一条出路(面板那颗「重试失败项」是订阅级的,
不碰 `fulltext_at`)。那个脚本的射程里**还躺着几百条正常的壳、以及走
`markdown_source` 写进来的 Markdown 原文**,所以它**只认 `readable.boilerplate_reason`
那一道 —— 写库准入那八道一道都不借**:⑧ 判坏过正常的 Markdown 原文(它允许重复)、
④ 判坏过正常的英文设置教程。正反两侧和那条能力边界都在这里钉着。

**按形状单独一个文件**:`tests/test_feed_fulltext.py` 已经 501 行、
`tests/test_fulltext_failures.py` 576 行,这一组挤进去会越过 700 行上限。脚手架
(`FullTextCase` / `SHELL` / `PAGE`、复查那一组的 `RecheckCase`)和页面夹具
(`page_html`)一律复用现成的 —— 各写一份的话两边的前提会各漂各的。
"""
from unittest.mock import Mock, patch

from kb import llm
from kb.collectors.feeds import FeedCollector
from kb.feeds import fulltext, grab, grab_page, readable, registry as reg, translate
from kb.feeds.fetching import Response
from tests.grab_fixtures import PAGE as PAGE_URL, GrabCase, html_resp, page_html
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
#: 一篇**正常的英文设置教程**,走 `markdown_source` 那条路入库。正文逐字取自审查第 2
#: 轮的夹具(683 字、按行去重 683 字:⑧ 对它压根不成立,所以它单钉 ⑥ 那一侧)。
#: 它提到一次「enable JavaScript」—— 上一版的句界不切 `. `,于是这一整段只算**一句**、
#: 份额 1.0,连标题带正文两行都提到它的 Markdown 那份同样 1.0。
_TUTORIAL = (
    'To enable JavaScript for the local reader, open the site settings for localhost and '
    'allow scripts for this origin. Reload the preview after changing the permission. '
    'The reader builds its article list in the browser and keeps the original Markdown '
    'files on disk. If the list is still empty, open the developer console and check '
    'whether the assets returned an HTTP error. A blocked content security policy can '
    'also prevent the preview bundle from loading. Inspect the response headers before '
    'changing the policy and avoid permitting scripts from unrelated origins. '
    'This setting affects only the local preview and does not require an account or '
    'change the contents of any saved article.')
TUTORIAL_MD = '# Enable JavaScript for the local reader\n\n' + _TUTORIAL
#: 同一篇教程的 **HTML** 身子 —— 走共用准入(`admit`)那条路。**上一版在这里误拒**
#: (句界不切 `. `、整段算一句、份额 1.0),补抓那边连抓三轮耗尽额度、永久退回摘要;
#: 现在份额 0.143,两端都收。三种排版同一个结论钉在
#: `tests/test_admit_three_ways.py` 的 `LayoutDoesNotChangeTheVerdictTests`。
TUTORIAL_PAGE = page_html(title='Local reader setup', body=[_TUTORIAL])
#: 同一批文字**按英文句号分成七段**:只有第一段提到那个短语 → 份额 1/7,照旧放行。
TUTORIAL_SPLIT = page_html(title='Local reader setup',
                           body=_TUTORIAL.replace('. ', '.\n\n').split('\n\n'))
#: 另一篇正常教程:**三个自然段各提一次**那个短语(逐字取自审查第 2 轮的夹具,
#: 954 字 / 306+310+334)。份额同样 1.0 —— 分段分得再开也救不回来,只要**每一段**都
#: 提到一次。它和上面那张连成一段的是同一类误拒,不是另一个洞。
_PARAGRAPHS = [
    'To enable JavaScript for the development preview, open the site settings for '
    'localhost and allow scripts only for that origin. Keep the original security '
    'policy in place for unrelated websites. Reload the page once after changing the '
    'permission, then inspect the browser console for blocked asset requests.',
    'When you enable JavaScript for a staging environment, confirm that the built '
    'bundle matches the deployment version. The asset paths should point to the same '
    'host as the preview document. A cached worker can continue serving older files, '
    "so clear only that application's cache before comparing the new response.",
    'If you cannot enable JavaScript on a managed device, ask the administrator '
    'whether the local preview origin can be approved. Record the exact console error '
    'and attach the response headers from the blocked script. Do not disable browser '
    'protection globally, because the rest of the browsing profile does not need '
    'access to the preview.']
MULTI_TUTORIAL = page_html(title='Local preview setup guide', body=_PARAGRAPHS)
#: 反过来钉一边的那一张:同样一整段英文、份额同样 1.0,但它通篇在复读同一句。
#: `body_confirmed` 对它**成立** —— `readable.substance` 按**行**去重,而这里只有
#: 一行,1300 字实打实。所以这一侧只有按细句去重才答得出来。
ENGLISH_GATE = page_html(title='站点公告', body=['Please enable JavaScript. ' * 50])
#: 一张**长、句句不同、纯文字**的拒绝说明页(491 字:访问被拒、请启用脚本和 Cookie、
#: 被安全策略屏蔽、联系管理员)。它**没有**关卡路由、编排组件或关卡标题 —— 身份只写
#: 在那段文字里,而那正是 ④ 答得出的那一问。给 ④ 开「够长、不复读就放行」的豁免之后
#: 它当场被收成正文、送去翻译、按文章导出(审查第 2 轮实测),所以那道豁免撤了。
DENIED_TEXT = (
    'Access to this page has been denied because we believe you are using automation tools. '
    'Please enable JavaScript and cookies in your browser before trying again. '
    'Your request was blocked by the security policy configured for this website. '
    'The owner of this site has set up a rule that stops requests from unknown clients. '
    'If you think this is a mistake, contact the site administrator with the reference below. '
    'Repeated attempts from the same address may extend the block for a longer period. ')
DENIED_PAGE = page_html(title='站点公告', body=[DENIED_TEXT])
#: 同一张拒绝说明页,**六句话各自成段**(每个 `<p>` 一行)。一个字都没增删 —— 提取
#: 之后去掉空白与原样那张逐字相同(`tests/test_admit_three_ways.py` 的
#: `LayoutDoesNotChangeTheVerdictTests` 先钉这个前提)。
#: **这一张就是审查第 1 轮漏放的那一例**:上一版的句界不切 `. `,份额从 1.0 掉到
#: 1/6、当时那六道一道都不命中,496 字「访问被拒绝」当正文入库。现在两种排版的份额
#: 都是 0.167、样板占比都是 0.833,由 ⑦ 一起拒掉。
DENIED_SPLIT = page_html(title='站点公告',
                         body=[f'{s}.' for s in DENIED_TEXT.split('. ') if s.strip()])
#: **⑦ 那条线的另一侧:一篇真·讲拦截页的英文技术文章。** 正文里原样引着两整句样板
#: 措辞(「your request was blocked …」「If you think this is a mistake, contact the
#: site administrator」),其余是正经内容 —— 样板占比 **0.322**,而那张真的拒绝说明页
#: 是 **0.833**。它和拒绝说明页在**文字上只差一个占比**,所以这一篇必须收进来:
#: 「这一页在拦正文」那一问只有响应自己答得出(① `page_gate.response_reason`)。
BLOCK_PAGE_ARTICLE = page_html(title='How WAF block pages work', body=[
    'A managed rule can decide that your request was blocked before it ever reaches the '
    'origin server. The interstitial page that follows is generated by the edge, not by '
    'the application, which is why it carries no application cookies. '
    'Operators often customise the copy to read "If you think this is a mistake, contact '
    'the site administrator" so that a human has somewhere to go. '
    'The useful part for debugging is the correlation identifier printed at the bottom of '
    'that page, because it lets support match your attempt to a single edge log line. '
    'Keep the rule narrow: a broad user-agent match will also stop feed readers and '
    'uptime probes that you actually want to let through. '
    'Measure the false positive rate before you turn the rule on for the whole zone.'])

#: **开头把同一段铺满、真内容排在 `MAX_CHARS` 之后**的那一页。每一段是一个独立的
#: `<p>`(= 独立的行),所以按行去重之后那个前缀只剩一段的长度 —— 真内容被截掉之后
#: ⑥ 当场成立。审查第 1 轮实测的那一张提取出 69133 字、落库 60000 字而不重复的只有
#: 202 字(基线那一版先截后判,这一页是直接拒掉的)。
_FILLER = '这一段在开头反复出现，它自己说不出任何新东西。' * 5
_REAL = ['真正的内容排在后面：运行时按同一份确定性日志重放每一次调用。' * 4,
         '第二段讲的是崩溃之后怎么从那份日志继续，而不是从头再来一遍。' * 4,
         '第三段换个角度说明同一份日志为什么必须是只增不改的。' * 4,
         '第四段把前面几段的结论收一下，这几段彼此都不一样。' * 4]
LONG_FILLED = page_html(body=[_FILLER] * 560 + _REAL)

#: 一份**正常**的 quick-start README,走 `markdown_source` 那条路入库(Markdown 原文,
#: 不过 `admit`)。几个平台各一段同样的安装命令 —— 550 字、按行去重只剩 215 字,
#: 所以 ⑥ 对它成立而它完全正常(审查第 1 轮实测的那份是 646 / 254)。
_INSTALL = '```sh\ncurl -fsSL https://tool.example.org/install.sh | sh\ntool init --yes\n```\n\n'
README_MD = ('# quick start\n\nInstall the toolchain, then run `tool init` once per repository.\n\n'
             + ''.join(f'## {name}\n\n{_INSTALL}' for name in
                       ('macOS', 'Debian / Ubuntu', 'Fedora', 'Arch Linux', 'Windows (WSL)')))

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
    """该拦的拦住:八道准入在补抓这条链路上也生效,而且拒绝时一个字节不改。"""

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

    def test_a_plain_text_refusal_notice_is_never_stored_as_the_body(self):
        """**纯文字的拒绝说明页照旧拒,一个字节都不许写进去。**

        两张形态:一整段复读同一句的(`ENGLISH_GATE`)、以及 491 字句句不同的那种
        (`DENIED_PAGE`:访问被拒、请启用脚本和 Cookie、被安全策略屏蔽、联系管理员)。
        **第二张没有关卡路由、没有编排组件、标题也人畜无害** —— 它的身份只写在那段
        文字里,而那正是 ④ 答得出的那一问。

        事故形态:给那一道开一条「够长、不复读就放行」的豁免之后,这一张当场被收成正文、
        写上 `fulltext_at`、退出补抓队列、整段拒绝说明送进翻译并按文章导出(审查实测:
        `request_calls=1`、第二轮不再补抓、`exported_as_content=True`)。**文字量和去重
        长度只证明得了「这里有多少字」,证明不了「这一页没在拦正文」。**

        **两张各由哪一道接住要分别钉住**,否则一道判据失效、另一道顺手兜住的时候
        这条用例还是绿的:复读那张靠 ⑥(份额 1.0),句句不同那张靠 ⑦(样板占比
        0.833 —— ⑥ 对它只有 0.167,比那篇必须放行的文章还低)。
        """
        for name, html, says, share, refusal in (
                ('一整段复读', ENGLISH_GATE, '验证', True, False),
                ('句句不同的拒绝说明', DENIED_PAGE, '访问被拒绝', False, True)):
            with self.subTest(page=name):
                self.setUp()                 # 每一档要一条干净的壳条目
                body = readable.extract(html)
                self.assertEqual(share,
                                 grab_page._gate_share(body) >= grab_page._GATE_SHARE)
                self.assertEqual(refusal,
                                 grab_page._refusal_share(body) >= grab_page._REFUSAL_SHARE)
                self.refused(html, says=says)

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
    """存量复查:**写库准入那八道一道都不借** —— 这个脚本只认 `boilerplate_reason`。

    **误拒的代价按路分三档**:手动收录那边用户当场看得见、能点「重新抓一次」;补抓那边
    没人在看、三次额度用完就永久退回摘要;**这条路上是把已经在库里的正文换成壳、译文
    作废、重导出一轮,不可逆**。同一个启发式担不起这三种后果。
    两次借过来都当场借出了事故(两轮审查各一次,都在下面钉着):⑥ 判坏了正常的
    Markdown 原文(它本来就允许重复),④ 判坏了正常的英文设置教程
    (`_gate_share` 刻意不按 `. ` 分句,一整段英文只算一句)。

    所以这一组钉的是**两侧**:射程之外的那些一个字节都不许动(正常 README、正常教程、
    feed 自己给的摘要、还没补抓的壳、补抓成功的真正文),以及那条**能力边界**本身 ——
    关卡页/登录墙被写成正文这一类,库里只剩那份纯文字,这个脚本收拾不了。

    样板区那一档的正反两侧(含备份恢复那四条)在 `tests/test_fulltext_failures.py` 的
    `StaleRecheckTests` 里,基线就钉着,这里不重复一遍。
    """

    def _survives(self, body, label):
        """一条「报过补抓成功、正文是这一份」的条目,scan 不选它、run 不动它。"""
        with self.subTest(form=label):
            key = self._already_wrong(body)
            self.assertEqual(0, self._recheck('scan'))
            self.assertEqual(0, self._recheck('run', '--yes'))
            self.assertEqual(body, self.row(key)['body'])
            return key

    def test_a_gate_body_already_in_the_library_is_out_of_reach(self):
        """**这是明确的能力边界,不是漏掉的一道。** 认出一张关卡页要的是最终地址、
        DOM 里那几个组件、响应头 —— 落库之后这些全没了,库里只剩那份纯文字。拿纯文本
        启发式去补这一刀,两轮审查各实测到一次误删正常内容(见类说明)。

        出路是**人点「重新抓一次」**(`grab.replace`),不是让脚本去猜。生产只读实测说
        存量的**文字里**找不出一条可识别的关卡提示(份额最高 0.0)——**但那推不出「存量里
        没有关卡页」**:身份证据多半在 DOM 上,而库里只剩文字(`COMPONENT_ONLY` 那张只靠
        编排脚本认出来的,正文和普通文章逐字相同、份额同样是 0)。已经丢掉的身份追认不
        回来;以后那些由 `admit` 先拦住。
        """
        for body, label in ((readable.extract(SAY_VERIFY), '关卡提示页'),
                            (readable.extract(TEXT_WALL), '纯文字登录墙')):
            self.setUp()
            key = self._survives(body, label)
            self.assertNotEqual('', self.row(key)['fulltext_at'])   # 它确实报过成功

    def test_a_normal_readme_fetched_as_markdown_is_never_touched(self):
        """**借 ⑥ 的那次事故(审查第 1 轮)。** `fulltext_at` 非空只说明「某条补抓路径
        成功过」,不说明「过了 HTML 准入」:`fetch_one` 有两条出口交出 **Markdown 原文**
        (`.md`/GitHub 地址走 `markdown_source.fetch`,任意地址声明 `text/markdown` 走
        `from_response`)—— 几个平台各一段同样的安装命令,按行去重之后过不了那道 400。

        后果实测:这份 README 被判坏、正文换成「标题 + 链接」、`fulltext_at` 清空、
        译文作废;而重抓把同一份内容再写回来,下一轮又选中 —— 没有终点。所以这里
        **走真的补抓**把它写进去,连 `fetch_one` 的出口一起钉。
        """
        self.assertGreater(len(README_MD), readable.MIN_CHARS)
        self.assertLess(readable.substance(README_MD), readable.MIN_CHARS)   # 它确实过不了 ⑥
        self._fetched_then_rechecked(README_MD, 'text/markdown; charset=utf-8')

    def test_a_normal_english_tutorial_is_never_touched(self):
        """**借 ⑥ 的那次事故(审查第 2 轮)。** 那一版的句界不切 `. `,于是**一整段
        英文只算一句** —— 正文里提一次「enable JavaScript」份额就是 **100%**;这份教程
        连标题带正文两行都提到它,同样 100%,于是被判坏、正文换成「标题 + 链接」。

        **句界修好之后这一篇的份额降到 0.25,借 ⑥ 过来已经判不坏它了 —— 这一条照旧
        钉在这儿,因为它要钉的不是那个数字。** 根因是:库里那份文字答得出的只有
        「它提到过那几个词」,答不出「这一页在拦正文」。那一问现在明确归 ①
        (`page_gate.response_reason`:响应头和状态码),而**存量那一列里一个响应头
        都没存** —— 所以这条边界不是「判据还不够准」,是「证据压根不在」。
        """
        # 前提:借 ⑥ 过来现在已经判不坏它了(句界修好之后 0.25),而 ⑧ 对它本来就
        # 不成立 —— 这一篇在**两道**文字判据下都是干净的,那正是边界该站的地方。
        self.assertLess(grab_page._gate_share(TUTORIAL_MD), grab_page._GATE_SHARE)
        self.assertGreater(readable.substance(TUTORIAL_MD), readable.MIN_CHARS)
        self._fetched_then_rechecked(TUTORIAL_MD, 'text/plain; charset=utf-8',
                                     url='https://e.org/setup.md')

    def _fetched_then_rechecked(self, text, mime, url='https://e.org/doc'):
        """走真的补抓写进去(两条 Markdown 出口各一条路),再跑一趟复查。

        **译文也摆上**:这条路会连 `title_zh`/`body_zh`/`translated_at` 一起清掉,
        而「钱已经付过的那份译文没了」是这个误删最贵的一半。
        """
        key = self.put(url=url)
        with patch.object(fulltext, 'request', Mock(return_value=Response(
                200, url, text.encode(), {'content-type': mime}))):
            self.assertEqual(1, fulltext.fetch_pending('rss')['done'])
        with reg.db() as c:
            c.execute("UPDATE feed_items SET title_zh='标题译文',body_zh='正文译文',"
                      "translated_at='2026-10-21T00:00:00+00:00' WHERE key=?", (key,))
        self.assertNotEqual('', self.row(key)['fulltext_at'])   # 它确实报了成功
        self.assertEqual(0, self._recheck('scan'))
        self.assertEqual(0, self._recheck('run', '--yes'))
        row = self.row(key)
        self.assertEqual(text, row['body'])
        self.assertEqual('content', row['content_kind'])
        self.assertEqual('正文译文', row['body_zh'])            # 译文没被作废

    def test_a_shell_waiting_for_its_body_is_never_touched(self):
        """**这一条是那 352 条的代表。** feed 原本那份壳(标题 + 域名 + HN 得分)说的
        东西本来就少,拿准入那几道问它必然判坏 —— 而它正是补抓还没轮到的正常状态。
        选中它的后果不是「多抓一次」:壳会被换成按标题与链接重建的那一份、译文作废、
        整批重导出。
        """
        key = self.put()
        self.assertLess(readable.substance(SHELL), readable.MIN_CHARS)   # 它确实过不了 ⑥
        self.assertEqual(0, self._recheck('scan'))        # 一条都不该选中
        self.assertEqual(0, self._recheck('run', '--yes'))
        row = self.row(key)
        self.assertEqual(SHELL, row['body'])
        self.assertEqual('summary', row['content_kind'])
        self.assertEqual(1, fulltext.pending('rss'))      # 照旧排在队列里等补抓

    def test_a_repetitive_feed_summary_is_not_this_scripts_business(self):
        """再钉一遍同一条边界,这次落在**还没补抓过**的那一侧:feed 自己给的正文可以
        又长又重复(同一段铺满的摘要),`fulltext_at` 本来就是空。把它换掉就是拿掉
        用户订阅到的内容(口径同「没开补抓的订阅一个都不碰」)。
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


class StoredBodyTests(GrabCase):
    """**准入那个结论必须落在最后真写进库的那一份正文上。**

    `admit` 之后还改正文,原来那个「这是正文」就不再是对最终内容的保证。审查第 1 轮
    实测的事故形态:手动收录把截断放在准入之后,于是一张开头铺满同一段、真内容排在
    6 万字之后的页面全文过关,落库那 60000 字**不重复的只有 202 字** —— 收录当场
    完成、页面紧接着提示「这一篇收下的内容看不出是正文」(阅读时后面的真内容也没了),
    而重抓只会截出同一个前缀。**基线那一版是先截后判、直接拒掉这一页的**,所以这是
    重构带出来的回退:判据下沉之后顺序被颠倒了。

    出路是把调用方那道上限交给 `admit`(`limit=`),不是在它外面补一道事后检查 ——
    两处各判一遍的话,哪天上限改了另一头不会跟着变(元规则:口径只有一份)。
    """

    def test_the_gate_judges_the_truncated_body_the_caller_will_store(self):
        full = readable.extract(LONG_FILLED)
        # 两头先钉住:全文过得了 ⑥(后面那两段各不相同),截到上限之后过不了。
        self.assertGreater(len(full), grab_page.MAX_CHARS)
        self.assertTrue(grab_page.body_confirmed(full))
        self.assertFalse(grab_page.body_confirmed(full[:grab_page.MAX_CHARS]))
        seen = grab_page.admit(PAGE_URL, LONG_FILLED, limit=grab_page.MAX_CHARS)
        self.assertEqual('', seen.body)
        # ⑧ 落在「证据不足」那一档:它说的是「我没能确认哪一段是正文」而不是
        # 「对方拦着你」—— 一张开头铺满同一段的页面多半只是对方的模板怪。
        self.assertEqual('', seen.gate)
        self.assertIn('复读同一段', seen.unsure)

    def test_the_subscription_side_keeps_its_own_reach(self):
        """**不许顺手给补抓那一侧加一道 60000 字截断。** 那一侧原样入库,而写进去的
        是终态 —— 悄悄截一刀是另一件事,不在这次的射程里(`limit=None`)。
        """
        seen = grab_page.admit(PAGE_URL, LONG_FILLED)
        self.assertEqual(('', ''), (seen.gate, seen.unsure))
        self.assertGreater(len(seen.body), grab_page.MAX_CHARS)

    def test_a_long_filled_page_is_refused_and_nothing_is_stored(self):
        """先验后果:这一条抛出来、**库里一行都没有**(`DO NOTHING` 之下存进去就改不回)。"""
        with patch.object(grab, 'request', return_value=html_resp(LONG_FILLED)):
            with self.assertRaises(grab.GrabError) as caught:
                grab.save(grab.grab(PAGE_URL))
        self.assertIn('复读同一段', str(caught.exception))
        self.assertEqual([], self.items())

    def test_both_ends_agree_on_the_same_page(self):
        """**两端共用一份准入,所以结论必须一样** —— 一端放行另一端拒就是两份口径了。

        四张各走一遍,**三档都在里面**:拒绝说明页原样(证据不足,两端都拒、库里一行
        都没有)、同一批文字六句分段(同样证据不足 —— 排版不改变结论)、那篇整段算
        一句的设置教程(两端都**收**)、按句分段的同一批文字(同样收)。
        **后两对是同一批文字的两种排版,它们的结论必须成对相同** —— 那正是上一版
        两侧都漏的形状:同一批字换个排版就换个结论。
        """
        for name, html, refused in (('拒绝说明页', DENIED_PAGE, True),
                                    ('六句分段的同一张拒绝说明', DENIED_SPLIT, True),
                                    ('整段算一句的教程', TUTORIAL_PAGE, False),
                                    ('按句分段的同一批文字', TUTORIAL_SPLIT, False)):
            with self.subTest(page=name):
                seen = grab_page.admit(PAGE_URL, html, limit=grab_page.MAX_CHARS)
                admitted = seen.body
                self.assertEqual(refused, bool(seen.gate or seen.unsure))
                with patch.object(grab, 'request', return_value=html_resp(html)):
                    if refused:
                        with self.assertRaises(grab.GrabError):
                            grab.save(grab.grab(PAGE_URL))
                        self.assertEqual([], self.items())
                    else:
                        grab.save(grab.grab(PAGE_URL))
                        rows = self.items()
                        self.assertEqual(1, len(rows))
                        self.assertEqual(admitted, rows[0]['body'])
                        self.setUp()         # 下一档要一张干净的库

    def test_what_admit_handed_over_is_byte_for_byte_what_gets_stored(self):
        """反过来钉一边:正常文章照旧收得进来,而且**存进去的就是准入放行的那一份**。"""
        page = page_html()
        seen = grab_page.admit(PAGE_URL, page, limit=grab_page.MAX_CHARS)
        admitted = seen.body
        self.assertEqual(('', ''), (seen.gate, seen.unsure))
        with patch.object(grab, 'request', return_value=html_resp(page)):
            grab.save(grab.grab(PAGE_URL))
        rows = self.items()
        self.assertEqual(1, len(rows))
        self.assertEqual(admitted, rows[0]['body'])
