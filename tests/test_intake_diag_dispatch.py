# -*- coding: utf-8 -*-
"""**搁置要拦住「别开始」那一头** —— 派发出去的那一瞬之前再问一次。

交付那一头在 `tests/test_intake_diag_export.py`。这一份管的是另一半:补抓和翻译
都**取样一批、再逐条去发请求**,而两件事之间隔着前面几条的网络等待和模型调用,
几分钟是常事。只认取样那一刻的结果就是把「取样时获准」当成「整批一直获准」——
审查第 2 轮 R1 实测:第一条正在调模型的时候把第二条「不再收录」,第二条随后照旧
被派发出去,**而那是一笔还没花的钱**。

两条路各一组,两头都钉:
① 搁置之后那一条**一次请求都不发**(额度、待办、错误一列都不动,统计单独报一个数);
② **反过来**:没搁置的那一条照旧派发 —— 不然「整个 worker 坏掉」也是全绿。

**这一道和写回那道判据不是一回事,所以两份回归都要有**(两份实现各有自己的红,
元规则)。它关不掉的那一瞬(这一问返回之后、系统调用发出之前)写在
`shelf.admitted` 的说明里,不在这里假装钉住。
"""
from __future__ import annotations

from unittest.mock import patch

from kb.feeds import fulltext, registry as reg, translate
from kb.intake_diag import shelf
from tests.intake_diag_case import DiagCase, key_of

HELD = 'https://news.example.org/held'
QUEUED = 'https://news.example.org/queued'
TRANSLATE = 'https://solo.example.org/translate-queued'


class FetchDispatchTests(DiagCase):
    """补抓那条路:`fetch_one` 调了几次就是「联了几次网」。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        # 两条都排在补抓队列里,一个 worker 串行跑 —— 不串行的话「第一条在路上
        # 时搁置第二条」这个交错压根不成立。
        self.mine = key_of('rss', QUEUED)
        self.other = self.put(reg.subscription('rss', self.ids['agg']),
                              'https://news.example.org/second-shell',
                              body='又一个几十字的跳转壳。')

    def run_round(self, shelve_other: bool):
        """跑真实的 `fetch_pending`,只把「联网那一下」换成固定回包。

        **搁置的那一条必须是「还没派发出去」的那一条**,所以在第一次抓取的回调里
        按当前这个链接反查 —— 取数顺序由 `fetch_pending` 自己定(失败过的优先、
        同一秒之间按 key),把「先跑哪一条」写死在用例里得到的只是一条看运气的红。
        """
        asked: list[str] = []

        def fetch(url):
            if not asked and shelve_other:
                self.victim = next(k for k in (self.mine, self.other)
                                   if url not in (self.row(k)['url'],
                                                  self.row(k)['fulltext_url']))
                self.shelve([self.victim], 'rss', shelf.EXCLUDE)
            asked.append(url)
            return '抓回来的完整正文。' * 40, ''

        with patch.object(fulltext, 'WORKERS', 1), \
             patch.object(fulltext, 'fetch_one', fetch):
            return asked, fulltext.fetch_pending('rss')

    def queued(self) -> list[str]:
        cond, args = fulltext.queued_sql()
        with reg.db() as c:
            return [r[0] for r in c.execute(
                f"SELECT key FROM feed_items WHERE kind='rss' AND ({cond})"
                ' ORDER BY key', args)]

    def test_an_item_excluded_mid_round_is_never_fetched(self):
        self.assertEqual(sorted([self.mine, self.other]), sorted(self.queued()))
        before = {key: self.row(key) for key in (self.mine, self.other)}
        asked, summary = self.run_round(shelve_other=True)
        self.assertEqual(1, len(asked))
        self.assertEqual(1, summary['shelved'])
        # **一列都不动**:额度没扣、待办没写、错误没记 —— 恢复之后它回到队列,
        # 下一轮照旧去取。
        self.assertEqual(before[self.victim], self.row(self.victim))
        # 这句话要如实说出来(元规则:跳过的如实列出),而且不许混进「换新作废」。
        said = fulltext.summarize(summary)
        self.assertIn('没有去取', said)
        self.assertNotIn('已被换新', said)

    def test_the_same_round_still_fetches_the_one_nobody_touched(self):
        """**反过来钉一边。**"""
        asked, summary = self.run_round(shelve_other=False)
        self.assertEqual(2, len(asked))
        self.assertEqual(0, summary['shelved'])
        self.assertEqual(2, summary['done'])


class TranslateDispatchTests(DiagCase):
    """翻译那条路:`translate_item` 调了几次就是「买了几次模型调用」。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.mine = key_of('rss', TRANSLATE)
        self.other = self.put(reg.subscription('rss', self.ids['plain']),
                              'https://solo.example.org/second-english',
                              body='An English article with substantive content. ' * 30)
        with reg.db() as c:
            c.execute("UPDATE feed_items SET lang='en',content_kind='content'"
                      ' WHERE key=?', (self.other,))

    def run_round(self, shelve_other: bool):
        """跑真实的 `translate_pending`,只把整条翻译换成固定成品。

        **`allowed` 这第三个参数不能省**:真实签名是
        `translate_item(item, model, allowed)`(那是逐段那道闸),替身少收一个的话
        绑参就 TypeError,而它会被「逐条隔离」那一层吞成一次普通失败 —— 于是这一
        轮一次调用都没发生,两条断言都在验一件压根没跑过的事。

        搁置的那一条按**当前正在译的这一条**反查,理由同上面补抓那一组。
        """
        bought: list[str] = []

        def call(item, model, allowed=None):
            if not bought and shelve_other:
                self.victim = next(k for k in (self.mine, self.other)
                                   if k != item['key'])
                self.shelve([self.victim], 'rss', shelf.EXCLUDE)
            bought.append(item['key'])
            return '译好的标题', '译好的正文。' * 40

        with patch.object(translate, 'WORKERS', 1), \
             patch.object(translate, '_model', return_value=object()), \
             patch.object(translate, 'translate_item', call):
            return bought, translate.translate_pending('rss')

    def test_an_item_excluded_mid_round_never_buys_a_model_call(self):
        before = {key: self.row(key) for key in (self.mine, self.other)}
        bought, summary = self.run_round(shelve_other=True)
        self.assertNotIn(self.victim, bought)
        self.assertEqual(1, summary['shelved'])
        self.assertEqual(before[self.victim], self.row(self.victim))
        said = translate.summarize(summary)
        self.assertIn('没有翻译', said)
        self.assertNotIn('原文在这期间换过', said)

    def test_the_same_round_still_translates_the_one_nobody_touched(self):
        """**反过来钉一边。**"""
        bought, summary = self.run_round(shelve_other=False)
        self.assertEqual(sorted([self.mine, self.other]), sorted(bought))
        self.assertEqual(0, summary['shelved'])
        self.assertEqual(2, summary['done'])
        for key in (self.mine, self.other):
            self.assertNotEqual('', self.row(key)['translated_at'])


class ChunkDispatchTests(DiagCase):
    """**同一篇文章里的第二段也要再问一次** —— 一条长文是好几次付费调用。

    上面那组 patch 的是 `translate_item`(整条),所以它钉住的是「下一**条**还发不
    发」。这一组钉的是「下一**段**还发不发」:正文超过 `translate.CHUNK` 时那一条
    会被切成好几段、每段各买一次模型调用,而把准入只问在条目开头等于第一段之后
    整篇都不再过闸 —— 一篇 10 段的长文,人在第一段刚发出去时按「不再收录」,余下
    9 次调用照旧全部花掉。
    """

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        # 四段、每段都比 CHUNK 的一半长 —— 真实切分规则由 `translate` 自己定,
        # 这里只保证它一定会切(下面先断言买了几次,拿实际段数当基准)。
        piece = 'An English paragraph that carries its own meaning. ' * 60
        self.mine = self.put(reg.subscription('rss', self.ids['plain']),
                             'https://solo.example.org/long-english',
                             body='\n\n'.join(piece for _ in range(4)),
                             lang='en', content_kind='content')
        # **这一轮里只许有这一篇。** 本组数的是「一篇长文买了几次调用」,而样例里
        # 另有一条等着译的英文内容 —— 它那一段会混进同一个计数,于是「余下几段一笔
        # 都没发」和「每一段都照旧买」两头都失真(前者会看到 1 条 done,后者会看到
        # 2 条)。把别的挪出队列(判据是 `lang`),比在断言里按差值倒推诚实。
        with reg.db() as c:
            c.execute("UPDATE feed_items SET lang='zh' WHERE kind='rss' AND key<>?",
                      (self.mine,))
        cond, args = translate.queued_sql()
        with reg.db() as c:
            self.assertEqual([self.mine], [r[0] for r in c.execute(
                f"SELECT key FROM feed_items WHERE kind='rss' AND ({cond})", args)])

    def run_round(self, shelve_at: int | None):
        """跑真实的 `translate_pending`,只把模型响应换成固定成品。

        `shelve_at` = 第几次调用**发出去的时候**把它「不再收录」(1 = 第一段)。
        """
        bought: list[int] = []

        def call(prompt, model):
            bought.append(len(bought) + 1)
            if shelve_at is not None and len(bought) == shelve_at:
                self.shelve([self.mine], 'rss', shelf.EXCLUDE)
            return '###TITLE###\n中文标题\n###BODY###\n这是一段中文译文。'

        with patch.object(translate, 'WORKERS', 1), \
             patch.object(translate, '_model', return_value=object()), \
             patch.object(translate, '_call', call):
            return bought, translate.translate_pending('rss')

    def test_a_long_article_shelved_mid_body_stops_buying_the_rest(self):
        before = self.row(self.mine)
        bought, summary = self.run_round(shelve_at=1)
        # 第一段那一笔已经花了(拦不住),**余下几段一笔都没发**。
        self.assertEqual([1], bought)
        self.assertEqual(1, summary['shelved'])
        self.assertEqual(0, summary['done'])
        # 译文一个字都没落库:半篇译文入库比没有更糟。
        self.assertEqual(before['translated_at'], self.row(self.mine)['translated_at'])
        self.assertEqual(before['body'], self.row(self.mine)['body'])
        # 额度也没扣 —— 这不是一次失败(恢复之后它回到队列照旧译)。
        self.assertEqual(before['translate_tries'], self.row(self.mine)['translate_tries'])

    def test_the_same_article_buys_every_chunk_when_nobody_shelves_it(self):
        """**反过来钉一边**:不搁置时每一段都照旧买,而且不止一段。

        只钉上面那条的话,「整条翻译都不发了」也是全绿 —— 而那样一篇长文永远译不完。
        """
        bought, summary = self.run_round(shelve_at=None)
        self.assertGreater(len(bought), 1)
        self.assertEqual(1, summary['done'])
        self.assertEqual(0, summary['shelved'])
        self.assertNotEqual('', self.row(self.mine)['translated_at'])
