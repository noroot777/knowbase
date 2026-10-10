# -*- coding: utf-8 -*-
"""订阅级的提取开关:feeds 那几类来源默认不买提取,逐条订阅自己说了算。

被测的那件事:`extract.pending_docs` 对 rss / social / web / digest 的文档多问
一句「它属的那条订阅开了 `subscriptions.extract` 吗」。订阅在 feeds.db、文档在
kb.db,判据靠 ATTACH 跨库 JOIN 落在同一条 SQL 里。

**判据落在后果上,不在过程上。** 「有没有 ATTACH」「SQL 里有没有那个子句」这类
断言,一个什么都没拦住的实现也能全绿;所以每条用例问的都是「这一篇最终进没进
`pending_docs`」——它是提取队列和面板那个「当前待处理 N 项」的同一个口径。

**反过来也钉住**:只验「该拦的拦住了」会让一个把 feeds 一律拦死、顺手把绿泡泡
也拦掉的实现全绿,所以非 feeds 来源照旧全量进队列那一条单独有用例;同理「开了
开关就该放行」也各有一条。

路径契约那组是这套判据的承重墙:两个库之间唯一能对上的是
`source/<kind>/<item_key>.md` 这个拼法,它由 `sync._export` 落盘、由
`extract._FEED_DOC_PATH` 在 SQL 里重建。那边哪天改了落盘路径,这边会**静默**
匹配不上(开着开关的订阅一篇都排不进队列),所以用真导出跑一遍再比。
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from kb import config, extract, maintenance, scanner, store
from kb.chunker import split_chunks
from kb.config import Source
from kb.feeds import registry as reg, sync


class OptInCase(unittest.TestCase):
    """脚手架:临时 feeds.db + 临时 kb.db + 临时 vault。自己不带断言。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vault = self.root / 'vault'
        # 维护锁的 lockfile 在 DATA_DIR 下:不指到 tmp 的话这些用例会去 flock
        # 生产那一个,被 kb-web 正在跑的一轮索引挂住(见 kb.maintenance)。
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(config, 'DB_PATH', self.root / 'kb.db'),
                  patch.object(scanner, 'VAULT_ROOT', self.vault),
                  patch.object(extract, 'VAULT_ROOT', self.vault),
                  patch.object(sync, 'VAULT_ROOT', self.vault)):
            p.start(); self.addCleanup(p.stop)

    def kb(self):
        # `store.connect` 的默认参数在函数定义时就绑死了,所以和被测代码一样
        # 显式现读 config.DB_PATH。
        return closing(store.connect(config.DB_PATH))

    def doc(self, source: str, name: str, body: str = '正文若干字。') -> str:
        """往 vault 里放一篇 md 并按索引那条真路径扫进 kb.db,返回它的相对路径。

        走 `_doc_from_file` + `upsert_document` 而不是手摆 documents 行:
        `content_hash` 的口径只有 `scanner.file_hash` 一份,手摆出来的是一个
        生产里不存在的状态(见 CLAUDE.md「提取阶段的两道判定」)。
        """
        md = self.vault / 'source' / source / f'{name}.md'
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(f'---\nsource: {source}\ntitle: {name}\n---\n\n# {name}\n\n{body}\n',
                      encoding='utf-8')
        d = scanner._doc_from_file(md, Source(source, self.vault / 'source' / source))
        with self.kb() as c:
            store.upsert_document(c, d, split_chunks(d.body), reg.now())
            c.commit()
        return d.path

    def pending(self) -> list[str]:
        with self.kb() as c:
            return sorted(r['path'] for r in extract.pending_docs(c))


class GateTests(OptInCase):
    """默认不走 / 开了才走 / 非 feeds 来源不受影响。"""

    def test_feed_doc_is_held_back_by_default(self):
        sub = reg.save_subscription('rss', {'name': '聚合源', 'url': 'https://a.example.org/feed.xml'})
        key = self.put(sub, 'https://a.example.org/1')
        path = self.doc('rss', key)
        # 东西确实在库里、也确实还没提取 —— 拦住它的是开关,不是别的
        with self.kb() as c:
            self.assertIsNone(c.execute(
                'SELECT extracted_at FROM documents WHERE path=?', (path,)).fetchone()[0])
        self.assertEqual([], self.pending())

    def test_opt_in_puts_it_in_the_queue(self):
        sub = reg.save_subscription('rss', {'name': '聚合源', 'url': 'https://a.example.org/feed.xml',
                                            'extract': True})
        key = self.put(sub, 'https://a.example.org/1')
        path = self.doc('rss', key)
        self.assertEqual([path], self.pending())

    def test_toggling_on_later_brings_it_back(self):
        """默认关不是终态:`extracted_at` 还是 NULL,开关一开就自动回队列。"""
        sub = reg.save_subscription('rss', {'name': '聚合源', 'url': 'https://a.example.org/feed.xml'})
        key = self.put(sub, 'https://a.example.org/1')
        path = self.doc('rss', key)
        self.assertEqual([], self.pending())
        reg.save_subscription('rss', {'extract': True}, sub['id'])
        self.assertEqual([path], self.pending())

    def test_non_feed_sources_are_untouched(self):
        """反向钉一边:绿泡泡和别的非 feeds 来源照旧全量进队列。

        少了这一条,一个「凡是没开开关的一律拦掉」的实现(把 dr-gzh 那 9000 多篇
        一起拦死)在上面几条用例下全绿。
        """
        paths = [self.doc('dr-gzh', 'gzh-1'), self.doc('dr', 'dr-1')]
        self.assertEqual(sorted(paths), self.pending())

    def test_opted_in_and_held_back_coexist(self):
        """同一类来源里,开了的进、没开的不进 —— 判据是逐条订阅,不是整类。"""
        on = reg.save_subscription('rss', {'name': '技术博客', 'url': 'https://b.example.org/feed.xml',
                                           'extract': True})
        off = reg.save_subscription('rss', {'name': '快讯', 'url': 'https://c.example.org/feed.xml'})
        want = self.doc('rss', self.put(on, 'https://b.example.org/1'))
        self.doc('rss', self.put(off, 'https://c.example.org/1'))
        self.assertEqual([want], self.pending())

    def test_shared_item_passes_when_any_owner_opted_in(self):
        """一个条目可能同时属于几条订阅:任一开着就提取。

        漏提取可恢复(开关一开自动回队列),静默多提取只是多几个标签 —— 两个
        方向不对等,所以这里取并集而不是交集。
        """
        on = reg.save_subscription('rss', {'name': '技术博客', 'url': 'https://b.example.org/feed.xml',
                                           'extract': True})
        off = reg.save_subscription('rss', {'name': '快讯', 'url': 'https://c.example.org/feed.xml'})
        url = 'https://shared.example.org/post'
        key = self.put(off, url)
        self.assertEqual(key, self.put(on, url))   # 同一个 key,两条订阅各挂一次
        self.assertEqual([self.doc('rss', key)], self.pending())

    def test_limit_applies_after_the_gate(self):
        """`limit` 截的是过滤之后的结果,不是先截再过滤。

        反过来写的话,`retry_failed_plan` 那句 `pending_docs(conn, limit=1)` 会
        在「第一条恰好是被拦下的 feeds 文档」时报「没有积压」,而绿泡泡那边
        明明还排着。
        """
        sub = reg.save_subscription('rss', {'name': '快讯', 'url': 'https://c.example.org/feed.xml'})
        self.doc('rss', self.put(sub, 'https://c.example.org/1'))
        kept = self.doc('dr-gzh', 'gzh-1')
        with self.kb() as c:
            self.assertEqual([kept], [r['path'] for r in extract.pending_docs(c, limit=1)])

    def put(self, sub: dict, url: str) -> str:
        """把一个条目挂到这条订阅下,返回它的 item_key(= vault 文件名)。"""
        import hashlib
        reg.put_items(sub, [{'url': url, 'title': 'T', 'body': 'B',
                             'published': '2026-10-08 09:00', 'content_kind': 'feed'}])
        return hashlib.sha256(f"{sub['kind']}:{url}".encode()).hexdigest()[:32]


class PathContractTests(OptInCase):
    """两个库之间唯一的接缝:`source/<kind>/<item_key>.md`。"""

    def test_exported_path_is_what_the_join_rebuilds(self):
        """真跑一遍导出,再按 SQL 里那个拼法反查 —— 两边必须指到同一篇。

        这组不是在重复上面的 Gate 用例:那边的 md 是用例自己按约定摆的,只能
        证明「判据对我摆的盘成立」。这里的 md 由 `sync._export` 真写出来,
        它哪天改了落盘路径(换目录、换文件名、加一层按日期分的子目录),
        上面那些用例照样全绿,而生产里开着开关的订阅会**一篇都排不进队列**。
        """
        sub = reg.save_subscription('rss', {'name': '技术博客', 'url': 'https://b.example.org/feed.xml',
                                            'extract': True})
        reg.put_items(sub, [{'url': 'https://b.example.org/1', 'title': 'Hello',
                             'body': '正文若干字。', 'published': '2026-10-08 09:00',
                             'content_kind': 'feed'}])
        with patch.object(sync, '_stage', lambda *a, **k: None):
            sync.export('rss')
        written = sorted((self.vault / 'source' / 'rss').glob('*.md'))
        self.assertEqual(1, len(written), '导出应当落一篇 md')
        rel = str(written[0].relative_to(self.vault))

        # SQL 里那个拼法(extract._FEED_DOC_PATH)对同一条订阅算出来的路径
        with closing(sqlite3.connect(reg.DB_PATH)) as c:
            rebuilt = [r[0] for r in c.execute(
                f"SELECT {extract._FEED_DOC_PATH} FROM subscriptions s "
                "JOIN subscription_items si ON si.subscription_id = s.id")]
        self.assertEqual([rel], rebuilt,
                         'sync._export 的落盘路径和 extract._FEED_DOC_PATH 的拼法对不上了')

        # 再走一遍整条判据:导出的这篇真的排进了队列
        d = scanner._doc_from_file(written[0], Source('rss', self.vault / 'source' / 'rss'))
        with self.kb() as c:
            store.upsert_document(c, d, split_chunks(d.body), reg.now())
            c.commit()
        self.assertEqual([rel], self.pending())


class UnreadableRegistryTests(OptInCase):
    """feeds.db 读不到时:抛,不静默放行也不静默拦死。"""

    def test_broken_registry_raises(self):
        """坏掉的 feeds.db 必须抛。

        静默返回空(当成「一条订阅都没开」)会让所有 feeds 来源的提取悄悄停住,
        而面板上「当前待处理」是 0、阶段是绿的 —— 和「真的没有积压」长得一模
        一样;静默放行则会在开关还没来得及读出来的时候按老口径全量提取。
        """
        sub = reg.save_subscription('rss', {'name': '聚合源', 'url': 'https://a.example.org/feed.xml',
                                            'extract': True})
        import hashlib
        key = hashlib.sha256(b'rss:https://a.example.org/1').hexdigest()[:32]
        reg.put_items(sub, [{'url': 'https://a.example.org/1', 'title': 'T', 'body': 'B',
                             'published': '2026-10-08 09:00', 'content_kind': 'feed'}])
        self.doc('rss', key)
        reg.DB_PATH.write_bytes(b'this is not a database')
        with self.kb() as c:
            with self.assertRaises(Exception):
                extract.pending_docs(c)

    def test_missing_registry_is_fine_without_feed_candidates(self):
        """反向钉一边:纯绿泡泡的库根本不碰 feeds.db。

        全新安装、或者从来没用过订阅的库压根没有那个文件,为它抛是在报一个
        不存在的故障 —— 而且这个函数坐在面板每次刷新那条路上。
        """
        path = self.doc('dr-gzh', 'gzh-1')
        self.assertFalse(reg.DB_PATH.exists(), '这一条的前提就是 feeds.db 不存在')
        self.assertEqual([path], self.pending())
        self.assertFalse(reg.DB_PATH.exists(), '不该为了问一句开关把 feeds.db 建出来')


class ColumnTests(unittest.TestCase):
    """这一列的持久化:老库补 0、换链接不清掉用户的选择。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        p = patch.object(reg, 'DB_PATH', self.root / 'feeds.db')
        p.start(); self.addCleanup(p.stop)

    def test_legacy_rows_backfill_to_off(self):
        """老库里的订阅一律补 0 —— 这是一次有意的口径收紧。

        补成 1(「保持原样」)会让存量那几十条聚合订阅继续往标签里灌噪声,
        而用户以为自己拿到的是新规则。
        """
        with closing(sqlite3.connect(self.root / 'feeds.db')) as c:
            c.execute('''CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL,
                url TEXT NOT NULL, feed_url TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'feed',
                group_name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                config TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(kind, url))''')
            c.execute("INSERT INTO subscriptions (kind,name,url,created_at,updated_at) "
                      "VALUES('rss','老订阅','https://old.example.org/feed.xml','x','x')")
            c.commit()
        self.assertEqual(0, reg.subscriptions('rss')[0]['extract'])

    def test_changing_the_link_keeps_the_choice(self):
        """换链接清的是采集进度,不是用户的配置(口径同 `fulltext`)。"""
        sub = reg.save_subscription('rss', {'name': '技术博客', 'url': 'https://b.example.org/feed.xml',
                                            'extract': True, 'fulltext': True})
        after = reg.save_subscription('rss', {'url': 'https://b2.example.org/feed.xml'}, sub['id'])
        self.assertEqual(1, after['extract'])
        self.assertEqual(1, after['fulltext'])
        self.assertEqual('', after['etag'], '采集进度该清的还是要清')

    def test_new_subscription_defaults_to_off(self):
        sub = reg.save_subscription('rss', {'name': '新订阅', 'url': 'https://n.example.org/feed.xml'})
        self.assertEqual(0, sub['extract'])


if __name__ == '__main__':
    unittest.main()
