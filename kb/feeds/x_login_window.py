# -*- coding: utf-8 -*-
"""X 登录窗口的生命周期:谁开的、此刻还在不在、什么时候关、关之前怎么认人。

从 `kb/feeds/x_cdp.py` 拆出来(单文件 700 行上限)。那边是**和 Chrome 说话**
(找二进制、起进程、CDP 帧、profile 占用);这边只答一件事:**那个有头登录窗口
现在算谁的**。两件事的失效方式完全不同 —— 那边错了是抓不到推文,这边错了是向一个
无关进程发 SIGKILL。

**依赖方向只有一个:本模块 → `x_cdp`**(用它的 `_alive` / `_reap`)。
那边一个字都不许反过来 import 本模块。公开的名字(`remember_login_window`、
`release_login_window`、`login_window_left`、`watch_login_window`)**直接从本模块取**。

硬约束见 `docs/decisions/x-channels.md`。
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from kb.feeds.x_cdp import _alive, _reap


def _ps_field(pid: int, field: str) -> str:
    """向 `ps` 问这个 pid 的一个字段。问不出来返回空串。

    **一次只问一个字段**:`ps -o a=,b=` 的输出要自己切词,而两边都可能带空格 ——
    `comm` 是可执行文件全路径(「/Applications/Google Chrome.app/…」),`lstart`
    的段数还跟着 locale 变(中文环境下是「四  8月/27 11:32:01 2026」四段,英文是
    五段)。切错一次就是把身份比歪,而比歪的方向**两边都危险**:比松了会动错进程,
    比紧了会让自动关闭整条失效。所以不切。
    """
    try:
        out = subprocess.run(['ps', '-p', str(pid), '-o', f'{field}='],
                             capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ''
    return out.stdout.strip()


def _proc_identity(pid: int) -> tuple[str, str]:
    """向系统问这个 pid **此刻是谁**:(启动时刻, 可执行文件)。问不出来返回 ('', '')。

    **pid 不是跨重启的身份** —— 系统会复用它。原来只核对「pid 对得上 + 进程还活着」,
    而这两件事在「服务退出期间 Chrome 死了、profile 里的锁和记录都没来得及清、之后
    系统把这个号分给了别的进程」那一档上**同时成立**:于是自动关闭会向一个无关进程
    发 SIGTERM,必要时 SIGKILL(审查 R7)。原来注释里说的「下次发现陈旧记录就删除」
    管不了这一档 —— 复用发生在发现之前。

    启动时刻和可执行文件合起来才是身份:号可以复用,但「这个号是什么时候、由哪个
    程序占上的」不会跟着复用。**问不出来就当成认不出**(空串),调用方据此一个字
    都不碰 —— 口径同「坏记录等于没有记录」:拿不准的两个方向都会动错进程。

    比的是两次 `ps` 输出的**字符串**。locale 换掉(服务换了个 LANG 重启)会让它
    比不上 —— 那一档落在安全的那一边:认不出 ⇒ 不动,代价是这个窗口要用户自己关,
    而面板上有话说。
    """
    if pid <= 0:
        return '', ''
    born, exe = _ps_field(pid, 'lstart'), _ps_field(pid, 'comm')
    return (born, exe) if born and exe else ('', '')


# ── 登录窗口:谁开的、什么时候关 ────────────────────────────────────────────
# `x_browser.open_login_window` 起的是一个**有头**窗口,起完就返回(登录要几分钟,
# 一个 HTTP 请求不该挂在那里)。原来它到此为止 —— 关窗口全靠用户记得,而那个窗口
# 攥着 profile 的 ProcessSingleton 锁:忘了关,这条通道从那一刻起**每一轮同步全红**,
# 账号一个不漏(2026-10-19 实测:09:52 那一轮 51/51 还好好的,10:24 变成 51/51 全失败,
# 失败明细里 50 条都是同一句「profile 正被另一个窗口占用」;用户点「重试失败项」,
# 10:34 又原样红了一遍 —— 窗口没关,重试多少次都一样)。
#
# 所以窗口的生死不再押在记性上,三道各有射程:
#   ① **起它的那一刻记下身份**(`remember_login_window`),并起一个守望线程到点关掉
#      它 —— 不依赖后面有没有人来抓;
#   ② **抓取和「检查连接」开工前各兜一次**(`release_login_window`):服务重启之后
#      守望线程没了,而那个窗口还开着,只剩这一道；
#   ③ **没到点的不动**(用户可能正在输密码、过两步验证),由 `login_window_left`
#      把"还有多久"交给面板照实说。
#
# **只关我们自己起的那一个。** 判据是「ProcessSingleton 指着的 pid == 我们记下的
# 那个」—— 用户自己拿同一个 `--user-data-dir` 开的窗口不归我们管。杀掉别人的进程
# 比堵着一条采集通道严重得多,所以这里宁可不关:不关的代价是这条通道继续被挡,
# 而那一档面板上有话说(①③),不是静默的。
#
# **记录落在 profile 目录里**,因为它描述的就是"谁占着这个目录";换数据目录、
# 删 profile 重来,这条记录跟着一起走,不会留下一个指着别人 pid 的孤儿。
# **时刻用墙上时钟**(`time.time()`)不用 `monotonic`:守望线程和下一次抓取可能
# 隔着一次进程重启,而 monotonic 的零点每个进程各不相同。

_LOGIN_NOTE = '.kb-login-window.json'

#: 登录窗口最多开这么久。给足两步验证和人机校验的时间;到点仍没关就是忘了关,
#: 而忘了关的代价是整条通道停摆。真没登录完,再点一次按钮就是了 —— 重开一次的
#: 代价是几秒钟,堵住的代价是每一轮同步全红。
LOGIN_WINDOW_TTL = 10 * 60

#: 刚起那几秒算**启动中**:`Popen` 返回只说明进程起来了,Chrome 要再花几百毫秒才
#: 建 ProcessSingleton 锁。这段时间里 `login_window_pid` 认不出我们的窗口 —— 而把
#: 「认不出」当成「窗口已经关了」就会**把记录删掉**,于是窗口开着、却再没有任何
#: 记录供守望、抓取前释放、连接检查这三道用(审查 R5:那就是退回"起完就不管了")。
LOGIN_WINDOW_STARTUP = 30.0


def _login_note(profile: str) -> str:
    return os.path.join(profile, _LOGIN_NOTE)


def remember_login_window(profile: str, pid: int) -> None:
    """记下「这个登录窗口是我们起的」。写不进去**不抛**。

    写不进去(满盘、权限)只意味着退回原来那个行为:窗口留着、要用户自己关,
    面板照旧报 profile 被占用。为它把「打开登录窗口」整个弄失败是反的 ——
    用户要的那件事(开窗口登录)已经成了。

    **身份连 pid 一起记**(`_proc_identity`):光一个 pid 不是身份,系统会复用它。
    记下来的那份在发信号之前要重新核对一次,对不上就一个字都不碰(审查 R7)。
    """
    pid = int(pid)                 # 调用方给的是 `Popen.pid`,先定型再往下用
    born, exe = _proc_identity(pid)
    try:
        with open(_login_note(profile), 'w', encoding='utf-8') as fh:
            json.dump({'pid': pid, 'at': time.time(),
                       'born': born, 'exe': exe}, fh)
    except OSError:
        pass


def forget_login_window(profile: str) -> None:
    try:
        os.unlink(_login_note(profile))
    except OSError:
        pass


def _read_login_note(profile: str) -> tuple[int, float, tuple[str, str]]:
    """(pid, 起始时刻, 进程身份)。读不出、格式不对、pid 不是正整数一律全空。

    **坏记录等于没有记录**,不是"当成占着"也不是"当成该关" —— 两个方向都会拿
    一份读不懂的文件去动别人的进程。

    身份(`born`/`exe`)缺席也算**认不出**:那是上一版写的记录,它没留下任何能
    排除 pid 复用的凭据 —— 拿不准就不动(审查 R7)。
    """
    try:
        with open(_login_note(profile), encoding='utf-8') as fh:
            note = json.load(fh)
        pid, at = int(note['pid']), float(note['at'])
        ident = (str(note.get('born') or ''), str(note.get('exe') or ''))
    except (OSError, ValueError, TypeError, KeyError):
        return 0, 0.0, ('', '')
    return (pid, at, ident) if pid > 0 else (0, 0.0, ('', ''))


def login_window_pid(profile: str) -> int:
    """**我们起的**那个登录窗口此刻的 pid;认不出就是 0。

    四个条件缺一不可:记录读得出、profile 真的被占着、占着的就是记录里那个 pid、
    **而且这个 pid 此刻还是当初那个进程**。前三条是「这是我们的窗口吗」,末一条是
    「这个号还是不是当初那个」—— `SingletonLock` 指着别人时那是用户自己开的窗口,
    而 pid 被系统复用之后指着的是一个跟 Chrome 毫无关系的进程(审查 R7),两档都
    一个字不许碰。

    **这是唯一的归属判据**,`release_login_window` 那条杀进程的路只从这儿进来。
    """
    pid, _, ident = _read_login_note(profile)
    if not pid:
        return 0
    try:
        target = os.readlink(os.path.join(profile, 'SingletonLock'))
    except OSError:
        return 0
    holder = target.rpartition('-')[2]
    if not (holder.isdigit() and int(holder) == pid and _alive(pid)):
        return 0
    # 身份对不上(或者压根问不出来/记录里没有)= 认不出,不是我们的窗口。
    return pid if ident != ('', '') and _proc_identity(pid) == ident else 0


def _starting(at: float) -> bool:
    """这份记录还在**启动中**那几秒里吗(见 `LOGIN_WINDOW_STARTUP`)。

    **「认不出我们的窗口」在这一档里不许当成「窗口已经关了」。** 两道清记录的路
    (守望线程第一跳、抓取开工前那一问)都要先过它。
    """
    return at > 0 and (time.time() - at) < LOGIN_WINDOW_STARTUP


def login_window_left(profile: str, *, ttl: float = LOGIN_WINDOW_TTL) -> float:
    """我们那个登录窗口还剩几秒自动关闭。0 = 没有我们的窗口,或者已经到点。

    **只读**:不杀进程、不删记录。面板那条路(`x_channels.readiness`)每次刷新都
    调它,而那个函数的契约是便宜且不改状态。
    """
    if not login_window_pid(profile):
        return 0.0
    at = _read_login_note(profile)[1]
    return max(0.0, at + ttl - time.time())


def release_login_window(profile: str, *, ttl: float = LOGIN_WINDOW_TTL) -> str:
    """到点了就关掉我们那个登录窗口。返回一句说明(没关就是空串)。

    **这一道是兜底,不是主路。** 主路是 `open_login_window` 起的守望线程;它在
    进程重启之后就没了,而窗口还开着 —— 那时只剩抓取开工前这一问。两道都有的
    理由是它们**失效的方式不一样**:守望线程死于进程退出,这一道死于"今天没人
    来抓"。少任何一道都会留下一段没人管的时间。

    没到点的**不动**:用户可能正在输密码、过两步验证、过人机校验,而这些恰恰
    是没法缩短的几分钟。那一档由调用方据 `login_window_left` 照实说还有多久,
    不是静默跳过。
    """
    note_pid, at, ident = _read_login_note(profile)
    # **先收尸再往下判。** 窗口被别人结束掉(用户强退、系统 OOM)时,它在我们
    # 这儿留下一格僵尸,而 `SingletonLock` 可能来不及删 —— 那两件事凑一起就是
    # 「`profile_busy` 永远为真、却没有窗口可关」。
    #
    # **收尸这一下不许押在身份核对上**(所以读的是记录里那个 pid,不是
    # `login_window_pid`):僵尸的 `ps` 身份恰恰读不出来(comm 变成空/« defunct »),
    # 押上去就等于永远不收尸。而它本身不需要归属判据:`waitpid` 只对**我们自己的
    # 子进程**成立(别人的直接抛 `ChildProcessError`),而且它对活着的子进程是
    # 空操作(`WNOHANG`)—— 它摆不到任何一个无关进程。
    if note_pid:
        _reap(note_pid)
    pid = login_window_pid(profile)
    if not pid:
        # **启动中的那几秒不许清**:这一刻认不出我们的窗口,是因为 Chrome 还没建好
        # ProcessSingleton 锁 —— 清掉记录就等于把刚起的那个窗口交出去,没人再管它
        # (审查 R5)。
        if _starting(at):
            return ''
        # 窗口已经不在了(用户自己关的,或者上一次就被我们关掉):顺手把陈旧记录
        # 清掉。留着它的话,下一次同一个 pid 被系统分配给别的进程时,上面那道
        # 「pid 对得上」就会把一个无辜的进程认成我们的窗口。
        if note_pid:
            forget_login_window(profile)
        return ''
    waited = time.time() - at
    if waited < ttl:
        return ''
    _close_login_window(pid, ident)
    forget_login_window(profile)
    return (f'已自动关掉开了 {int(waited // 60)} 分钟的 X 登录窗口'
            f'(上限 {int(ttl // 60)} 分钟),它占着抓取要用的浏览器')


def _close_login_window(pid: int, ident: tuple[str, str]) -> None:
    """先好好说一次,不行就 kill。**两步都有界,而且都不往外抛。**

    **每发一次信号之前重新核对身份**(`ident` 是记录里那一份)。归属在
    `login_window_pid` 里已经判过一次,可两刀之间隔着 6 秒宽限期 —— 它在这中间
    自己走掉、系统把这个号分给别人,第二刀就落在一个无关进程上(审查 R7)。
    核不上就当它已经走了:收个尸、收工。

    理由同 `Chrome._shutdown`:`SIGTERM` 让 Chrome 把 profile 存盘(登录态就在
    那里面,而用户开这个窗口正是为了它);但一个卡住的 Chrome 连信号都不处理,
    那时唯一有意义的事是把它结束掉 —— 留着它等于这条通道永远用不了。

    **抛出去的代价是记录不被清掉**,而陈旧记录会让下一次把别人的 pid 认成我们的
    窗口,所以这里一个异常都不许漏出去。
    """
    import signal
    for sig, grace in ((signal.SIGTERM, 6.0), (signal.SIGKILL, 3.0)):
        if _proc_identity(pid) != ident:
            _reap(pid)
            return
        try:
            os.kill(pid, sig)
        except OSError:
            _reap(pid)
            return                       # 它自己先走了,或者不归我们这个用户管
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            # **先收尸再问死活**:它是我们 `Popen` 起的子进程,SIGTERM 之后会在
            # 进程表里留一格僵尸,而 `os.kill(pid, 0)` 对僵尸照样成功。少了这一下,
            # 这个循环会把两段宽限期走满(实测 9 秒)、还对一个死了的进程补一刀。
            _reap(pid)
            if not _alive(pid):
                return
            time.sleep(0.1)


def watch_login_window(profile: str, *, proc=None,
                       ttl: float = LOGIN_WINDOW_TTL) -> None:
    """起一个守望线程:到点把登录窗口关掉。

    **daemon 线程**:它不该拦着进程退出 —— 进程退出时那个窗口归
    `release_login_window` 那一道管(下一次抓取或「检查连接」时)。

    轮询而不是 `waitpid`:窗口是 `start_new_session` 起的独立会话,而且我们关心的
    事件是"到点了",不是"它退出了"。

    `proc` 是**我们自己手里那个 `Popen` 句柄**,由 `x_browser.open_login_window`
    传进来。有它的话「它还在不在」就由它回答,而不是由 profile 里那两个文件回答
    —— 后者在两档上都会把「还在」说成「不在」:①`Popen` 刚返回那几百毫秒里
    ProcessSingleton 锁还没建好;②记录文件一次暂时读不出来(满盘、权限、换目录)。
    认成"窗口关了"的后果是**第一跳就把记录删掉收工**,而窗口已经开着了 —— 守望、
    抓取前释放、连接检查三道全都没有记录可读,等于退回"起完就不管了"(审查 R5)。
    句柄不会被 pid 复用骗到,所以它是这三者里最硬的那一个。

    **认不出我们的窗口时只接着守,不动任何进程。** 杀谁的判据只有
    `login_window_pid` 那一份(它要身份对得上)—— 拿句柄去补一条杀的路就是把
    「这是不是我们的窗口」判在两处(审查 R7 的反面),而那两处迟早岔开。
    """
    import threading

    def _watch() -> None:
        started = time.time()
        while True:
            # ① 手里那个进程自己退了 —— 这是「窗口关了」最硬的证据，比「启动中」
            #    那道宽限硬—— 所以这儿自己清记录，不走 `release_login_window`（它的
            #    启动宽限会把刚写下的记录留住）。退了的进程没什么可关的。
            if proc is not None and proc.poll() is not None:
                forget_login_window(profile)
                return
            if not login_window_pid(profile):
                # ② 认不出:启动中,或者记录暂时读不出来。手里的句柄还活着就接着守
                #    (上限是这个窗口自己的 TTL —— 再久也没有可守的了)。
                if proc is not None and time.time() - started < ttl:
                    time.sleep(2.0 if time.time() - started < LOGIN_WINDOW_STARTUP
                               else 15.0)
                    continue
                release_login_window(profile, ttl=ttl)   # 清陈旧记录
                return
            # ③ 到点了就关(关不关、关谁,全在 `release_login_window` 里判)
            left = login_window_left(profile, ttl=ttl)
            if left <= 0:
                release_login_window(profile, ttl=ttl)
                return
            time.sleep(min(15.0, left))

    threading.Thread(target=_watch, name='x-login-window', daemon=True).start()
