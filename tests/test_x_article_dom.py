# -*- coding: utf-8 -*-
"""两段**在页面里跑**的 JS:文章页的正文读取(`x_article._READ_JS`)和时间线的
条目收集(`x_browser._COLLECT_JS`)。

**为什么非得起一个真浏览器**:别处那些用例的假 CDP 会话是直接交出一份现成的
`blocks` / `items`,压根不执行这两段 JS —— 所以 DOM 解析写错了它们全是绿的
(行内表情吃掉整段文字那次就是这么漏过去的)。这一组把 DOM 摆成 Draft.js 和
X 时间线的真实形状,在一个**独立临时 profile 的无头壳**里真跑一遍,比的是
「页面上看得见的字」和「JS 交出来的东西」逐字对得上。

不碰用户日常那个浏览器:自己起、自己收,profile 在 tmp 里。

跑:  python -m unittest tests.test_x_article_dom -v
"""
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from kb.feeds import x_article, x_browser
from kb.feeds.x_cdp import Chrome
from tests.headless import INSTALL_HINT, SHELL
from tests.test_x_article import _FakeChrome, snapshot

_NEEDS_SHELL = unittest.skipUnless(
    SHELL is not None, '缺 chrome-headless-shell —— ' + INSTALL_HINT)

EMOJI = 'https://abs-0.twimg.com/emoji/v2/svg/1f642.svg'
PHOTO = 'https://pbs.twimg.com/media/HUIE7DSbEAEJXcR.jpg'

# Draft.js 的真实形状:每个段落一个 `[data-block="true"]`,文字包在
# `[data-text="true"]` 里,表情是一个带 alt 的 `<img>`(twemoji),配图单独
# 占一个 atomic 块。另外摆两样容易出错的:`display:none` 的辅助节点(innerText
# 不会带它,我们按子节点遍历就得自己跳过)和一个 `<br>`。
ARTICLE_HTML = f'''<!doctype html><meta charset="utf-8"><title>Article</title>
<div data-testid="twitterArticleReadView">
  <h1 style="font-size:34px">X书签保存Skill x Teamily</h1>
  <div data-testid="twitterArticleRichTextView" style="font-size:15px">
    <div data-contents="true">
      <div data-block="true"><span data-offset-key="a0"><span data-text="true"
        >第一步，保存你真正想读的资料。</span></span><img alt="🙂" src="{EMOJI}"
        ><span data-offset-key="a1"><span data-text="true"
        >然后安排每天二十分钟完成练习，别只收藏不行动。</span></span></div>
      <div data-block="true"><span data-text="true">第二段正文独立成段。</span></div>
      <div data-block="true"><figure><img src="{PHOTO}"></figure></div>
      <div data-block="true"><span data-text="true">第三段</span><br
        ><span data-text="true">换行之后还在同一段里。</span></div>
      <div data-block="true"><span data-text="true">第四段。</span
        ><span style="display:none">这句是辅助节点，不算正文</span></div>
      <div data-block="true"><span data-text="true">&#8203;</span></div>
    </div>
  </div>
</div>'''

# X 时间线的真实形状。三条:①写了介绍 + 一张长文卡片;②引用了**别人**的长文
# (卡片在 `div[role="link"]` 这个引用容器里);③普通推文带一条 t.co 短链。
TIMELINE_HTML = '''<!doctype html><meta charset="utf-8"><title>Timeline</title>
<article data-testid="tweet">
  <div data-testid="User-Name">Example Person\n@example</div>
  <time datetime="2026-10-09T08:01:00Z">8:01</time>
  <a href="/example/status/900">link</a>
  <div data-testid="tweetText">我写了一篇新文章，欢迎阅读。</div>
  <a href="/i/article/123456789"><div>X书签保存Skill x Teamily</div></a>
</article>
<article data-testid="tweet">
  <div data-testid="User-Name">Example Person\n@example</div>
  <time datetime="2026-10-09T07:00:00Z">7:00</time>
  <a href="/example/status/901">link</a>
  <div data-testid="tweetText">看看这个</div>
  <div role="link"><a href="/i/article/555000111"><div>别人的文章</div></a></div>
</article>
<article data-testid="tweet">
  <div data-testid="User-Name">Example Person\n@example</div>
  <time datetime="2026-10-09T06:00:00Z">6:00</time>
  <a href="/example/status/902">link</a>
  <div data-testid="tweetText">随手一条 https://t.co/abcdef</div>
</article>'''


@_NEEDS_SHELL
class DomTests(unittest.TestCase):
    """一次浏览器启动跑完两段 JS(起一个壳要两三秒)。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / 'article.html').write_text(ARTICLE_HTML, encoding='utf-8')
        (root / 'timeline.html').write_text(TIMELINE_HTML, encoding='utf-8')
        cls.root = root
        # chrome-headless-shell 自己就是无头的,不要再加 --headless=new。
        cls.page = Chrome(str(root / 'profile'), headless=False, chrome_path=SHELL)
        cls.page.__enter__()
        cls.page.open_page('about:blank')

    @classmethod
    def tearDownClass(cls):
        cls.page.__exit__(None, None, None)
        cls.tmp.cleanup()

    def open(self, name, ready='[data-block="true"], article'):
        """打开 fixture,等到**被测的那个 DOM 结构**出现为止。

        **判据不是 `document.readyState == 'complete'`。** 那一档要等所有子资源,
        而 fixture 里那两张图(`EMOJI` / `PHOTO`)指着**真实外网地址** —— 于是这条
        等待的耗时取决于能不能连上 twimg,跑测试的机器离线或者网络抖一下就报
        「页面没加载完」,栈停在这里、压根没走到断言,看着像被测的那两段 JS 回归了
        (2026-10-09 实测:同一个 fixture 连开三次,`goto` 都是 0.01 秒,而等到
        `complete` 用了 2.60 / 0.42 / 0.65 秒,**首态已经是 `interactive`**;那条
        4 秒上限因此是概率性越线的)。
        而这几条用例读的是 DOM 文本和 `img` 的 `alt` / `src` **属性**,图片加载
        成不成功一点关系都没有 —— 被测的 `browser_fetch` 自己也不等 `complete`,
        它只等自己要的那几个节点。
        """
        self.page.goto((self.root / name).as_uri())
        probe = f'document.readyState !== "loading" && !!document.querySelector({ready!r})'
        for _ in range(100):
            if self.page.eval(probe):
                return
            time.sleep(0.05)
        self.fail('页面没加载完')

    # ── 文章页正文 ──────────────────────────────────────────────────────────

    def test_an_inline_emoji_does_not_swallow_the_paragraph(self):
        """**后果先断言**:那一整段字都在,表情按它的 alt 还原。

        红的那一版把这一段换成了一张小图(`![](…emoji…)`),而且照样报成功 ——
        缺字的正文一旦落库就改不回来(`put_items` 是 DO NOTHING)。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        self.assertIn('第一步，保存你真正想读的资料。', blocks[0])
        self.assertIn('然后安排每天二十分钟完成练习，别只收藏不行动。', blocks[0])
        self.assertIn('🙂', blocks[0], '表情按 alt 还原')
        self.assertNotIn('![](', blocks[0], '行内表情不该写成 markdown 图片')

    def test_a_media_only_block_is_still_an_image(self):
        """反过来钉一边:独立配图块照旧是一张 markdown 图片。"""
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        self.assertIn(f'![]({PHOTO})', blocks)

    def test_the_whole_body_matches_what_the_page_shows(self):
        """整篇:段落边界、`<br>`、隐藏节点、零宽占位块各归各位。"""
        self.open('article.html')
        snap = json.loads(self.page.eval(x_article._READ_JS))
        blocks = snap['blocks']
        self.assertEqual(5, len(blocks), f'零宽占位块不该成段:{blocks}')
        self.assertEqual('第二段正文独立成段。', blocks[1])
        self.assertEqual('第三段\n换行之后还在同一段里。', blocks[3])
        self.assertEqual('第四段。', blocks[4], 'display:none 的辅助节点不进正文')
        self.assertEqual('X书签保存Skill x Teamily', snap['title'])

    def test_the_real_dom_output_becomes_a_complete_article(self):
        """把真浏览器的这份输出交给 `browser_fetch`:正文逐字完整。"""
        self.open('article.html')
        snap = json.loads(self.page.eval(x_article._READ_JS))
        _FakeChrome.reset(snapshot(blocks=snap['blocks'], title=snap['title']))
        got = x_article.browser_fetch(_FakeChrome(), 'https://x.com/example/status/9',
                                      wait=1.0)
        self.assertIn('然后安排每天二十分钟完成练习', got['body'])
        self.assertIn('🙂', got['body'])
        self.assertIn(f'![]({PHOTO})', got['body'])
        self.assertEqual('X书签保存Skill x Teamily', got['title'])
        self.assertEqual(5, len(got['body'].split('\n\n')))

    # ── 时间线的长文卡片 ────────────────────────────────────────────────────

    def collect(self):
        self.open('timeline.html')
        items = json.loads(self.page.eval(x_browser._COLLECT_JS))['items']
        return {i['id']: i for i in items}

    def test_the_card_link_comes_out_of_the_timeline(self):
        """**后果先断言**:这条推文的 entry 带着信号,于是补全那一步会问它。

        红的那一版正文非空、另两条判据都不命中,整篇文章悄悄漏掉。
        """
        got = self.collect()
        self.assertIn('123456789', got['900']['article'])
        out = x_browser._entry(got['900'])
        self.assertIn('123456789', out[x_article.HINT])
        self.assertTrue(x_article.needs_article(out))

    def test_a_quoted_article_is_not_this_tweet_s(self):
        """引用推里的卡片是**别人**的文章 —— 按它去补就是记错归属。"""
        got = self.collect()
        self.assertEqual('', got['901']['article'])
        self.assertFalse(x_article.needs_article(x_browser._entry(got['901'])))

    def test_a_plain_tweet_carries_no_hint(self):
        got = self.collect()
        self.assertEqual('', got['902']['article'])
        out = x_browser._entry(got['902'])
        self.assertNotIn(x_article.HINT, out)
        self.assertFalse(x_article.needs_article(out), '不拿 t.co 当信号')


if __name__ == '__main__':
    unittest.main()
