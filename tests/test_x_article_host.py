# -*- coding: utf-8 -*-
"""长文地址的**主机名起点边界**:别人家的地址不许被当成 X 文章链接剪断。

`_ARTICLE_LINK` / `_TCO` 里的 `x.com` / `twitter.com` / `t.co` 都只是一段字符,
**压根不要求它是主机名**。不加起点那道闸时,一条合法的外部地址只要内部出现这一段
就会从中间被当成起点、后半截被 `lead_text` 删掉 —— 而 `put_items` 是
`ON CONFLICT(key) DO NOTHING`,**这种删除永久固定**,重复同步也补不回来
(2026-10-09 实测:`参考:https://fxtwitter.com/Example/article/10001` 在库里和
导出的 md 里都只剩 `https://fx`,而 `added=1`、`errors=[]`、`partial=[]`、水位
照常推进,一行提示都没有)。同一道缺口还让 `needs_article` 判据① 把这些推文误判
成"要去问一次长文",每条白跑一次询问。

**和 `test_x_article_gap.py` ⑤族(尾巴)是同一句话的两头**:那边管"地址右边紧挨着
的标点和文字不许跟着走",这边管"地址左边必须是主机名的起点"。两边的代价也一样
——多删一个字符 = 作者的字永久消失。

**两头都要钉**:只验"外部地址还在"的话,一个压根不摘地址的实现也能全绿;只验
"真地址摘掉了"的话,被审那一版照样过。所以每一条端到端同时断言三件事(外部地址
逐字在、真地址摘掉了、全文补上了),另有一族反过来钉住**各种前导字符后面的真
地址照旧摘得掉、判据① 照旧为真** —— 其中英文冒号那条是"`:` 故意不在禁止集里"
那个取舍的射程,把它加进禁止集的实现会在那里红。

**另拆一个文件**是因为 `test_x_article_gap.py` 已经 603 行,再加一族越过仓库那条
700 行上限;摆盘(`FeedgrabRound`、④族那两个 round helper)仍共用它那一份。

跑:  python -m unittest tests.test_x_article_host -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import x_article
from kb.feeds.x_article import lead_text, needs_article
from tests.test_x_article import OLD_WATER
from tests.test_x_article_gap import FULL, ArticlePathFormTests
from tests.test_x_feedgrab_contract import TODAY, FeedgrabRound

# 推文自己的地址,`needs_article` 要靠它才会往下判。
TWEET = 'https://x.com/Example/status/10001'
# 这条推文的规范文章地址(= `article_page(TWEET)`),是真要摘掉的那一条。
CANON = 'https://x.com/Example/article/10001'
# 审查那份证据里的外部参考地址:`fx` 后面那一段逐字就是
# `twitter.com/<handle>/article/<id>`,所以它是最容易被当成起点的一种。
MIRROR = 'https://fxtwitter.com/Example/article/10001'


class HostBoundaryTests(FeedgrabRound):
    """真跑两条保留通道的一轮采集,看库里那条外部地址还在不在。"""

    LEAD = '我写了一篇新文章：'
    PROSE = '，重点看第三节。'
    REF = '\n参考：' + MIRROR

    # 跟④⑤族逐字相同的两个摆盘,直接借过来而不是复制一份。
    feedgrab_round = ArticlePathFormTests.feedgrab_round
    browser_round = ArticlePathFormTests.browser_round

    def test_an_external_reference_survives_feedgrab(self):
        """**后果先断言**:库里那一行还带着整条参考地址。

        这是审查那份证据的回归版(`https://fxtwitter.com/…` → `https://fx`),
        真跑一轮 `sync.collect()`,外部程序和入库都是真的。三句断言缺一不可:
        少了"真地址摘掉了"那句,一个压根不摘的实现也能过;少了"全文补上了"那句,
        一个顺手把整条判据关掉的实现也能过。水位照常推进尤其要紧 —— 它意味着
        下一轮不会再碰这一条。
        """
        body = f'{self.LEAD}{CANON}{self.PROSE}{self.REF}'
        summary, row, calls, s = self.feedgrab_round(body)
        got = row.get('body') or ''
        self.assertIn(MIRROR, got, '别人家的地址被当成 X 文章链接剪断了')
        self.assertNotIn(CANON, got, '而真的那条照旧要摘掉')
        self.assertIn(FULL, got, '全文还是要补上')
        self.assertIn(self.LEAD, got)
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(1, len(calls.urls))

    def test_an_external_reference_survives_the_browser_channel(self):
        """另一条保留通道:同一条正则,所以两条各钉一条端到端。"""
        body = f'{self.LEAD}https://x.com/example/article/9{self.PROSE}{self.REF}'
        got, calls = self.browser_round(body)
        text = got['entries'][0]['body']
        self.assertIn(MIRROR, text, '别人家的地址被当成 X 文章链接剪断了')
        self.assertNotIn('/example/article/9', text, '而真的那条照旧要摘掉')
        self.assertIn(FULL, text)
        self.assertEqual('', got['incomplete'], '补到了,这一轮算读全了')
        self.assertNotEqual(OLD_WATER, got['watermark'])

    def test_a_tweet_with_only_an_external_reference_is_not_asked_about(self):
        """只有外部参考地址的普通推文:一次询问都不许发,正文一个字不许动。

        这是同一道缺口的另一半后果 —— 判据① 误判之后每条推文白买一次调用,
        而答复多半是"它没有长文"。
        """
        body = f'我在读这一篇：{MIRROR}'
        summary, row, calls, s = self.feedgrab_round(body)
        self.assertEqual([], calls.urls, '它不是 X 的文章地址,不许去问')
        self.assertEqual(body, row.get('body'))
        self.assertEqual(1, summary['added'])
        self.assertEqual(TODAY, self.water(s['id']))


class HostBoundaryFormTests(unittest.TestCase):
    """逐个形态直测两头:哪些该原样留着、哪些该照旧摘掉。"""

    # 六种合法的外部地址,每一种都因为内部含着那一段而被剪断过(2026-10-09 实测,
    # 前四条逐字取自审查报告)。
    OUTSIDERS = [
        (MIRROR, '镜像站:`fx` 后面逐字就是 twitter.com/<handle>/article/<id>'),
        ('https://notx.com/Example/article/10001', '域名后缀撞上 x.com'),
        ('https://help.x.com/Example/article/10001', '子域名'),
        ('https://example.org/docs/x.com/Example/article/10001',
         '真实主机名是 example.org,x.com 只是它路径里的内容'),
        ('https://my-x.com/Example/article/10001', '连字符接在前面'),
        ('https://fxtwitter.com/i/article/2108252660529049601', '旧式形式同理'),
    ]
    # t.co 那条共用同一道闸:它要求 `https://` 紧贴,所以漏的是"嵌在别人查询串里"。
    TCO_OUTSIDERS = [
        'https://evil.example/?u=http://t.co/aBcD12&next=1',
        'https://evil.example/p#t.co/aBcD12',
    ]

    def test_external_addresses_are_left_alone(self):
        """**后果**:这些地址一个字符都不许少 —— 删掉的部分永久补不回来。"""
        for url, why in self.OUTSIDERS:
            with self.subTest(why=why):
                self.assertEqual(url, lead_text(url))

    def test_external_addresses_do_not_trigger_a_lookup(self):
        """同一批输入也不许让判据① 为真(白跑一次询问)。"""
        for url, why in self.OUTSIDERS:
            with self.subTest(why=why):
                self.assertFalse(needs_article({'url': TWEET, 'body': url}))

    def test_a_t_co_inside_someone_elses_url_is_left_alone(self):
        """`_TCO` 共用这道闸,所以它那两种形态各钉一条。"""
        for url in self.TCO_OUTSIDERS:
            with self.subTest(url=url):
                self.assertEqual(url, lead_text(url))

    # --- 反过来钉一边:真地址在各种前导字符后面照旧摘得掉 --------------------

    LEAD_INS = [
        ('我写了一篇新文章：', '中文冒号(最常见的一种)'),
        ('我写了', '紧跟汉字 —— 禁止集写成 `\\w` 的实现会在这里红'),
        ('', '行首'),
        ('见 ', '空格'),
        ('(', '英文开括号'),
        ('【', '中文方括号'),
        ('前一句。', '中文句号'),
        ('第一条 https://x.com/Example/article/1,', '英文逗号接在另一条地址后面'),
    ]

    def test_a_real_article_link_is_still_stripped_after_any_lead_in(self):
        """**反过来钉一边**:收窄起点不许把真地址弄成摘不掉。

        漏认的代价比多删还重一个量级 —— 那条地址会当成正文入库,`DO NOTHING`
        之下那一篇永久只有一个链接。
        """
        for lead, why in self.LEAD_INS:
            with self.subTest(why=why):
                body = f'{lead}{CANON}'
                self.assertNotIn('/article/', lead_text(body), '那条地址没摘掉')
                self.assertTrue(needs_article({'url': TWEET, 'body': body}),
                                '判据① 也该为真,不然这一条压根不会去补')

    def test_a_real_article_link_after_an_ascii_colon_is_still_stripped(self):
        """`:` **故意不在禁止集里**,这条用例就是那个取舍的射程。

        `参考:<地址>` 这种英文冒号紧贴的写法里那条地址是真的,把 `:` 加进禁止集
        就摘不下来了(而漏认 = 空正文永久固定)。代价是某条外部地址正好在 `:`
        后面嵌一个 X 文章地址时照旧会被摘 —— 那比漏认轻。
        """
        body = f'参考:{CANON}'
        self.assertEqual('参考:', lead_text(body))
        self.assertTrue(needs_article({'url': TWEET, 'body': body}))

    def test_a_real_t_co_short_link_is_still_stripped(self):
        """再钉一边:`_TCO` 那条也不许因为加了起点闸就摘不掉真短链。"""
        self.assertEqual('短链   完', lead_text('短链 https://t.co/aBcD1234 完'))

    def test_the_guard_is_a_fixed_width_lookbehind_on_ascii_only(self):
        """直测那道闸本身:汉字不在禁止集里,URL 内部字符在。

        和上面那些走 `lead_text` / `needs_article` 的用例各管一头 —— 判据写错时
        一个报"这一处该不该摘",一个报"这道闸自己认了什么"。
        """
        hit = x_article._ARTICLE_LINK.search
        self.assertTrue(hit(f'中文{CANON}'), '汉字前导不许挡住真地址')
        self.assertTrue(hit(f'见:{CANON}'))
        self.assertTrue(hit(CANON))
        for bad in ('x' + CANON, '.' + CANON, '/' + CANON, '-' + CANON,
                    '=' + CANON, '?' + CANON, '#' + CANON, '@' + CANON,
                    '%' + CANON, '&' + CANON, '~' + CANON, '+' + CANON,
                    '_' + CANON, '9' + CANON):
            with self.subTest(bad=bad[:1]):
                self.assertIsNone(hit(bad), 'URL 内部字符后面不是主机名起点')


if __name__ == '__main__':
    unittest.main()
