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
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from kb import config, extract, maintenance, pipeline_stages, scanner, store
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

    def test_a_missing_registry_with_feed_candidates_raises(self):
        """库文件没了、而 vault 里还躺着 feeds 文档 —— 抛,不许静默抹平。

        这条路为了补列迁移会借一次 `reg.db()`,而它是**幂等地确保库在**的入口:
        不先判文件在不在的话,它会把一个空库连同默认订阅一起建出来,于是「库被
        删了」这种该让人看一眼的状态变成「一条开着提取的订阅都没有」,所有 feeds
        文档从此不提取,而面板上一切正常(定点注入实测:去掉那道判断红 0 条 ——
        原来的两条用例一条验「没有 feeds 候选」、一条验「库是坏的」,都够不着
        这一档)。
        """
        sub = reg.save_subscription('rss', {'name': '聚合源', 'extract': True,
                                            'url': 'https://a.example.org/feed.xml'})
        import hashlib
        key = hashlib.sha256(b'rss:https://a.example.org/1').hexdigest()[:32]
        reg.put_items(sub, [{'url': 'https://a.example.org/1', 'title': 'T', 'body': 'B',
                             'published': '2026-10-08 09:00', 'content_kind': 'feed'}])
        self.doc('rss', key)
        reg.DB_PATH.unlink()
        with self.kb() as c:
            with self.assertRaises(Exception):
                extract.pending_docs(c)
        self.assertFalse(reg.DB_PATH.exists(), '不许顺手把库建回来')

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


class LegacyRegistryTests(OptInCase):
    """升级后第一条命令就是提取时,补列迁移必须先于这次只读 JOIN 发生。"""

    def _legacy_registry(self):
        """裸 sqlite3 造一个「升级前」的注册库:有订阅、有条目、**没有 extract 列**。

        全程不碰 `reg.db()` —— 碰一下迁移就跑了,这条用例的前提当场消失。
        """
        with closing(sqlite3.connect(reg.DB_PATH)) as c:
            c.executescript("""
                CREATE TABLE subscriptions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL,
                  url TEXT NOT NULL, feed_url TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'feed',
                  group_name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                  config TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE(kind, url));
                CREATE TABLE subscription_items (
                  subscription_id INTEGER NOT NULL, item_key TEXT NOT NULL,
                  PRIMARY KEY(subscription_id, item_key));
                INSERT INTO subscriptions (kind,name,url,created_at,updated_at)
                  VALUES('rss','老订阅','https://old.example.org/feed.xml','x','x');
                INSERT INTO subscription_items VALUES(1,'oldkey');
            """)
            c.commit()

    def test_a_cold_extract_on_a_legacy_registry_does_not_blow_up(self):
        """升级后直接提取:补列迁移由这条路自己保证,不靠「先打开一次面板」。

        不保证的后果是 `no such column: s.extract`,`kb extract` 退出码 1 ——
        而先去面板点一下再跑同一条命令就好了(那正是最难查的一类)。
        """
        self._legacy_registry()
        self.doc('rss', 'oldkey')                     # 候选里有 feeds 文档,才会去 ATTACH
        with self.kb() as c:
            self.assertEqual([], self.pending_of(c))  # 老订阅补 0 = 不提取,但不许报错
        with closing(sqlite3.connect(reg.DB_PATH)) as c:
            cols = {r[1] for r in c.execute('PRAGMA table_info(subscriptions)')}
        self.assertIn('extract', cols, '这条路自己要把补列做掉')

    def test_a_legacy_registry_still_lets_the_opted_in_through(self):
        """反向钉一边:迁移跑完之后,开着的订阅照旧放行 ——
        一个「老库一律拦死」的实现在上面那条里全绿。"""
        self._legacy_registry()
        self.doc('rss', 'oldkey')
        with closing(sqlite3.connect(reg.DB_PATH)) as c:   # 先让迁移跑一遍
            pass
        reg.subscriptions('rss')
        with closing(sqlite3.connect(reg.DB_PATH)) as c:
            c.execute('UPDATE subscriptions SET extract=1'); c.commit()
        self.assertEqual(['source/rss/oldkey.md'], self.pending())

    def pending_of(self, conn):
        return sorted(r['path'] for r in extract.pending_docs(conn))


class AwkwardDataDirTests(unittest.TestCase):
    """数据目录名里有 `\'` / `#` / `?` 时,整条判据照常成立。

    三种字符各走一条不同的坏路:单引号把拼进 SQL 的字符串截断
    (`near "s": syntax error`);`#` 和 `?` 不报错 —— URI 解析把地址在那儿截成
    **另一个库**,于是查 `feedsreg.subscriptions` 说表不存在,还在截断处凭空
    建一个文件,连 `mode=ro` 一起丢掉(2026-10-11 审查实测)。
    """

    def _run_in(self, dirname: str) -> list[str]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / dirname
        (root / 'vault' / 'source' / 'rss').mkdir(parents=True)
        for target, attr, value in (
                (reg, 'DB_PATH', root / 'feeds.db'),
                (maintenance, 'DATA_DIR', root),
                (config, 'DB_PATH', root / 'kb.db'),
                (scanner, 'VAULT_ROOT', root / 'vault'),
                (extract, 'VAULT_ROOT', root / 'vault')):
            p = patch.object(target, attr, value)
            p.start(); self.addCleanup(p.stop)
        sub = reg.save_subscription('rss', {'name': '技术博客', 'extract': True,
                                            'url': 'https://b.example.org/feed.xml'})
        key = hashlib.sha256(b'rss:https://b.example.org/1').hexdigest()[:32]
        reg.put_items(sub, [{'url': 'https://b.example.org/1', 'title': 'T', 'body': 'B',
                             'published': '2026-10-08 09:00', 'content_kind': 'feed'}])
        md = root / 'vault' / 'source' / 'rss' / f'{key}.md'
        md.write_text('---\nsource: rss\ntitle: T\n---\n\n# T\n\n正文若干字。\n',
                      encoding='utf-8')
        d = scanner._doc_from_file(md, Source('rss', md.parent))
        with closing(store.connect(config.DB_PATH)) as c:
            store.upsert_document(c, d, split_chunks(d.body), reg.now())
            c.commit()
            rows = sorted(r['path'] for r in extract.pending_docs(c))
        # 顺带钉住「没在别处凭空建出一个库」:`#`/`?` 那两档的症状之一就是
        # 在截断处留下一个文件,而它看起来只是「查不到表」。
        stray = [q.name for q in root.parent.iterdir() if q != root]
        self.assertEqual([], stray, f'在数据目录外建了东西:{stray}')
        return rows

    def test_a_quote_in_the_path(self):
        self.assertEqual(['source/rss/' + hashlib.sha256(
            b'rss:https://b.example.org/1').hexdigest()[:32] + '.md'],
            self._run_in("reader's data"))

    def test_a_hash_in_the_path(self):
        self.assertEqual(1, len(self._run_in('reader#data')))

    def test_a_question_mark_in_the_path(self):
        self.assertEqual(1, len(self._run_in('reader?data')))


class StageOutcomeTests(OptInCase):
    """队列压根没读出来时,这一步的**最终**状态必须是红的。

    判据落在收尾之后那一次 `_stage` 上,不是中途那一次 —— 中途落红、收尾又按
    「计数全 0」刷成绿色「没有待提取的文档」的实现,只看中途的话全绿,而真页面
    上同时显示红色错误日志和绿色「同步已完成」(2026-10-11 审查实测)。
    """

    def setUp(self):
        super().setUp()
        self.stages = []
        for p in (patch.object(pipeline_stages, '_stage',
                               side_effect=lambda *a, **k: self.stages.append((a, k))),
                  patch.object(pipeline_stages, '_log', lambda *a, **k: None)):
            p.start(); self.addCleanup(p.stop)

    def final(self):
        """最后一次阶段通知 —— 用户在面板上看到的就是它。"""
        return self.stages[-1][0] if self.stages else ()

    def test_a_single_extract_run_ends_red(self):
        with patch.object(extract, 'pending_docs', side_effect=RuntimeError('库读不到')):
            pipeline_stages._run_extract()
        self.assertEqual('error', self.final()[1], self.stages)
        self.assertIn('库读不到', self.final()[2])

    def test_the_two_pass_pipeline_ends_red_too(self):
        """管道里 crawl+extract 同选时走的是「存量 + 增量」两轮共用一份 stats
        (kb/pipeline.py 那段),收尾是同一个函数 —— 两条路都要钉。"""
        stats = pipeline_stages._new_extract_stats()
        with patch.object(extract, 'pending_docs', side_effect=RuntimeError('库读不到')):
            pipeline_stages._extract_pass('存量', stats)
            pipeline_stages._extract_pass('增量', stats)
        pipeline_stages._finish_extract_stage(stats)
        self.assertEqual('error', self.final()[1], self.stages)

    def test_the_reason_is_in_the_failure_detail(self):
        """面板上展开得到原因,不是只有一句红字。"""
        with patch.object(extract, 'pending_docs', side_effect=RuntimeError('库读不到')):
            pipeline_stages._run_extract()
        failures = self.stages[-1][1].get('failures') or []
        self.assertTrue(any('库读不到' in (f.get('reason') or '') for f in failures), failures)

    def test_an_empty_queue_is_still_green(self):
        """反向钉一边:真的没有待提取文档时照旧是绿的 ——
        一个「一律报红」的实现在上面三条里全绿。"""
        pipeline_stages._run_extract()
        self.assertEqual('ok', self.final()[1], self.stages)
        self.assertIn('没有待提取的文档', self.final()[2])


class ReadOnlyMountTests(OptInCase):
    """挂上来的订阅库是只读的,而且这条连接得真的认 `file:` 地址。"""

    def test_the_mounted_registry_rejects_writes(self):
        """`mode=ro` 真的生效 —— 跨库写在 WAL 下不是原子的,所以物理上禁掉。"""
        reg.save_subscription('rss', {'name': '聚合源', 'url': 'https://a.example.org/feed.xml'})
        with self.kb() as c:
            with extract._attached_feeds(c):
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    c.execute("UPDATE feedsreg.subscriptions SET extract=1")
        self.assertIn('readonly', str(caught.exception))

    def test_the_connection_asks_for_uri_support(self):
        """`store.connect` 必须显式传 `uri=True`。

        **这一条是过程断言,而且在本机恒绿** —— 本机 Homebrew 的 SQLite
        (3.53.2)编译时开了 `SQLITE_USE_URI`,不带这个参数 ATTACH 照样认
        `file:…?mode=ro`,上面那条后果断言因此对它完全不敏感(定点注入实测
        红 0 条)。而项目实际跑服务用的是 `uv run --offline` 那份(3.50.4,
        **没开**):在那边 ATTACH 一律 `unable to open database`,连普通目录
        都失败,整个待提取查询取不到结果 —— 混在里面的绿泡泡积压也跟着不处理
        (2026-10-11 审查实测)。
        跨构建的后果测不了,就把构造钉住。
        """
        with patch('sqlite3.connect', wraps=sqlite3.connect) as spy:
            store.connect(config.DB_PATH).close()
        self.assertTrue(spy.call_args.kwargs.get('uri'),
                        f'store.connect 没开 URI 支持:{spy.call_args}')


if __name__ == '__main__':
    unittest.main()
