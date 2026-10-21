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
- **主防线是空闲超时**(`KB_FIXTURE_IDLE` 秒,默认 15 分钟,`0` 关闭)。**在途的请求不算
  空闲** —— 只在进门时续一次命的话,一个慢请求跑到一半夹具就退了,客户端拿到 502。
- **副防线是所有权凭据**(`KB_FIXTURE_OWNER`),只有 `fixture_server()` 给得出。它盖的是
  「跑测试那个进程被 SIGKILL,`finally` 里的 `reap()` 一行不跑」那一档。

凭据为什么是一根 pipe,不是一个 pid
----------------------------------
第一版传的是调用方的 pid,夹具拿「现在的 ppid 还等于它吗」判这个值可不可信。**那是错
的**:七个 ASGI 夹具在开服务之前要建库、导出、建索引,调用方死在这几十秒里时 ppid 早
变成 1 了,于是**正确的 pid 被当成继承来的陈旧值丢掉**,副防线整个不装。实测:真调用方
被 SIGKILL,夹具照样从暂停点继续开服务、还能访问。把检查前移也只是把窗口改小。

现在传的是一根**继承来的** pipe 读端,外加父进程预先写进去的随机 token:
- **没有窗口**:fd 在 `exec` 那一刻就在了,调用方死在哪一步都立刻 EOF;
- **可验证**:陈旧的环境变量带不来那根 pipe,token 对不上就不认;
- **手起这条路照旧没有 owner**,只受空闲超时管 —— 那条合法的路不受影响。

日志不许挡住任何一道防线,也不许动调用方的东西
--------------------------------------------
没人消费 stderr 时那根管道会满,而满管道上的 `write` 是**阻塞**不是异常,`try/except`
一个字都挡不住。实测这一下能把整条防线堵死,堵法有两种:
- `_bail` 自己卡在发信号之前,连后面 `os._exit` 那道硬退都执行不到;
- 访问日志卡住请求线程,`handle_one_request` 永远不返回,那个请求就永远算「在途」,
  空闲超时看见还有在途便不退 —— 加了在途计数之后,这条反而成了新的堵法。

**第二版把 fd 2 切成非阻塞来躲开它,那是错的**:`O_NONBLOCK` 不挂在「我这个进程的 2
号」上,它挂在 open-file-description 上,而手起夹具继承的 stderr 跟调用方是**同一份**。
改了它,调用方和一切共享者一起看得见 —— 实测调用方自己什么都没做,它的 `write` 开始抛
`BlockingIOError`、输出缺行。`dup` 也隔不开,复制出来的 fd 指的还是那一份。
**借来的 fd 不是我们可以随便重设状态的私有对象。**

现在的做法是**把 stderr 挪出所有防线的路径**:谁要说话都只往一个有界队列里丢
(`_say`,满了就丢,从不等),真正的 `write` 由一个专门的守护线程做。那个线程卡在满管道
上无所谓 —— 没有任何人等它。**日志是可丢弃的,防线和调用方的 IO 不是。**

回归 `tests/test_fixture_life.py`。
"""

from __future__ import annotations

import contextlib
import math
import os
import queue
import selectors
import signal
import stat
import threading
import time

#: 所有权凭据:`"<fd>:<token>"`。fd 是一根**继承来的** pipe 读端(`pass_fds`),
#: token 是父进程在起我们之前就写进那根 pipe 的随机串。唯一的写入方是
#: `tests/headless.py` 的 `fixture_server()`。
OWNER_ENV = 'KB_FIXTURE_OWNER'

#: 多久没人来就自己退(秒)。`0` = 关掉这道防线,留给手动调试页面的人。
IDLE_ENV = 'KB_FIXTURE_IDLE'
IDLE_DEFAULT = 900.0

#: 问一次「该退了吗」的间隔。空闲超时按分钟算,这个值只决定判出来之后多快动手。
POLL_SECONDS = 0.4

#: 每一级收场信号之间等多久(见 `_bail`)。
GRACE_SECONDS = 10.0

#: **这三样是一份状态,一把锁。** 在途数、上一个请求回完的时刻、以及「已经决定收场
#: 了」这个闸门 —— 分开更新就会出现「没有在途,而完成时刻还是上一个请求的」那一瞬,
#: 看门狗正好在这里醒来便把刚回完的请求判成空闲很久(见 `_decide`)。
_COUNT = threading.Lock()
_LAST = time.monotonic()
_INFLIGHT = 0
_CLOSING = False

_OWNER_GONE = False
_LIMIT = IDLE_DEFAULT
_IDLE_ARMED = False
_BAILING = threading.Lock()

#: 要说的话先进这里,由 `_log_pump` 一个线程写出去。满了就丢。
_LOGS: queue.Queue = queue.Queue(maxsize=256)


class OwnerUnusable(RuntimeError):
    """父进程交了个有效凭据,但我们在这台机器上**盯不住**它。

    **这一类不许静默降级成「手起无主」。** 那条路一道防线都不装,而调用方以为副防线
    在 —— 它取消之后页面还开着、进程还留着。实测过这个形态:上一版用
    `select.select()` 等凭据可读,而它在 fd ≥ `FD_SETSIZE`(1024)时抛 `ValueError`;
    `os.pipe()` 不保证给低号、`pass_fds` 又原号传下来,所以调用方只要先开着一千来个
    文件,凭据就落在高位上,接收端把 `ValueError` 当成「从来没有 owner」—— 取消之后
    页面照旧 200,进程还是同一个。

    所以机制失败一律抛出去:模块加载失败 → 夹具起不来 → `fixture_server` 当场报
    「启动失败」。**「少一道防线」必须是响的,不能是静的。** 注意这跟「凭据验不过」
    是两件事:token 对不上、不是 FIFO、号已经坏了,那些说的是「继承来的陈旧值」,
    照旧安静地退回无主(手起那条合法的路要靠它)。
    """


def _owner_selector(fd: int) -> selectors.BaseSelector:
    """给凭据那根 fd 建一个等待器。**不许用 `select.select`** —— 见 `OwnerUnusable`。

    `selectors.DefaultSelector` 在 macOS 上是 kqueue、Linux 上是 epoll,都没有
    `FD_SETSIZE` 那道上限(实测 fd 1103 照样等得到)。万一在某个平台上退化成
    `SelectSelector`,那道上限又回来了 —— 那就抛,别假装没有 owner。
    """
    sel = None
    try:
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ)
    except Exception as exc:
        if sel is not None:
            with contextlib.suppress(Exception):
                sel.close()
        if isinstance(exc, OSError):
            raise                    # fd 本身坏了,交给上面按「验不过」处理
        raise OwnerUnusable(f'这个等待机制盯不住 fd {fd}:{exc!r}') from exc
    return sel


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


def claim_owner() -> int:
    """认领所有权凭据,返回那根 pipe 的读端;没有、或者验不过,返回 `-1`。

    **token 是「这根 pipe 确实是起我们那一位给的」的证据**。只看 fd 号的话,一个从环境
    里继承下来的陈旧号可能在当前进程里指着别的东西。所以先要求它是个 FIFO、不是 stdio,
    再把父进程预先写好的 token 读出来比对 —— 父进程在 `Popen` **之前**就写完了,所以
    这一下读得到,哪怕它在我们起身的途中已经被杀。
    """
    spec = os.environ.get(OWNER_ENV, '').strip()
    raw, _, token = spec.partition(':')
    if not raw or not token:
        return -1
    try:
        fd = int(raw)
    except ValueError:
        return -1
    if fd <= 2:                      # 0/1/2 是 stdio,不可能是凭据
        return -1
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            return -1                # 陈旧的号指到了别的东西
    except (OSError, OverflowError, ValueError):
        # `int()` 收得下任意大的数,`fstat` 到那儿才抛 `OverflowError` —— 畸形凭据
        # 要按「没有凭据」处理,不许让整个模块 import 失败。
        return -1
    want = token.encode()
    # **这一下读绝不许阻塞,而且绝不许改这个 fd 的模式。** 父进程在 `Popen` 之前就把
    # token 写进去了,正常情况一次就读全(远小于 PIPE_BUF,写是原子的)。但凭据可能是
    # 畸形的 —— 比如环境里那个 token 比管道里的长 —— 阻塞读会在读完已有数据之后
    # **永远等下去**,而这一段跑在模块加载里:夹具卡死在 import,一个字的日志都没有。
    #
    # 上一版靠 `set_blocking(fd, False)` 躲开,再在 `finally` 里设回阻塞 —— 两处都错:
    # ①那个标志挂在 open-file-description 上,这根 fd 此刻还**没验过**,它可能是调用方
    # 共享的任何东西;②拒绝凭据时「设回阻塞」不是恢复原状,是按我们的猜测改了别人的
    # 东西(实测:给一个原本非阻塞的 pipe、token 故意写错,凭据被拒,而它被留成阻塞的)。
    # `select` 问「现在可读吗」,一个比特都不用动。
    #
    # 走到这里,这个 fd 已经是「一根真管道」了 —— **凭据看起来是真的**,所以从这一步
    # 起,等待机制建不出来是 `OwnerUnusable`(抛),不是「没有 owner」(返回 -1)。
    try:
        sel = _owner_selector(fd)
    except OSError:
        return -1                    # 号坏了,按陈旧值办
    got = b''
    deadline = time.monotonic() + 1.0
    try:
        while len(got) < len(want):
            left = deadline - time.monotonic()
            if left <= 0:
                break                # 凭据不完整,按没有凭据办
            if not sel.select(left):
                break
            try:
                chunk = os.read(fd, len(want) - len(got))
            except OSError:
                return -1
            if not chunk:
                break                # 写端已经关了,凭据不完整
            got += chunk
    finally:
        with contextlib.suppress(Exception):
            sel.close()
    return fd if got == want else -1


def owner_gone() -> bool:
    """原调用方没了吗(没有 owner 时恒为 False)。"""
    return _OWNER_GONE


def _watch_owner(fd: int, sel: selectors.BaseSelector) -> None:
    """盯着那根 pipe:调用方一死,写端被内核关掉,我们这边立刻读到 EOF。

    **这是唯一不依赖调用方临死前跑任何代码的信号** —— 它被 SIGKILL 那一档也盖得住,
    而写在它那边的 `finally` / handler 一行都不会跑。

    **等可读走等待器,不靠这个 fd 是阻塞的。** 它是调用方给的,可能本来就是非阻塞的
    (我们也不许去改它);直接 `os.read` 的话会当场拿到 `BlockingIOError` —— 那是
    `OSError` 的子类,会被下面那个 `except` 吞成「写端关了」,夹具刚起来就自己退掉。

    **等待器由 `_arm_owner` 在模块加载时就建好传进来**,不在这个线程里建:建不出来是
    `OwnerUnusable`,那得在启动路径上响出来(见那个异常的说明),而线程里抛谁也看不见。
    """
    global _OWNER_GONE
    try:
        while True:
            sel.select()             # 没有超时:就在这儿等着
            if not os.read(fd, 1):
                break                # EOF —— 写端关了,调用方没了
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(Exception):
            sel.close()
    _OWNER_GONE = True
    _check()


def idle_limit() -> float:
    """这次的空闲上限(秒);给不出有效数字就回那个保守的默认值。

    `nan` / `inf` 按**无效配置**办:前者任何比较都是假、后者永远不到点,照单全收的话
    手起且没有 owner 的夹具会悄悄失去唯一那道防线。有意写 `0` 的出口照旧保留。
    """
    raw = os.environ.get(IDLE_ENV, '').strip()
    if not raw:
        return IDLE_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return IDLE_DEFAULT
    if not math.isfinite(value):
        return IDLE_DEFAULT
    return max(0.0, value)


def touch() -> None:
    """有人来过 —— 续一次命。"""
    global _LAST
    with _COUNT:
        _LAST = time.monotonic()


def idle_seconds() -> float:
    """离上一个请求回完多久了。"""
    with _COUNT:
        return time.monotonic() - _LAST


def inflight() -> int:
    """手上还有几个请求没回完。"""
    with _COUNT:
        return _INFLIGHT


def _enter() -> bool:
    """接纳一个请求;**已经决定收场了就不接纳**,返回 `False`。

    这个返回值是「已接纳的请求不会被空闲判据截断」那条保证的另一半:判定和接纳共用
    `_COUNT`,所以不存在「判据读到 busy=0 之后、动手之前又进来一个」那个缝 —— 那个
    请求要么在判定之前进来(于是 busy 不是 0,不会判退),要么在之后被这里拒掉。
    """
    global _INFLIGHT
    with _COUNT:
        if _CLOSING:
            return False
        _INFLIGHT += 1
        return True


def _leave() -> None:
    """一个请求回完了:**计数和完成时刻在同一把锁里一起落**。

    上一版是「锁里减到 0,放了锁再 `touch()`」。中间那一瞬的状态是「没有在途,而完成
    时刻还是上一个请求甚至启动时刻」—— 看门狗正好在这里醒来就把它判成空闲很久。实测
    (确定性线程注入)一个 4 秒的慢请求正常返回之后 19 毫秒夹具就退了,远没到承诺的
    空闲时间。
    """
    global _INFLIGHT, _LAST
    with _COUNT:
        _INFLIGHT -= 1
        _LAST = time.monotonic()


@contextlib.contextmanager
def active():
    """一次请求从进门到回完的整段 —— 这期间不算空闲。

    只在进门时 `touch()` 一次是不够的:读上游、发响应、等流式数据那几秒全被算成空闲,
    实测一个 4 秒的慢请求在 1 秒空闲上限下会被拦腰砍断,客户端拿到 502。

    `yield` 出来的是「接纳了吗」。没接纳说明这个进程已经在收场,两条适配路照旧把请求
    转下去(能回多少算多少),但不记在途 —— **收场的决定不会因为它而翻盘**。
    """
    accepted = _enter()
    try:
        yield accepted
    finally:
        if accepted:
            _leave()


def should_exit(limit: float, gone: bool, busy: int, idle: float) -> str:
    """该退就返回退的理由,不该退返回空串 —— **判据只有这一份**,回归直接打它。

    是个**纯函数**:`busy` 和 `idle` 由调用方在同一把锁里一起取(见 `_decide`)。自己
    去读时钟的话,读到的就不是和 `busy` 同一瞬的状态,竞争就搬进这里了。

    `limit <= 0` 关掉空闲那一道;`gone` 为假时不看 owner。两道都不成立就永远不退
    (那是手动调试页面时想要的)。

    **丢了 owner 要立刻退,不等在途请求回完**:那一刻已经没人在等这个结果了。
    """
    if gone:
        return '原调用方已经退出'
    if limit > 0 and busy == 0 and idle >= limit:
        return f'已经 {limit:.0f} 秒没有请求'
    return ''


def _decide(limit: float, gone: bool) -> str:
    """取状态、下判断、落闸门 —— **三件事在同一把锁里**,这是那条不变量所在。

    返回非空就表示「这一次由你去收场」,`_CLOSING` 已经落下:后面来的请求不再被接纳,
    重复判出来的也只会拿到空串。
    """
    global _CLOSING
    with _COUNT:
        if _CLOSING:
            return ''                # 已经在收场了
        why = should_exit(limit, gone, _INFLIGHT, time.monotonic() - _LAST)
        if why:
            _CLOSING = True
        return why


def _bail(why: str) -> None:
    """打一行再优雅收场。重入只有第一个管用。

    **发的是 SIGINT,不是 SIGTERM。** Python 对 SIGINT 的默认行为是抛
    `KeyboardInterrupt`,`main()` 里那个 `with contextlib.ExitStack()` 退得了栈 ——
    手动跑这一档那份临时根归夹具自己删(见 `tests/fixture_root.py`)。SIGTERM 没有
    handler 时是**直接结束**,清理一行都不跑:第一版就是发的 SIGTERM,实测退出码
    -15、盘上留下一份完整的 `kb-source-qa-*` 沙箱库 —— 跟 `fixture_root.py` 记载的
    那个事故形态一模一样,等于把它又犯了一遍。
    uvicorn 两个信号都接;`serve_workspace` 那个裸 server 靠 `except
    KeyboardInterrupt` 接住。两条路都走得通。

    **后面两道是兜底**:SIGINT 要对端配合(收不到,或者卡在清理里),所以再给一次
    SIGTERM,还不走就硬退 —— 一道自己卡住的防线比没有更糟。

    这一整段**不碰 stderr**:理由丢进队列,最多给它半秒把话说完(`_drain_logs`),
    然后照样发信号。上一版在这里把 fd 2 切成非阻塞,那会连调用方的 stderr 一起改掉。
    """
    if not _BAILING.acquire(blocking=False):
        return                       # 两条看门狗同时判出来,收一次就够
    _say(f'tests/fixture_life.py: {why},自行退出\n')
    _drain_logs(0.5)             # 能打出来最好;打不出来也不许拖住收场
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(GRACE_SECONDS)
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(GRACE_SECONDS)
    os._exit(143)                    # 143 = 被信号收场的惯例


def _check() -> None:
    """过一遍那份唯一的判据,该退就退。两条看门狗都只经过它。

    **不要在这里自己拼 `should_exit(..., inflight(), idle_seconds())`** —— 那是两次
    独立取锁,中间状态会变,竞争就又回来了。`_decide` 是唯一那道一致的边界。
    """
    why = _decide(_LIMIT, owner_gone())
    if why:
        _bail(why)


def _watch_idle() -> None:
    while True:
        _check()
        time.sleep(POLL_SECONDS)


def install(*, limit: float | None = None) -> None:
    """装上空闲那一道。所有权那一道在模块加载时就装好了(见文件末尾)。

    **要装在摆盘之后**(就在 `uvicorn.run` / `serve_forever` 那一下):装早了的话建库、
    导出、建索引那几十秒本身就是「没有请求」,夹具会在起身途中被自己的空闲超时收掉。

    线程是 `daemon`:它只是「夹具还活着的时候顺便盯一眼」,不该自己把进程留住
    (对应 `probe_shell.mjs` 那个 interval 的 `unref()`)。
    """
    global _LIMIT, _IDLE_ARMED
    _LIMIT = idle_limit() if limit is None else limit
    touch()
    _say(f'tests/fixture_life.py: {banner(_LIMIT)}\n')
    if _LIMIT <= 0 or _IDLE_ARMED:
        return                       # 幂等:`announce()` 和 `guard()` 都会叫它
    _IDLE_ARMED = True
    threading.Thread(target=_watch_idle, daemon=True, name='fixture-idle').start()


def announce(text: str) -> None:
    """摆盘结束、要打启动提示了:**先把防线装上,再说话**。

    这两件事的顺序是硬的,而且必须由**这一个出口**保证 —— 六个入口各写一遍
    `print(...)` 然后 `guard(app)` 的话,顺序对不对就要逐个入口去看。上一版就是那样,
    六个全错:`install()` 在 `guard` 里面,而 stdout 没人消费时那次 `print` 就阻塞在
    那儿,`install()` 永远执行不到。手起这条路本来就没有 owner,于是**一道防线都没
    装**,进程永久留着;`print` 阻塞不抛异常,外面什么都看不到。

    实测:给手起的 `serve_sources.py` 一根预先填满的 stdout,过了空闲上限之后进程还
    在、端口不响应;一开始消费那根管道,它立刻起来服务,随后按空闲正常退掉。

    **这一行提示本身是可丢弃的**(走那个有界队列,没人消费 stdout 就丢)。提示丢得起,
    防线丢不起 —— 口径同 stderr 那一侧。
    """
    install()
    _say(text if text.endswith('\n') else text + '\n', stream=1)


def banner(limit: float | None = None) -> str:
    """启动那行里跟着打的一句,好让看日志的人知道它会自己退、怎么关掉。"""
    limit = idle_limit() if limit is None else limit
    if limit <= 0:
        return f'（{IDLE_ENV}=0：不会因空闲自行退出）'
    return f'（空闲 {limit:.0f} 秒自行退出；{IDLE_ENV}=0 可关掉）'


class guard:
    """包住 ASGI app:每个请求记一次在途,同时把空闲那道防线装上。

    用法 `uvicorn.run(fixture_life.guard(app), ...)`。uvicorn 认的是「这东西是不是
    async callable」,类实例的 `__call__` 是 async,会被判成 asgi3。
    """

    def __init__(self, app, *, limit: float | None = None):
        self._app = app
        install(limit=limit)

    async def __call__(self, scope, receive, send):
        # lifespan 从起到停贯穿整个进程,它**不是**一次请求 —— 算进在途的话空闲
        # 超时永远不会到点,这道防线就等于没装。
        if scope.get('type') == 'lifespan':
            await self._app(scope, receive, send)
            return
        with active():               # 没接纳(已在收场)也照转,只是不记在途
            await self._app(scope, receive, send)


class TouchingHandlerMixin:
    """给 `http.server` 那一侧的适配:每个请求记一次在途。

    **在途从「请求行已经读到」开始算,不从 `handle_one_request` 进门算**:keep-alive
    连接空闲等下一个请求时就阻塞在读请求行上,把那一段算成在途的话,浏览器挂着一条
    连接就能让夹具永不退出 —— 空闲超时还是等于没装,只是换了个失守的方向。
    """

    def log_message(self, fmt, *args):  # noqa: D102 - 见类 docstring
        # 访问日志必须走那条不阻塞的路:`http.server` 原来的实现写 `sys.stderr`,
        # 管道一满这个请求就永远停在「在途」上,把空闲超时那道防线一起堵死。
        _say(f'{self.address_string()} - - [{self.log_date_time_string()}] '
             f'{fmt % args}\n')

    def parse_request(self):            # noqa: D102 - 见类 docstring
        ok = super().parse_request()
        if ok and not getattr(self, '_fixture_active', False):
            # 记在途**只在真被接纳时**:已经决定收场了就不接纳,那个决定不许因为
            # 这个请求翻盘(口径在 `_enter`)。
            self._fixture_active = _enter()
        return ok

    def handle_one_request(self):       # noqa: D102 - 见类 docstring
        self._fixture_active = False
        try:
            super().handle_one_request()
        finally:
            if getattr(self, '_fixture_active', False):
                self._fixture_active = False
                _leave()


def _arm_logs() -> None:
    """写 stderr 的那个线程**在模块加载时就起**。

    `_say` 从那一刻起就只是「往队列里丢」,所以所有权看门狗、空闲看门狗、访问日志、
    收场理由没有一处会因为 stderr 不动而变慢 —— 而且谁都不用去碰 fd 2 的状态。
    """
    threading.Thread(target=_log_pump, daemon=True, name='fixture-log').start()


_arm_logs()


def _arm_owner() -> None:
    """所有权那道防线**在模块加载时就装**,不等 `install()`。

    七个 ASGI 夹具在 `install()` 之前要建库、导出、建索引 —— 那几十秒正是调用方最可能
    被取消的窗口,等到那时候再装就晚了。没有凭据(手起这条路)就什么也不起。

    **凭据有效却装不上,一律抛出去。** 不管是等待机制盯不住这个号,还是线程起不来 ——
    抛出去就是模块加载失败,夹具起不来,`fixture_server` 当场报启动失败。安静地少一道
    防线才是最坏的结果:调用方以为它在,取消之后页面还开着(见 `OwnerUnusable`)。
    """
    fd = claim_owner()               # 机制失败会抛 OwnerUnusable
    if fd < 0:
        return
    sel = _owner_selector(fd)        # 同样,抛出去 = 启动失败,不静默降级
    try:
        threading.Thread(target=_watch_owner, args=(fd, sel), daemon=True,
                         name='fixture-owner').start()
    except Exception as exc:
        with contextlib.suppress(Exception):
            sel.close()
        raise OwnerUnusable(f'盯原调用方的线程起不来:{exc!r}') from exc


_arm_owner()
