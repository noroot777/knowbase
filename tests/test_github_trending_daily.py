"""GitHub Trending 那一类榜单订阅进日报:发布时间、送进模型的摘要、提示词里的席位。

形态:榜单 feed 的条目上没有 `pubDate`,只有 `<channel>` 上那一个。旧口径下每一条都
是「无发布时间」,日报只能把它们塞进补录、归并成一行「(无发布时间)」,没有出处编号
也就没有链接;而送进模型的摘要是 README 开头那串去掉了 `>` 的 HTML 徽章。

三层各钉一次后果,并各自反过来钉住不该动的那一边:
- 普通 RSS 的无日期条目拿到 feed 的时间;**过期的** feed 时间不认(退回无发布时间,
  由补录接住、不被回溯上限静默排除);条目自己的时间照旧优先;简报和社交那两条入口不顶。
- 摘要剥掉标签和徽章、往后读到有字为止;**干净的**正文原样照旧(素材指纹不许被白白换掉)。
- 提示词给榜单一个有上下界的席位,补录要求带出处编号。
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kb import daily, daily_excerpt, store
from kb.daily_prompt import PROMPT, late_section
from kb.feeds import digest, fetching

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


# 一个真实 README 头一个 chunk 的形态(chunker 已经把 `>` 剥掉了)。
README = '''本文由英文原文自动翻译，专有名词保留原文；如有歧义请对照原文。
<!-- markdownlint-disable MD033 --
<div align="center"
<h1kimi-k3-in-c</h1
<p<strong享受新闻本身。其他的交给 Horizon</strong</p
<a href="https://trendshift.io/r/1?utm=badge&amp;x=1" target="blank"<img src="https://trendshift.io/api/badge/1" alt="a | Trendshift" width="250"/</a
[![许可证](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) ![GPT](https://img.shields.io/badge/GPT-10A37F?logo=data:image/svg%2bxml;base64,PHN2ZyByb2xlPSJpbWciIHZpZXdCb3g9IjAgMCAyNCAyNCIgeG1s
<b2.78T 参数 <subGPU</sub
</div'''


class ExcerptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.conn = store.connect(Path(temp.name) / 'kb.db')
        self.addCleanup(self.conn.close)

    def add(self, path, *chunks, summary=''):
        self.conn.execute(
            'INSERT INTO documents(path,source,title,account,published,url,summary,indexed_at,'
            'extracted_at,promo_level) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (path, 'rss', 'a/b', 'GitHub Trending', '2026-11-05 15:34', 'https://github.com/a/b',
             summary, '2026-11-06 08:00', '', ''))
        for i, text in enumerate(chunks):
            self.conn.execute('INSERT INTO chunks(path,seq,heading,text) VALUES(?,?,?,?)',
                              (path, i, '', text))
        self.conn.commit()

    def test_markup_is_stripped_down_to_the_readable_text(self):
        self.assertEqual('kimi-k3-in-c 享受新闻本身。其他的交给 Horizon 2.78T 参数 GPU',
                         daily_excerpt.clean(README))

    def test_text_that_only_looks_like_markup_survives(self):
        text = 'List<T> 和 Vec<String> 都是泛型;中文正文一大段不带空格也照留' * 3
        self.assertEqual(text, daily_excerpt.clean(text))

    def test_reads_on_until_there_is_something_to_say(self):
        self.add('source/rss/a.md', '<div align="center"', README,
                 'Horizon 是一个聚合新闻阅读器。')
        got = daily_excerpt.from_chunks(self.conn, 'source/rss/a.md', 400)
        self.assertEqual('kimi-k3-in-c 享受新闻本身。其他的交给 Horizon 2.78T 参数 GPU'
                         ' Horizon 是一个聚合新闻阅读器。', got)
        self.assertEqual(got[:20], daily_excerpt.from_chunks(self.conn, 'source/rss/a.md', 20))

    def test_a_clean_first_chunk_is_taken_verbatim(self):
        """干净的正文一个字节都不许变(连换行):摘要进素材指纹,白白换掉的话升级那一
        刻今天带走的每一条都被面板报成「再生成能多带进来」。"""
        text = '本文由英文原文自动翻译，专有名词保留原文；如有歧义请对照原文。\n第一段\n\n第二段  '
        self.add('source/rss/a.md', text, '第二个 chunk 不该被读进来')
        self.assertEqual(text.strip(), daily_excerpt.from_chunks(self.conn, 'source/rss/a.md', 400))

    def test_collect_sends_the_clean_excerpt_and_keeps_summaries(self):
        since, until = datetime(2026, 11, 5, 8, 57), datetime(2026, 11, 6, 8, 57)
        self.add('source/rss/a.md', '<div align="center"', README)
        self.add('source/rss/b.md', README, summary='模型提取过的摘要')
        got = {d['path']: d['excerpt'] for d in daily.collect(self.conn, since, until).items}
        self.assertEqual({'source/rss/a.md': daily_excerpt.clean(README),
                          'source/rss/b.md': '模型提取过的摘要'}, got)

    def test_nothing_but_markup_is_no_excerpt_not_markup(self):
        self.add('source/rss/c.md', '<div align="center"', '<p align="center"')
        self.assertEqual('', daily_excerpt.from_chunks(self.conn, 'source/rss/c.md', 400))


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
