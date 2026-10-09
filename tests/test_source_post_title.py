# -*- coding: utf-8 -*-
"""「大V观点的条目不显示标题」的浏览器回归。

X 的采集器把推文正文的前 500 字符原样当成 title(`x_browser._entry` /
`x_feedgrab._entry`;桥接那条走 `fetching.parse_feed`,源没给标题时回退成正文
首行)—— 推文本来就没有标题这回事。于是来源面板上那行大标题和紧跟着的正文是同一
句话显示两遍,而标题那个 <button> 继承了 base.css 的 `white-space: nowrap`,长推文
一行到底、直接撑出卡片右边界。

页面那一侧由 tests/source_post_probe.mjs 真跑一遍(自带 mock 后端,CDP 直连
chrome-headless-shell,不装 playwright、不连网、始终无头、临时 profile);
这里另外钉住**采集器那一头的前提** —— 判据 `content_kind === 'post'` 认的是
"这条内容是一条动态",两条 X 采集器和桥接那条都得给这个值,不然前端那道判断
永远不成立,而页面上看起来只是"标题又回来了"。

跑:  python -m unittest tests.test_source_post_title -v
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.headless import probe_env, requires_headless  # noqa: E402

_PROBE = Path(__file__).resolve().parent / "source_post_probe.mjs"


class EntryShapeTests(unittest.TestCase):
    """采集器交给面板的那份条目形状:social 的每一条都是 `post`。"""

    def test_browser_channel_marks_tweets_as_post(self):
        from kb.feeds import x_browser
        entry = x_browser._entry({'url': 'https://x.com/someone/status/1',
                                  'text': '一条推文', 'datetime': '2026-10-09T06:58:00.000Z',
                                  'name': '某人', 'author': 'someone'})
        self.assertEqual('post', entry['content_kind'])
        # 标题就是正文(这正是面板上那行重复的来源),所以它不能当"内容叫什么"用
        self.assertEqual(entry['body'], entry['title'])

    def test_feedgrab_channel_marks_tweets_as_post(self):
        from kb.feeds import x_feedgrab
        entry = x_feedgrab._entry({'source': 'https://x.com/someone/status/2',
                                   'author_name': '某人', 'title': '一条推文'},
                                  '一条推文', 'someone')
        self.assertEqual('post', entry['content_kind'])

    def test_rss_articles_are_not_post(self):
        """反过来钉一边:订阅文章有真标题,这一档照旧要显示它。"""
        from kb.feeds import fetching
        feed = ('<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
                '<item><title>一篇文章的标题</title><link>https://example.org/a</link>'
                '<description>正文</description></item></channel></rss>')
        entries = fetching.parse_feed(feed.encode(), 'https://example.org/feed', social=False)
        self.assertEqual('summary', entries[0]['content_kind'])
        self.assertNotEqual(entries[0]['body'], entries[0]['title'])


@requires_headless
class BrowserTests(unittest.TestCase):
    """真页面:大V观点不显示标题、正文不溢出卡片,而 RSS 照旧有标题。"""

    def test_posts_render_without_a_title(self):
        r = subprocess.run([shutil.which("node"), str(_PROBE)],
                           capture_output=True, text=True, timeout=300,
                           env=probe_env())
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
