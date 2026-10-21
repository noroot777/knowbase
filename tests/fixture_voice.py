# -*- coding: utf-8 -*-
"""夹具**说话**这一侧:它绝不许堵住任何一道防线。

从 `tests/fixture_life.py` 拆出来的,原因只有一个:那份过了 700 行的上限。职责本来就是
两件事 —— 那边管「什么时候该退」,这边管「话怎么说出去」。**依赖是单向的**:这个模块
不认识 `fixture_life`,所以不会有环。

两条硬约束都在各自的说明里:①要说的话先进一个**有界**队列,由一个专门的线程阻塞着写
出去 —— 满了就丢,谁说话都不会被 stderr 堵住;②**摆盘那一段的 stdout 换成一根谁都堵不
住的管子**,`install()` 那一刻换回真身。
"""

from __future__ import annotations

import contextlib
import os
import queue
import sys
import threading
import time



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
    """
    with contextlib.suppress(Exception):
        _LOGS.put_nowait((stream, text))


def _drain_logs(budget: float) -> None:
    """给日志线程一点时间把话说完,**最多等 budget 秒**。

    只在 `_bail` 里用:收场理由能打出来最好,但它绝不许拖住收场。队列空了只说明话已经
    交给那个线程,不保证已经落到 stderr —— 真卡在满管道上时谁也救不了它,那就丢掉。
    """
    deadline = time.monotonic() + budget
    while not _LOGS.empty() and time.monotonic() < deadline:
        time.sleep(0.01)


#: 摆盘期间 fd 1 的真身(`os.dup` 出来的),没在摆盘时是 -1
_REAL_STDOUT = -1


def _running_as_entry() -> bool:
    """这个进程是不是某个 `tests/serve_*.py` 自己在跑?

    摆盘边界只能在**夹具进程**里立 —— 被 `tests/test_fixture_life*.py` import 进来时,
    这个进程是测试运行器,换掉它的 fd 1 会把别人的输出一起吞掉。
    """
    return os.path.basename(sys.argv[0] or '').startswith('serve_')


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


def _begin_staging() -> None:
    """**摆盘这一段的 stdout 换成一根谁都堵不住的管子。**

    `announce()` 保证了「先装防线、再说话」,但那只管入口自己那行提示。摆盘途中还会有
    别人替我们说话:阅读页和片段那两个夹具要先调 `kb.cli._index_pass()` 建索引,它完成
    时那行 `print` 走的是普通 stdout。默认缓冲下那一小行留在 Python 的缓冲区里,所以满
    stdout 的烟测照旧能开页面 —— 但 `PYTHONUNBUFFERED=1` / `-u` 下它立刻落到底层 fd:
    手起、又没人消费那根管道时,进程就**堵在建库里**,`install()` 根本还没跑到,唯一那
    道空闲防线一行都没装上。

    所以边界不立在入口里立在这里:**模块一加载就换,`install()` 那一刻换回去**。八个入
    口都在这个窗口里面,将来谁在摆盘阶段多打一行也不用再想一次。管子由一个守护线程一直
    读着,所以写端永远不会满;读出来的东西进那个有界队列,真没人消费就按老规矩丢掉 ——
    **提示丢得起,防线丢不起**。

    **`dup2` 改的是我们自己这张 fd 表,不是那个共享的 open-file-description** —— 所以
    它跟「不许给借来的 fd 设非阻塞」不冲突:调用方那一侧的 fd 1 一个比特都没动。
    """
    global _REAL_STDOUT
    if _REAL_STDOUT >= 0 or not _running_as_entry():
        return
    try:
        real = os.dup(1)
    except OSError:
        return                       # 连 dup 都不成,那就什么都别动
    try:
        read_fd, write_fd = os.pipe()
    except OSError:
        with contextlib.suppress(OSError):
            os.close(real)
        return
    try:
        sys.stdout.flush()
        os.dup2(write_fd, 1)
    except (OSError, ValueError):
        for fd in (real, read_fd, write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
        return
    finally:
        with contextlib.suppress(OSError):
            os.close(write_fd)       # fd 1 自己握着写端了
    _REAL_STDOUT = real
    threading.Thread(target=_forward_staging, args=(read_fd, real),
                     daemon=True, name='fixture-staging').start()


def _end_staging() -> None:
    """摆盘结束:fd 1 换回真身。幂等 —— `announce()` 和 `guard()` 都会走到这儿。

    **`_REAL_STDOUT` 不关**:队列里可能还排着摆盘期间的话,搬运线程要按这个号把它们写
    出去。关掉它、号又被回收的话,写的就是别人的东西了。一个 fd 换这份确定性,划得来。
    """
    global _REAL_STDOUT
    if _REAL_STDOUT < 0:
        return
    with contextlib.suppress(Exception):
        sys.stdout.flush()           # Python 层缓冲里的先落进管子
    with contextlib.suppress(OSError):
        os.dup2(_REAL_STDOUT, 1)     # 写端最后一个引用没了 → 搬运线程读到 EOF 自己退
    _REAL_STDOUT = -1

def _arm_logs() -> None:
    """写 stderr 的那个线程**在模块加载时就起**。

    `_say` 从那一刻起就只是「往队列里丢」,所以所有权看门狗、空闲看门狗、访问日志、
    收场理由没有一处会因为 stderr 不动而变慢 —— 而且谁都不用去碰 fd 2 的状态。
    """
    threading.Thread(target=_log_pump, daemon=True, name='fixture-log').start()
    _begin_staging()                 # 紧接着就把摆盘那段的 stdout 圈起来


_arm_logs()
