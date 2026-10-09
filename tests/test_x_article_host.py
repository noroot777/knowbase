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

**这道闸按候选带不带 scheme 分两档,所以 `PunctuationLeadInTests` 管的是另一头**:
把两档合成一档(一律禁"URL 内部字符紧贴")会让 `新文章...<地址>` 这种**真**地址
被自己前面那个标点挡住 —— 判据① 一条都不命中,那条链接本身当成正文入库,而
`DO NOTHING` 之下重复同步也补不回来(2026-10-09 实测,两种前导 × 两条保留通道)。
那一族里也反过来钉着:放行正文标点不许顺带放行别人家的地址,嵌套的 X 文章地址
照旧一个字不动。

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
        ('我写了一篇新文章...', '英文省略号紧贴 —— 第 3 轮审查那个样本'),
        ('新文章~', '波浪号紧贴 —— 同一轮的第二个样本'),
        ('新文章--', '英文破折号'),
        ('Check it out...', '英文词 + 省略号:连左边那个字母一起落在同一段里'),
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

    # 裸域名形式的同一条地址(没有 scheme),用来直测第二档。
    BARE = 'x.com/Example/article/10001'

    def test_the_scheme_arm_only_rejects_url_structure_separators(self):
        """直测第一档:`https://` 自带起点宣告,所以只排"嵌在另一条 URL 里"。

        嵌套只发生在外层 URL 的结构位置(查询值、片段、路径段),那几个位置的紧前
        一个字符必然是 `=` `&` `?` `#` `/` 之一。**正文标点一律放行** —— 把它们
        也禁掉的实现会让 `新文章...<地址>` 这种真地址漏认,而漏认 = 空正文永久
        固定(第 3 轮审查那份证据)。
        """
        hit = x_article._ARTICLE_LINK.search
        for lead in ('.', '..', '...', '~', '-', ':', 'x', '9', '@', '%', '+', '_',
                     '中文', '', ' ', '('):
            with self.subTest(ok=lead):
                self.assertTrue(hit(lead + CANON),
                                '带 scheme 的真地址不许被正文标点挡住')
        for bad in ('=', '&', '?', '#', '/'):
            with self.subTest(bad=bad):
                self.assertIsNone(hit(bad + CANON),
                                  'URL 的结构分隔符后面是一条嵌套地址,不是我们的')

    def test_the_bare_arm_still_rejects_every_url_character(self):
        """直测第二档:裸域名的起点就是主机名第一个字符,禁止集一个都不许少。

        它和分档之前那道闸**逐字相同** —— 域名后缀(`fx`/`not`/`help.`/`my-`)和
        路径嵌套全靠它挡着,放宽一个字符就会把**别人的文章**记成这条推文的内容。
        `:` 两档都放行,所以它单列在下面那条用例里。
        """
        hit = x_article._ARTICLE_LINK.search
        self.assertTrue(hit(f'中文{self.BARE}'), '汉字前导不许挡住真地址')
        self.assertTrue(hit(f'见:{self.BARE}'))
        self.assertTrue(hit(self.BARE))
        for bad in ('x', '.', '/', '-', '=', '?', '#', '@', '%', '&', '~', '+',
                    '_', '9'):
            with self.subTest(bad=bad):
                self.assertIsNone(hit(bad + self.BARE),
                                  'URL 内部字符后面不是主机名起点')


class PunctuationLeadInTests(FeedgrabRound):
    """起点那道闸不许把紧贴**正文标点**的真地址挡住(第 3 轮审查那条)。

    `.` 和 `~` 有两个互相冲突的身份:在 `help.x.com` / `my-x.com` 里它们是域名的
    一部分,在 `新文章...<地址>` 里它们是作者的标点。合成一档去禁"URL 内部字符
    紧贴"的后果实测过:`needs_article` 三条判据一条都不命中,那条链接本身当成正文
    入库 —— `added=1`、`errors=[]`、`partial=[]`、水位照常推进、标题回落成
    「某某 的动态」,而 `DO NOTHING` 之下重复同步也补不回来。

    **后果先断言**:只验"问过那次接口"的话,一个把链接当正文存下来、却顺手问了
    一次的实现也能过。
    """

    feedgrab_round = ArticlePathFormTests.feedgrab_round
    browser_round = ArticlePathFormTests.browser_round

    # 两个前导逐字取自审查报告的输入。
    LEAD_INS = ['我写了一篇新文章...', '新文章~']

    def check_feedgrab(self, lead):
        """真跑一轮 feedgrab:库里那一行是全文,而且那个标点自己也还在。

        一轮只建一个订阅,所以两个样本各走一次完整的摆盘(`subTest` 循环会撞上
        「这个链接已经在订阅列表中」)。
        """
        summary, row, calls, s = self.feedgrab_round(f'{lead}{CANON}')
        got = row.get('body') or ''
        self.assertIn(FULL, got, '标点紧贴时长文全文没补进来')
        self.assertNotIn(CANON, got, '那条地址照旧要摘掉')
        self.assertIn(lead, got, '作者写的那几个字一个都不许少')
        self.assertEqual('X书签保存Skill x Teamily', row.get('title'),
                         '标题回落成「某某 的动态」= 压根没去补过')
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(1, len(calls.urls), '根本没去问那一次')

    def test_an_ellipsis_hugging_the_link_still_reaches_the_knowledge_base(self):
        """第 3 轮审查那个样本:`我写了一篇新文章...<地址>`。"""
        self.check_feedgrab('我写了一篇新文章...')

    def test_a_tilde_hugging_the_link_still_reaches_the_knowledge_base(self):
        """同一轮的第二个样本:`新文章~<地址>`。"""
        self.check_feedgrab('新文章~')

    def test_punctuation_hugging_the_link_works_on_the_browser_channel(self):
        """另一条保留通道,而且是**没有卡片信号**那一档。

        有卡片时判据② 会把这一条救回来,漏判就看不出来了 —— 正文里那条地址本来
        就该是独立的第二道信号。
        """
        for lead in self.LEAD_INS:
            with self.subTest(lead=lead):
                got, calls = self.browser_round(
                    f'{lead}https://x.com/example/article/9')
                text = got['entries'][0]['body']
                self.assertIn(FULL, text, '标点紧贴时长文全文没补进来')
                self.assertNotIn('/example/article/9', text)
                self.assertIn(lead, text)
                self.assertEqual('', got['incomplete'], '补到了,这一轮算读全了')
                self.assertNotEqual(OLD_WATER, got['watermark'])
                self.assertEqual(1, len(calls.urls))

    def test_an_external_address_after_the_same_punctuation_is_still_left_alone(self):
        """**反过来钉一边**:放行正文标点不许顺带放行别人家的地址。

        这两件事靠的是不同的那一档 —— 外部地址被裸域名那一档的 `fx`/`not`/`help.`
        挡着,跟它前面是什么标点没有关系。写成"标点后面一律放行"的实现会在这里红。
        """
        for lead in self.LEAD_INS:
            for url, why in HostBoundaryFormTests.OUTSIDERS:
                with self.subTest(lead=lead, why=why):
                    body = f'我在读这一篇{lead}{url}'
                    self.assertEqual(body, lead_text(body))
                    self.assertFalse(needs_article({'url': TWEET, 'body': body}))

    def test_an_x_article_nested_in_someone_elses_url_is_still_left_alone(self):
        """放宽第一档之后新的风险点:**嵌套**的 X 文章地址照旧一个字不许动。

        它靠的是那五个结构分隔符(`=` `&` `?` `#` `/`)—— 真实的嵌套入口全在里面。
        把它们也放行的实现会把别人那条地址从中间剪断,而剪断永久固定。
        """
        for url in (f'https://evil.example/?u={CANON}',
                    f'https://evil.example/p#{CANON}',
                    f'https://evil.example/?a=1&u={CANON}',
                    f'https://evil.example/go/{CANON}'):
            with self.subTest(url=url):
                self.assertEqual(url, lead_text(url))
                self.assertFalse(needs_article({'url': TWEET, 'body': url}))


if __name__ == '__main__':
    unittest.main()
