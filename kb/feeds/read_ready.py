# -*- coding: utf-8 -*-
"""只读连接之前:这个库是不是已经是当前代码要的样子(`registry.read_db` 用)。

只读连接不建表也不补列,升级后第一问若是它,就撞上 `no such column`。可**已经迁好的库
不许为此去抢写锁** —— 那正是 `read_db` 存在的理由(别人握着写事务多久,`db()` 就排多久)。
所以分三档:

1. 这个进程里已经有一次 `db()` 跑完了建表补列(`mark`),直接放行;
2. 没有的话,用**只读连接**比一比库的样子:表、列、索引、触发器,外加 `feed_meta` 里那几
   个一次性迁移留下的记号。比的标准不手抄,是拿 `build` 在内存库上从零建一遍得出来的 ——
   和 `db()` 走的是同一份建表补列(`registry._upgrade`),改了迁移这里自动跟上。够了就
   登记、放行,全程不碰写锁;
3. 真缺东西才开一次 `db()`,而且**只用调用方剩下的那点时限**:`read_db(timeout=…)` 的
   调用方(X 长文那几问给的是整笔 10 秒)说的是这一问总共等多久,不是迁移另算 30 秒。

同一个库的首访在进程内排队:几问同时到达时只有一问去比、去迁,其余等它的结果,同样只等
各自剩下的时限。认 `(路径, inode)`:测试会在同一个路径上删掉重建一个库。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path

_READY: set[tuple[str, int]] = set()
_GUARD = threading.Lock()
_FIRST: dict[str, threading.Lock] = {}
_EXPECTED: tuple[dict, dict] | None = None


def _key(path: Path) -> tuple[str, int]:
    return str(path.resolve()), path.stat().st_ino


def mark(path: Path) -> None:
    """`db()` 跑完建表补列之后登记一句:这个进程里这个库已经是当前的样子。"""
    with suppress(FileNotFoundError):
        _READY.add(_key(path))


def _shape(conn) -> tuple[dict, dict]:
    objects = {}
    for kind, name in conn.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall():
        columns = frozenset(row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')) \
            if kind == 'table' else frozenset()
        objects[name] = (kind, columns)
    meta = dict(conn.execute('SELECT key,value FROM feed_meta').fetchall())
    return objects, meta


def _expected(build) -> tuple[dict, dict]:
    global _EXPECTED
    if _EXPECTED is None:
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            build(conn)
            _EXPECTED = _shape(conn)
        finally:
            conn.close()
    return _EXPECTED


def _covers(have: tuple[dict, dict], want: tuple[dict, dict]) -> bool:
    """库里有的 ⊇ 当前代码要的。多出来的(另一条分支加的表或列)不算缺。

    `feed_meta` 里以 `-rev` 结尾的是补种修订号,要不低于当前值;其余是一次性迁移的记号,
    在就行。"""
    objects, meta = have
    for name, (kind, columns) in want[0].items():
        if name not in objects or objects[name][0] != kind or not columns <= objects[name][1]:
            return False
    for key, value in want[1].items():
        if key not in meta:
            return False
        if key.endswith('-rev') and int(meta[key] or 0) < int(value):
            return False
    return True


def _current(path: Path, build, left: float) -> bool:
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=left)
    except sqlite3.Error:
        return False
    try:
        conn.execute('PRAGMA query_only=ON')
        return _covers(_shape(conn), _expected(build))
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def ensure(path: Path, build, migrate, deadline: float) -> None:
    """保证 `path` 已是当前代码要的样子,最多等到 `deadline`(`time.monotonic()` 口径)。

    `build(conn)` 在空连接上建出当前的完整结构;`migrate(left)` 开一次 `db(timeout=left)`
    (它跑完会自己 `mark`)。等不到就抛 `sqlite3.OperationalError`,和 busy 超时同一句话。
    """
    with suppress(FileNotFoundError):
        if _key(path) in _READY:
            return
    with _GUARD:
        first = _FIRST.setdefault(str(path.resolve()), threading.Lock())
    if not first.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise sqlite3.OperationalError('database is locked')
    try:
        if path.exists():
            key = _key(path)
            if key in _READY:
                return
            if _current(path, build, max(0.0, deadline - time.monotonic())):
                _READY.add(key)
                return
        migrate(max(0.0, deadline - time.monotonic()))
    finally:
        first.release()
