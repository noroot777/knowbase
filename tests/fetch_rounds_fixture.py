# -*- coding: utf-8 -*-
"""抓取账本那两组用例共用的夹具:临时数据目录 + 临时 vault + 一个假采集器。

`_Base` 把 `fetch_rounds.DATA_DIR`、`config.VAULT_ROOT`、`config.SOURCES` 全指到
临时目录,并且每条用例前后都把模块级的 `fetch_rounds._unsaved` 收干净 —— 不收的话
一条写失败用例会把后面每一条都拖成「不可确认」。

消费方:tests/test_fetch_rounds.py(账本本体与恢复协议)、
tests/test_fetch_receipt.py(成果归属)。
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from kb import config, fetch_rounds
from kb.collectors import Fetched


def T(text):
    return datetime.fromisoformat(text)


def BOOK(n, added, at, partial=False, fresh=None, carried=0, unsure=0):
    """面板那颗药丸该看到的**整份**事实(`fetch_rounds.today()` 的全部字段)。

    整份比,不挑字段:少比一个键的话,「这一轮全不全」或者那句分解悄悄变了都
    发现不了。`fresh` 默认等于 `added` —— 正常情形就是「这一轮交回来的都是这
    一次新抓到的」,要钉补导/说不清那两档时各自传进来。
    """
    return {'n': n, 'added': added, 'fresh': added if fresh is None else fresh,
            'carried': carried, 'unsure': unsure, 'partial': partial, 'at': at}


class _Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vault = self.root / 'vault'
        # `_unsaved` 是模块级的:不收干净的话,一条写失败用例会把后面每一条都
        # 拖成「不可确认」(同 tests/test_*.py 那条共用数据目录的教训)。
        fetch_rounds._unsaved = []
        self.addCleanup(setattr, fetch_rounds, '_unsaved', [])
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

    def caught(self, key, *rels):
        """这一轮**新抓到**的那几篇,按真实回执的形状交。

        回执里每一条都带 `(稳定内容身份, 发现它的那一次抓取, 这次写的路径)`;
        传裸路径的话抓取身份是空串,那是另一档事实(「说不清」),不该拿它当
        正常情形的替身。
        """
        return frozenset(Fetched(f'rss:{rel}', key, rel) for rel in rels)

    def nameless(self, *rels):
        """交得出路径、交不出抓取身份的那几篇(存量内容、手工导入的链接)。"""
        return frozenset(Fetched(f'rss:{rel}', '', rel) for rel in rels)

    def lines(self):
        raw = self.root / fetch_rounds.FILE_NAME
        return raw.read_text(encoding='utf-8').splitlines() if raw.exists() else []


class _Fake:
    """一个假采集器,**按真实契约交回执**:物化真往 vault 写文件,返回那几篇的
    路径(或者 `None` = 说不出来)。

    上一版的夹具是把 `_per_channel` 整个换成一个"写文件"的动作 —— 于是真正的
    逐来源循环、`finally` 里那次记账、以及"回执由谁给"这件事全都没被跑到,
    审查正是据此指出这组用例发现不了归属问题。现在只换采集器,循环走真的。
    """

    def __init__(self, case, name, writes=(), body='正文', receipt=True,
                 boom=False, aside=(), carried=(), carried_by='batch-older',
                 nameless=()):
        self.case, self.name, self.label = case, name, name
        self.writes, self.body = list(writes), body
        self.receipt, self.boom, self.aside = receipt, boom, list(aside)
        # carried:这一轮真写下去、但**上一轮才抓到**的那几篇(抓取当天没拿到
        # 正文、隔两天补上才导出,这条路天天走到)。nameless:写下去了、可是
        # 说不清是哪一轮抓到的(存量、手工导入)。
        self.carried, self.carried_by = list(carried), carried_by
        self.nameless = list(nameless)

    def fetch(self, scope):
        pass

    def materialize(self):
        # 只报真**新建**的那几篇(判据和 `sync.export` 同一条:这一篇之前有没有
        # 进过 vault)。真导出器那一侧另有自己的红,见
        # `TheRealExporterReportsOnlyFreshFilesTests` —— 只钉夹具等于没钉。
        all_writes = self.writes + self.carried + self.nameless
        fresh = [r for r in all_writes if not (self.case.vault / r).exists()]
        self.case.write(*all_writes, body=self.body)
        # aside:物化**这段时间里**别的进程往同一批树里落的文件(实时 saver、
        # 手工导入、另一条管道)。它们不在这个采集器的回执里。
        for rel in self.aside:
            self.case.write(rel)
        if self.boom:
            raise RuntimeError('某个来源炸了')
        if not self.receipt:
            return None
        # 抓取身份由采集器自己的库给。这里按 `fetch_rounds.round_key()` 取当前
        # 那一轮 —— 真采集器问的是同一个口径(`feed_items.fetch_round` 就是入库
        # 那一刻记下的这个值)。
        mine = fetch_rounds.round_key()
        owner = {**{r: mine for r in self.writes},
                 **{r: self.carried_by for r in self.carried},
                 **{r: '' for r in self.nameless}}
        return frozenset(Fetched(f'{self.name}:{r}', owner[r], r) for r in fresh)

