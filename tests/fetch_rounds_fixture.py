# -*- coding: utf-8 -*-
"""抓取账本那两组用例共用的夹具:临时数据目录 + 临时 vault + 一个假采集器。

`_Base` 把 `fetch_rounds.DATA_DIR`、`config.VAULT_ROOT`、`config.SOURCES` 全指到
临时目录,并且每条用例前后都把模块级的那三份内存状态收干净(`_unsaved` 没落盘的
行、`_opened` 本进程开过的轮次、`_gaps` 缺过行的轮次)—— 不收的话一条写失败用例
会把后面每一条都拖成「不可确认」,而 `_opened` 留着会让别的用例在读失败时多写出
一行来。

消费方:tests/test_fetch_rounds.py(账本本体与恢复协议)、
tests/test_fetch_receipt.py(成果归属)。
"""
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from kb import config, fetch_rounds


def T(text):
    return datetime.fromisoformat(text)


class _Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vault = self.root / 'vault'
        # 这三份都是模块级的:不收干净的话,一条写失败用例会把后面每一条都
        # 拖成「不可确认」(同 tests/test_*.py 那条共用数据目录的教训)。
        for name in ('_unsaved', '_opened', '_gaps'):
            setattr(fetch_rounds, name, [])
            self.addCleanup(setattr, fetch_rounds, name, [])
        for p in (patch.object(fetch_rounds, 'DATA_DIR', self.root),
                  patch.object(config, 'VAULT_ROOT', self.vault),
                  patch.object(config, 'SOURCES', self._sources())):
            p.start(); self.addCleanup(p.stop)

    def _sources(self):
        """一棵正常树 + 一棵衍生树 + 一棵停用树 + 一个 skip_dirs。"""
        return [
            config.Source(name='rss', root=self.vault / 'source' / 'rss'),
            config.Source(name='dr-youtube-dig',
                          root=self.vault / 'source' / 'dr-youtube-dig',
                          skip_dirs=frozenset({'_graph'})),
            config.Source(name='dr-gzh-projects',
                          root=self.vault / 'source' / 'dr-gzh-projects',
                          derived=True),
            config.Source(name='dr', root=self.vault / 'source' / 'dr',
                          enabled=False),
        ]

    def write(self, *rels, body='正文'):
        """往 vault 落几篇 md(模拟物化那一步真写出来的文章)。"""
        for rel in rels:
            f = self.vault / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f'---\ntitle: {rel}\npublished: 2026-09-28 09:00\n'
                         f'---\n{body}', encoding='utf-8')

    def lines(self):
        raw = self.root / fetch_rounds.FILE_NAME
        return raw.read_text(encoding='utf-8').splitlines() if raw.exists() else []

    @contextmanager
    def ledger_read_fails(self, times=1):
        """让**账本**的下 `times` 次读取各抛一次 `OSError`,之后完全恢复。

        只打这一个文件:整份 `Path.read_text` 打桩会把配置、vault 正文一起带红,
        那就不是"短暂的读账故障"这一档了,断言也就不指向账本协议。
        """
        left = [times]
        real = Path.read_text

        def maybe_fail(path, *a, **kw):
            if path == fetch_rounds._file() and left[0] > 0:
                left[0] -= 1
                raise OSError('读账暂时不可用')
            return real(path, *a, **kw)

        with patch.object(Path, 'read_text', maybe_fail):
            yield


class _Fake:
    """一个假采集器,**按真实契约交回执**:物化真往 vault 写文件,返回那几篇的
    路径(或者 `None` = 说不出来)。

    上一版的夹具是把 `_per_channel` 整个换成一个"写文件"的动作 —— 于是真正的
    逐来源循环、`finally` 里那次记账、以及"回执由谁给"这件事全都没被跑到,
    审查正是据此指出这组用例发现不了归属问题。现在只换采集器,循环走真的。
    """

    def __init__(self, case, name, writes=(), body='正文', receipt=True,
                 boom=False, aside=()):
        self.case, self.name, self.label = case, name, name
        self.writes, self.body = list(writes), body
        self.receipt, self.boom, self.aside = receipt, boom, list(aside)

    def fetch(self, scope):
        pass

    def materialize(self):
        # 只报真**新建**的那几篇(判据和 `sync.export` 同一条:写之前 vault 里
        # 有没有这个路径)。真导出器那一侧另有自己的红,见
        # `TheRealExporterReportsOnlyFreshFilesTests` —— 只钉夹具等于没钉。
        fresh = [r for r in self.writes if not (self.case.vault / r).exists()]
        self.case.write(*self.writes, body=self.body)
        # aside:物化**这段时间里**别的进程往同一批树里落的文件(实时 saver、
        # 手工导入、另一条管道)。它们不在这个采集器的回执里。
        for rel in self.aside:
            self.case.write(rel)
        if self.boom:
            raise RuntimeError('某个来源炸了')
        return frozenset(fresh) if self.receipt else None
