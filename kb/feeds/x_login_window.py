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
    _write_login_note(profile, pid, time.time(), _proc_identity(pid))


def _write_login_note(profile: str, pid: int, at: float,
                      ident: tuple[str, str]) -> None:
    """落盘那一下只有这一个出口(`remember_login_window` 和重新认人那条路都过它)。

    **`at` 由调用方给**:重新认人时要把原来那个起始时刻原样留住 —— 换成"现在"就是
    把 TTL 重新计时,那个窗口于是永远到不了点。
    """
    born, exe = ident
    try:
        with open(_login_note(profile), 'w', encoding='utf-8') as fh:
            json.dump({'pid': pid, 'at': at, 'born': born, 'exe': exe}, fh)
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


def login_window_stuck(profile: str) -> bool:
    """**有我们开的窗口、它还占着这个 profile,可是认不出来** —— 到点不会自动关。

    这一档是真会发生的:`_proc_identity` 问 `ps` 要身份,**那一问失败一次就永久
    失配** —— 记录里存着 `('', '')`,后面 `ps` 和锁都恢复了也没人再去问第二遍
    (审查 R5)。同判据的另一个实例是启动脚本:它先自己初始化、再 `exec` 真正的
    Chrome,于是同一个子进程的 `comm` 在我们记下身份**之后**变了。

    两种都让 `login_window_pid` 恒为 0,而守望线程手里有句柄时会去**重新认一次**
    (`_relearn_identity`)、到点用句柄自己关掉它 —— 所以这一档只在**没有句柄**时
    留下来:服务重启过,守望线程没了,只剩抓取开工前那一问,而那一问不许凭一份对
    不上的身份去杀进程。

    **那时面板必须改口。** 认不出 ⇒ `login_window_left` 是 0 ⇒ 面板那段「登录窗口
    开着,X 分钟后自动关闭」整段不说,于是 readiness 一路走到「Cookies 在,就绪」
    —— 窗口明明占着浏览器,而面板说一切正常,每一轮同步照旧全红。这正是这块代码
    要防的那场事故,换了个入口又回来了。

    判据**只认我们自己那份记录指着的那个 pid**:锁指着别人时那是用户自己开的窗口
    (不归我们管,口径同 `login_window_pid`),不在这一档里。
    """
    pid, at, _ = _read_login_note(profile)
    if not pid or _starting(at) or login_window_pid(profile):
        return False
    try:
        target = os.readlink(os.path.join(profile, 'SingletonLock'))
    except OSError:
        return False
    holder = target.rpartition('-')[2]
    return holder.isdigit() and int(holder) == pid and _alive(pid)


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
        # **「认不出来」和「已经退出」是两件事,而它们的结论正好相反。**
        # 记录还指着一个活着的、而且确实还占着这个 profile 的进程时,清掉记录就等于
        # 把唯一的诊断依据删了:面板那段「不会自动关闭,请手动把那个窗口关掉」靠的
        # 就是这份记录(`x_channels.readiness` 读 `login_window_stuck`)。删掉之后
        # readiness 一路走到「Cookies 在,就绪」—— 窗口明明占着浏览器,而面板说一切
        # 正常,每一轮同步照旧全红(审查 N2:抓取和「检查连接」各会先释放一次,
        # 所以这条路一天里走好几遍)。
        # **保留记录不等于放宽身份判据**:这一档照旧不发任何信号(认不出身份就不许
        # 杀进程),只是把「它还占着」这件事留着让人看见。
        if login_window_stuck(profile):
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
    opened = int(waited // 60)
    if not _close_login_window(pid, ident):
        # **没确认它退出,就不许说已经关掉,也不许删记录。** 删了的话:面板上那一行
        # 没了、守望和下一次释放都读不到它,而那个窗口还占着 profile —— 用户照着
        # 「已自动关掉」去抓,下一轮照旧起不来,而一个线索都没有(第 3 轮审查 N2)。
        # **留着记录不放宽身份判据**:`login_window_pid` 照旧要身份对得上,所以 pid
        # 复用骗不到它;而它这会儿确实还活着,那个号也就不会被分给别人。
        # 口径同上面「认不出那个窗口」那一档:**改口,不代劳**。
        return (f'关不掉那个开了 {opened} 分钟的 X 登录窗口'
                f'(上限 {int(ttl // 60)} 分钟),它还占着抓取要用的浏览器，'
                '请手动关掉它')
    forget_login_window(profile)
    return (f'已自动关掉开了 {opened} 分钟的 X 登录窗口'
            f'(上限 {int(ttl // 60)} 分钟),它占着抓取要用的浏览器')


#: 两步关闭各等多久(秒):先 SIGTERM 等 6 秒让 Chrome 把 profile 存盘,再 SIGKILL
#: 等 3 秒。**抽成常量是为了给回归一个旋钮** —— 「连 SIGKILL 都没送走」那一档只有
#: 真把两段宽限期走满才复现得出来,照原值跑一次要 9 秒。句柄那条路同样两步
#: (`_close_by_handle`),它那边等的是 `proc.wait`。
_CLOSE_GRACE = (6.0, 3.0)


def _close_login_window(pid: int, ident: tuple[str, str]) -> bool:
    """先好好说一次,不行就 kill。**两步都有界,而且都不往外抛。**

    返回**确认它退出了没有**。「关的动作做完了」不等于「它已经退出」:两段宽限期
    都走满而它还活着、或者发信号时收到一个 `OSError`(可能是 EPERM —— 它活着、
    只是不归我们这个用户管),这两档都是**状态未知**。报成功的代价是调用方把诊断
    记录删掉、面板上写一句「已自动关掉」,而那个窗口还占着 profile —— 用户照着
    那句话去抓,下一轮照旧起不来,而面板上一个线索都没有(第 3 轮审查 N2)。

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
    for sig, grace in zip((signal.SIGTERM, signal.SIGKILL), _CLOSE_GRACE):
        now = _proc_identity(pid)
        if not any(now):
            # **问不出来不等于它走了。** `_proc_identity` 在 `ps` 超时或读失败时
            # 返回 `('', '')`,而那一刻窗口多半还活着。把它算进下面那条「身份核不
            # 上」里的话:收个尸、报确认退出,调用方跟着删掉诊断记录、面板上写一句
            # 「已自动关掉」—— 而那个窗口还占着抓取要用的 profile,用户照着那句话
            # 去抓,下一轮照旧起不来,一个线索都没有(第 4 轮审查 N1)。
            # **这一档也不许发信号**:身份不明的那个号可能已经被系统分给别人了,
            # 而这里下一句就是 SIGTERM/SIGKILL。状态未知时唯一诚实的动作是什么都
            # 不做、如实报「没确认」,让上层保留记录并改口。
            return False
        if now != ident:
            # 身份核不上 = 这个号上已经不是我们那个窗口了。收个尸、收工,**而且这
            # 算确认退出**:要关的那个进程确实不在了(它自己走掉了,或者上一刀已经
            # 生效)。这和「问不出来」以及下面那两档「状态未知」都不是一回事。
            _reap(pid)
            return True
        try:
            os.kill(pid, sig)
        except OSError:
            # 它自己先走了,或者不归我们这个用户管 —— **这两档在这里分不开**
            # (ESRCH vs EPERM),所以一律报「没确认」:多留一份诊断顶多让人多看
            # 一眼,报成功是把一个还占着 profile 的窗口说成已经关了。
            _reap(pid)
            return not _alive(pid)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            # **先收尸再问死活**:它是我们 `Popen` 起的子进程,SIGTERM 之后会在
            # 进程表里留一格僵尸,而 `os.kill(pid, 0)` 对僵尸照样成功。少了这一下,
            # 这个循环会把两段宽限期走满(实测 9 秒)、还对一个死了的进程补一刀。
            _reap(pid)
            if not _alive(pid):
                return True
            time.sleep(0.1)
    # 两段宽限期都走满了,它还在。**连 SIGKILL 都没送走**(内核态卡死、或者信号
    # 压根没到),这时唯一诚实的答复是「没关掉」。
    return False


def _relearn_identity(profile: str, proc) -> bool:
    """手里那个句柄还活着时,**重新问一次它的身份并写回记录**。认出来了返回 True。

    `_proc_identity` 问的是 `ps`,而那一问**失败一次就永久失配**:记录里存着
    `('', '')`,于是 `login_window_pid` 恒为 0 —— 守望、抓取前释放、面板那一行全都
    认不出这个窗口,而它开着(审查 R5)。启动脚本先初始化再 `exec` 真 Chrome 的那
    一档同理:身份是在 exec **之前**记下的。

    **重新认人只在手里有活着的句柄时做,这就是它的安全边界**:`proc.poll()` 还是
    None 意味着这个子进程还没被回收,系统不可能把这个号分给别人 —— 也就是说"这个
    pid 是我们的"由句柄作证,不是由 `ps` 作证。没有句柄(服务重启过)时一个字都不
    改:那时唯一能排除 pid 复用的凭据就是记录里那份身份。

    `at` 原样留住(见 `_write_login_note`):重新认人不是重新开始计时。
    """
    if proc is None or proc.poll() is not None:
        return False
    pid, at, ident = _read_login_note(profile)
    if pid != proc.pid:
        return False                     # 记录说的不是手里这一个,不碰
    fresh = _proc_identity(pid)
    if fresh == ('', ''):
        return False                     # 这会儿还是问不出来,下一跳再试
    if fresh != ident:
        _write_login_note(profile, pid, at, fresh)
    return True


def _close_by_handle(proc) -> bool:
    """用手里那个 `Popen` 句柄关掉它。**句柄是精确身份,摆不到别的进程上。**

    返回**确认它退出了没有**,口径同 `_close_login_window`:两段宽限期都超时、
    或者 `terminate()` 抛了 `OSError`,都是状态未知,一律报「没确认」。

    认不出身份的那一档只剩这一条路:`ps` 坏了、或者启动脚本 exec 换掉了可执行
    文件,`login_window_pid` 恒为 0,而窗口开着、占着整条采集通道。这里不用 pid
    发信号 —— `proc.terminate()` 作用在我们自己起的那个子进程上,`poll()` 还是
    None 就意味着它还没被回收、这个号没被别人拿走,所以**这条路天生没有误杀**
    (防误杀那道闸一点没松:pid 那条路照旧要身份对得上)。

    两步同 `_close_login_window`:先 SIGTERM 让 Chrome 把 profile 存盘(登录态就
    在里面),卡住不动才 SIGKILL。一个异常都不往外抛 —— 抛出去就是记录不被清掉。
    """
    for stop, grace in zip((proc.terminate, proc.kill), _CLOSE_GRACE):
        if proc.poll() is not None:
            return True                  # 已经退了,句柄说的就是确凭
        try:
            stop()
            proc.wait(timeout=grace)     # wait 顺带收尸
            return True
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return proc.poll() is not None
    return False


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
                # ② 认不出:启动中、记录暂时读不出来,或者**身份那一问失败过**
                #    (`ps` 抽了一下、或者启动脚本 exec 换掉了可执行文件)。手里的
                #    句柄还活着就先**重新认一次人**,再接着守(上限是这个窗口自己的
                #    TTL —— 再久也没有可守的了)。
                if proc is not None and time.time() - started < ttl:
                    _relearn_identity(profile, proc)
                    time.sleep(2.0 if time.time() - started < LOGIN_WINDOW_STARTUP
                               else 15.0)
                    continue
                # 到点了还是认不出,而句柄说它**还活着**:这就是审查 R5 那一档 ——
                # 认不出不等于关了,所以用句柄自己关掉它(`_close_by_handle`:精确
                # 身份,没有误杀的余地),而不是把记录一删了之、留一个开着的窗口
                # 挡住每一轮同步。
                if proc is not None and proc.poll() is None:
                    # **确认退出了才删记录**(口径同 `release_login_window`):关不掉
                    # 的那一档记录留着,面板和下一次释放才看得见它还占着。
                    if _close_by_handle(proc):
                        forget_login_window(profile)
                    return
                release_login_window(profile, ttl=ttl)   # 清陈旧记录
                return
            # ③ 到点了就关(关不关、关谁,全在 `release_login_window` 里判)
            left = login_window_left(profile, ttl=ttl)
            if left <= 0:
                release_login_window(profile, ttl=ttl)
                return
            time.sleep(min(15.0, left))

    threading.Thread(target=_watch, name='x-login-window', daemon=True).start()
