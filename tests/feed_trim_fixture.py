"""存量清理脚本(scripts/feed_trim_history.py)的测试夹具,两个测试文件共用。

`test_feed_trim.py` 验清理脚本自己那摊事,`test_index_lock.py` 验它和索引之间的
互斥 —— 两边都要「一条旧文同时落在 feeds.db / vault / kb.db 三处」这份摆盘,
各写一份的话两边的前提会各漂各的。
"""
import argparse
import importlib.util
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kb import maintenance, store
from kb.feeds import registry as reg, sync

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    'feed_trim_history', ROOT / 'scripts' / 'feed_trim_history.py')
trim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trim)

OLD = 'https://example.org/old-post'
_real_connect = store.connect
_real_busy = trim._pipeline_busy          # setUp 会把它换成桩,先留一份真的


class TrimFixture:
    """把 feeds.db / vault / kb.db / 维护锁文件全指到一个临时目录里。

    `maintenance.DATA_DIR` 也要指过来:不指的话这些用例会去 flock 生产那个
    lockfile —— 既可能被一轮真在跑的索引堵住,也可能反过来把它堵住。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / 'vault'
        for target, attr, value in ((reg, 'DB_PATH', self.root / 'feeds.db'),
                                    (reg, 'DATA_DIR', self.root),
                                    (maintenance, 'DATA_DIR', self.root),
                                    (trim, 'DATA_DIR', self.root),
                                    (trim, 'VAULT_ROOT', self.vault),
                                    (sync, 'VAULT_ROOT', self.vault)):
            p = patch.object(target, attr, value)
            p.start(); self.addCleanup(p.stop)
        for p in (patch.object(store, 'connect', lambda *a, **k: _real_connect(self.root / 'kb.db')),
                  patch.object(trim, '_pipeline_busy', return_value='')):
            p.start(); self.addCleanup(p.stop)

    def sub(self, name='Review A', url='https://example.org/a.xml'):
        return reg.save_subscription('rss', {'name': name, 'url': url})

    def entry(self, url=OLD, published='2026-07-01 08:00'):
        return dict(url=url, title='Historical post', body='historical review content',
                    published=published, author='Review', content_kind='feed')

    def legacy(self, source):
        """老库里的订阅:空下界(migrate 补出来就是这样)。"""
        with reg.db() as c:
            c.execute("UPDATE subscriptions SET history_floor='' WHERE id=?", (source['id'],))
        return reg.subscription('rss', source['id'])

    def seed(self, source, entry=None):
        """把一条旧文落到三处:feeds.db、vault、kb.db(含可检索的 chunk)。"""
        e = entry or self.entry()
        reg.put_items(source, [e])
        sync.export('rss')
        with reg.db() as c:
            item = dict(c.execute('SELECT * FROM feed_items WHERE url=?', (e['url'],)).fetchone())
        conn = store.connect()
        conn.execute('INSERT OR IGNORE INTO documents(path,source,title,published,indexed_at) '
                     'VALUES(?,?,?,?,?)', (item['path'], 'rss', item['title'],
                                           item['published'], '2026-10-01 09:00:00'))
        cid = conn.execute('INSERT INTO chunks(path,seq,text) VALUES(?,?,?)',
                           (item['path'], 0, e['body'])).lastrowid
        conn.execute('INSERT INTO chunks_fts(chunk_id,title_tok,text_tok) VALUES(?,?,?)',
                     (cid, 'historical', e['body']))
        conn.commit(); conn.close()
        return item

    def args(self, row, **over):
        # 形参叫 row 而不是 source:`source` 是 Namespace 里那个字段名,用例要能
        # `self.args(s, source='Only One')` 覆盖它(按名字查那几条就这么写)。
        base = dict(source=str(row['id']), floor=None, yes=True,
                    allow_shared=False, allow_favorites=False, verbose=False,
                    pending_translation=False)
        return argparse.Namespace(**(base | over))

    def state(self, item, source=None):
        with reg.db() as c:
            row = c.execute('SELECT exported_hash,content_hash FROM feed_items WHERE key=?',
                            (item['key'],)).fetchone()
            links = c.execute('SELECT count(*) FROM subscription_items WHERE item_key=?',
                              (item['key'],)).fetchone()[0]
        conn = store.connect()
        docs = conn.execute('SELECT count(*) FROM documents WHERE path=?',
                            (item['path'],)).fetchone()[0]
        hits = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'historical'"
                            ).fetchone()[0]
        conn.close()
        return dict(feed_row=bool(row), memberships=links,
                    exported=bool(row and row['exported_hash'] == row['content_hash']),
                    file=(self.vault / item['path']).exists(), documents=docs, searchable=hits)

    def favorites(self):
        conn = store.connect()
        n = conn.execute('SELECT count(*) FROM favorites').fetchone()[0]
        conn.close()
        return n

    def documents_without_a_file(self):
        """库里指向不存在文件的那些行 —— 「复活」最后留下的痕迹就是它。"""
        conn = store.connect()
        try:
            return [r['path'] for r in conn.execute('SELECT path FROM documents')
                    if not (self.vault / r['path']).exists()]
        finally:
            conn.close()

    def floor_of(self, source):
        return reg.subscription('rss', source['id'])['history_floor']

    def aged(self, days, url=None):
        """发布于 N 天前的一条 —— 口径用例拿它和那道闸对表。"""
        day = (datetime.now(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=days)).isoformat()
        return {'url': url or f'https://example.org/p{days}', 'title': 'x', 'body': 'y',
                'published': f'{day} 08:00', 'author': 'a', 'content_kind': 'feed'}
