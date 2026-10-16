# -*- coding: utf-8 -*-
"""抓取账本那两组用例共用的夹具:临时数据目录 + 临时 vault + 一个假采集器。

`_Base` 把 `fetch_rounds.DATA_DIR`、`config.VAULT_ROOT`、`config.SOURCES` 全指到
临时目录,并且每条用例前后都把模块级的那三份内存状态收干净(`_unsaved` 没落盘的
行、`_opened` 本进程开过的轮次、`_gaps` 缺过行的轮次)—— 不收的话一条写失败用例
会把后面每一条都拖成「不可确认」,而 `_opened` 留着会让别的用例在读失败时多写出
一行来。

`_Pending` 在 `_Base` 上再加一层:那把旁挂锁、那几笔独立凭证、那笔开工标记怎么弄坏
和修好,以及「另一个进程读出来是多少」(`restart_today`)。

消费方:tests/test_fetch_rounds.py(账本本体与完整性)、
tests/test_fetch_recovery.py(写故障之后的重放协议)、
tests/test_fetch_receipt.py(回执是谁交的)、tests/test_fetch_identity.py(回执里
那一条属于哪一次抓取)、tests/test_fetch_pending.py(攥着的行与那笔凭证)、
tests/test_fetch_evidence.py(参与证据的寿命与分代清理)。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from kb import config, fetch_rounds
from kb import pipeline_state as ps
from kb.collectors import Fetched

REPO = Path(__file__).resolve().parent.parent


def T(text):
    return datetime.fromisoformat(text)


def BOOK(n, added, at, partial=False, fresh=None, carried=0, unsure=0):
    """面板那段 tooltip 该看到的**整份**事实(`fetch_rounds.today()` 的全部字段)。

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


class _Pending(_Base):
    """账本那把锁、那几笔凭证、那笔开工标记的小工具。"""

    def lock(self):
        return self.root / fetch_rounds.LOCK_NAME

    def break_lock(self):
        """把旁挂锁占成一个目录 —— 取锁从此失败(审查复现用的就是这一手)。"""
        self.lock().unlink(missing_ok=True)
        self.lock().mkdir()

    def fix_lock(self):
        self.lock().rmdir()

    def pending(self):
        return self.root / fetch_rounds.PENDING_DIR

    def certs(self):
        folder = self.pending()
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    def block_pending(self):
        """把凭证目录的位置占成一个普通文件 —— 那一笔从此写不下去。"""
        self.pending().write_text('占住凭证目录的位置', encoding='utf-8')

    def marker(self):
        return self.root / 'pipeline_state_open.json'

    def mark_open(self, run_id):
        """落一笔**真的**开工标记(走生产那条 `pipeline_open.mark`,不手搓 JSON)。

        位置跟着 `pipeline_state._STATE_FILE` 派生,所以这里把它也指到临时目录 ——
        两边指到同一个库,才是生产里那个形状。**抓取那一侧不读这一笔**(它的寿命是
        那一轮运行的,见 `kb.fetch_gate`),所以这个小工具现在只用来钉
        「它在不在都不改变结论」。
        """
        with patch.object(ps, '_STATE_FILE', self.root / 'pipeline_state.json'):
            self.assertEqual('', ps.mark_open(
                {'run_id': run_id, 'seq_gen': 1, 'started_at': '2026-09-29 09:00',
                 'run_kind': 'sync'}))

    def restart_today(self):
        """**另一个进程**读出来的那个数(内存里什么都没有,盘上留下什么就是什么)。

        只把账本那一侧指到临时目录 —— 审查复现脚本也是这么打桩的,而参与证据必须
        在这种打桩下也找得到(两处证据都在账本自己那个数据目录里)。
        """
        code = ('import json; from pathlib import Path; from kb import fetch_rounds; '
                f'fetch_rounds.DATA_DIR = Path({str(self.root)!r}); '
                'print(json.dumps(fetch_rounds.today()))')
        done = subprocess.run([sys.executable, '-c', code], capture_output=True,
                              text=True, check=True, cwd=str(REPO))
        return json.loads(done.stdout.strip().splitlines()[-1])

    def process_gone(self):
        """模拟进程没了:内存那三份状态随它消失,盘上留下什么就是什么。"""
        for name in ('_unsaved', '_opened', '_gaps'):
            setattr(fetch_rounds, name, [])


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
