# -*- coding: utf-8 -*-
"""写库准入的**三档**:确认为正文 / 确定关卡 / 证据不足(`grab_page.Admission`)。

被测的那件事:**「没认出关卡」和「确认是正文」是两件事。** 上一版把它们合成一件,
后果是两头都错 —— 份额未达线被直接当成「已经拿到正文」,于是把一张拒绝说明页的
六句话各自分段就够了:份额 1.0 → 1/6、当时那六道一道都不命中,496 字「访问被
拒绝」当正文入库、写上 `fulltext_at`、退出重试队列、送翻译并按文章导出;而份额
达线被直接当成「对方拦着你」,于是一篇提到一次「enable JavaScript」的正常设置教程
连抓三轮耗尽额度、永久退回摘要(2026-10-22 审查第 1 轮两侧俱在)。

三组各钉一件:
- `LayoutDoesNotChangeTheVerdictTests` —— **同一批文字换排版不换结论**(缺陷的根在
  句界,不在阈值);
- `ResponseIdentityTests` —— **① 这一次响应自己的身份**,唯一一道不靠页面文字的
  判据,两侧都钉(关卡决策头算、CDN 在场的指纹不算);
- `EvidenceTooThinTests` —— **第三档本身**:不写库、记成「我没能判断」而不是「对方
  拦着你」、额度用完之后留得下痕迹(`fulltext.held`)、人点一次能放回队列
  (`fulltext.release`)。

**按形状单独一个文件**:`tests/test_fulltext_admit.py` 加上这三组会越过 700 行硬
上限。脚手架(`AdmitCase`)和页面夹具一律从那一份 import —— 各写一份的话两边的
前提会各漂各的(口径同那一份自己对 `FullTextCase` / `RecheckCase` 的复用)。
硬约束在 `docs/decisions/fulltext.md` 第一节。
"""
from unittest.mock import Mock, patch

from kb.feeds import fulltext, grab_page, page_gate, readable
from kb.feeds.fetching import Response
from tests.test_feed_fulltext import PAGE, SHELL
from tests.test_fulltext_admit import (AdmitCase, BLOCK_PAGE_ARTICLE, COMPONENT_ONLY,
                                       DENIED_PAGE, DENIED_SPLIT, MULTI_TUTORIAL,
                                       TUTORIAL_PAGE, TUTORIAL_SPLIT, resp)


class LayoutDoesNotChangeTheVerdictTests(AdmitCase):
    """**同一批文字,排版怎么换结论都一样。** 这一组原先是那个缺陷的形状快照
    (「④ 随排版翻转」),现在钉的是它的反面 —— 不变量。

    两批文字各三种排版,**六个结论两两相同**:
    - 正常设置教程(连成一段 / 按英文句号分成七段 / 三个自然段各提一次那个短语)
      → 三种都**收**;
    - 纯文字拒绝说明页(连成一段 / 六句各自成段)→ 两种都**拒**。

    **缺陷的根在句界,不在阈值。** 上一版 `_SENTENCE` 刻意不切 `. `,于是英文的
    「一个自然段」被当成「一句」:正文里提一次「enable JavaScript」份额就是 1.0。
    两侧都实测到过后果 —— 683 字的正常教程被拒三轮、永久退回摘要(误拒);而把
    拒绝说明页六句各自分段,份额 1.0 → 1/6、当时那六道一道都不命中,496 字
    「访问被拒绝」当正文入库(漏放)。
    **切了 `. ` 之后两批文字的份额都与排版无关**(教程 0.143、拒绝页 0.167),而
    拒绝页由 ⑦ 那道样板占比接住(0.833,同样与排版无关)。

    **这一组不替代响应身份那一道。** 这两批文字落在份额阈值的同一侧(0.143 对
    0.167),分开它们的是**不同的一道判据**而不是一条更准的线 —— 真正能一刀切开
    「这一页在拦正文」的只有 ① `page_gate.response_reason`(见
    `ResponseIdentityTests`),这里钉的只是「文字那几道不再自相矛盾」。
    """

    def test_the_same_words_get_the_same_verdict_whatever_the_layout(self):
        for name, html, refused in (('教程·连成一段', TUTORIAL_PAGE, False),
                                    ('教程·按英文句号分成七段', TUTORIAL_SPLIT, False),
                                    ('教程·三个自然段各提一次', MULTI_TUTORIAL, False),
                                    ('拒绝说明·连成一段', DENIED_PAGE, True),
                                    ('拒绝说明·六句各自成段', DENIED_SPLIT, True)):
            with self.subTest(layout=name):
                self.setUp()                 # 每一档要一条干净的壳条目
                self.refused(html, says='正文') if refused else self.fetched(html)

    def test_the_share_itself_no_longer_moves_with_the_layout(self):
        """**判据那一层也要钉,不只是结论。** 只验结论的话,哪天份额又开始随排版
        抖、而两种排版恰好落在同一侧,这一组照旧全绿 —— 而那个缺陷已经回来了。
        """
        for name, one, split in (('教程', TUTORIAL_PAGE, TUTORIAL_SPLIT),
                                 ('拒绝说明', DENIED_PAGE, DENIED_SPLIT)):
            with self.subTest(words=name):
                a, b = readable.extract(one), readable.extract(split)
                # 先钉住两张页面的字真的一样(否则下面比的是两篇不同的文章)
                self.assertEqual(' '.join(a.split()), ' '.join(b.split()))
                self.assertEqual(grab_page._gate_share(a), grab_page._gate_share(b))
                self.assertEqual(grab_page._refusal_share(a), grab_page._refusal_share(b))


class ResponseIdentityTests(AdmitCase):
    """**① 这一次响应自己的身份 —— 唯一一道不靠页面文字的判据。**

    为什么非要有它:一张拒绝说明页和一篇**真·讲拦截页**的技术文章在**文字上不可
    分离**。那篇文章正文里原样引着「your request was blocked ...」和「If you think
    this is a mistake, contact the site administrator」两整句(实测 ⑦ 占比 0.322、
    ⑥ 份额 0.50),而它必须收进来。所以文字那几道只能说「我确认不了」,说得出
    「对方拦着你」的只有响应自己。

    **判据是「对方这次真的拦了才会发」的那几个头,不是「CDN 在场」。**
    `cf-ray`、`server: cloudflare` 这类在正常文章的响应上也常态出现 —— 进表等于按
    「这站挂在 Cloudflare 后面」整批拒稿。两侧都钉在下面。
    """

    def _with_response(self, html, status=200, headers=None):
        """同一张页面,换一次响应身份。**页面一个字都不动** —— 这一组要证明的正是
        「结论来自响应而不是文字」。"""
        key = self.put()
        resp = Response(status, 'https://e.org/post', html.encode(),
                        {'content-type': 'text/html; charset=utf-8', **(headers or {})})
        with patch.object(fulltext, 'request', Mock(return_value=resp)):
            summary = fulltext.fetch_pending('rss')
        return summary, self.row(key)

    def test_a_gate_header_refuses_a_page_that_reads_like_an_article(self):
        """**整篇读起来就是一篇正常文章,身份只写在响应头里。** 文字那八道一道都
        不命中它 —— 漏掉 ① 就是把这一页当正文永久写进库。
        """
        # 先钉住这张页面在文字上完全无辜(否则这条用例测不到 ① 那一道)
        seen = grab_page.admit('https://e.org/post', PAGE)
        self.assertNotEqual('', seen.body)
        for name, headers in (('cf-mitigated', {'cf-mitigated': 'challenge'}),
                              ('大小写不敏感', {'CF-Mitigated': 'challenge'}),
                              ('cf-chl-bypass', {'cf-chl-bypass': '1'}),
                              ('x-amzn-waf-action', {'x-amzn-waf-action': 'captcha'}),
                              ('x-sucuri-block', {'x-sucuri-block': 'HTTP/1.1'})):
            with self.subTest(header=name):
                self.setUp()
                summary, row = self._with_response(PAGE, headers=headers)
                self.assertEqual((0, 1, 0), (summary['done'], summary['failed'],
                                             summary['held']))
                self.assertEqual(SHELL, row['body'])
                self.assertEqual('', row['fulltext_at'])
                # **确定关卡不留「等人看一眼」的待办**:它已经查清了
                self.assertEqual('', row['fulltext_hold'])
                self.assertIn('验证', row['fulltext_error'])

    def test_a_cdn_fingerprint_alone_does_not_refuse_anything(self):
        """**反过来钉一边。** 被 Cloudflare 代理的站点占了一大片互联网,它们的正常
        响应上就带着 `cf-ray` / `server: cloudflare` / `x-datadome: protected`。
        """
        for name, headers in (('cf-ray', {'cf-ray': '8a1b2c3d4e5f-SJC'}),
                              ('server', {'server': 'cloudflare'}),
                              ('datadome 常态头', {'x-datadome': 'protected'}),
                              ('空值的关卡头', {'cf-mitigated': '   '})):
            with self.subTest(header=name):
                self.setUp()
                summary, row = self._with_response(PAGE, headers=headers)
                self.assertEqual(1, summary['done'])
                self.assertIn('The runtime schedules tools', row['body'])

    def test_the_transport_says_what_a_gated_4xx_actually_is(self):
        """**4xx/5xx 那一类在传输层就被拦下,话术也要跟着对。** `fetching.request`
        对 `>= 400` 一律抛,原先一句「请检查来源或填写专用订阅链接」—— 而对着一张
        `403 + cf-mitigated: challenge`,用户改不出任何东西,因为挡着的不是地址。
        **表和话术只有一份**(`page_gate`),写库准入那道 ① 问的是同一组头。
        """
        self.assertEqual(page_gate.SAY_REPLY, page_gate.response_reason(403, {}))
        self.assertEqual(page_gate.SAY_REPLY,
                         page_gate.response_reason(200, {'cf-mitigated': 'challenge'}))
        # 普通的 404 / 500 不是关卡:那是「换个地址」而不是「在浏览器里打开一次」
        self.assertEqual('', page_gate.response_reason(404, {}))
        self.assertEqual('', page_gate.response_reason(500, {}))
        self.assertEqual('', page_gate.response_reason(304, {}))


class EvidenceTooThinTests(AdmitCase):
    """**第三档:证据不足。** 不写库(和确定关卡一样严),但不说「对方拦着你」,
    而且**额度用完之后留得下痕迹**而不是静默退回摘要。

    上一版只有两档,后果是两头都错:份额未达线被直接当成「已经拿到正文」(一张分段
    的拒绝说明页因此永久写成正文),份额达线被直接当成「对方拦着你」(一篇正常教程
    因此耗尽额度)。这一组钉的是第三档本身 —— 它的**写库后果**、**账**、以及那条
    可恢复状态与人工出口。
    """

    def test_a_split_refusal_notice_is_held_not_stored(self):
        """**那张分段拒绝说明页** —— 审查第 1 轮漏放的那一例。先验后果:库里一个
        字节没动,而这一次的结论**记成了「我没能判断」**而不是「对方拦着你」。
        """
        got = self.refused(DENIED_SPLIT, says='访问被拒绝')
        self.assertEqual(1, got['summary']['held'])
        self.assertIn('访问被拒绝', got['row']['fulltext_hold'])
        self.assertEqual(1, len(got['summary']['errors']))   # 面板明细照旧列出它

    def test_an_article_about_block_pages_is_still_stored(self):
        """**反过来钉一边,而且这是 ⑦ 最该钉的那一侧。** 一篇真·讲拦截页的技术
        文章,正文里原样引着两整句样板措辞 —— 它和一张真的拒绝说明页在文字上只差
        一个占比(0.322 对 0.833),所以这一条是那条线的另一侧。
        """
        body = readable.extract(BLOCK_PAGE_ARTICLE)
        self.assertGreater(grab_page._refusal_share(body), 0)       # 它真的引了样板
        self.assertLess(grab_page._refusal_share(body), grab_page._REFUSAL_SHARE)
        got = self.fetched(BLOCK_PAGE_ARTICLE)
        self.assertIn('your request was blocked', got['row']['body'])
        self.assertEqual('', got['row']['fulltext_hold'])

    def test_a_definite_gate_leaves_no_hold_to_look_at(self):
        """**两档的账要分开。** 确定关卡已经查清了「对方拦着你」,它不该出现在
        「等人看一眼」那个列表里 —— 混进去的后果是那个列表攒满已经有答案的行,
        人从此不再看它。
        """
        got = self.refused(COMPONENT_ONLY, says='验证')
        self.assertEqual(0, got['summary']['held'])
        self.assertEqual('', got['row']['fulltext_hold'])
        self.assertEqual([], fulltext.held('rss'))

    def test_the_round_says_how_many_it_could_not_judge(self):
        """面板那句汇报要说得出「其中几条我没能判断」—— 这几条的下一步和别的失败
        不同(别的下一轮自己重试,这几条试满额度要人看一眼)。"""
        got = self.refused(DENIED_SPLIT)
        said = fulltext.summarize(got['summary'])
        self.assertIn('1 条抓取失败', said)
        self.assertIn('看不出是不是正文', said)

    def test_evidence_too_thin_does_not_silently_lose_the_full_text(self):
        """**可恢复状态 + 人工出口 —— 这一条是第三档存在的理由。**

        上一版里这一类和「对方真拦着」走同一个出口:试满 `MAX_TRIES` 之后**静默**
        离开队列、按壳入库,面板上再也没有任何痕迹 —— 而它和真关卡的区别恰恰是
        **我们并不知道**那一页是什么。

        四段一起钉:①额度照旧计次(不计次它就永远排在队首、每轮重抓同一页);
        ②额度用完之后它**出现在 `held()` 里**、带着那半句话;③人点一次
        `release()` 就回到队列;④放回去之后**真的会再抓一次**。
        """
        key = self.put()
        with patch.object(fulltext, 'request',
                          Mock(return_value=resp(DENIED_SPLIT))) as req:
            for _ in range(fulltext.MAX_TRIES + 1):
                fulltext.fetch_pending('rss')
        self.assertEqual(fulltext.MAX_TRIES, req.call_count)   # 出队之后不再敲对方
        self.assertEqual(0, fulltext.pending('rss'))
        self.assertEqual(SHELL, self.row(key)['body'])         # 正确的降级:壳还在
        waiting = fulltext.held('rss')
        self.assertEqual([key], [r['key'] for r in waiting])
        self.assertIn('访问被拒绝', waiting[0]['fulltext_hold'])
        # 人工出口:额度还回去,这一条回到队列
        self.assertEqual(1, fulltext.release([key]))
        self.assertEqual([], fulltext.held('rss'))
        self.assertEqual(1, fulltext.pending('rss'))
        with patch.object(fulltext, 'request', Mock(return_value=resp(PAGE))) as again:
            self.assertEqual(1, fulltext.fetch_pending('rss')['done'])
        self.assertEqual(1, again.call_count)
        self.assertIn('The runtime schedules tools', self.row(key)['body'])

    def test_a_budget_that_is_not_used_up_is_not_a_human_todo(self):
        """**额度还没用完的不算待办。** 它们还排在队列里、下一轮同步自己会再试 ——
        这时候喊人等于把正常的重试说成要人工处理。
        """
        self.refused(DENIED_SPLIT)
        self.assertEqual([], fulltext.held('rss'))
        self.assertEqual(1, fulltext.pending('rss'))

    def test_a_held_item_that_gets_its_body_after_all_is_no_longer_a_todo(self):
        """**补到全文之后那个待办要消失。** 留着它的话 `held()` 会把一条已经补好
        的条目列成待办,人点一次 `release()` 还会把它推回队列白抓一轮。
        """
        key = self.put()
        with patch.object(fulltext, 'request', Mock(return_value=resp(DENIED_SPLIT))):
            fulltext.fetch_pending('rss')
        self.assertNotEqual('', self.row(key)['fulltext_hold'])
        with patch.object(fulltext, 'request', Mock(return_value=resp(PAGE))):
            self.assertEqual(1, fulltext.fetch_pending('rss')['done'])
        self.assertEqual('', self.row(key)['fulltext_hold'])
        self.assertEqual([], fulltext.held('rss'))

    def test_releasing_never_touches_a_row_that_already_has_its_full_text(self):
        """**人工出口那一下的判据待在 UPDATE 自己的 WHERE 里**(元规则):这一下
        和补抓 worker 是两个写入方。这期间补到全文的那一条放行不了,`rowcount==0`
        就是「不用管了」。
        """
        key = self.put()
        with patch.object(fulltext, 'request', Mock(return_value=resp(PAGE))):
            fulltext.fetch_pending('rss')
        row = self.row(key)
        self.assertEqual(0, fulltext.release([key, 'no-such-key']))
        self.assertEqual(row['body'], self.row(key)['body'])
        self.assertNotEqual('', self.row(key)['fulltext_at'])
