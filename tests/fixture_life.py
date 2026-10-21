# -*- coding: utf-8 -*-
"""`tests/serve_*.py` 这些夹具**自己什么时候退**的口径,只有这一份。

起它的那一侧收不住的那一档
--------------------------
`tests/headless.py` 的 `fixture_server()` 起的夹具是有主的:它挑端口、建临时根、在
`finally` 里 `reap()`。**漏的是另一条路** —— 会话为了跑一轮界面探针,在 Bash 里直接
`python tests/serve_workspace.py --port 51005 &`。那个 shell 一退,夹具就被过继给
init,而这些脚本本身是裸的 `serve_forever()` / `uvicorn.run()`,**没有任何一方负责
收它**。2026-10-21 在本机实测到 9 个这样的孤儿:存活 20 小时到 10 天 22 小时,8 个
里 7 个所属的 worktree 早就删了、进程还钉着那份已删的 `.venv`。

判据是「没人用了」,不是「父进程没了」
------------------------------------
`tests/probe_shell.mjs` 那边盯的是 `process.ppid`,**这边不能照搬**:探针是
`run_probe()` 起的、一轮之内跑完,父进程全程活着;而夹具手起时的**合法**用法恰恰是
跨多个 Bash 调用共用一个 —— 起服务那个 shell 当场就退,随后另一个 shell 才跑探针。
拿「ppid 变了」当判据的话,夹具一起跑就自杀,这条合法的路直接断掉。

所以这边反过来排:
- **主防线是空闲超时**(`KB_FIXTURE_IDLE` 秒,默认 15 分钟,`0` 关闭)。每个请求续一次
  命,够久没人来就自己退。它不关心谁是父进程,因此**手起这条路也盖得住** —— 那正是
  实测泄漏的全部 9 个。
- **副防线才是原调用方看门狗**,而且**只在调用方显式告知时启用**(`KB_PROBE_PARENT`,
  `fixture_server()` 写自己的 pid)。它盖的是「pytest 被 SIGKILL,`finally` 里的
  `reap()` 一行不跑」那一档 —— 空闲超时也兜得住,只是要等满 15 分钟,这一道把它缩到
  一次轮询。**没有 ppid 回退**:现读 ppid 正是上面那条自杀路径。

两道都不依赖调用方临死前跑任何代码,这是它们和 `reap()` 的分工所在。

回归 `tests/test_fixture_life.py`。
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

#: `fixture_server()` 把自己的 pid 放这儿;跟 `tests/headless.py` 认同一个变量名。
PARENT_ENV = 'KB_PROBE_PARENT'

#: 多久没人来就自己退(秒)。`0` = 关掉这道防线,留给手动调试页面的人。
IDLE_ENV = 'KB_FIXTURE_IDLE'
IDLE_DEFAULT = 900.0

#: 问一次「该退了吗」的间隔。空闲超时按分钟算,这个值只决定副防线有多快。
POLL_SECONDS = 0.4

#: SIGTERM 发给自己之后再等这么久;还没退就硬退(见 `_bail`)。
GRACE_SECONDS = 10.0

_LAST = time.monotonic()


def caller() -> int:
    """原调用方的 pid,`0` = 没有谁在等这一轮的结果。

    **启动这一刻它必须正是我们的父进程**,否则这个值是从环境里继承来的陈旧 pid
    (说的是别人的调用方),一律不认 —— 认了的话夹具会凭一个无关进程的死活自杀。
    """
    try:
        want = int(os.environ.get(PARENT_ENV, '') or 0)
    except ValueError:
        return 0
    return want if want > 1 and os.getppid() == want else 0


def idle_limit() -> float:
    """这次的空闲上限(秒);给不出数就用默认值,负数按关掉办。"""
    raw = os.environ.get(IDLE_ENV, '').strip()
    if not raw:
        return IDLE_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return IDLE_DEFAULT


def touch() -> None:
    """有人来过 —— 续一次命。两个适配层都只经过这一个出口。"""
    global _LAST
    _LAST = time.monotonic()


def idle_seconds() -> float:
    """离上一次有人来过多久了。"""
    return time.monotonic() - _LAST


def should_exit(limit: float, origin: int) -> str:
    """该退就返回退的理由,不该退返回空串 —— **判据只有这一份**,回归直接打它。

    `limit <= 0` 关掉空闲那一道;`origin <= 0` 关掉看门狗那一道。两道都关就永远不退
    (那是手动调试页面时想要的)。
    """
    if origin > 0 and os.getppid() != origin:
        return f'原调用方 {origin} 已经退出'
    if limit > 0 and idle_seconds() >= limit:
        return f'已经 {limit:.0f} 秒没有请求'
    return ''


def _bail(why: str) -> None:
    """打一行再优雅收场。

    **发的是 SIGINT 不是 SIGTERM。** Python 对 SIGINT 的默认行为是抛
    `KeyboardInterrupt`,`main()` 里那个 `with contextlib.ExitStack()` 退得了栈 ——
    手动跑这一档那份临时根归我们自己删(见 `tests/fixture_root.py`)。SIGTERM 没有
    handler 时是**直接结束**,清理一行都不跑:第一版就是发的 SIGTERM,实测退出码
    -15、盘上留下一份完整的 `kb-source-qa-*` 沙箱库 —— 跟 `fixture_root.py` 记载的
    那个事故形态一模一样,等于把它又犯了一遍。
    uvicorn 两个信号都接;`serve_workspace` 那个裸 server 靠 `except
    KeyboardInterrupt` 接住。两条路都走得通。

    **后面两道是兜底**:SIGINT 要对端配合(收不到,或者卡在清理里),所以再给一次
    SIGTERM,还不走就硬退 —— 一道自己卡住的防线比没有更糟。
    """
    try:
        sys.stderr.write(f'tests/fixture_life.py: {why},自行退出\n')
        sys.stderr.flush()
    except Exception:                # stderr 多半通向一个已经没了的调用方
        pass
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(GRACE_SECONDS)
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(GRACE_SECONDS)
    os._exit(143)                    # 143 = 被信号收场的惯例


def _watch(limit: float, origin: int) -> None:
    while True:
        why = should_exit(limit, origin)
        if why:
            _bail(why)
            return
        time.sleep(POLL_SECONDS)


def install(*, limit: float | None = None, origin: int | None = None) -> None:
    """装上两道防线。两道都关就什么也不起,免得白留一个线程。

    线程是 `daemon`:它只是「夹具还活着的时候顺便盯一眼」,不该自己把进程留住
    (对应 `probe_shell.mjs` 那个 interval 的 `unref()`)。
    """
    limit = idle_limit() if limit is None else limit
    origin = caller() if origin is None else origin
    touch()
    if limit <= 0 and origin <= 0:
        return
    try:
        sys.stderr.write(f'tests/fixture_life.py: {banner(limit)}\n')
        sys.stderr.flush()
    except Exception:
        pass
    threading.Thread(target=_watch, args=(limit, origin),
                     daemon=True, name='fixture-life').start()


def banner(limit: float | None = None) -> str:
    """启动那行里跟着打的一句,好让看日志的人知道它会自己退、怎么关掉。"""
    limit = idle_limit() if limit is None else limit
    if limit <= 0:
        return f'（{IDLE_ENV}=0：不会因空闲自行退出）'
    return f'（空闲 {limit:.0f} 秒自行退出；{IDLE_ENV}=0 可关掉）'


class guard:
    """包住 ASGI app:每个请求续一次命,同时把防线装上。

    用法 `uvicorn.run(fixture_life.guard(app), ...)`。uvicorn 认的是「这东西是不是
    async callable」,类实例的 `__call__` 是 async,会被判成 asgi3。
    """

    def __init__(self, app, *, limit: float | None = None, origin: int | None = None):
        self._app = app
        install(limit=limit, origin=origin)

    async def __call__(self, scope, receive, send):
        touch()
        await self._app(scope, receive, send)


class TouchingHandlerMixin:
    """给 `http.server` 那一侧的适配:每个请求续一次命。

    挂在 `handle_one_request` 上而不是各个 `do_*`:keep-alive 连接上的每一个请求都
    走它,而 `do_*` 得一个个记得加。
    """

    def handle_one_request(self):           # noqa: D102 - 见类 docstring
        touch()
        super().handle_one_request()
