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

**这道闸分四层,所以另三族管的是另外几头**:
①`PunctuationLeadInTests` —— 两档合成一档(一律禁"URL 内部字符紧贴")会让
  `新文章...<地址>` 这种**真**地址被自己前面那个标点挡住;
②`QuestionMarkLeadInTests` —— 分了档也不够,`?` 和 `#` 在①档里还得按上下文判
  (`_nested`),一律拒的话 `读过这篇吗?<地址>` 同样被挡住(第 4 轮审查那条);
③`OuterPathCharTests` —— 那个上下文判据往左追外层起点时的字符集少认一个合法
  路径字符,外层起点就追不回来,别人那条参考地址被从中间剪断(第 5 轮审查那条)。
①②两种漏认的末态逐字相同:判据① 一条都不命中,那条链接本身当成正文入库,而
`DO NOTHING` 之下重复同步也补不回来(2026-10-09 实测,各两种前导 × 两条保留
通道);③是反方向的同一种永久损坏 —— 删掉的是作者明确附带的外部地址。三族里都
反过来钉着:放行正文标点不许顺带放行别人家的地址、嵌套的 X 文章地址照旧一个字
不动、一次询问都不发,而加宽那个集合也不许把句读后面的真地址一起关掉。

**另拆一个文件**是因为 `test_x_article_gap.py` 已经 603 行,再加一族越过仓库那条
700 行上限;摆盘(`FeedgrabRound`、④族那两个 round helper)仍共用它那一份。

跑:  python -m unittest tests.test_x_article_host -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import sync, x_article
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
# **别人**那篇长文的规范地址,用来摆"嵌在外层 URL 的片段/查询里"那一族。
OTHER = 'https://x.com/Other/article/20002'


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

    @staticmethod
    def hit(text):
        """走**对外那个入口**,不是裸 `.search`。

        起点判据现在是两道:定宽后视在正则里,`?`/`#` 那一道上下文判断在正则
        外面(`_nested`)。只测正则的话后一道压根不在射程里,而 `needs_article`
        判据① 和 `lead_text` 走的都是这个入口。
        """
        return x_article._first(x_article._ARTICLE_LINK, text)

    def test_the_scheme_arm_only_rejects_url_structure_separators(self):
        """直测第一档:`https://` 自带起点宣告,所以只排"嵌在另一条 URL 里"。

        嵌套只发生在外层 URL 的结构位置(查询值、片段、路径段)。**正文标点一律
        放行** —— 把它们也禁掉的实现会让 `新文章...<地址>` 这种真地址漏认,而
        漏认 = 空正文永久固定(第 3 轮审查那份证据)。
        `?` 和 `#` **不在这道定宽后视里**:它们同时也是作者的句读,归 `_nested`
        按上下文判(见下一条),所以这里只剩 `=` `&` `/` 三个。
        """
        for lead in ('.', '..', '...', '~', '-', ':', 'x', '9', '@', '%', '+', '_',
                     '中文', '', ' ', '(', '?', '#'):
            with self.subTest(ok=lead):
                self.assertTrue(self.hit(lead + CANON),
                                '带 scheme 的真地址不许被正文标点挡住')
        for bad in ('=', '&', '/'):
            with self.subTest(bad=bad):
                self.assertIsNone(self.hit(bad + CANON),
                                  'URL 的结构分隔符后面是一条嵌套地址,不是我们的')

    def test_a_question_mark_is_judged_by_what_sits_left_of_it(self):
        """直测 `_nested`:`?` 和 `#` 判的是**左边那一段自己是不是一条 URL**。

        它们有两个身份,光看紧贴的那一个字符分不开:`读过这篇吗?<地址>` 里是作者
        的句读(那条地址是他自己的),`https://example.org/#<地址>` 里是外层 URL 的
        片段符(那条地址是别人家的)。第 4 轮审查那份证据正是前一种被当成了后一种
        —— 判据① 一条都不命中,那条链接本身当成正文入库,`DO NOTHING` 之下重复
        同步也补不回来。

        后两个样本钉的是"**只往左吃 URL 合法字符**,不是数到最近的空白"这个选择:
        外层 URL 早在中文处断了、或者两条地址用逗号紧连时,那条 X 地址是**真的**
        —— 数到空白的实现会把它们一起误拒,而误拒 = 漏认。
        """
        for ok in ('Have you read this?', '读过这篇吗?', 'What#', '第三节#',
                   'Really??', 'v1.2?', 'x=1?',
                   '参考https://example.org/说明?', 'https://example.org/doc,'):
            with self.subTest(ok=ok):
                self.assertTrue(self.hit(ok + CANON),
                                '句读紧贴的是作者自己那条地址')
        for bad in ('https://example.org/#', 'https://example.org?',
                    'www.example.org/a#', 'WWW.example.org/a#',
                    'https://example.org/a:b#', '参考 https://example.org/p#'):
            with self.subTest(bad=bad):
                self.assertIsNone(self.hit(bad + CANON),
                                  '左边是一条 URL,这个 ?/# 是它的结构字符')

    def test_the_bare_arm_still_rejects_every_url_character(self):
        """直测第二档:裸域名的起点就是主机名第一个字符,禁止集一个都不许少。

        它和分档之前那道闸**逐字相同** —— 域名后缀(`fx`/`not`/`help.`/`my-`)和
        路径嵌套全靠它挡着,放宽一个字符就会把**别人的文章**记成这条推文的内容。
        `?` 和 `#` 在这一档里照旧一律拒(**不走** `_nested`):"标点 + 裸域名"被拒
        是故意的取舍 —— 裸写法不是 X 展开出来的形态,给它开口子得把字符集拆成两份
        (`_URL_RUN` 必须留着这两个字符),换一种没有真实触发路径的写法,不值。
        `:` 两档都放行,所以它单列在上面那条用例里。
        """
        for ok in (f'中文{self.BARE}', f'见:{self.BARE}', self.BARE):
            with self.subTest(ok=ok):
                self.assertTrue(self.hit(ok), '汉字/英文冒号前导不许挡住真地址')
        for bad in ('x', '.', '/', '-', '=', '?', '#', '@', '%', '&', '~', '+',
                    '_', '9'):
            with self.subTest(bad=bad):
                self.assertIsNone(self.hit(bad + self.BARE),
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

    def check_browser(self, lead):
        """另一条保留通道,而且是**没有卡片信号**那一档。

        有卡片时判据② 会把这一条救回来,漏判就看不出来了 —— 正文里那条地址本来
        就该是独立的第二道信号。
        """
        got, calls = self.browser_round(f'{lead}https://x.com/example/article/9')
        text = got['entries'][0]['body']
        self.assertIn(FULL, text, '标点紧贴时长文全文没补进来')
        self.assertNotIn('/example/article/9', text)
        self.assertIn(lead, text)
        self.assertEqual('', got['incomplete'], '补到了,这一轮算读全了')
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_punctuation_hugging_the_link_works_on_the_browser_channel(self):
        """两个样本各在浏览器通道上走一遍。"""
        for lead in self.LEAD_INS:
            with self.subTest(lead=lead):
                self.check_browser(lead)

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

        四种真实的嵌套入口分走两道判据:`=` `&` `/` 那三条靠 `_SCHEME_HEAD` 那道
        定宽后视,`#`(以及 `?`)那条靠 `_nested` 看一眼左边是不是一条 URL —— 后者
        是第 4 轮之后的事,在那之前 `#` 也在后视里,代价是句末问号紧贴的真地址一起
        被挡住(见 `QuestionMarkLeadInTests`)。放行它们的实现会把别人那条地址从
        中间剪断,而剪断永久固定。
        """
        for url in (f'https://evil.example/?u={CANON}',
                    f'https://evil.example/p#{CANON}',
                    f'https://evil.example/?a=1&u={CANON}',
                    f'https://evil.example/go/{CANON}'):
            with self.subTest(url=url):
                self.assertEqual(url, lead_text(url))
                self.assertFalse(needs_article({'url': TWEET, 'body': url}))


class QuestionMarkLeadInTests(FeedgrabRound):
    """第 4 轮审查那条:**句末英文问号**紧贴时长文照旧补得到。

    `?` 和 `#` 比 `.` `~` 还难办 —— 它们在一条已经开始的 URL 里是查询串/片段的
    分隔符(真实的嵌套入口),在正文里却是作者的句读,而**两头都有真实写法**。
    上一轮把它们留在定宽后视里一律拒,于是
    `Have you read this?<文章地址>` 和 `读过这篇吗?<文章地址>`(中文不带空格,
    半角问号紧贴是常见写法)里那条**真**地址被自己的句末问号挡住:`needs_article`
    三条判据一条都不命中,那条链接本身当成正文入库 —— `added=1`、`errors=[]`、
    `partial=[]`、水位照常推进、标题回落成「某某 的动态」,而 `DO NOTHING` 之下
    重复同步也补不回来(2026-10-09 实测,两种路径形式、两条通道逐字一样)。

    **后果先断言**:只验"问过那次接口"的话,一个把链接当正文存下来、却顺手问了
    一次的实现也能过。摆盘和判据共用 `PunctuationLeadInTests` 那一份 —— 两族验的
    是同一道闸的两类前导字符,各写一份必然漂。
    """

    feedgrab_round = ArticlePathFormTests.feedgrab_round
    browser_round = ArticlePathFormTests.browser_round
    check_feedgrab = PunctuationLeadInTests.check_feedgrab
    check_browser = PunctuationLeadInTests.check_browser

    # 前两个逐字取自第 4 轮审查报告的输入,`What#` 是同一道闸上的另一个字符。
    LEAD_INS = ['Have you read this?', '读过这篇吗?', 'What#']

    def test_an_english_question_mark_hugging_the_link_reaches_the_knowledge_base(self):
        """审查报告那个样本:`Have you read this?<地址>`。"""
        self.check_feedgrab('Have you read this?')

    def test_a_question_mark_after_chinese_text_reaches_the_knowledge_base(self):
        """同一轮的第二个样本 —— 中文不带空格,所以这种写法最常见。"""
        self.check_feedgrab('读过这篇吗?')

    def test_a_hash_hugging_the_link_reaches_the_knowledge_base(self):
        """`#` 和 `?` 是同一道闸上的同一个二义性,所以它也得有自己那一条。"""
        self.check_feedgrab('What#')

    def test_a_question_mark_works_on_the_browser_channel(self):
        """另一条保留通道,同样是**没有卡片信号**那一档。"""
        for lead in self.LEAD_INS:
            with self.subTest(lead=lead):
                self.check_browser(lead)

    def test_an_address_in_someone_elses_fragment_is_never_asked_about(self):
        """**反过来钉一边(端到端)**:嵌在别人片段里的那条地址,一次都不问。

        这是放宽 `#` 之后新的风险面:它从定宽后视里拿掉了,只剩 `_nested` 挡着。
        判据不能只是"正文没被改" —— 那条地址被误认时还会白买一次询问,而答复会
        是别人那篇文章的全文,真写进库里就是把**别人的内容**记成这条推文的观点。
        """
        body = f'参考 https://evil.example/p#{CANON}'
        summary, row, calls, s = self.feedgrab_round(body)
        self.assertEqual([], calls.urls, '别人家的地址不许触发一次询问')
        self.assertEqual(body, (row.get('body') or '').strip(),
                         '别人那条地址一个字都不许少')
        self.assertNotIn(FULL, row.get('body') or '', '别人的全文不许写进来')
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual(TODAY, self.water(s['id']))


class OuterPathCharTests(FeedgrabRound):
    """第 5 轮审查那条:外层 URL 的**合法路径字符**不许让 `_nested` 失效。

    `_nested` 回答的是"紧贴这个 `?`/`#` 左边那一段是不是一条 URL",办法是往左吃
    一段 URL 合法字符再找起手式 —— 所以**那个字符集少认一个合法字符,外层起点就
    追不回来**:`_nested` 误答"不是嵌套",于是别人家片段/查询里那条 X 文章地址被
    当成这条推文自己的地址,`_drop` 把它删掉,别人那条参考地址从中间被剪断。
    `put_items` 的 `DO NOTHING` 让这种删除**永久固定**,重复同步也补不回来。

    原来这个集合复用②档的禁止集 `_URL_CHARS` 再补一个 `:`,于是 RFC 3986 里
    `(` `)` `;` `$` `\'` `,` `!` `*` 这八个合法路径字符一个都不认(2026-10-09 实测:
    `…/docs(v2)#<别人的文章地址>` 在库里和导出的 md 里都只剩 `…/docs(v2)#`,而
    `added=1`、`errors=[]`、`partial=[]`、水位照常推进、一行提示都没有)。

    **两头都要钉**:只验"外部地址还在"的话,一个压根不摘地址的实现也能全绿;而
    把这个集合加宽**加过头**(比如把汉字也收进来)会反过来让句读紧贴的真地址被判
    成嵌套 —— 那是漏认、空正文永久固定,所以反钉那条用例一条都不许少。
    """

    LEAD = '我写了一篇新文章：'
    feedgrab_round = ArticlePathFormTests.feedgrab_round
    browser_round = ArticlePathFormTests.browser_round

    # 八个合法路径字符各一条,外加三条本来就判得对的作对照(普通片段、路径里的
    # `:`、百分号编码的中文路径)—— 后三条让这套用例分得出"少认了几个字符"和
    # "整道判据没了"。
    OUTER = [
        ('https://example.org/docs(v2)#' + OTHER, '括号'),
        ('https://example.org/path;v=2?' + OTHER, '分号'),
        ('https://example.org/$docs#' + OTHER, '美元号'),
        ("https://example.org/a'q'#" + OTHER, '单引号'),
        ('https://example.org/a,b?' + OTHER, '逗号'),
        ('https://example.org/a!b#' + OTHER, '叹号'),
        ('https://example.org/a*b?' + OTHER, '星号'),
        ('https://example.org/go(1)/x?' + OTHER, '括号在路径段里'),
        ('https://example.org/p#' + OTHER, '普通片段'),
        ('https://example.org/a:b?' + OTHER, '路径里的冒号'),
        ('https://example.org/%E6%96%87%E6%A1%A3#' + OTHER, '百分号编码的路径'),
    ]

    def check_feedgrab(self, ref):
        """真跑一轮 feedgrab,**连导出的那篇 md 一起核**。

        报告那份证据两处都看了:库里那一行和 vault 里那篇 —— 后者才是用户真正
        读到的东西,而它是 `sync.export` 另写一遍的,少钉一处就少一个消费方。
        """
        summary, row, calls, s = self.feedgrab_round(f'{self.LEAD}{CANON}\n参考：{ref}')
        got = row.get('body') or ''
        self.assertIn(ref, got, '别人那条参考地址被从中间剪断了')
        self.assertNotIn(CANON, got, '而真的那条照旧要摘掉')
        self.assertIn(FULL, got, '这条推文自己的全文还是要补上')
        self.assertEqual('X书签保存Skill x Teamily', row.get('title'),
                         '标题回落成「某某 的动态」= 压根没去补过')
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(1, len(calls.urls), '只问这条推文自己那一次')
        sync.export('social')
        mds = list(sync.VAULT_ROOT.rglob('*.md'))
        self.assertEqual(1, len(mds), mds)
        md = mds[0].read_text(encoding='utf-8')
        self.assertIn(ref, md, '导出的 md 里那条参考地址也被剪断了')
        self.assertIn(FULL, md)

    def test_parentheses_in_the_outer_path_keep_the_reference_intact(self):
        """审查报告第一组输入:`…/docs(v2)#<别人的文章地址>`。"""
        self.check_feedgrab('https://example.org/docs(v2)#' + OTHER)

    def test_a_semicolon_in_the_outer_path_keeps_the_reference_intact(self):
        """审查报告第二组输入:`…/path;v=2?<别人的文章地址>`。"""
        self.check_feedgrab('https://example.org/path;v=2?' + OTHER)

    def test_the_browser_channel_keeps_it_intact_too(self):
        """另一条保留通道,而且是**没有卡片信号**那一档。"""
        ref = 'https://example.org/docs(v2)#' + OTHER
        got, calls = self.browser_round(
            f'{self.LEAD}https://x.com/example/article/9\n参考：{ref}')
        text = got['entries'][0]['body']
        self.assertIn(ref, text, '别人那条参考地址被从中间剪断了')
        self.assertNotIn('/example/article/9', text, '而真的那条照旧要摘掉')
        self.assertIn(FULL, text)
        self.assertEqual('', got['incomplete'], '补到了,这一轮算读全了')
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_every_legal_path_character_still_hides_the_nested_address(self):
        """逐个形态直测两个消费方:正文一个字不许动,判据① 也不许为真。

        后半句是同一道缺口的另一半后果 —— 误认之后每条推文白买一次询问,而答复
        会是**别人那篇文章**的全文,真写进库里就是把别人的内容记成这条推文的观点。
        """
        for url, why in self.OUTER:
            with self.subTest(why=why):
                body = f'参考：{url}'
                self.assertEqual(body, lead_text(body))
                self.assertFalse(needs_article({'url': TWEET, 'body': body}))

    def test_widening_the_run_does_not_swallow_a_real_address(self):
        """**反过来钉一边**:加宽那个集合不许把句读紧贴的真地址判成嵌套。

        这一族和 `QuestionMarkLeadInTests` 是同一道判据的两个方向:那边要的是
        "句读后面的真地址认得出",这边要的是"外层 URL 内部的地址认不出"。往这个
        集合里多塞一个**正文里也会出现**的字符(汉字、空白)就会把前者一起关掉,
        而那是漏认 = 空正文永久固定。
        """
        for lead in ('Have you read this?', '读过这篇吗?', 'What#', '第三节#',
                     'Really??', 'v1.2?', 'x=1?', '完(1)?', '见[2]#', '算了!?',
                     '参考https://example.org/说明?', 'https://example.org/doc,'):
            with self.subTest(lead=lead):
                self.assertTrue(x_article._first(x_article._ARTICLE_LINK,
                                                 lead + CANON),
                                '句读紧贴的是作者自己那条地址')
                self.assertNotIn(CANON, lead_text(lead + CANON),
                                 '认出来了就得摘掉')

    def test_a_non_ascii_outer_path_is_a_known_gap(self):
        """**已知留着的一道缝**,如实钉成当前取舍:非 ASCII 的外层路径。

        汉字加不进那个集合 —— `…/文档#<地址>`(这里)和
        `参考https://example.org/说明?<地址>`(上一条用例里那个**真**地址)
        **完全同形**:都是"往左吃到一个汉字就断"。两个方向都是永久损坏,所以这不是
        "哪个对"的问题;线划在 ASCII 上的理由是它可解释(非 ASCII 不是 URL 的原生
        形态,浏览器要先百分号编码,而编码之后的写法上一条用例里钉着)。
        **改方向时这两条用例要一起改**,只改一条就是把另一头静默打开。
        """
        body = '参考：https://example.org/文档#' + OTHER
        self.assertNotIn(OTHER, lead_text(body),
                         '这一档目前判成"不是嵌套",改了就要一起改反钉那条')


if __name__ == '__main__':
    unittest.main()
