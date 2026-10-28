# -*- coding: utf-8 -*-
"""「问题与待办」那几份回归共用的脚手架:临时 registry + 临时 vault。

**自己不带一条用例**(故意的):带用例的基类被 unittest 收走时会在每个子类里
再跑一遍,一次性 smoke 继承它就等于在执行回合里跑了回归。

种数据有两条路:`seed()` 摆整套样例(和浏览器探针用的是同一份
`tests/intake_diag_fixture.py`,两边同源 —— 面板上看到的形态就是这里断言的形态),
`put()` 摆一条自己捏的。
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kb import config, maintenance
from kb.feeds import registry as reg, sync
from tests import intake_diag_fixture as fixture


def key_of(kind: str, url: str) -> str:
    """条目身份:和 `registry.item_key` 同一条规则。**不去列表里取第 0 条** ——
    排序是 published DESC,同一时刻的几条谁在前面不该由用例来猜。"""
    return hashlib.sha256(f'{kind}:{url}'.encode()).hexdigest()[:32]


class DiagCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        # 维护锁的 lockfile 在 DATA_DIR 下,kb.db 在 config.DB_PATH 上:不指到 tmp
        # 的话这些用例会去 flock 生产那一个、写生产索引库(口径同
        # `tests/test_feed_fulltext.py` 的那段说明)。
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(config, 'DB_PATH', self.root / 'kb.db'),
                  patch.object(sync, 'VAULT_ROOT', self.root / 'vault')):
            p.start(); self.addCleanup(p.stop)

    def seed(self) -> dict:
        """摆整套样例,返回 `{'agg': id, ...}`。"""
        return fixture.populate()

    def put(self, source, url: str, *, body: str, title: str = '一条样例',
            published: str = '2026-10-08 09:00', **columns) -> str:
        """摆一条,顺手改几列。返回它的 key。"""
        reg.put_items(source, [{'url': url, 'title': title, 'body': body,
                                'published': published, 'content_kind': 'summary'}])
        key = key_of(source['kind'], url)
        if columns:
            sets = ','.join(f'{name}=?' for name in columns)
            with reg.db() as c:
                c.execute(f'UPDATE feed_items SET {sets} WHERE key=?',
                          (*columns.values(), key))
        return key

    def shelve(self, keys, kind: str, decision: str, reason: str = '') -> list[str]:
        """搁置这几条,返回**这几次决定的编号** —— 恢复那一下要绑它(见
        `shelf.release`)。顺手核一句返回的 key 数和计数对得上。"""
        from kb.intake_diag import shelf

        done = shelf.shelve(keys, kind, decision, reason)
        self.assertEqual(len(done['keys']), done['shelved'])
        self.assertEqual(len(done['keys']), len(done['decisions']))
        return done['decisions']

    def row(self, key: str) -> dict:
        with reg.db() as c:
            return dict(c.execute('SELECT * FROM feed_items WHERE key=?', (key,)).fetchone())

    def count(self, where: str, args=()) -> int:
        with reg.db() as c:
            return c.execute(f'SELECT count(*) FROM feed_items WHERE {where}',
                             list(args)).fetchone()[0]
