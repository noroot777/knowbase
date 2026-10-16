# -*- coding: utf-8 -*-
"""手动收录那几组用例共用的夹具(kb/feeds/grab.py + grab_page.py)。

**打桩的那几处就是这一块的全部外部依赖**:临时 feeds.db、临时 kb.db(`_indexed`
要只读打开它)、临时 vault(导出落盘和「那一篇还在吗」都读它)、维护锁的
lockfile。少指一个,那一组用例就会去问**生产**那一份,答案跟着本机内容变、而且
不可重放。

**判据一律先断言后果**(各组用例共同的方法论):抓不成的那几档都要钉「库里确实
没有这一条」,而不是只验它抛了异常 —— 空正文一旦落库,`put_items` 的
`DO NOTHING` 之下永久改不回来,那正是这一块唯一不可恢复的失败形态。反过来也钉
一边:该收的照旧收得下、重复投递不改动库里那一份。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml

from kb import config, maintenance, pipeline, pipeline_state
from kb.collectors import FetchScope
from kb.feeds import grab, grab_page, registry as reg, sync, x_article
from kb.feeds.fetching import Response
from kb.source_types import feed_types

TWEET = 'https://x.com/example/status/12345'
PAGE = 'https://example.org/posts/hello'


def tweet_payload(**tweet) -> bytes:
    base = {'url': TWEET, 'text': '随手记一笔', 'created_timestamp': 1792063812,
            'author': {'name': '某某', 'screen_name': 'example'}}
    return json.dumps({'code': 200, 'tweet': {**base, **tweet}}).encode()


def page_html(*, title='Hello world', body=None, extra='') -> str:
    # 段落要够长:`readable.MIN_CHARS` 是 400,比它短的一律算「没找到正文」。
    paras = body or ['这是正文第一段，长度要过得了提取器那道门槛。' * 12,
                     '第二段同样要够长，否则 readable 会判成"没找到正文"。' * 12]
    return ('<html><head><meta charset="utf-8">'
            f'<meta property="og:title" content="{title}">{extra}'
            '</head><body><nav>导航 首页 关于</nav><article>'
            + ''.join(f'<p>{p}</p>' for p in paras)
            + '</article><footer>版权所有</footer></body></html>')


def html_resp(html: str, url: str = PAGE) -> Response:
    return Response(200, url, html.encode('utf-8'),
                    {'Content-Type': 'text/html; charset=utf-8'})


class GrabCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # 维护锁的 lockfile 在 DATA_DIR 下:不指到 tmp 的话这些用例会去 flock
        # 生产那一个(口径同 tests/test_feed_sources.py)。
        # `config.DB_PATH` 是 `grab._indexed` 要只读打开的那个库:不指到 tmp 的话
        # 「这一篇索引进去了没有」会去问**生产那一个** kb.db —— 答案跟着本机内容
        # 变,而且这组用例从此不可重放。
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(config, 'DB_PATH', self.root / 'kb.db'),
                  # `_indexed` 要读 vault 里那一篇算 hash:`config.VAULT_ROOT` 和
                  # 导出用的 `sync.VAULT_ROOT` 各打一次(生产里是同一个值)。
                  patch.object(config, 'VAULT_ROOT', self.root / 'vault'),
                  patch.object(sync, 'VAULT_ROOT', self.root / 'vault')):
            p.start()
            self.addCleanup(p.stop)
        self.state = deepcopy(pipeline_state._state)
        pipeline_state._state.update(running=False, stop_requested=False)
        self.addCleanup(lambda: pipeline_state._state.update(self.state))

    def items(self) -> list[dict]:
        with reg.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM feed_items ORDER BY key')]

    def export_vault(self, path: str, text: str = '# A\n\n正文') -> None:
        """假装导出阶段把 vault 里那一篇写了出来(只写文件,不碰索引)。"""
        full = config.VAULT_ROOT / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding='utf-8')

    def index_doc(self, path: str, text: str = '# A\n\n正文') -> None:
        """假装导出 + 索引都走完了:vault 里有这一篇,`documents` 记着**它当前
        这一版**的 `content_hash`。

        **两边一起摆才试得出东西。** `grab._indexed` 问的是版本(口径借
        `scanner.file_hash` + `store.doc_needs_reindex`),只往 `documents` 插一行
        路径的话,「库里还是上一版」那档漏报在用例里压根显不出来 —— 第二轮审查
        逮到的正是这个形态。`store.connect()` 这条路要加载 sqlite_vec、跑一串
        迁移(还要维护锁),为了回答「搜得到吗」不该付这笔钱。
        """
        from kb.scanner import file_hash

        self.export_vault(path, text)
        conn = sqlite3.connect(self.root / 'kb.db')
        conn.execute('CREATE TABLE IF NOT EXISTS documents'
                     ' (path TEXT PRIMARY KEY, content_hash TEXT)')
        conn.execute('INSERT INTO documents (path,content_hash) VALUES (?,?)'
                     ' ON CONFLICT(path) DO UPDATE SET content_hash=excluded.content_hash',
                     (path, file_hash(text)))
        conn.commit()
        conn.close()

    def pending_export(self) -> int:
        """下一轮 export 会选中几条(判据逐字照 `sync._export` 那一句 WHERE)。"""
        with reg.db() as c:
            return c.execute('SELECT COUNT(*) FROM feed_items WHERE kind=?'
                             ' AND exported_hash<>content_hash', ('inbox',)).fetchone()[0]

    def mark_exported(self, url: str, path: str) -> None:
        """假装导出阶段写过 vault 里那一篇(`sync._export` 落的就是这两列)。"""
        with reg.db() as c:
            c.execute('UPDATE feed_items SET path=?,exported_hash=content_hash'
                      ' WHERE url=?', (path, url))

    def grab_tweet(self, **tweet):
        """抓一条推文:`request` 打桩成 fxtwitter 的回包。

        **打的是 `grab.request`,不是 `fetching.request`** —— grab 在模块顶层
        import 了它;长文那一步走的是 `x_article.request`,各打各的,于是"主路和
        后路各注入一次"分得开(元规则「两份实现各有自己的回归」)。
        """
        with patch.object(grab, 'request', return_value=Response(
                200, TWEET, tweet_payload(**tweet), {'Content-Type': 'application/json'})):
            return grab.grab(TWEET)
