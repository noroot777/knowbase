# -*- coding: utf-8 -*-
"""跨平台文件锁:`kb.filelock` 是唯一的入口,谁也不许再直接 `import fcntl`。

**为什么要有这一份。** `fcntl` 是 POSIX-only 的标准库模块,Windows 上压根没有。
而 `kb.server` 的 import 链上有 6 个模块在**顶层**写着 `import fcntl`
(maintenance、pipeline_failures、fetch_rounds、daily_carry、feeds/x_cdp、
wechat_setup)—— 于是整个面板在 Windows 上连 import 都过不去,报出来的是一句
`ModuleNotFoundError: No module named 'fcntl'`,**指不到任何跟锁、跟并发、跟
业务有关的东西**。谁都不会从这句话想到「有一处为了互斥写了 flock」。

所以锁这件事抽成了 `kb/filelock.py` 一份:POSIX 上原样转给 `fcntl.flock`,
Windows 上用 `LockFileEx`/`UnlockFileEx` 实现同一组语义。**本文件钉的是结果**:

1. **结构**:`kb/` 和 `scripts/wiki/` 下没有任何模块在顶层 import fcntl。
   这一条是防回归的那一条 —— 下一个要加互斥的人顺手写 `import fcntl` 的话,
   广域回归**全绿**(开发机是 macOS,那里它工作得很好),坏的只有 Windows,
   而坏法是「面板起不来 + 一句指不到原因的报错」。
   唯一的例外是 `kb/feeds/x_cdp.py`:那条通道整条只能在 POSIX 上跑(它要把
   os.pipe 搬到 Chrome 写死的 fd 3/4),它的 import 是 try/except 包着的,
   而且真正用的时候会显式报「这条通道 Windows 上没有」。例外**连它的守卫一起钉**。
2. **行为**:维护锁真的把第二个进程挡在外面 —— 不管这台机器用的是哪个后端。
   判据落在「另一个进程能不能同时进去」,不是「flock 调用返回了没有」。
3. **反过来那一边**:持锁者退出之后,第二个进程**拿得到**。只验「拦住了」的话,
   一个永远失败的锁也能让用例全绿,而那等于把索引、清理、同步全冻死。
4. **同线程重入照旧放行**。filelock 故意**不**支持在同一个 handle 上转换锁
   (Windows 那边没有对等语义),所以重入必须留在上层(`maintenance._held`、
   `llm._lock_depth`)—— 这一条红了说明有人把重入挪进了 filelock。
5. **「有人占着」和「这个平台锁不住」是两档,异常类别必须分开**:前者
   `BlockingIOError`、后者别的 `OSError`。两处调用方拿这个区别决定说哪句话
   (`wechat_setup._lock` 只接 BlockingIOError,`x_login_import` 的 OSError 那一支
   说的是「文件系统可能不支持文件锁」)。混成一档的后果是**两个方向的误报**:
   一边把正常撞锁说成「你的文件系统有问题」,另一边把真的环境故障说成「有人在跑」
   —— 两句话都会把人带到完全错的地方去查。
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

from kb import filelock

REPO = Path(__file__).resolve().parent.parent

#: 允许顶层 import fcntl 的模块(仓库内相对路径)。**加进来之前先想清楚**:
#: 它必须整条路都只在 POSIX 上跑,而且 import 要被 try/except 包住、真用的时候
#: 显式报错。写进这里的每一个都在下面那条用例里连守卫一起被钉住。
POSIX_ONLY = {'kb/feeds/x_cdp.py'}

#: 「这个文件讲的就是 fcntl 本身」的那两份 —— 它们的 docstring 里出现
#: `fcntl.flock` 是在**说明**,不是在调用,所以探针串那条用例放它们过。
#: 别往这里加第三个:再有文件需要谈论 fcntl,该谈的内容八成属于这两份之一。
FCNTL_IS_THE_SUBJECT = {'kb/filelock.py', 'tests/test_filelock.py'}

#: 结构扫描覆盖的四棵树。**`tests/` 必须在里面**:实测漏掉它的代价是
#: `tests/test_pipeline_failure_drop.py` 顶层那一句 `import fcntl` 让**整份文件
#: 在 Windows 上一条用例都收不上来**(unittest 把它记成一条 `_FailedTest`),
#: 于是「删一条明细要先记账本」那一族的不变量一条都没验 —— 而全量回归的结尾
#: 只多出一行 ImportError,混在几百条平台差异里看不见。
SCAN_TREES = ('kb/**/*.py', 'scripts/**/*.py', 'collector/**/*.py', 'tests/**/*.py')


def _modules_under_scan() -> list[Path]:
    out: list[Path] = []
    for pattern in SCAN_TREES:
        out += sorted(REPO.glob(pattern))
    return [p for p in out if '__pycache__' not in p.parts]


def _parse(path: Path) -> ast.AST:
    """解析一份源码。**两条扫描共用这一处** —— 口径只有一份。

    **压掉转义序列那一族警告。** `kb/feeds/x_article_links.py` 和
    `x_article_nest.py` 里有三处非 raw 的 docstring 带着 `\\S` / `` \\` `` /
    `\\[`,解析时各报一条。那是本任务之外的既有问题(属于 x-article 那一块),
    不在这里改;但也不该让这两条结构用例每跑一次就刷一屏跟 fcntl 毫无关系的
    警告 —— 看起来像是用例自己有毛病。

    **两个类别都要压。** 同一件事在不同 Python 上报的类别不一样:3.12 起是
    `SyntaxWarning`,3.11(本项目 venv 就是 3.11)报的是 `DeprecationWarning`。
    只写一个的话,在另一半解释器上这段抑制等于没有 —— 实测只写 SyntaxWarning
    时 3.11 上照旧刷警告,而且扫描从两棵树扩到四棵之后刷得更多。
    """
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)
        warnings.simplefilter('ignore', DeprecationWarning)
        return ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


def _string_literals(path: Path) -> list[tuple[int, str]]:
    """文件里所有字符串字面量(行号, 内容)。

    **为什么要单独看字符串。** 下面那条 AST import 扫描看不见探针:
    `tests/test_index_lock.py` 和 `test_feed_trim.py` 把子进程的代码写成一段
    `textwrap.dedent('''...''')` 的字符串,里面那句 `import fcntl` 对 AST 来说
    只是一个 str 常量。而那两段**正是用来验「维护锁真的跨进程互斥」的**,
    在 Windows 上它们会双双报 ModuleNotFoundError —— 于是锁这件事在这个平台上
    一条都没验到,而红出来的那两行指向的是 `<string>`,看不出跟维护锁有关。
    """
    return [(n.lineno, n.value) for n in ast.walk(_parse(path))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _top_level_imports(path: Path) -> set[str]:
    """模块顶层(不在 try / 函数 / 类里)import 了哪些模块名。

    只看顶层:函数内的 import 推迟到真调用时,而 try 包着的那种本身就是
    「没有也行」的写法 —— 这两类都不会让 import kb.server 当场失败。
    """
    tree = _parse(path)
    names: set[str] = set()
    for node in tree.body:                       # 只遍历顶层语句
        if isinstance(node, ast.Import):
            names |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split('.')[0])
    return names


class NoDirectFcntlTest(unittest.TestCase):
    """没有一个模块在顶层 import fcntl —— 那一句会让 Windows 上面板起不来。"""

    def _modules(self) -> list[Path]:
        return _modules_under_scan()

    def test_no_top_level_fcntl(self) -> None:
        offenders = []
        for path in self._modules():
            rel = path.relative_to(REPO).as_posix()
            if rel in POSIX_ONLY:
                continue
            if 'fcntl' in _top_level_imports(path):
                offenders.append(rel)
        self.assertEqual(offenders, [], '顶层 import fcntl 会让 Windows 上 '
                                        'import kb.server 当场失败,报一句指不到'
                                        '原因的 ModuleNotFoundError;改用 '
                                        'from kb import filelock')

    def test_no_fcntl_inside_probe_sources(self) -> None:
        """探针串里也不许有 —— 那一层 AST 扫描看不见,见 `_string_literals`。

        判据是**调用写法**(`fcntl.flock(` / `fcntl.LOCK_`),不是「出现过 fcntl
        这几个字」:后者会把所有解释这件事的注释和 docstring 一起判红,包括本文件
        自己的。谈论它的那两份在 `FCNTL_IS_THE_SUBJECT` 里豁免。
        """
        pat = re.compile(r'\bfcntl\.(flock|LOCK_)')
        offenders = []
        for path in self._modules():
            rel = path.relative_to(REPO).as_posix()
            if rel in FCNTL_IS_THE_SUBJECT:
                continue
            for lineno, text in _string_literals(path):
                if pat.search(text):
                    offenders.append(f'{rel}:{lineno}')
        self.assertEqual(offenders, [], '某段子进程源码里还在直接用 fcntl:'
                                        'Windows 上那个子进程会报 '
                                        'ModuleNotFoundError,而它要验的互斥'
                                        '一条都没验到(红出来只指向 <string>)。'
                                        '探针里改用 from kb import filelock')

    def test_the_probe_scan_can_actually_see_a_probe(self) -> None:
        """反过来钉一边:上面那条用例的扫描真的看得见 dedent 里的那种写法。

        `_string_literals` 要是哪天退化成「只看 docstring」,上面那条会永远绿,
        而探针里的 `import fcntl` 照旧。所以这里拿一份**真的长成那样**的源码
        喂给它:`textwrap.dedent` 包着的三引号串,里面是子进程代码。
        """
        probe = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, probe, True)
        bad = Path(probe) / 'fake_test.py'
        bad.write_text("import textwrap\n"
                       "HOLDER = textwrap.dedent('''\n"
                       "    import fcntl, sys\n"
                       "    fcntl.flock(sys.stdin.fileno(), fcntl.LOCK_EX)\n"
                       "''')\n", encoding='utf-8')
        pat = re.compile(r'\bfcntl\.(flock|LOCK_)')
        hits = [ln for ln, text in _string_literals(bad) if pat.search(text)]
        self.assertTrue(hits, '扫描看不见 dedent 串里的 fcntl 调用 —— '
                              '上面那条用例已经失效了')
        self.assertNotIn('fcntl', _top_level_imports(bad),
                         'AST import 扫描居然看见了串里那一句 —— '
                         '那说明这份反向样料没测到该测的东西')

    def test_posix_only_modules_are_guarded(self) -> None:
        """例外名单里的那些:import 必须包在 try 里,而且真用的时候要显式报错。

        少了任何一半都不行 —— 只包 try 的话 Windows 上它会带着 `fcntl = None`
        一路跑到某个 `None.flock`,报 AttributeError;只报错不包 try 的话
        import 就已经炸了。
        """
        for rel in sorted(POSIX_ONLY):
            path = REPO / rel
            with self.subTest(rel):
                self.assertNotIn('fcntl', _top_level_imports(path),
                                 f'{rel} 的 import fcntl 没有包在 try 里')
                src = path.read_text(encoding='utf-8')
                self.assertIn('import fcntl', src, f'{rel} 压根没 import fcntl,'
                                                   f'把它从 POSIX_ONLY 里删掉')
                self.assertIn('fcntl is None', src,
                              f'{rel} 没有「这个平台没有 fcntl」的显式守卫:'
                              f'Windows 上会一路跑到 None.flock')


class BackendTest(unittest.TestCase):
    """这台机器上确实有一个可用的后端,而且是该用的那一个。"""

    def test_backend_matches_platform(self) -> None:
        self.assertTrue(filelock.HAVE_FLOCK, '这个平台上没有可用的文件锁')
        expect = 'LockFileEx' if os.name == 'nt' else 'fcntl'
        self.assertEqual(filelock.BACKEND, expect)


# 子进程里问一句「现在拿得到维护锁吗」。只用标准库 + kb.config/kb.maintenance,
# 所以不需要装任何依赖就能跑。
_PROBE = r'''
import sys
sys.path.insert(0, sys.argv[1])
from kb import maintenance
try:
    with maintenance.maintenance_lock(wait=0):
        print('ACQUIRED')
except maintenance.LockBusy:
    print('BUSY')
'''


class CrossProcessTest(unittest.TestCase):
    """维护锁的后果:另一个进程进不来;持锁者走了之后进得来。"""

    def _probe(self) -> str:
        env = dict(os.environ)
        env['KB_DATA_DIR'] = str(maintenance_data_dir())
        env['PYTHONIOENCODING'] = 'utf-8'
        out = subprocess.run([sys.executable, '-c', _PROBE, str(REPO)],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, f'探针自己炸了:{out.stderr}')
        return out.stdout.strip()

    def test_excludes_then_releases(self) -> None:
        from kb import maintenance
        with maintenance.maintenance_lock(wait=0):
            self.assertEqual(self._probe(), 'BUSY',
                             '锁没拦住第二个进程 —— 并发的索引与清理会用删除'
                             '之前的快照把删掉的东西复活')
        # 反过来钉一边:一个永远失败的锁也能让上面那条绿,而那等于把索引、
        # 清理、同步全冻死。
        self.assertEqual(self._probe(), 'ACQUIRED',
                         '持锁者退出之后锁没释放 —— 下一趟维护会一直等到超时')

    def test_reentrant_same_thread(self) -> None:
        """同线程重入直接放行(重入在 maintenance._held 上,不在 filelock 里)。"""
        from kb import maintenance
        with maintenance.maintenance_lock(wait=0):
            with maintenance.maintenance_lock(wait=0):
                pass            # 没抱死就是对的


def maintenance_data_dir() -> Path:
    """被测进程和探针进程必须锁**同一个** lockfile,否则两边各锁一把、永远不冲突。"""
    from kb.maintenance import DATA_DIR
    return DATA_DIR


# 直接问 filelock 本身:在一个指定的文件上,现在拿得到独占锁吗。
_LOCK_PROBE = r'''
import sys
sys.path.insert(0, sys.argv[1])
from kb import filelock
with open(sys.argv[2], 'a+') as fh:
    try:
        filelock.flock(fh, filelock.LOCK_EX | filelock.LOCK_NB)
        print('ACQUIRED')
    except OSError:
        print('BUSY')
'''


class UnlockTest(unittest.TestCase):
    """`LOCK_UN` 这条路得单独钉 —— 上面那几条用例一条都走不到它。

    `maintenance_lock` 是靠 `fh.close()` 释放的(flock 和 LockFileEx 都在句柄
    关闭时自动解锁),压根不调 `LOCK_UN`。所以把 `LOCK_UN` 整个改坏,维护锁那几条
    用例照样全绿 —— 这是反向验证当场发现的洞。而 `kb.fetch_rounds` 用的是显式
    `LOCK_UN`:坏了的后果是一个进程攥着水位文件不放,后面每一轮抓取都在等锁。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.lockfile = Path(self._tmp) / 'probe.lock'
        self.lockfile.write_bytes(b'')

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _probe(self) -> str:
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        out = subprocess.run([sys.executable, '-c', _LOCK_PROBE,
                              str(REPO), str(self.lockfile)],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, f'探针自己炸了:{out.stderr}')
        return out.stdout.strip()

    def test_unlock_releases(self) -> None:
        fh = self.lockfile.open('a+')
        try:
            filelock.flock(fh, filelock.LOCK_EX | filelock.LOCK_NB)
            self.assertEqual(self._probe(), 'BUSY', '独占锁没拦住另一个进程')
            filelock.flock(fh, filelock.LOCK_UN)
            self.assertEqual(self._probe(), 'ACQUIRED',
                             'LOCK_UN 没把锁放开 —— 显式解锁的调用方(fetch_rounds '
                             '的水位文件)会把后面每一轮都堵在等锁上')
        finally:
            fh.close()

    def test_close_releases(self) -> None:
        """反过来那一半:就算谁忘了 LOCK_UN,关句柄也必须放开。

        维护锁走的就是这条路(`maintenance_lock` 的 finally 里只有 `fh.close()`),
        而且进程崩掉时靠的也是它 —— 这一条坏了的话,一次崩溃就能把库永久锁死。
        """
        fh = self.lockfile.open('a+')
        filelock.flock(fh, filelock.LOCK_EX | filelock.LOCK_NB)
        self.assertEqual(self._probe(), 'BUSY')
        fh.close()
        self.assertEqual(self._probe(), 'ACQUIRED',
                         '关掉句柄没有释放锁 —— 持锁进程崩一次就把库永久锁死')

    def test_shared_allows_shared_blocks_exclusive(self) -> None:
        """共享锁的两边都要钉:共享之间不互斥,共享对独占互斥。

        `fetch_rounds` 读水位用的就是 `LOCK_SH`。只钉独占的话,一个把 LOCK_SH
        也当独占实现的后端能让用例全绿,而代价是读水位的并发全被串起来。
        """
        fh = self.lockfile.open('a+')
        try:
            filelock.flock(fh, filelock.LOCK_SH | filelock.LOCK_NB)
            self.assertEqual(self._probe(), 'BUSY',
                             '共享锁没挡住独占锁 —— 有人读着的时候别人就能改写')
            fh2 = self.lockfile.open('a+')
            try:
                filelock.flock(fh2, filelock.LOCK_SH | filelock.LOCK_NB)
            except OSError as exc:
                self.fail(f'两个共享锁互相挡住了:{exc}')
            finally:
                fh2.close()
        finally:
            fh.close()


class ContentionIsItsOwnErrorTest(unittest.TestCase):
    """撞锁抛 `BlockingIOError`,别的失败抛别的 `OSError` —— 两档不许混。

    这不是品味问题:`wechat_setup._lock` 整段只写了 `except BlockingIOError`,
    `x_login_import` 则按两档说两句完全不同的话。混成一档之后两个方向都是误报,
    而两句话各自把人带去一个错的方向(「换个文件系统」/「等那个任务结束」)。

    **这一条在 POSIX 上是冗余带、在 Windows 上承重**:fcntl 本来就抛
    BlockingIOError,而 `ctypes.WinError` 把 `ERROR_LOCK_VIOLATION` 映到 EACCES,
    照搬的话这两处调用方在 Windows 上同时坏掉。如实记着,别因为它在开发机上
    一次没红过就删掉。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.lockfile = Path(self._tmp) / 'contend.lock'
        self.lockfile.write_bytes(b'')

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_a_held_lock_raises_blockingioerror(self) -> None:
        """两个独立句柄锁同一段:第二个要抛 BlockingIOError。

        用同一个进程的两个句柄就够 —— flock 认的是 open file description、
        LockFileEx 认的是 handle,两边都互斥(`LOCK_SH` 那条用例靠的也是这一点)。
        """
        held = self.lockfile.open('a+')
        self.addCleanup(held.close)
        filelock.flock(held, filelock.LOCK_EX | filelock.LOCK_NB)
        second = self.lockfile.open('a+')
        self.addCleanup(second.close)
        with self.assertRaises(BlockingIOError):
            filelock.flock(second, filelock.LOCK_EX | filelock.LOCK_NB)

    def test_other_failures_are_not_blockingioerror(self) -> None:
        """反过来钉一边:不是撞锁的那些错不许也被翻成 BlockingIOError。

        整段 `except OSError: raise BlockingIOError(...)` 能让上面那条绿,代价是
        真的环境故障(句柄坏了、文件系统没有锁)被报成「另一个任务正在执行」——
        人去等一个压根不存在的任务。坏句柄两个平台都抛 OSError(POSIX 是 EBADF,
        Windows 上 `msvcrt.get_osfhandle` 自己就抛),而两边都不该是 BlockingIOError。
        """
        with self.assertRaises(OSError) as caught:
            filelock.flock(-1, filelock.LOCK_EX | filelock.LOCK_NB)
        self.assertNotIsInstance(
            caught.exception, BlockingIOError,
            '坏句柄被报成了「有人占着」—— 调用方会让人去等一个不存在的任务')


if __name__ == '__main__':
    unittest.main()