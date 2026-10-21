# -*- coding: utf-8 -*-
"""夹具**说话**这一侧:它绝不许堵住任何一道防线。

从 `tests/fixture_life.py` 拆出来的,原因只有一个:那份过了 700 行的上限。职责本来就是
两件事 —— 那边管「什么时候该退」,这边管「话怎么说出去」。**依赖是单向的**:这个模块
不认识 `fixture_life`,所以不会有环。

三条硬约束都在各自的说明里:①要说的话先进一个**有界**队列,由一个专门的线程阻塞着写
出去 —— 满了就丢,谁说话都不会被 stderr 堵住;②**摆盘那一段的 stdout 和 stderr 两条
都换成谁都堵不住的管子**,`install()` 那一刻换回真身;③**这个边界立不起来就当场退**,
不许继续到一道防线都没有的运行状态。
"""

from __future__ import annotations

import contextlib
import os
import queue
import sys
import threading
import time
from typing import NoReturn

#: 立不起输出边界时的退出码 —— 跟 `_bail` 那个 143 一样,是个一眼认得出来的固定号
NO_BOUNDARY_RC = 126

#: 摆盘期间被换掉的那几个号 → `os.dup` 出来的真身。没在摆盘时是空的(见 `_begin_staging`)
_STAGED: dict[int, int] = {}

#: 要说的话先进这里,由 `_log_pump` 一个线程写出去。满了就丢。
_LOGS: queue.Queue = queue.Queue(maxsize=256)


def _log_pump() -> None:
    """唯一一个真正写 stderr 的线程 —— **它卡住是允许的,没有人等它**。

    所以这里就用普通的阻塞 `os.write`:**不许**把 fd 2 设成非阻塞来让它不卡。那个标志
    挂在 open-file-description 上,手起夹具继承的 stderr 跟调用方是同一份,改了它调用
    方的 `write` 会跟着开始抛 `BlockingIOError`(实测过,调用方自己什么都没做)。借来的
    fd 不是我们的,状态一个比特都不动。
    """
    while True:
        stream, text = _LOGS.get()
        with contextlib.suppress(Exception):
            os.write(stream, text.encode('utf-8', 'replace'))


def _say(text: str, *, stream: int = 2) -> None:
    """说一句话 —— **只往队列里丢,满了就丢掉,绝不等**。

    防线上的每一处输出都走这里:退出理由、访问日志、启动那行提示。队列是有界的,所以
    就算 stderr 彻底不动,这个函数的耗时也不随之变化 —— 这正是上一版用「把 fd 2 切成
    非阻塞」换来的东西,只是这一版没有碰调用方的任何状态。

    **号在这一刻就换成真身。** 摆盘期间 fd 1 / fd 2 指着那根搬运管子,照着 2 写等于把
    自己的话又送回搬运线程绕一圈。`_STAGED` 里记的是 `os.dup` 出来的真身,而那几个号
    **从不关闭**(见 `_end_staging`),所以摆盘结束之后还排在队列里的话照样写得出去。
    """
    with contextlib.suppress(Exception):
        _LOGS.put_nowait((_STAGED.get(stream, stream), text))


def _drain_logs(budget: float) -> None:
    """给日志线程一点时间把话说完,**最多等 budget 秒**。

    只在 `_bail` 和 `_no_boundary` 里用:收场理由能打出来最好,但它绝不许拖住收场。队列
    空了只说明话已经交给那个线程,不保证已经落到 stderr —— 真卡在满管道上时谁也救不了
    它,那就丢掉。
    """
    deadline = time.monotonic() + budget
    while not _LOGS.empty() and time.monotonic() < deadline:
        time.sleep(0.01)


#: 摆盘要圈住的两条通道。**stdout 和 stderr 都得圈**:摆盘途中两条通道上都有别人替我们说话。
_CHANNELS = (1, 2)


def _running_as_entry() -> bool:
    """这个进程是不是某个 `tests/serve_*.py` 自己在跑?

    摆盘边界只能在**夹具进程**里立 —— 被 `tests/test_fixture_life*.py` import 进来时,
    这个进程是测试运行器,换掉它的 fd 1 / fd 2 会把别人的输出一起吞掉,`_no_boundary`
    那条「当场退」更是会把整轮测试带走。
    """
    return os.path.basename(sys.argv[0] or '').startswith('serve_')


def _flush(fd: int) -> None:
    """把 Python 层那点缓冲先倒出去,免得它在换号之后才落到另一头。"""
    with contextlib.suppress(Exception):
        (sys.stdout if fd == 1 else sys.stderr).flush()


def _forward_staging(read_fd: int, target: int) -> None:
    """把摆盘管子里的东西搬进那个有界队列 —— **它一直读,所以管子永远不会满**。"""
    while True:
        try:
            chunk = os.read(read_fd, 4096)
        except OSError:
            break
        if not chunk:
            break                    # 写端都关了:摆盘结束
        _say(chunk.decode('utf-8', 'replace'), stream=target)
    with contextlib.suppress(OSError):
        os.close(read_fd)


def _no_boundary(why: str) -> NoReturn:
    """**输出边界立不起来就当场退**,不许继续到一道防线都没有的运行状态。

    立不起来之后这个进程接下来**可能**一切正常,也可能在摆盘途中某一次别人的同步输出上
    永久阻塞 —— 而那正是「一道防线都没装、进程永久留着」那个形态:手起这条路没有
    owner,空闲那道又必须等摆盘结束才装。拿不准就别固化终态:这里退掉只是一次看得见的
    启动失败(`_wait_listening` 当场报 `rc`、`fixture_server` 报启动失败),安静地留下来
    才是静默漏一个永久夹具。

    **用 `os._exit`,不抛异常。** 抛出去 Python 要把 traceback 写到 stderr 上,而「stderr
    现在写不写得动」正是我们判不准的那件事,一写就可能永久卡在这儿 —— 那就从「漏一个在
    服务的夹具」换成「挂一个什么都不干的进程」,没换好。理由走那个有界队列,给它半秒说
    得出来最好,说不出来也照样退。
    """
    _say(f'tests/fixture_voice.py: {why},夹具不起 —— 立不起输出边界就没有防线\n')
    _drain_logs(0.5)
    os._exit(NO_BOUNDARY_RC)


def _begin_staging() -> None:
    """**摆盘那一段的 stdout 和 stderr 都换成谁都堵不住的管子。**

    `announce()` 保证了「先装防线、再说话」,但那只管入口自己那行提示。摆盘途中还有别人
    替我们说话,而且**两条通道上都有**:阅读页和片段那两个夹具要先调 `kb.cli._index_pass()`
    建索引,它完成时那行 `print` 走普通 stdout;七个 ASGI 入口导入 `kb.server` 那一路会发
    弃用警告,`kb/store.py` 导入的 jieba 还会按 `logging.StreamHandler(sys.stderr)` 把初始
    化日志同步写 stderr。默认缓冲下那点东西留在 Python 的缓冲区里,所以满管道的烟测照旧
    能开页面 —— 但 `PYTHONUNBUFFERED=1` / `-u` 下它立刻落到底层 fd:手起、又没人消费那根
    管道时,进程就**堵在建库里**,`install()` 根本还没跑到,唯一那道空闲防线一行都没装上。
    实测给真阅读/片段入口各一根填满的 **stderr**:过了三倍空闲上限进程还在、HTTP 不响应、
    沙箱根还在;一开始读那根管子,它八秒内就起来服务、随后按空闲正常退掉 rc=0。

    所以边界不立在入口里立在这里:**模块一加载就换,`install()` 那一刻换回去**。八个入口
    都在这个窗口里面,将来谁在摆盘阶段多打一行、哪个依赖多写一句日志,都不用再想一次。
    管子由一个守护线程一直读着,所以写端永远不会满;读出来的东西进那个有界队列,真没人
    消费就按老规矩丢掉 —— **提示丢得起,防线丢不起**。

    **先起搬运线程、再换号。** 反过来的话线程起不来那一瞬间 fd 1/2 已经指着一根没人读的
    管子,下一次输出就是永久阻塞,比不圈还糟。中途哪一步失败都走 `_no_boundary`,**一律
    不许「就这样算了」继续跑**。

    **`dup2` 改的是我们自己这张 fd 表,不是那个共享的 open-file-description** —— 所以它跟
    「不许给借来的 fd 设非阻塞」不冲突:调用方那一侧的 fd 1 / fd 2 一个比特都没动。

    边界只圈到 `install()`:之后每一处输出都自己走 `_say`(访问日志、收场理由),七个
    ASGI 入口又都是 `log_level='warning'`,没有逐请求的同步输出。
    """
    if _STAGED or not _running_as_entry():
        return
    plumbing: dict[int, tuple[int, int, int]] = {}
    try:
        for fd in _CHANNELS:
            real = os.dup(fd)
            read_fd, write_fd = os.pipe()
            plumbing[fd] = (real, read_fd, write_fd)
        for fd, (real, read_fd, _write_fd) in plumbing.items():
            threading.Thread(target=_forward_staging, args=(read_fd, real),
                             daemon=True, name=f'fixture-staging-{fd}').start()
    except Exception as exc:         # dup / pipe / 线程都在这儿:一个都不许静默跳过
        _no_boundary(f'摆盘期的输出边界立不起来({exc!r})')
    for fd, (real, _read_fd, write_fd) in plumbing.items():
        _flush(fd)
        try:
            os.dup2(write_fd, fd)
        except (OSError, ValueError) as exc:
            _no_boundary(f'{fd} 号换不成那根管子({exc!r})')
        _STAGED[fd] = real
    for _fd, (_real, _read_fd, write_fd) in plumbing.items():
        with contextlib.suppress(OSError):
            os.close(write_fd)       # fd 1 / fd 2 自己握着写端了


def _end_staging() -> None:
    """摆盘结束:两个号都换回真身。幂等 —— `announce()` 和 `guard()` 都会走到这儿。

    **真身那两个号不关**:队列里可能还排着摆盘期间的话,搬运线程和 `_say` 都按这个号把
    它们写出去。关掉它、号又被回收的话,写的就是别人的东西了。两个 fd 换这份确定性,
    划得来。
    """
    for fd, real in list(_STAGED.items()):
        _flush(fd)                   # Python 层缓冲里的先落进管子
        with contextlib.suppress(OSError):
            os.dup2(real, fd)        # 写端最后一个引用没了 → 搬运线程读到 EOF 自己退
        del _STAGED[fd]


def _arm_logs() -> None:
    """写 stderr 的那个线程**在模块加载时就起**。

    `_say` 从那一刻起就只是「往队列里丢」,所以所有权看门狗、空闲看门狗、访问日志、
    收场理由没有一处会因为 stderr 不动而变慢 —— 而且谁都不用去碰 fd 2 的状态。
    """
    threading.Thread(target=_log_pump, daemon=True, name='fixture-log').start()
    _begin_staging()                 # 紧接着就把摆盘那两条通道圈起来


_arm_logs()
