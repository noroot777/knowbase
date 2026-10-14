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
from tests.test_x_article_fidelity import DATA_CODE, _SP as SP
from tests.test_x_article import _FakeChrome, snapshot

_NEEDS_SHELL = unittest.skipUnless(
    SHELL is not None, '缺 chrome-headless-shell —— ' + INSTALL_HINT)

EMOJI = 'https://abs-0.twimg.com/emoji/v2/svg/1f642.svg'
PHOTO = 'https://pbs.twimg.com/media/HUIE7DSbEAEJXcR.jpg'
DIAGRAM = 'https://pbs.twimg.com/media/HUIE7DSbEAEJXcS.png'
# 带描述的配图:`alt` 是给人读的说明,不是一个表情字符。
CAPTION = '系统架构示意图'
# 一个 ZWJ 组合表情(女 + ZWJ + 电脑)。删掉 ZWJ 它就裂成两个字符。
COMBO = chr(0x1F469) + chr(0x200D) + chr(0x1F4BB)
# 真实缩进:`if` 下面四格、`return` 下面八格,压成一格就编译不过。
CODE = 'def answer():' + chr(10) + '    if True:' + chr(10) + '        return 42'
# 片段式代码:从一段代码中间截出来的,**首行就带缩进**。收尾用 `.trim()` 的
# 实现会把那一格吃掉,而它决定了这几行在不在一个块里面。
SNIPPET = '    .deep {' + chr(10) + '       color: red;' + chr(10) + '     }'
# 代码里**独占一行**的 ```:围栏长度得避开它,否则它把围栏提前收尾。
NESTED = '示例:' + chr(10) + chr(96) * 3 + chr(10) + '里面' + chr(10) + chr(96) * 3
# **行尾空白可能是数据**:下面这段的第二行末尾有两个空格,它们在三引号字符串
# **里面**。真实 Draft.js 是一行一个 block,所以"每块收尾去空白"就等于"每行
# 收尾去空白" —— 那一版照旧 `compile()` 得过、围栏也在,只有**执行输出**看得
# 出来(2026-10-09 第 4 轮审查实测)。样本和主路那条用例共用一份,别各写一份。
DATA_PRE = '<pre>' + ''.join(
    '<div data-block="true"><span data-text="true">' + ln + '</span></div>'
    for ln in DATA_CODE.split(chr(10))) + '</pre>'
# 组边界那一道:Draft.js 的**占位空块**(零宽)该削,而**一行空格不该** —— 它
# 可能是字符画、定宽表格、多行字符串的内容。后路这一侧有自己那份实现,所以单独
# 再钉一遍:拿 `trim()` 判边界的那一版在主路那条直测用例上是绿的(自查发现)。
PAD_BODY = SP + chr(10) + '|  x  |' + chr(10) + SP
PAD_PRE = '<pre>' + ''.join(
    '<div data-block="true"><span data-text="true">' + ln + '</span></div>'
    for ln in ['&#8203;', SP, '|  x  |', SP, '&#8203;']) + '</pre>'
# **代码块里那几个不可见字符是数据**:不换行空格(U+00A0)、零宽空格(U+200B)、
# BOM(U+FEFF)。归一成普通空格、或者当占位符删掉之后,代码照旧 `compile()` 得过
# 而**字符串的值变了** —— 只有**执行输出**看得出来(2026-10-09 第 1 轮审查的不拦
# 项①:接口那条路原样给出 `[97,160,98,8203,99,65279,100]`,这条路给的是
# `[97,32,98,99,100]`)。源码里写成 HTML 实体,免得文件里躺着三个看不见的字符。
BYTES_SAMPLE = 'a' + chr(0xA0) + 'b' + chr(0x200B) + 'c' + chr(0xFEFF) + 'd'
BYTES_ORDS = '[97, 160, 98, 8203, 99, 65279, 100]'
BYTES_CODE = ("sample = '" + BYTES_SAMPLE + "'" + chr(10)
              + 'assert [ord(c) for c in sample] == ' + BYTES_ORDS)
BYTES_PRE = '<pre>' + ''.join(
    '<div data-block="true"><span data-text="true">' + ln + '</span></div>'
    for ln in ["sample = 'a&#160;b&#8203;c&#65279;d'",
               'assert [ord(c) for c in sample] == ' + BYTES_ORDS]) + '</pre>'
# **组中间整行只有零宽字符时它是数据,不是占位符。** 下面这段三引号字符串里
# 第二、三行各只有一个零宽码点(U+200B / U+FEFF)。逐块判「整行是不是只有零宽」
# 的那一版把它们清成空行 —— 代码照旧 `compile()` 得过、围栏也在,只有**执行
# 输出**看得出来(2026-10-10 第 2 轮审查的不拦项:接口那条路原样保留两个码点,
# 这条路删掉了它们)。**和组边界那一道的区别就是位置**:`PAD_PRE` 里零宽在
# 首尾(那只能是 Draft.js 的占位符),这里在中间(那只能是内容)。
# 三个常量就地定义:源码里写出这几个看不见的字符会让文件本身带上它们。
LF, Z, B = chr(10), chr(0x200B), chr(0xFEFF)
GAP_ORDS = '[8203, 65279]'
GAP_CODE = ('gap = """before' + LF + Z + LF + B + LF + 'after"""' + LF
            + 'assert [ord(c) for c in gap if ord(c) > 127] == ' + GAP_ORDS)
GAP_PRE = '<pre>' + ''.join(
    '<div data-block="true"><span data-text="true">' + ln + '</span></div>'
    for ln in ['gap = """before', '&#8203;', '&#65279;', 'after"""',
               'assert [ord(c) for c in gap if ord(c) > 127] == ' + GAP_ORDS]) + '</pre>'

# Draft.js 的真实形状:每个段落一个 `[data-block="true"]`,文字包在
# `[data-text="true"]` 里,表情是一个带 alt 的 `<img>`(twemoji),配图单独
# 占一个 atomic 块。另外摆上几样容易出错的:`display:none` 的辅助节点
# (innerText 不会带它,我们按子节点遍历就得自己跳过)、一个 `<br>`、一张**带
# 描述**的配图、一段真缩进的代码、一个声明了 `white-space: pre-wrap` 的容器、
# 一个 ZWJ 组合表情、一处只存在于 HTML 源码里的换行缩进(该折叠成空格)、一段
# **真实 Draft.js 形状**的代码块(`<pre>` 包着一行一个 `data-block`),以及一段
# 带**行内** `<code>` 的普通正文(反过来钉一边:那一段不许被包成代码块),外加
# 一段**首行就有缩进**的代码片段。
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
      <div data-block="true"><figure><img alt="{CAPTION}" src="{DIAGRAM}"></figure></div>
      <div data-block="true"><pre><code>{CODE}</code></pre></div>
      <div data-block="true"><div style="white-space:pre-wrap">行一
  行二缩两格</div></div>
      <div data-block="true"><span data-text="true">组合表情:{COMBO}。</span></div>
      <div data-block="true"><span data-text="true">折叠前</span>
        <span data-text="true">折叠后</span></div>
      <div data-block="true"><pre><code>{SNIPPET}</code></pre></div>
      <div data-block="true"><span data-text="true">两个代码块之间的正文。</span></div>
      <div data-block="true"><pre><code>{NESTED}</code></pre></div>
      <div data-block="true"><span data-text="true">装上 </span><code>npm i</code
        ><span data-text="true"> 就能用。</span></div>
      <pre><div data-block="true"><span data-text="true">for i in range(3):</span></div
        ><div data-block="true"><span data-text="true">    print(i)</span></div></pre>
      <div data-block="true"><span data-text="true">下面那段的行尾有空白。</span></div>
      {DATA_PRE}
      <div data-block="true"><span data-text="true">&#8203;</span></div>
      {PAD_PRE}
      <div data-block="true"><span data-text="true">下面那段里有三个看不见的字符。</span></div>
      {BYTES_PRE}
      <div data-block="true"><span data-text="true">下面那段里有两行只有零宽字符。</span></div>
      {GAP_PRE}
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
        # 建完立刻登记(理由同 test_x_article_fidelity:setUpClass 抛异常时
        # tearDownClass 不跑,只跑 doClassCleanups)
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        (root / 'article.html').write_text(ARTICLE_HTML, encoding='utf-8')
        (root / 'timeline.html').write_text(TIMELINE_HTML, encoding='utf-8')
        cls.root = root
        # chrome-headless-shell 自己就是无头的,不要再加 --headless=new。
        cls.page = Chrome(str(root / 'profile'), headless=False, chrome_path=SHELL)
        cls.page.__enter__()
        cls.addClassCleanup(cls.page.__exit__, None, None, None)
        cls.page.open_page('about:blank')

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

        **所以别再把上限往上调。** 另一条分支为同一个 flaky 把 4 秒放宽到 5 秒
        (合并时两种改法撞在这一行上),那只把概率压低:外网连不上时等多久都等不到
        `complete`。换判据之后这个模块压根不碰网络,实测从 4.3~8.2 秒降到 0.3 秒。
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
        # 判据是「这一段里一张图片都没有」,**不是**「没有 `![](`」:后者被
        # `![🙂](…emoji.svg)` 轻松绕过(里面照样有那个字符),于是「表情身份
        # 判据失效、每个 emoji 都变成一条图片链接」这一版照样全绿(自查发现)。
        self.assertNotIn('![', blocks[0], '行内表情不该写成 markdown 图片')

    def test_a_media_only_block_is_still_an_image(self):
        """反过来钉一边:独立配图块照旧是一张 markdown 图片。"""
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        self.assertIn(f'![]({PHOTO})', blocks)

    def test_a_captioned_photo_keeps_its_url(self):
        """**后果先断言**:配图配了一句描述之后,图片地址还在。

        红的那一版拿「alt 非空」当表情判据,于是 `alt="系统架构示意图"` 的配图
        只剩那句描述、`src` 整个丢掉 —— 而**剩下的文字让正文看着非空**,
        `browser_fetch` 那道「正文空不空」的判据发现不了图没了,一篇缺图的正文
        照样落库,`put_items` 是 DO NOTHING、从此改不回来。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        self.assertIn(f'![{CAPTION}]({DIAGRAM})', blocks,
                      f'带描述的配图该连地址带说明一起留下:{blocks}')

    def test_code_indentation_survives(self):
        """**后果先断言**:读回来的代码**真的编译得过**,而且装在围栏里。

        两道各管一截,少哪一道复制出来都跑不了:①读取时对所有块做
        `[ \t]+` → 单空格,四格和八格缩进一起压成一格;②读对了却当普通段落
        交出去,导出的 md 是裸代码,阅读页按 markdown 段落渲染、HTML 空白折叠
        把换行和缩进一起吃掉 —— 页面上和复制出来都是一行(2026-10-09 审查实测)。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        got = next((b for b in blocks if 'def answer()' in b), '')
        self.assertEqual(f'```\n{CODE}\n```', got,
                         f'代码块要连围栏一起交出来:{got!r}')
        code = got.split('\n', 1)[1].rsplit('\n', 1)[0]
        self.assertEqual(CODE, code, '围栏里的空白是内容,一个空格都不许折叠')
        compile(code, '<extracted>', 'exec')   # 红的那一版在这里抛 IndentationError

    def test_the_first_line_of_a_snippet_keeps_its_indent(self):
        """**首行缩进也是内容。** 收尾只许去首尾的空行,不许去行首空格。

        片段式代码(从一段代码中间截出来)就是这个形状,而那一格缩进决定了这几行
        在不在一个块里面 —— `.trim()` 把它吃掉之后照着复制同样跑不了。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        self.assertIn('```\n' + SNIPPET + '\n```', blocks,
                      f'片段首行那一格缩进没留住:{blocks}')

    def test_the_fence_is_longer_than_the_backticks_inside(self):
        """代码里独占一行的 ``` 要靠更长的围栏兜住 —— 和主路那条各一份实现。"""
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        bar = chr(96) * 4
        self.assertIn(bar + '\n' + NESTED + '\n' + bar, blocks,
                      f'围栏长度没避开内容里那一行反引号:{blocks}')

    def test_adjacent_code_blocks_merge_into_one_fence(self):
        """**真实 Draft.js 的形状:`<pre>` 包着一行一个 block。**

        每行各发一个围栏的话,复制出来是一堆分散的小代码块、`compile()` 过不去;
        合并成一个围栏才行。上一条那种「一个块里装多行」走的是同一条路。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        fences = [b for b in blocks if b.startswith('```')]
        got = next((b for b in fences if 'range(3)' in b), '')
        self.assertEqual('```\nfor i in range(3):\n    print(i)\n```', got,
                         f'相邻的预格式化块要合并成一个围栏:{fences}')
        compile(got.split('\n', 1)[1].rsplit('\n', 1)[0], '<extracted>', 'exec')

    def test_a_trailing_space_inside_a_string_is_data(self):
        """**后果先断言**:真浏览器读出来的这段代码,跑起来那句 `assert` 得过。

        和上一条同一个形状(`<pre>` 包着一行一个块),差别在**行尾有空白**:
        逐块收尾去空白就等于逐行去,而那两个空格在三引号字符串里面。那一版
        围栏在、`compile()` 也过,**只有执行输出看得出来**。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        got = next((b for b in blocks if 'record = ' in b), '')
        self.assertEqual(chr(96) * 3 + chr(10) + DATA_CODE + chr(10) + chr(96) * 3,
                         got, f'行尾那两个空格被删了:{got!r}')
        code = got.split(chr(10), 1)[1].rsplit(chr(10), 1)[0]
        exec(compile(code, '<extracted>', 'exec'), {})   # 删掉空格时 AssertionError

    def test_only_a_fully_empty_block_is_trimmed_at_the_code_edges(self):
        """组边界那一道:**占位空块该削,一行空格不该。**

        一行只有空格同样可能是内容(字符画、定宽表格、多行字符串),而 Draft.js
        的占位空块是空字符串 —— 两者分得开。后路有自己那份实现,所以这里单独
        钉一遍:拿 `trim()` 判边界的那一版在主路那条直测用例上是绿的。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        got = next((b for b in blocks if '|  x  |' in b), '')
        self.assertEqual(chr(96) * 3 + chr(10) + PAD_BODY + chr(10) + chr(96) * 3,
                         got, f'边界只许削完全空的行:{got!r}')

    def test_invisible_characters_inside_code_are_data(self):
        """**后果先断言**:读回来那段代码**跑一遍**,里面那句 `assert` 得过。

        散文里 nbsp 是排版产物、零宽是 Draft.js 的占位,所以非代码段照旧归一;
        代码块里它们是**数据**。两边用的是同一个 `textOf`,判据只有「这一块是
        不是代码」那一道 —— 把归一写在那道**外面**的实现在这里红,而且
        `compile()` 和围栏那几条照旧全绿(第 1 轮审查的不拦项①)。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        got = next((b for b in blocks if 'sample = ' in b), '')
        self.assertEqual(chr(96) * 3 + chr(10) + BYTES_CODE + chr(10) + chr(96) * 3,
                         got, f'代码块里的不可见字符被动过了:{got!r}')
        code = got.split(chr(10), 1)[1].rsplit(chr(10), 1)[0]
        exec(compile(code, '<extracted>', 'exec'), {})   # 动过就 AssertionError

    def test_a_whole_line_of_zero_width_inside_code_is_data(self):
        """**后果先断言**:读回来那段代码**跑一遍**,里面那句 `assert` 得过。

        组**中间**整行只有零宽码点时它是三引号字符串里的数据;只有组**边界**上
        的那种才是 Draft.js 的占位空块(`PAD_PRE` 那条钉的就是边界那一道)。
        **位置是唯一分得开它们的证据** —— 逐块无条件清掉的那一版在这里红,而
        `compile()`、围栏、边界那几条照旧全绿:值变了,只有执行输出看得出来
        (2026-10-10 第 2 轮审查的不拦项)。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        got = next((b for b in blocks if 'gap = ' in b), '')
        self.assertEqual(chr(96) * 3 + LF + GAP_CODE + LF + chr(96) * 3, got,
                         f'组中间那两行零宽被当成占位符清掉了:{got!r}')
        code = got.split(LF, 1)[1].rsplit(LF, 1)[0]
        exec(compile(code, '<extracted>', 'exec'), {})   # 清掉了就 AssertionError

    def test_an_inline_code_span_does_not_fence_the_paragraph(self):
        """**反过来钉一边**:段落里插一个行内 `<code>` 不许把整段包成代码块。

        判据是「这一块的全部文字是不是都装在一个预格式化容器里」,所以这一段
        (`code` 外面还有别的字)必然为假。拿「块里有没有 code 标签」当判据的
        实现会在这里红 —— 那会把大半篇正文显示成代码。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        self.assertIn('装上 npm i 就能用。', blocks)
        self.assertNotIn('```\n装上 npm i 就能用。\n```', blocks)

    def test_a_preformatted_container_keeps_its_indent(self):
        """计算样式那一道:`white-space: pre-wrap` 的容器同样不折叠。

        和上一条两道判据各走一条 —— 标签名那道管 CSS 没加载的情况,计算样式
        那道管「标签不是 pre、但页面说了它是预格式化」。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        self.assertIn('行一' + chr(10) + '  行二缩两格', blocks)

    def test_a_compound_emoji_stays_whole(self):
        """ZWJ 不许当零宽字符删掉:删了 `COMBO` 就裂成两个独立表情。"""
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        self.assertIn('组合表情:' + COMBO + '。', blocks)

    def test_ordinary_markup_whitespace_still_collapses(self):
        """反过来钉一边:**只存在于 HTML 源码里**的那些换行和缩进照旧折叠。

        不然「保住代码缩进」就会变成「把源码排版一起收进正文」—— 页面上这两段
        之间只有一个空格。
        """
        self.open('article.html')
        blocks = json.loads(self.page.eval(x_article._READ_JS))['blocks']
        self.assertIn('折叠前 折叠后', blocks)

    def test_the_whole_body_matches_what_the_page_shows(self):
        """整篇:段落边界、`<br>`、隐藏节点、零宽占位块各归各位。"""
        self.open('article.html')
        snap = json.loads(self.page.eval(x_article._READ_JS))
        blocks = snap['blocks']
        self.assertEqual(22, len(blocks), f'零宽占位块不该成段:{blocks}')
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
        self.assertEqual(22, len(got['body'].split('\n\n')))

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
