"""GitHub Trending 那一类榜单订阅进日报:发布时间、送进模型的摘要、提示词里的席位。

形态:榜单 feed 的条目上没有 `pubDate`,只有 `<channel>` 上那一个。旧口径下每一条都
是「无发布时间」,日报只能把它们塞进补录、归并成一行「(无发布时间)」,没有出处编号
也就没有链接;而送进模型的摘要是 README 开头那串去掉了 `>` 的 HTML 徽章。

三层各钉一次后果,并各自反过来钉住不该动的那一边:
- **确认过的榜单快照源**(`fetching.SNAPSHOT_FEEDS`)的无日期条目拿到 feed 的时间;别的
  feed 一律照旧无日期,哪怕条目链接同样是 `github.com/owner/repo`(「历年推荐」存档);
  **过期的** feed 时间不认(退回无发布时间,由补录接住、不被回溯上限静默排除);条目自己
  的时间照旧优先;简报和社交那两条入口不顶。
- 摘要只对原文开头真有 HTML 块的那些按 CommonMark 结构取字:HTML 块交给 HTML 解析器,
  段落里的技术正文(`Box<U>`、`std::vector<int>`、`__init__`、行内代码)一个字不改;
  **别的**原样照旧取 chunk(素材指纹不许被白白换掉)。
- 提示词给榜单一个有上下界的席位,补录要求带出处编号。
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kb import daily, daily_excerpt, store
from kb.chunker import split_chunks
from kb.daily_prompt import PROMPT, late_section
from kb.feeds import digest, fetching
from kb.scanner import parse_frontmatter

TZ = ZoneInfo('Asia/Shanghai')


def stamp(when: datetime) -> str:
    return when.astimezone(TZ).strftime('%Y-%m-%d %H:%M')


def feed(channel_date: datetime | None, items: list[tuple[str, datetime | None]]) -> bytes:
    head = f'<pubDate>{format_datetime(channel_date)}</pubDate>' if channel_date else ''
    body = ''.join(
        f'<item><title>{url.rsplit("/", 2)[-2]}/{url.rsplit("/", 1)[-1]}</title><link>{url}</link>'
        f'<description>&lt;p&gt;repo about text&lt;/p&gt;</description>'
        + (f'<pubDate>{format_datetime(when)}</pubDate>' if when else '') + '</item>'
        for url, when in items)
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>GitHub Daily Trending'
            f'</title>{head}{body}</channel></rss>').encode()


class ListedDateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def parse(self, body, **kw):
        return {e['url']: e['published'] for e in
                fetching.parse_feed(body, 'https://example.org/trending.xml', **kw)}

    def test_undated_items_take_the_fresh_channel_date(self):
        listed = self.now - timedelta(hours=17)
        own = self.now - timedelta(days=2)
        got = self.parse(feed(listed, [('https://github.com/a/one', None),
                                       ('https://github.com/b/two', own)]), listed=True)
        self.assertEqual({'https://github.com/a/one': stamp(listed),
                          'https://github.com/b/two': stamp(own)}, got,
                         '无日期的顶上 feed 的时间,自己带了时间的照旧用自己的')

    def test_only_confirmed_snapshot_feeds_take_the_listing_time(self):
        """channel 时间只说明整份 feed 更新过:今天更新的博客里 2020 年的旧文、「历年项目推荐」
        存档里 2018 年那条仓库推荐(链接同样是仓库主页),都不许被定成今天 —— 定了就进当天
        日报正文,而且 `put_items` 是 DO NOTHING、改不回来。判据在来源上,不在条目长相上。"""
        listed = self.now - timedelta(hours=1)
        body = feed(listed, [('https://blog.example/2020/install-guide', None),
                             ('https://github.com/old/recommended-2018', None)])
        rss = fetching.ADAPTERS['rss']
        archive = rss.parse(body, 'https://picks.example/archive.xml')
        self.assertEqual(['', ''], [e['published'] for e in archive])
        trending = rss.parse(body, 'https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml')
        self.assertEqual([stamp(listed)] * 2, [e['published'] for e in trending])
        self.assertTrue(fetching.is_snapshot_feed(
            'https://mshibanami.github.io/GitHubTrendingRSS/weekly/python.xml'))
        for other in ('https://mshibanami.github.io/other/feed.xml',
                      'https://evil.example/GitHubTrendingRSS/daily/all.xml',
                      'https://mshibanami.github.io.evil.example/GitHubTrendingRSS/x.xml'):
            self.assertFalse(fetching.is_snapshot_feed(other), other)

    def test_the_rss_adapter_is_the_one_that_opts_in(self):
        listed = self.now - timedelta(hours=3)
        body = feed(listed, [('https://github.com/a/one', None)])
        url = 'https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml'
        rss = fetching.ADAPTERS['rss'].parse(body, url)
        self.assertEqual([stamp(listed)], [e['published'] for e in rss])
        social = fetching.ADAPTERS['social'].parse(body, url)
        self.assertEqual([''], [e['published'] for e in social], '社交那一档不顶')

    def test_stale_channel_date_is_not_trusted(self):
        """写死成多年前的 channel 时间不能顶上:顶上之后条目在日报里被回溯上限排除、
        在新订阅上被历史下界挡掉,两处都是静默的。退回无发布时间 = 由补录接住。"""
        stale = self.now - fetching.LISTED_FRESH - timedelta(hours=1)
        got = self.parse(feed(stale, [('https://github.com/a/one', None)]), listed=True)
        self.assertEqual({'https://github.com/a/one': ''}, got)
        fresh = self.now - fetching.LISTED_FRESH + timedelta(hours=1)
        got = self.parse(feed(fresh, [('https://github.com/a/one', None)]), listed=True)
        self.assertEqual({'https://github.com/a/one': stamp(fresh)}, got, '边界内的照认')

    def test_future_channel_date_is_clamped_to_now(self):
        before = stamp(self.now)
        got = self.parse(feed(self.now + timedelta(days=2),
                              [('https://github.com/a/one', None)]), listed=True)
        after = stamp(datetime.now(timezone.utc))
        self.assertIn(got['https://github.com/a/one'], {before, after})

    def test_no_channel_date_and_default_callers_stay_undated(self):
        self.assertEqual({'https://github.com/a/one': ''},
                         self.parse(feed(None, [('https://github.com/a/one', None)]), listed=True))
        self.assertEqual({'https://github.com/a/one': ''},
                         self.parse(feed(self.now, [('https://github.com/a/one', None)])),
                         '不开 listed 的调用方行为不变')

    def test_digest_issue_dates_still_come_from_the_issue_url(self):
        """简报没日期时从期号地址里认一个,那比整份 feed 的时间准 —— 不许被顶掉。"""
        body = feed(self.now, [('https://tldr.example/ai/2026-11-03', None)])
        self.assertEqual([('https://tldr.example/ai/2026-11-03', '2026-11-03')],
                         digest.issues(body, 'https://tldr.example/feed.xml'))


# vault 里一篇 README 正文开头的真实形态(md + 内嵌 HTML,标签完整)。md 徽章前后空一行 ——
# 不空的话它算 HTML 块里的字,GitHub 上就原样显示成一串 `[![…](…)]`,作者都会空这一行。
README = """---
source: rss
title: a/b
---

# a/b

> 本文由英文原文自动翻译，专有名词保留原文；如有歧义请对照原文。

<!-- markdownlint-disable MD033 -->
<div align="center">
<h1>kimi-k3-in-c</h1>
<p><strong>享受新闻本身。</strong>其他的交给 Horizon</p>
<a href="https://trendshift.io/r/1?utm=badge&amp;x=1" target="_blank"><img src="https://trendshift.io/api/badge/1" alt="a | Trendshift" width="250"/></a>
<p><b>2.78T</b> 参数,0 <sub>GPU</sub></p>

[![许可证](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) ![GPT](https://img.shields.io/badge/GPT-10A37F?logo=data:image/svg%2bxml;base64,PHN2ZyByb2xlPSJpbWci)

</div>

用 std::vector<int> 存权重,std::vector<string> 存词表;x <bits 时直接失败,见 [文档](https://e.org/d)。
类型参数 Box<U>、Box<I>、Box<B>、Box<S> 都保留,只有 x <b && y>c 时才删;实现 `__init__`、__enter__ 和 **粗体**。

<p>调用 `__builtin_popcount`。</p>
"""
README_TEXT = ('kimi-k3-in-c 享受新闻本身。其他的交给 Horizon 2.78T 参数,0 GPU 用 std::vector<int> 存权重,'
               'std::vector<string> 存词表;x <bits 时直接失败,见 文档。 类型参数 Box<U>、Box<I>、Box<B>、'
               'Box<S> 都保留,只有 x <b && y>c 时才删;实现 __init__、__enter__ 和 **粗体**。 '
               '调用 `__builtin_popcount`。')    # HTML 块里的反引号是字面字符,GitHub 也原样显示


class ExcerptTests(unittest.TestCase):
    """chunk 上没了 `>`,「是不是标签」判不出来;回 vault 原文,按 CommonMark 的结构取字。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'vault'
        self.conn = store.connect(Path(temp.name) / 'kb.db')
        self.addCleanup(self.conn.close)
        p = patch.object(daily, 'VAULT_ROOT', self.root)
        p.start(); self.addCleanup(p.stop)

    def add(self, path, raw=None, summary=''):
        """按真实链路落一篇:vault 原文 → `chunker.split_chunks` 切出来的 chunk。"""
        self.conn.execute(
            'INSERT INTO documents(path,source,title,account,published,url,summary,indexed_at,'
            'extracted_at,promo_level) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (path, 'rss', 'a/b', 'GitHub Trending', '2026-11-05 15:34', 'https://github.com/a/b',
             summary, '2026-11-06 08:00', '', ''))
        if raw is not None:
            for i, chunk in enumerate(split_chunks(parse_frontmatter(raw)[1])):
                self.conn.execute('INSERT INTO chunks(path,seq,heading,text) VALUES(?,?,?,?)',
                                  (path, i, chunk.heading, chunk.text))
        self.conn.commit()

    def write(self, path, raw):
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(raw, encoding='utf-8')
        self.add(path, raw)

    def get(self, path, limit=400):
        return daily_excerpt.excerpt(self.conn, self.root, path, limit)

    def test_html_is_stripped_and_typed_code_survives(self):
        """HTML 块和徽章剥掉;段落里的技术正文一个字不改:`std::vector<int>`、`Box<U>`、比较式、
        `__init__`(行内代码只去反引号)、强调符号。正则猜标签的两版各在其中一类上删过字。"""
        self.write('source/rss/a.md', README)
        self.assertEqual(README_TEXT, self.get('source/rss/a.md'))
        self.assertEqual(README_TEXT[:20], self.get('source/rss/a.md', 20))

    def test_html_block_follows_html_rules(self):
        """HTML 块里的字照 HTML 的规矩解释,和 GitHub 显示给人看的那一份相同:`<b && y>` 在那儿
        就是一个 `<b>` 开始标签;块里反引号是字面字符。"""
        self.assertEqual('只有 x c 时', daily_excerpt.clean('<div>只有 x <b && y>c 时</div>\n'))
        self.assertEqual('调用 `__init__` 文字', daily_excerpt.clean(
            '<p align="center">\n调用 `__init__`\n文字\n</p>\n'))

    def test_html_display_text_is_never_reinterpreted(self):
        """HTML 解出来的字就是最终文字:实体转义的代码、写给人看的 `![badge](…)`,不许被再当一遍
        Markdown 解析成标签和图片删掉(同一段换成 md 代码块本来就完整,两条路要一致)。"""
        code = 'std::vector<unsigned int> values; 只有 x <b && y>c 时; literal ![badge](image.svg)'
        html = ('<div>\n<p>Example tool</p>\n<pre><code>std::vector&lt;unsigned int&gt; values; '
                '只有 x &lt;b &amp;&amp; y&gt;c 时; literal ![badge](image.svg)</code></pre>\n</div>\n')
        self.assertEqual('Example tool ' + code, daily_excerpt.clean(html))
        self.assertEqual(code, daily_excerpt.clean(f'```\n{code}\n```\n'))
        self.assertEqual('行内 Vec<U> 和 __init__',
                         daily_excerpt.clean('<p>行内 <code>Vec&lt;U&gt;</code> 和 __init__</p>\n'))

    def test_inline_tags_need_attributes_to_count(self):
        """段落里的 `<...>` 只有带属性的(和它的闭合标签)才拿掉;`<b>`、`<U>` 这种没证据的留着。"""
        self.assertEqual('前 后 <b>主题</b> Box<U> 完', daily_excerpt.clean(
            '前 <a href="x"><img src="y"/></a> 后 <b>主题</b> Box<U> <!-- c --> 完'))

    def test_text_that_only_looks_like_markup_is_left_alone(self):
        text = 'List<T>、Vec<u8>、Map<i, j>、std::vector<int>、x <bits 和 a<b 都不是标签'
        self.assertEqual(text, daily_excerpt.clean(text))
        self.assertFalse(daily_excerpt.has_html_block(text))

    def test_technical_text_without_html_keeps_the_old_excerpt_verbatim(self):
        """chunk 里有 `<`、原文里却没有完整标签:照旧取头一个 chunk,一个字节不动。"""
        raw = '---\ntitle: t\n---\n\n用 std::vector<int> 存。\n\n第二段\n'
        self.write('source/rss/a.md', raw)
        first = self.conn.execute("SELECT text FROM chunks WHERE path='source/rss/a.md' "
                                  'ORDER BY seq LIMIT 1').fetchone()[0]
        self.assertIn('<int', first, '前提:chunk 里那个 > 已经没了')
        self.assertEqual(first.strip(), self.get('source/rss/a.md'))

    def test_a_clean_first_chunk_is_taken_verbatim(self):
        """干净的正文一个字节都不许变(连换行):摘要进素材指纹,白白换掉的话升级那一
        刻今天带走的每一条都被面板报成「再生成能多带进来」。"""
        text = '本文由英文原文自动翻译，专有名词保留原文；如有歧义请对照原文。\n第一段\n\n第二段  '
        self.add('source/rss/a.md')
        self.conn.execute('INSERT INTO chunks(path,seq,heading,text) VALUES(?,?,?,?)',
                          ('source/rss/a.md', 0, '', text))
        self.assertEqual(text.strip(), self.get('source/rss/a.md'))

    def test_unreadable_original_falls_back_to_the_chunk(self):
        self.add('source/rss/a.md', README)          # 只有 chunk,没有 vault 文件
        first = self.conn.execute("SELECT text FROM chunks WHERE path='source/rss/a.md' "
                                  'ORDER BY seq LIMIT 1').fetchone()[0]
        self.assertEqual(first.strip()[:400], self.get('source/rss/a.md'))

    def test_collect_sends_the_clean_excerpt_and_keeps_summaries(self):
        since, until = datetime(2026, 11, 5, 8, 57), datetime(2026, 11, 6, 8, 57)
        self.write('source/rss/a.md', README)
        self.write('source/rss/b.md', README.replace('a/b', 'c/d'))
        self.conn.execute("UPDATE documents SET summary='模型提取过的摘要' WHERE path='source/rss/b.md'")
        self.conn.commit()
        got = {d['path']: d['excerpt'] for d in daily.collect(self.conn, since, until).items}
        self.assertEqual({'source/rss/a.md': README_TEXT, 'source/rss/b.md': '模型提取过的摘要'}, got)

    def test_nothing_but_markup_is_no_excerpt_not_markup(self):
        self.write('source/rss/c.md', '---\ntitle: t\n---\n\n<div align="center">\n<p align="center">'
                   '<img src="x.png"/></p>\n</div>\n')
        self.assertEqual('', self.get('source/rss/c.md'))


class PromptTests(unittest.TestCase):
    def test_trending_gets_a_bounded_slot_in_tools(self):
        tools = PROMPT.split('## 工具与项目', 1)[1].split('## 观点与讨论', 1)[0]
        self.assertIn('GitHub Trending', tools)
        self.assertIn('3~6 个', tools, '有下限(权重)也有上限(不占满日报)')
        self.assertIn('其余的榜单条目不要在别的节里罗列', tools)
        self.assertIn('出处编号', tools)

    def test_late_section_asks_for_reference_numbers_in_both_shapes(self):
        for n in (5, 50):
            text = late_section(n)
            self.assertIn('出处编号', text, n)
            self.assertIn('标着「无发布时间」的就不写时间', text, n)


if __name__ == '__main__':
    unittest.main()
