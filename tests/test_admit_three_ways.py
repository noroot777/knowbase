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
import socket
from unittest.mock import Mock, patch

import httpx

from kb.feeds import fetching, fulltext, grab, grab_page, page_gate, readable, translate
from kb.feeds import registry as reg
from kb.feeds.fetching import FeedError, Response
from tests.test_feed_fulltext import PAGE, SHELL
from tests.test_fulltext_admit import (AdmitCase, BLOCK_PAGE_ARTICLE, COMPONENT_ONLY,
                                       DENIED_PAGE, DENIED_SPLIT, DENIED_TEXT, MULTI_TUTORIAL,
                                       TUTORIAL_PAGE, TUTORIAL_SPLIT, resp)

#: 见 `ResponseGateBeatsFormatDispatchTests.transport` —— **必须在任何 patch 之前
#: 取**:`patch.object(fetching.httpx, 'Client')` 改的就是 `httpx.Client` 本身
#: (同一个模块对象),到那时候再取拿到的是上一轮留下的 Mock。
_REAL_CLIENT = httpx.Client


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
                # 拒绝说明页落在**证据不足**那一档(⑥⑦ 读的都是正文里的字),
                # 所以这里问的是 `withheld` 而不是 `refused` —— 结论不随排版变,
                # 落地那一档也不随排版变。
                self.withheld(html, says='正文') if refused else self.fetched(html)

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

    def test_a_status_code_on_its_own_is_never_a_gate(self):
        """**状态码一个都不算关卡身份,这一条是审查第 1 轮点出来的。**

        上一版把 `401/403/429/503/511` 也当成「这个状态码本身就是一句你过不去」,
        后果有两头:没有关卡头的 429 是限流、503 是对方不可用,它们**说得出下一步**
        (稍后重试),而说成「要过一道验证」把人指向一件他做不了也不需要做的事;
        顺带还让 `fetching.request` 原来那句准确的「服务限流,请稍后重试」变成不
        可达。所以 `response_reason` 只问响应头,状态码的分类留在传输层自己那段
        话术里(`test_the_transport_keeps_saying_which_kind_of_failure_it_was`)。
        """
        self.assertEqual(page_gate.SAY_REPLY,
                         page_gate.response_reason({'cf-mitigated': 'challenge'}))
        for name, headers in (('什么头都没有', {}),
                              ('只有 CDN 在场的指纹', {'cf-ray': '8a1b-SJC'}),
                              ('关卡头的值是空的', {'cf-mitigated': '  '})):
            with self.subTest(headers=name):
                self.assertEqual('', page_gate.response_reason(headers))


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
        got = self.withheld(DENIED_SPLIT, says='访问被拒绝')
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
        不同(别的下一轮自己重试,这几条已经挂起、等人看一眼)。"""
        got = self.withheld(DENIED_SPLIT)
        said = fulltext.summarize(got['summary'])
        self.assertIn('1 条抓取失败', said)
        self.assertIn('看不出是不是正文', said)

    def test_evidence_too_thin_does_not_silently_lose_the_full_text(self):
        """**可恢复状态 + 人工出口 —— 这一条是第三档存在的理由。**

        上一版里这一类和「对方真拦着」走同一个出口:试满 `MAX_TRIES` 之后**静默**
        离开队列、按壳入库,面板上再也没有任何痕迹 —— 而它和真关卡的区别恰恰是
        **我们并不知道**那一页是什么。

        四段一起钉:①**一次就挂起,而且确定失败那份预算一次都没扣** —— 再敲同一
        扇门不会让我们知道得更多,而扣掉预算的后果正是上一版那个静默末态;
        ②它**出现在 `held()` 里**、带着那半句话;③人点一次 `release()` 就回到
        队列;④放回去之后**真的会再抓一次**。

        **「不再敲对方」这件事按请求次数钉,不按轮数。** 连跑 `MAX_TRIES + 1` 轮
        同步,原文只该被请求 **1** 次:上一版是 3 次(扣满额度才出队),那三次里后
        两次是明知故问 —— 同一张页面、同一套判据,答案不会变。
        """
        key = self.put()
        with patch.object(fulltext, 'request',
                          Mock(return_value=resp(DENIED_SPLIT))) as req:
            for _ in range(fulltext.MAX_TRIES + 1):
                fulltext.fetch_pending('rss')
        self.assertEqual(1, req.call_count)                    # 挂起之后不再敲对方
        self.assertEqual(0, row_tries := self.row(key)['fulltext_tries'], row_tries)
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

    def test_a_plain_failure_is_not_a_human_todo(self):
        """**反过来钉一边:普通失败不是待办。** 超时、连不上这一类下一轮自己会
        重试,这时候喊人等于把正常的重试说成要人工处理 —— 而待办列表里混进不需要
        人做的行,人就不再看它了。
        """
        self.put()
        with patch.object(fulltext, 'request',
                          Mock(side_effect=FeedError('订阅连接超时，请稍后重试'))):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((1, 0), (summary['failed'], summary['held']))
        self.assertEqual([], fulltext.held('rss'))
        self.assertEqual(1, fulltext.pending('rss'))           # 还排着队,自己会再试

    def test_a_held_item_that_gets_its_body_after_all_is_no_longer_a_todo(self):
        """**补到全文之后那个待办要消失。** 留着它的话 `held()` 会把一条已经补好
        的条目列成待办,人点一次 `release()` 还会把它推回队列白抓一轮。

        走的是完整的那条路:挂起 → 人点恢复 → 这一轮真抓到了正文。
        """
        key = self.put()
        with patch.object(fulltext, 'request', Mock(return_value=resp(DENIED_SPLIT))):
            fulltext.fetch_pending('rss')
        self.assertNotEqual('', self.row(key)['fulltext_hold'])
        self.assertEqual(1, fulltext.release([key]))
        with patch.object(fulltext, 'request', Mock(return_value=resp(PAGE))):
            self.assertEqual(1, fulltext.fetch_pending('rss')['done'])
        self.assertEqual('', self.row(key)['fulltext_hold'])
        self.assertEqual([], fulltext.held('rss'))

    def test_storing_a_body_clears_a_hold_that_a_crossing_round_wrote_back(self):
        """**`_save` 里那句 `fulltext_hold=''` 不是死代码,这一条钉住它。**

        正常那条路上 `release()` 已经先把 hold 清了,于是 `_save` 看到的行本来就
        没有待办 —— 光走正常路证明不了这一句有用,而删掉它之后交错那一侧会留下
        一条「已经补到全文、却还列在待办里」的行(`release` 的说明里那个取舍:人
        发起恢复之前就发出去的那一轮回来之后照旧写得进去,它会把 hold 写回来)。

        所以这里直接把那个交错的末态摆出来:hold 非空、正文这一轮拿到了。
        """
        key = self.put()
        with patch.object(fulltext, 'request', Mock(return_value=resp(DENIED_SPLIT))):
            fulltext.fetch_pending('rss')
        row = self.row(key)
        self.assertNotEqual('', row['fulltext_hold'])
        # 工作身份一律过 `work_version`(内容两道 + 抓取目标),不在这里手拼元组
        # —— 手拼那一份在加第三道的时候就和实现分叉了。
        self.assertTrue(fulltext._save(key, 'A real body.' * 60,
                                       fulltext.work_version(row)))
        self.assertEqual('', self.row(key)['fulltext_hold'])
        self.assertEqual([], fulltext.held('rss'))

    def test_an_unresolved_hold_survives_a_result_that_settled_nothing(self):
        """**一次尝试的结果不能充当正文身份的最终结论 —— 审查第 1 轮的第 3 条。**

        上一版 `_fail` 每次都写 `fulltext_hold`,空串也写,理由写的是「它记的是最后
        一次的结论」。那个判据是错的:超时、连不上、对方 500、提取器交白卷,这几样
        **什么都没查清**,而把它们当成「先前那个问题已经判清」的后果实测过 —— 待办
        被抹掉、`held()` 变空列表,人再也看不到这一条,站点恢复之后也不会再抓它。

        **报告里那个「前两轮说看不出正文、第三轮刚好超时」的序列,在挂起语义下已经
        不存在了**:判出证据不足就离开队列,不会有第二、第三轮自动抓。但根因没变,
        它现在的形态是**交错**那一侧(`release` 的说明):人点恢复之前就发出去的那
        一轮回来了,它手里那份 `work_version` 照旧有效、照旧写得进去。所以这里直接
        把那一下摆出来 —— 挂着一个没答案的问题,然后来一个什么都没查清的结果。

        **只有两件事有资格清掉那个待办**:真拿到了正文(`_save`),或者对方自己说
        它拦了这一次请求(`Gated`,那是查清了,下一条钉它)。
        """
        for name, failure in (('超时', FeedError('订阅连接超时，请稍后重试')),
                              ('拿到一页但认不出正文',
                               '页面上没有识别出正文，可能需要登录或由脚本渲染')):
            with self.subTest(settled_nothing=name):
                self.setUp()
                key = self.put()
                with patch.object(fulltext, 'request',
                                  Mock(return_value=resp(DENIED_SPLIT))):
                    fulltext.fetch_pending('rss')
                row = self.row(key)
                said = row['fulltext_hold']
                self.assertNotEqual('', said)
                # 这一轮是在 `release` 之前就发出去的,所以它那份取样仍然有效
                self.assertTrue(fulltext._fail(key, failure,
                                               fulltext.work_version(row)))
                after = self.row(key)
                self.assertEqual(SHELL, after['body'])            # 正文一个字节没动
                self.assertEqual('', after['fulltext_at'])
                # **这一次什么都没查清,所以那个待办原样留着**
                self.assertEqual(said, after['fulltext_hold'])
                self.assertEqual([key], [r['key'] for r in fulltext.held('rss')])
                self.assertEqual(1, after['fulltext_tries'])      # 它是一次确定失败

    def test_a_definite_gate_does_clear_an_earlier_hold(self):
        """**反过来钉一边:查清了的就该清掉。** 先判出证据不足、下一轮对方自己说
        它拦了这一次请求 —— 那个悬着的问题有答案了,待办跟着消失。留着它的后果是
        `held()` 攒满「其实已经查清是对方拦着」的行,人从此不再看那个列表。
        """
        key = self.put()
        with patch.object(fulltext, 'request', Mock(return_value=resp(DENIED_SPLIT))):
            fulltext.fetch_pending('rss')
        self.assertNotEqual('', self.row(key)['fulltext_hold'])
        self.assertEqual(1, fulltext.release([key]))
        with patch.object(fulltext, 'request', Mock(return_value=resp(COMPONENT_ONLY))):
            fulltext.fetch_pending('rss')
        row = self.row(key)
        self.assertEqual('', row['fulltext_hold'])
        self.assertEqual(1, row['fulltext_tries'])       # 确定关卡照旧计次
        self.assertEqual([], fulltext.held('rss'))

    def test_turning_fulltext_off_takes_the_item_off_the_human_todo_list(self):
        """**关掉补抓就是「这条订阅的正文我不要了」,待办要跟着消失。**

        `release()` 放行它是句空话:`queued_sql` 第一条会把它挡在队列外面,而面板
        上却显示「已恢复 1 条」—— 答不上「那它什么时候再抓」的承诺不如不给。**也
        不许反过来替用户把那个开关打开。**
        """
        key = self.put()
        with patch.object(fulltext, 'request', Mock(return_value=resp(DENIED_SPLIT))):
            fulltext.fetch_pending('rss')
        self.assertEqual([key], [r['key'] for r in fulltext.held('rss')])
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET fulltext=0 WHERE id=?',
                      (self.source['id'],))
        self.assertEqual([], fulltext.held('rss'))
        self.assertEqual(0, fulltext.release([key]))     # 也不假装恢复得了
        self.assertNotEqual('', self.row(key)['fulltext_hold'])   # 那一列原样留着

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


class ResponseGateBeatsFormatDispatchTests(AdmitCase):
    """**响应身份排在「这是什么格式」的分派之前,两个收录入口都是。**

    审查第 1 轮实测的那个洞:① 原先只接在 HTML 那条路上,而两条链路各有**两个更
    早的成功出口** —— 专用原文抓取(`.md` / GitHub 地址)和 `Content-Type` 协商
    (`text/markdown`)。于是一个 status=200、带着 `cf-mitigated: challenge`、正文
    就是那段「访问被拒绝、请启用脚本和 Cookie」的响应,在两个入口都被收成了正文:
    补抓那边 `done=1`、492 字拒绝说明写进 body、`fulltext_at` 写上、`pending` 归零;
    手动收录那边返回成功条目、阅读区完整展示那段拒绝说明。

    **出路不是在那四个出口各补一次判断**(那就是「两头各拼一遍,其中一头迟早漏
    掉」的同一个形状),而是把它放到**传输层**:`fetching.request` 一拿到响应就问,
    认出来抛 `Gated`。响应身份与正文是什么格式无关 —— 格式声明不该撤销更硬的证据。

    **这一组必须真跑 `fetching.request`**,所以打桩打在 httpx 那一层。打桩
    `fulltext.request` / `grab.request` 的话这道判据整个被绕过去,用例看着绿而缺陷
    原样在(那正是上一版的 `_with_response` 测不到这一条的原因)。
    """

    def transport(self, handler):
        """让真 `fetching.request` 跑起来,只把 socket 和 httpx 换成桩。

        口径同 `tests/test_feed_transport.py` 的同名助手 —— DNS 固定解析到一个
        公网地址(SSRF 那道守卫要的),传输层换成 `MockTransport`。

        **真的那个 `httpx.Client` 要在模块导入时就存下来**(`_REAL_CLIENT`):
        `patch.object(fetching.httpx, 'Client')` 改的就是 `httpx.Client` 本身
        (同一个模块对象),所以在这里现取的话,`subTest` 循环第二次拿到的是上一
        轮那个 Mock,factory 调用它就炸在 `_mock_call` 上 —— 而那个红看着像产品
        代码坏了。
        """
        def factory(**kwargs):
            return _REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handler))
        for mock in (patch.object(fetching.httpx, 'Client', side_effect=factory),
                     patch.object(fetching.socket, 'getaddrinfo',
                                  return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                                                 ('93.184.216.34', 443))])):
            mock.start(); self.addCleanup(mock.stop)

    #: 四个出口两两成对:地址决定走不走专用抓取,`Content-Type` 决定走不走协商。
    EXITS = (('协商 Markdown', 'https://e.org/post', 'text/markdown'),
             ('专用原文抓取', 'https://e.org/post.md', 'text/plain'))

    def test_a_gated_response_is_refused_before_any_format_dispatch(self):
        """**补抓这一端**:四个出口里任何一个都不许把它交付成正文。"""
        for name, url, mime in self.EXITS:
            with self.subTest(exit=name):
                self.setUp()
                key = self.put(url=url)
                self.transport(lambda r: httpx.Response(
                    200, content=DENIED_TEXT.encode(),
                    headers={'content-type': f'{mime}; charset=utf-8',
                             'cf-mitigated': 'challenge'}))
                summary = fulltext.fetch_pending('rss')
                row = self.row(key)
                self.assertEqual((0, 1), (summary['done'], summary['failed']))
                self.assertEqual(SHELL, row['body'])          # 一个字节都没动
                self.assertEqual('', row['fulltext_at'])
                self.assertIn('验证', row['fulltext_error'])
                # 确定关卡:查清了,所以不留「等人看一眼」的待办
                self.assertEqual('', row['fulltext_hold'])
                self.assertEqual([], fulltext.held('rss'))

    def test_manual_grab_refuses_the_same_response_at_the_same_four_exits(self):
        """**手动收录这一端**:同一个响应、同一个结论。两端各拼一遍的话,其中一头
        迟早漏掉一道 —— 那正是把判据收进 `admit` / 传输层的来历。
        """
        for name, url, mime in self.EXITS:
            with self.subTest(exit=name):
                self.transport(lambda r: httpx.Response(
                    200, content=DENIED_TEXT.encode(),
                    headers={'content-type': f'{mime}; charset=utf-8',
                             'x-amzn-waf-action': 'captcha'}))
                with self.assertRaises(grab.GrabError) as caught:
                    grab.grab(url)
                self.assertIn('验证', str(caught.exception))
                # **拒对了还得说得出下一步。** 传输层那一支原先只把原因转过来,
                # 用户看到的是一句「对方拦在了一道验证上」然后没有然后 —— 而
                # `admit` 判出关卡那条路一直带着这半句(口径同 `_gate`)。
                self.assertIn('在浏览器里打开', str(caught.exception))

    def test_an_innocent_markdown_response_still_goes_through(self):
        """**反过来钉一边。** 没有关卡头的 Markdown 照旧两个出口都收得进来 ——
        否则这道判据就成了「凡是 Markdown 一律拒」。
        """
        for name, url, mime in self.EXITS:
            with self.subTest(exit=name):
                self.setUp()
                key = self.put(url=url)
                self.transport(lambda r: httpx.Response(
                    200, content=b'# Title\n\nA perfectly ordinary README body.\n',
                    headers={'content-type': f'{mime}; charset=utf-8',
                             'cf-ray': '8a1b2c3d4e5f-SJC'}))
                self.assertEqual(1, fulltext.fetch_pending('rss')['done'])
                self.assertIn('perfectly ordinary', self.row(key)['body'])

    def test_the_transport_keeps_saying_which_kind_of_failure_it_was(self):
        """**没有关卡头的 4xx/5xx 不许说成「要过一道验证」。** 429 是限流、503 是
        对方不可用,它们说得出下一步;而关卡那句话指向的是「在浏览器里打开一次」。
        上一版把这几个状态码也算进关卡表,于是这句准确的限流提示变成不可达。
        """
        for status, says, kind in ((429, '服务限流', fetching.FeedError),
                                   (503, 'HTTP 503', fetching.FeedError),
                                   (403, 'HTTP 403', fetching.FeedError)):
            with self.subTest(status=status):
                self.transport(lambda r: httpx.Response(status, content=b''))
                with self.assertRaises(kind) as caught:
                    fetching.request('https://e.org/post', {})
                self.assertIn(says, str(caught.exception))
                self.assertNotIn('验证', str(caught.exception))
        # 带着关卡头的那一张换成 `Gated`,话术也跟着换
        self.transport(lambda r: httpx.Response(403, content=b'',
                                                headers={'cf-mitigated': 'challenge'}))
        with self.assertRaises(fetching.Gated) as caught:
            fetching.request('https://e.org/post', {})
        self.assertIn('验证', str(caught.exception))
        self.assertIn('HTTP 403', str(caught.exception))

    def test_a_refused_request_is_not_retried_behind_another_accept_header(self):
        """**关卡必须穿透专用原文抓取那条路,不许被吞成「这条路走不通,换主路」。**

        `markdown_source.fetch` 把失败吞成 `None` 是对的 —— 抓不到 `.md` 就走通用
        HTML 那条路。但对方**明说拦了这一次请求**时吞掉就不是降级而是重试:换一个
        `Accept` 对同一扇门再敲一次,而这一次「恰好没带关卡头」的响应会被整条收下。

        所以这里钉的后果是**敲门次数**:穿透时只敲一次就停,吞掉时第二次响应(这
        里故意给一篇读起来完全正常的文章)会被写进 `body`、写上 `fulltext_at`、
        条目离开重试队列 —— 而我们手里唯一的硬证据说的是「这一次被拦住了」。
        """
        self.setUp()
        key = self.put(url='https://e.org/post.md')
        knocks: list[str] = []

        def handler(request):
            knocks.append(str(request.url))
            if len(knocks) == 1:   # 专用原文抓取那一次:对方拦着
                return httpx.Response(200, content=DENIED_TEXT.encode(),
                                      headers={'content-type': 'text/plain; charset=utf-8',
                                               'cf-mitigated': 'challenge'})
            return httpx.Response(200, content=PAGE.encode(),   # 第二次:干净得看不出问题
                                  headers={'content-type': 'text/html; charset=utf-8'})

        self.transport(handler)
        summary = fulltext.fetch_pending('rss')
        row = self.row(key)
        self.assertEqual(1, len(knocks))                  # 只敲了一次门
        self.assertEqual((0, 1), (summary['done'], summary['failed']))
        self.assertEqual(SHELL, row['body'])
        self.assertEqual('', row['fulltext_at'])
        self.assertIn('验证', row['fulltext_error'])
        self.assertEqual('', row['fulltext_hold'])        # 确定关卡,不是「等人看一眼」


class EligibilityIsOneQuestionTests(AdmitCase):
    """**「这一条现在还该不该等全文」只有一份判据**(`fulltext._eligible`)。

    这件事曾经被分别回答过四遍,于是漏了第四处。补抓队列、挂起待办、人工恢复三处
    都问了「这条订阅还开着补抓吗」,而翻译那道闸门(`unsettled_sql`)只在「还排着
    队」那半边问、挂起那半边没问 —— 后果实测过:关掉补抓(或者删掉最后一条开着
    补抓的归属)之后,那一条落进一个**没有出口的夹缝**:翻译那边永久当它「正文还没
    定下来」,英文摘要再也不译成中文;待办那边按资格把它排除,面板上看不到;人点
    恢复也以 `released=0` 拒绝。

    **所以这一组的判据落在「摘要到底译不译」上**,不是「那个 SQL 片段长什么样」。
    """

    def held_item(self, source=None, url='https://e.org/post'):
        """摆一条、抓一轮把它挂起,返回 key。"""
        key = self.put(source or self.source, url=url)
        with patch.object(fulltext, 'request', Mock(return_value=resp(DENIED_SPLIT))):
            fulltext.fetch_pending('rss')
        self.assertNotEqual('', self.row(key)['fulltext_hold'])   # 摆盘自检
        return key

    def test_turning_fulltext_off_hands_the_summary_back_to_translation(self):
        """**关掉补抓 = 「这条订阅的正文我不要了」,摘要就是终稿。**

        反过来钉一边同时在这条里:开着补抓时它**不该**进翻译队列(壳马上要被整条
        换掉,译一遍是白买一次模型调用),关掉之后才该进。
        """
        key = self.held_item()
        self.assertEqual(0, translate.pending('rss'))       # 开着:先等正文
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET fulltext=0 WHERE id=?',
                      (self.source['id'],))
        self.assertEqual(1, translate.pending('rss'))       # 关掉:摘要就是终稿
        self.assertEqual([], fulltext.held('rss'))          # 待办跟着消失
        self.assertEqual(0, fulltext.release([key]))        # 也不假装恢复得了
        self.assertEqual(0, fulltext.pending('rss'))

    def test_deleting_the_last_subscription_that_wanted_it_does_the_same(self):
        """**删订阅是另一条通往同一个末态的路。** 删订阅刻意保留已收录的条目
        (产品承诺),于是归属没了、资格也没了 —— 只在某个 PATCH 路径里清那一列
        的话,这条路照旧把条目留在夹缝里。
        """
        self.held_item()
        reg.delete_subscription('rss', self.source['id'])
        self.assertEqual(1, translate.pending('rss'))
        self.assertEqual([], fulltext.held('rss'))

    def test_one_source_switched_off_is_not_all_of_them(self):
        """**反过来钉一边:还有别的归属开着补抓,就照旧等全文。**

        一条内容可能同时属于聚合源和单号源,任意一条开着就该抓(补到的正文对另一条
        同样有用)。所以修法不能是「关掉开关就把整条状态清掉」。
        """
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET fulltext=1 WHERE id=?', (self.plain['id'],))
        key = self.held_item()
        self.put(self.plain)                                 # 同一条内容的第二条归属
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET fulltext=0 WHERE id=?',
                      (self.source['id'],))
        self.assertEqual(0, translate.pending('rss'))        # 另一条还要正文
        self.assertEqual([key], [r['key'] for r in fulltext.held('rss')])
        self.assertEqual(1, fulltext.release([key]))         # 恢复也照旧算数

    def test_a_key_from_another_kind_is_not_released_by_this_kind(self):
        """**范围要约束写入本身**(元规则:判据待在写语句自己的 WHERE 里)。

        **用的是另一个真实来源**(`web`,网页专栏),不是一个不存在的名字 —— 后者
        只能钉住「查无此来源」,而这里要钉的是「那一条真的挂着、也真的属于别人」。
        CLI 那一侧刻意不传 `kind`:它的 `--release <key>` 文档写明按 key 跨来源,
        那是有意的契约,不该反过来把 CLI 也锁死在一个来源上。
        """
        key = self.held_item()
        self.assertEqual(0, fulltext.release([key], kind='web'))    # 别人的来源管不着
        self.assertNotEqual('', self.row(key)['fulltext_hold'])
        self.assertEqual(1, fulltext.release([key], kind='rss'))    # 自己的来源照旧
        self.assertEqual(1, fulltext.pending('rss'))

    def test_the_same_key_three_times_is_still_one_release(self):
        """**统计按实际写入结果报**(元规则)。不带「现在还挂着吗」那一条时,同一个
        key 给三次会报 `released=3`:那几列已经是目标值了,UPDATE 照样算 1 行。于是
        「恢复了 3 条」而库里只有一条,已经恢复过的、压根没挂起过的也一律报成功。
        """
        key = self.held_item()
        self.assertEqual(1, fulltext.release([key, key, key]))
        self.assertEqual(0, fulltext.release([key]))        # 已经不挂着了,不再算
        self.assertEqual(1, fulltext.pending('rss'))        # 真放回去的只有这一条
