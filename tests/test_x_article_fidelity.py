# -*- coding: utf-8 -*-
"""长文正文**写出去之后**还对不对:代码能不能跑、谁转发的还在不在。

上一轮已经钉住「读出来的字符串逐字正确」(`tests/test_x_article_dom.py` 在真
浏览器里跑那两段 JS)。这一组钉的是**下一段**:那串字符经过序列化、导出、阅读页
渲染之后,用户复制走的东西还成不成立。两条各管一个缺口,都是 2026-10-09 审查用
真导出 + 真 `/doc/` 阅读页 + 真选区复制实测出来的:

①**代码变成一行**:读回来的 `def answer():\n    if True:\n        return 42`
  `compile()` 得过,可它当普通段落交出去 —— 导出的 md 是裸代码,阅读页按
  markdown 段落渲染、HTML 的空白折叠把换行和缩进一起吃掉,页面上和复制出来都是
  `def answer(): if True: return 42`,`compile()` 报 `invalid syntax`。
②**谁转发的没了**:`fill` 拿 `lead_text`(它故意滤掉 `> …` 行)的结果当重建正文
  的唯一依据,于是正文补上了、`x_browser._entry` 放在开头那行转推上下文被一起
  丢掉 —— 数据库、md 和页面三处一起缺,而 `put_items` 是 `DO NOTHING`。

所以每条都**先断言后果**(页面上那段代码真的编译得过、md 里真的写着谁转发的),
不是"有没有生成围栏"这类过程观察。按形状和 `test_x_article.py`(661 行,贴着
仓库那条 700 行上限)、`test_x_article_gap.py`(该去问而没去问的那几道缺口)分开:
这边钉的是"问到了、但写出去的东西坏了"。

跑:  PYTHONPATH=.:tests python -m unittest tests.test_x_article_fidelity -v
"""
import json
import re
import shutil
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from kb.feeds import registry as reg, sync, x_article, x_browser
from kb.feeds.x_cdp import Chrome
from kb.web import pages
from tests.headless import SHELL, requires_shell
from tests.test_x_article import FRESH, answering, article_payload
from tests.test_x_channels import ChannelBase

REPO = Path(__file__).resolve().parents[1]
CODE = 'def answer():\n    if True:\n        return 42'
REPOST = 'Example reposted'

_SP = ' ' * 2      # 行尾那两个空格 —— 它在三引号字符串**里面**,是数据不是排版
_BS = chr(92)
# **判据是执行输出,不是能不能编译。** 逐行 `rstrip` 删掉上面那两个空格之后,
# 这段代码照旧 `compile()` 得过,而跑起来那句 `assert` 当场失败 —— 多行字符串、
# 提示词模板、定宽文本、字符画都把行尾空白当内容(2026-10-09 第 4 轮审查:接口、
# 后路、导出到阅读页复制四条路各复现一次)。常量摆在这里给 `test_x_article_dom`
# 那条真浏览器用例共用,两边各写一份的话样本会漂。
DATA_CODE = '\n'.join([
    'record = """first line',
    'second line' + _SP,
    'third line"""',
    f'assert record == "first line{_BS}nsecond line{_SP}{_BS}nthird line"',
])


def _code_blocks(lines):
    return [{'type': 'code-block', 'text': ln} for ln in lines]


# ------------------------------------------------------------------ ① 代码围栏

class FenceTests(unittest.TestCase):
    """主路(FxTwitter 回包)那一侧:`code-block` 要变成一个 markdown 围栏。

    判据不是"字符串里有没有 ```",而是**把它当 markdown 渲染一遍**看那段代码是不是
    一整块代码 —— 比字符串的话,围栏长度算错(内容里本来就有 ``` )那种实现也能绿。
    """

    def md_code(self, body: str) -> list[str]:
        """按 markdown 解析,返回每个代码块的内容。用的是页面那一侧同族的解析器。"""
        from markdown_it import MarkdownIt
        md = MarkdownIt('gfm-like')
        # fence token 的 content 带一个收尾换行,那是 markdown 的结构、不是内容。
        return [t.content.rstrip('\n') for t in md.parse(body) if t.type == 'fence']

    def test_adjacent_code_blocks_become_one_fence(self):
        """**后果先断言**:渲染出来是**一整块**代码,而且 `compile()` 得过。

        Draft.js 的代码块是一行一个 block(`blockRenderMap` 把相邻的几个包进同一个
        `<pre>`),每行各发一个围栏的话复制出来是一堆分散的小代码块。
        """
        body = x_article.render({'content': {
            'blocks': [{'type': 'unstyled', 'text': '开头一段。'}]
                      + _code_blocks(CODE.split('\n'))
                      + [{'type': 'unstyled', 'text': '收尾一段。'}],
            'entityMap': []}})
        got = self.md_code(body)
        self.assertEqual([CODE], got, f'相邻的 code-block 要合并成一个围栏:{body!r}')
        compile(got[0], '<rendered>', 'exec')   # 裸段落那一版在这里 invalid syntax
        self.assertIn('开头一段。', body)
        self.assertIn('收尾一段。', body)

    def test_a_blank_line_inside_the_code_survives(self):
        """代码**中间**的空行是内容;代码块外面的空块照旧不成段。"""
        body = x_article.render({'content': {'blocks': _code_blocks(
            ['', 'a = 1', '', 'b = 2', '']), 'entityMap': []}})
        self.assertEqual(['a = 1\n\nb = 2'], self.md_code(body))

    def test_the_first_line_keeps_its_indent(self):
        """**首行缩进也是内容** —— 片段式代码(从中间截一段)就是这个形状。

        收尾用 `.strip()` 的实现会把那一格吃掉,而它决定了这几行在不在一个块
        里面:照着复制同样跑不了。
        """
        snippet = ['    if x:', '        pass']
        body = x_article.render({'content': {
            'blocks': _code_blocks(snippet), 'entityMap': []}})
        self.assertEqual(['\n'.join(snippet)], self.md_code(body),
                         f'首行那一格缩进没留住:{body!r}')

    def test_the_fence_avoids_backticks_already_in_the_code(self):
        """代码里**独占一行**的 ``` 会把围栏提前收尾,所以围栏要更长。

        讲 markdown 的文章里这是常见形态。**判据只能是渲染一遍** —— 比字符串的话
        围栏算错那种实现也能绿;而**行内**带反引号(`echo "```"`)测不出来,
        CommonMark 的闭合行只许有反引号,所以那种样本写死 3 也不坏(自查发现)。
        """
        lines = ['示例:', '```', '里面', '```']
        body = x_article.render({'content': {
            'blocks': _code_blocks(lines), 'entityMap': []}})
        self.assertEqual(['\n'.join(lines)], self.md_code(body),
                         f'围栏长度没避开内容里那一行反引号:{body!r}')

    def test_a_trailing_space_inside_a_string_is_data(self):
        """**后果先断言**:这段代码跑起来,里面那句 `assert` 得过。

        行尾空白**可能正是数据**。真实 Draft.js 是一行一个 `code-block`,所以
        "每块收尾去空白"就等于"每行收尾去空白" —— 逐行 `rstrip` 的那一版照旧
        `compile()` 得过,**编译通过证明不了字符串的值没被改写**,所以判据只能是
        执行输出(2026-10-09 第 4 轮审查实测:`record` 少了那两个空格)。
        """
        body = x_article.render({'content': {
            'blocks': _code_blocks(DATA_CODE.split('\n')), 'entityMap': []}})
        got = self.md_code(body)
        self.assertEqual(1, len(got), f'没有围栏:{body!r}')
        exec(compile(got[0], '<rendered>', 'exec'), {})    # 删掉空格时 AssertionError
        self.assertEqual(DATA_CODE, got[0], f'行尾那两个空格被删了:{body!r}')

    def test_only_fully_empty_lines_are_trimmed_at_the_edges(self):
        """直测边界口径:**完全空**的行在整组首尾削掉,纯空格的一个都不削。

        Draft.js 的占位空块是空字符串;而一行空格可能是定宽文本、字符画、多行
        字符串的内容 —— 拿 `strip()` 判边界的实现会把它一起吃掉。两头各钉一边:
        该削的还得削,不然围栏里凭空多出空行。
        """
        self.assertEqual('a', x_article._code_body(['', '', 'a', '', '']))
        self.assertEqual(_SP + '\na\n' + _SP,
                         x_article._code_body([_SP, 'a', _SP]))
        self.assertEqual('', x_article._code_body(['', '']))

    def test_a_whitespace_only_line_at_the_code_edge_survives(self):
        """走一遍 `render`:组**边界**上那行空格也留着,占位空块照旧削掉。

        上一条是直测 `_code_body`,这一条管的是它外面那段循环 —— 在那里按
        `strip()` 跳过"开头的空块"的实现,交给 `_code_body` 的已经少了一行,
        直测用例对它压根不敏感(自查发现)。后路那一侧在 `test_x_article_dom`
        里各钉一条,两份实现各有自己的用例。
        """
        lines = [_SP, '|  x  |', _SP]
        body = x_article.render({'content': {
            'blocks': _code_blocks([''] + lines + ['']), 'entityMap': []}})
        self.assertEqual(['\n'.join(lines)], self.md_code(body),
                         f'边界只许削完全空的行:{body!r}')

    def test_prose_is_not_fenced(self):
        """**反过来钉一边**:普通段落、标题、列表、引用一个都不许被包成代码块。

        把大半篇正文显示成代码比丢掉缩进更难看出来 —— 它不报错、也不缺字。
        """
        body = x_article.render(article_payload()['tweet']['article'])
        self.assertEqual([], self.md_code(body), f'正文被包成代码块了:{body!r}')


# ------------------------------------------------------------- ② 转推上下文

def _repost_item(text='', article='https://x.com/i/article/123456'):
    """时间线采集给一条**转发**的长文推文留下的形状。"""
    return {'url': 'https://x.com/OriginalAuthor/status/800', 'text': text,
            'context': REPOST, 'datetime': FRESH,
            'author': 'OriginalAuthor', 'name': '原作者', 'article': article}


class RepostContextTests(ChannelBase):
    """补完正文之后,「谁转发了这篇文章」必须还在 —— 库里、md 里、页面上。"""

    def filled(self, item, payload=None):
        entry = x_browser._entry(item)
        self.assertIn('> ' + REPOST, entry['body'], '采集阶段本来就该带着它')
        req, _ = answering(payload if payload is not None else article_payload())
        with req:
            kept, note = x_article.fill([entry])
        return kept, note

    def exported(self, kept):
        source = self.sub('OriginalAuthor')
        reg.put_items(source, kept)
        sync.export('social', hold_untranslated=False)
        with reg.db() as c:
            row = c.execute('SELECT body, path FROM feed_items').fetchone()
        md = (sync.VAULT_ROOT / row['path']).read_text(encoding='utf-8')
        return row['body'], md

    def test_the_repost_context_survives_the_article_fill(self):
        """**后果先断言**:库里、md 里都写着谁转发的,而且正文也补上了。"""
        kept, note = self.filled(_repost_item())
        self.assertEqual('', note)
        body, md = self.exported(kept)
        self.assertIn('> ' + REPOST, body, f'库里的正文丢了转推上下文:{body!r}')
        self.assertIn('> ' + REPOST, md, f'导出的 md 丢了转推上下文:{md!r}')
        self.assertIn('整理书签', body, '文章正文也要在')

    def test_the_lead_text_stays_between_the_context_and_the_body(self):
        """三段按原顺序:转推上下文 → 推文自己写的介绍 → 文章正文。"""
        kept, _ = self.filled(_repost_item(text='这篇值得读 https://t.co/abc'))
        parts = kept[0]['body'].split('\n\n')
        self.assertEqual('> ' + REPOST, parts[0])
        self.assertEqual('这篇值得读', parts[1])
        self.assertIn('整理书签', parts[2])

    def test_a_tweet_that_needs_no_article_keeps_its_context(self):
        """**阴性对照**:这一条不走补全,上下文照旧 —— 丢失是补全那一步引入的。"""
        entry = x_browser._entry({**_repost_item(text='就是转一下'), 'article': ''})
        req, calls = answering(article_payload())
        with req:
            kept, note = x_article.fill([entry])
        self.assertEqual([], calls.urls, '这一条没有长文信号,不该去问')
        self.assertEqual('> ' + REPOST + '\n\n就是转一下', kept[0]['body'])


# ------------------------------------------------- ① 的端到端:导出 → 阅读页 → 复制

@requires_shell
class ReaderRoundTripTests(ChannelBase):
    """整条链:补正文 → `put_items` → `sync.export` → 真 `/doc/` 阅读页 → 复制。

    **只验到解析字符串不算**:审查那次 12 条 DOM 测试全绿,而页面上那段代码已经
    塌成一行了(报告原话:「现有 12 条 DOM 测试止于解析字符串…故全绿无法排除这个
    缺陷」)。所以这一组真起一个无头壳,把 `pages.doc_page` **原样**生成的那一页
    渲染出来,断言可见文本里的代码 `compile()` 得过。

    页面一个字节都不改,只把两个 vendor 脚本的 `/static/` 换成同目录的相对路径
    (file:// 下没有那个绝对路径);marked 和 DOMPurify 用的就是仓库里那两份。
    """

    @classmethod
    def setUpClass(cls):
        cls.shell = TemporaryDirectory()
        # 建完/进完**立刻**登记(LIFO:先退浏览器、再删目录)。只写在 tearDownClass
        # 里的话,`__enter__` 或 `open_page` 抛出来时 unittest 不调它 —— 浏览器和那份
        # profile 都留在后台。
        cls.addClassCleanup(cls.shell.cleanup)
        cls.page = Chrome(str(Path(cls.shell.name) / 'profile'),
                          headless=False, chrome_path=SHELL)
        cls.page.__enter__()
        cls.addClassCleanup(cls.page.__exit__, None, None, None)
        cls.page.open_page('about:blank')

    def read(self, body: str) -> dict:
        """把这份正文真走一遍导出和阅读页,返回页面上看得见的东西。"""
        source = self.sub('Example')
        reg.put_items(source, [{'url': 'https://x.com/Example/status/900',
                                'title': '代码示例长文', 'body': body,
                                'published': '2026-10-09 17:15',
                                'content_kind': 'post'}])
        sync.export('social', hold_untranslated=False)
        with reg.db() as c:
            rel = c.execute('SELECT path FROM feed_items').fetchone()['path']
        self.assertIn(body.splitlines()[0],
                      (sync.VAULT_ROOT / rel).read_text(encoding='utf-8'))
        with patch.object(pages, 'VAULT_ROOT', sync.VAULT_ROOT), \
                patch.object(pages.store, 'connect', _no_docs):
            html = pages.doc_page(rel).body.decode('utf-8')
        out = Path(self.shell.name) / 'doc.html'
        vendor = out.parent / 'vendor'
        vendor.mkdir(exist_ok=True)
        for name in ('marked.min.js', 'purify.min.js'):
            shutil.copy(REPO / 'kb/static/vendor' / name, vendor / name)
            html = html.replace(f'src="/static/vendor/{name}"',
                                f'src="vendor/{name}"')
        out.write_text(html, encoding='utf-8')
        self.page.goto(out.as_uri())
        for _ in range(60):
            if self.page.eval('document.readyState') == 'complete':
                break
            time.sleep(0.1)
        return json.loads(self.page.eval(
            "JSON.stringify({text: document.getElementById('content').innerText,"
            " pre: document.querySelectorAll('#content pre code').length})"))

    def test_the_code_still_runs_after_export_and_rendering(self):
        """**后果先断言**:页面上复制走的那段代码**真的编译得过**。"""
        req, _ = answering(article_payload(blocks=_code_blocks(CODE.split('\n'))))
        with req:
            got = x_article.fetch('https://x.com/Example/status/900')
        seen = self.read(got['body'])
        self.assertEqual(1, seen['pre'], f'阅读页上没有代码块:{seen["text"]!r}')
        code = re.sub(r'\n+$', '', seen['text'].split('代码示例长文', 1)[-1].strip('\n'))
        self.assertEqual(CODE, code, f'页面上的代码和原文不一致:{code!r}')
        compile(code, '<copied-from-reader>', 'exec')

    def test_a_trailing_space_survives_export_and_the_reader(self):
        """**后果先断言**:从阅读页上取走的那段代码**跑起来那句 `assert` 得过**。

        这是最远的那个终点:接口解析 → 入库 → 导出 → 真 marked/DOMPurify 渲染。
        行尾空白被当装饰删掉时这一路一个字都不报错,只有执行输出看得出来。
        """
        req, _ = answering(article_payload(
            blocks=_code_blocks(DATA_CODE.split('\n'))))
        with req:
            got = x_article.fetch('https://x.com/Example/status/900')
        seen = self.read(got['body'])
        self.assertEqual(1, seen['pre'], f'阅读页上没有代码块:{seen["text"]!r}')
        code = re.sub(r'\n+$', '', seen['text'].split('代码示例长文', 1)[-1].strip('\n'))
        exec(compile(code, '<copied-from-reader>', 'exec'), {})
        self.assertEqual(DATA_CODE, code, f'页面上的代码和原文不一致:{code!r}')

    def test_a_bare_code_paragraph_is_how_it_used_to_break(self):
        """**阴性对照**:同一段代码不带围栏时,页面上真的塌成一行、编译不过。

        这一条证明上面那条对「有没有围栏」敏感 —— 它复现的正是审查报告里那份
        证据(`def answer(): if True: return 42` + `invalid syntax`)。
        """
        seen = self.read(CODE)
        self.assertEqual(0, seen['pre'])
        self.assertIn('def answer(): if True: return 42', seen['text'])
        with self.assertRaises(SyntaxError):
            compile(seen['text'].split('代码示例长文', 1)[-1].strip(),
                    '<copied-from-reader>', 'exec')


def _no_docs(*a, **kw):
    """`doc_page` 要查一行 `documents`。这一组不建索引,所以给它一个空表 —— 别去
    碰生产那个 kb.db(`store.connect` 会在它上面跑迁移)。"""
    import sqlite3
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE documents (path TEXT, title TEXT, account TEXT,'
                 ' published TEXT, url TEXT)')
    return conn


if __name__ == '__main__':
    unittest.main()
