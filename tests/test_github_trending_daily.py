"""GitHub Trending 那一类榜单订阅进日报:发布时间、送进模型的摘要、提示词里的席位。

形态:榜单 feed 的条目上没有 `pubDate`,只有 `<channel>` 上那一个。旧口径下每一条都
是「无发布时间」,日报只能把它们塞进补录、归并成一行「(无发布时间)」,没有出处编号
也就没有链接;而送进模型的摘要是 README 开头那串去掉了 `>` 的 HTML 徽章。

三层各钉一次后果,并各自反过来钉住不该动的那一边:
- 普通 RSS 里**仓库主页那一类**无日期条目拿到 feed 的时间;普通文章照旧无日期(channel
  时间只是它的上界,定成今天就混进当天正文);**过期的** feed 时间不认(退回无发布时间,
  由补录接住、不被回溯上限静默排除);条目自己的时间照旧优先;简报和社交那两条入口不顶。
- 摘要回 vault 原文剥 HTML(chunk 上没了 `>`,`std::vector<int>` 会被当成 `<i` 剥掉);
  **干净的**正文原样照旧取 chunk(素材指纹不许被白白换掉)。
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

    def test_ordinary_articles_stay_undated(self):
        """channel 时间只是文章发表时间的上界:今天更新过的博客 feed,2020 年那篇不带日期的
        旧文不许被定成今天(定了就进当天日报正文);只有仓库主页那一类拿列榜时间。"""
        listed = self.now - timedelta(hours=1)
        got = self.parse(feed(listed, [('https://blog.example/2020/install-guide', None),
                                       ('https://github.com/a/one/issues/7', None),
                                       ('https://github.com/a/one', None)]), listed=True)
        self.assertEqual({'https://blog.example/2020/install-guide': '',
                          'https://github.com/a/one/issues/7': '',
                          'https://github.com/a/one': stamp(listed)}, got)

    def test_the_rss_adapter_is_the_one_that_opts_in(self):
        listed = self.now - timedelta(hours=3)
        body = feed(listed, [('https://github.com/a/one', None)])
        rss = fetching.ADAPTERS['rss'].parse(body, 'https://example.org/trending.xml')
        self.assertEqual([stamp(listed)], [e['published'] for e in rss])
        social = fetching.ADAPTERS['social'].parse(body, 'https://example.org/trending.xml')
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


# vault 里一篇 README 正文开头的真实形态(md + 内嵌 HTML,标签完整)。
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
[![许可证](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) ![GPT](https://img.shields.io/badge/GPT-10A37F?logo=data:image/svg%2bxml;base64,PHN2ZyByb2xlPSJpbWci)
<b>2.78T</b> 参数,0 <sub>GPU</sub>
</div>

用 std::vector<int> 存权重,std::vector<string> 存词表;x <bits 时直接失败,见 [文档](https://e.org/d)。
"""
README_TEXT = ('kimi-k3-in-c 享受新闻本身。 其他的交给 Horizon 2.78T 参数,0 GPU 用 std::vector<int> 存权重,'
               'std::vector<string> 存词表;x <bits 时直接失败,见 文档。')


class ExcerptTests(unittest.TestCase):
    """chunk 上没了 `>`,「是不是标签」判不出来;要剥的那些回 vault 原文去读。"""

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
        """标签剥掉;`std::vector<int>`、`<string>`、`x <bits` 一个字都不许少 —— 在 chunk 上
        猜标签会把它们当成 `<i` / `<s` / `<b` 剥掉。"""
        self.write('source/rss/a.md', README)
        self.assertEqual(README_TEXT, self.get('source/rss/a.md'))
        self.assertEqual(README_TEXT[:20], self.get('source/rss/a.md', 20))

    def test_text_that_only_looks_like_markup_is_left_alone(self):
        text = 'List<T>、Vec<u8>、Map<i, j>、std::vector<int>、x <bits 和 a<b 都不是标签'
        self.assertEqual(text, daily_excerpt.clean(text))
        self.assertFalse(daily_excerpt.noisy(text))

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
