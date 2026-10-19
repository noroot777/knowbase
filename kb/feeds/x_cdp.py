# -*- coding: utf-8 -*-
"""最小 CDP 客户端:协议层 + 「自己起一个独立 profile 的 Chrome」这条传输。

**拿到一个浏览器有两条路,这里是其中一条。** 另一条是直连用户日常那个浏览器
(`kb/feeds/cdp_attach.py`,CDP over WebSocket)。两条并列、各有边界,选哪条是
一个设置(`x_channels` 的 `browser` / `attach` 两档):

| | 独立 profile(这里) | 直连日常浏览器(`cdp_attach`) |
|---|---|---|
| 登录态 | 要在这个 profile 里单独登录一次 | 用户浏览器里现成的那一份 |
| 可用性 | 随时能起,不依赖用户在不在 | 只在那个浏览器开着、且开了调试开关时 |
| 传输 | pipe(fork 时继承的 fd 3/4) | WebSocket(本机 loopback) |
| 暴露面 | 除了起它的进程谁也连不上 | 调试开关开着时本机进程都连得上 |

**这条路用管道而不是调试端口**,是因为它压根不需要端口:浏览器是我们自己
fork 出来的,管道就在手里,再开一个端口只会白白多一个入口。想用调试端口的
那种场景(复用用户已经登录好的浏览器)走 `cdp_attach` —— 那条路的代价写在
它自己的注释里,由用户自己决定要不要付。

**为什么不引入 playwright**:CDP over pipe 的协议就是「JSON + `\\0` 分隔」,
一个 recv 循环就够;而 playwright 要再下载一份自己的 Chromium(几百 MB),
这个项目的依赖已经驮着 torch 了。用户本机的 Google Chrome 就是这条通道要的
那个浏览器 —— 登录态在它的 profile 里,换个内核反而不对。

**fd 重映射为什么绕 `/bin/sh`**:Chrome 把这两个 fd 的编号写死成「读 3 / 写 4」,
而 `subprocess` 没有「把这个管道放到 fd 3」这种参数。`preexec_fn` 里 dup2 到 3
实测不成立——子进程 exec 时 fd 3 又是空的(2026-10-08 实测:Chrome 报
`Remote debugging pipe file descriptors are not open`,`ls -l /dev/fd` 显示 3 号
没人占)。所以改成父进程先把两端复制到一个空闲高位 fd、`pass_fds` 带过去,
再由 shell 的 `exec 3<&N 4>&M` 落到位。

高位编号用 `fcntl.F_DUPFD` **现取**而不是写死两个常量:kb-web 这个进程里
sqlite、httpx、uvicorn 的 socket 都在占 fd,写死一个编号就可能 dup2 到别人
头上——dup2 会**静默关掉**目标 fd 原来的那个对象,表现是另一条路上的数据库
连接或请求莫名失败,而这里一个字都看不出来。F_DUPFD 的语义正是「给我一个
不小于 N 的空闲 fd」,由内核保证不撞。
"""

from __future__ import annotations

import fcntl
import json
import os
import select
import shutil
import subprocess
import time

# 写超时的常量放在 `ws_client`(它谁都不依赖,判据也写在它的模块注释里)。
# 两条传输共用一个值是有意的:「几 KB 写不进去就是对端卡住了」跟传输无关。
from kb.feeds.ws_client import WRITE_TIMEOUT

# F_DUPFD 的下界:躲开标准流和 Chrome 自己要用的 3/4。
_FD_FLOOR = 10
# Chrome 在 macOS 上的默认位置。装在别处时由设置里的 chrome_path 覆盖。
CHROME_CANDIDATES = (
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
)
# 一次 CDP 调用的上限。整轮抓取另有自己的预算(见 x_browser),这里只是
# 「别永久阻塞」的兜底 —— embed_pool 那次事故的教训:攥着锁阻塞的 read 会把
# 一整条链冻住,所以读永远带超时,把「挂住」变成「报错重试」。
CALL_TIMEOUT = 45.0


class CdpError(RuntimeError):
    """浏览器侧的失败。message 是成品文案,直接给用户看。"""


def find_chrome(explicit: str = '') -> str:
    """定位 Chrome 可执行文件。找不到时的文案要能指到下一步。"""
    if explicit:
        if not os.access(explicit, os.X_OK):
            raise CdpError(f'指定的 Chrome 路径不可执行:{explicit}')
        return explicit
    for path in CHROME_CANDIDATES:
        if os.access(path, os.X_OK):
            return path
    found = shutil.which('google-chrome') or shutil.which('chromium')
    if found:
        return found
    raise CdpError('没找到 Chrome。请安装 Google Chrome,'
                   '或在浏览器通道设置里填写它的完整路径')


class Session:
    """CDP 协议层 + 页面语义。传输由子类给:`_post` 发一条、`_take` 收一条。

    抽象点落在「一条 JSON 消息」上而不是字节上:两条传输的分帧完全不同
    (pipe 是 `\\0` 分隔,WebSocket 是它自己那套帧),而 CDP 这一层只认消息。
    于是 `_harvest_one` 那些页面脚本一行都不用管自己跑在哪条传输上。
    """

    #: 开页面时要不要 `background: true`。直连用户日常浏览器那条必须要 ——
    #: 不加的话新标签页会抢到前台,把用户正在看的东西顶掉。
    _BACKGROUND = False
    #: 要不要再加 `hidden: true`。**在用户自己的浏览器里干活的那条要**:隐藏
    #: 标签页压根不进标签栏(他看不见、不会去关、也不会以为自己中了什么),而且
    #: 它的生命周期跟着这条连接 —— 断开即消失,连漏关一张空白页这种事都不会有。
    #: 协议规定它只能和 `background: true` 同时给,所以两个标志是连动的。
    _HIDDEN = False

    def __init__(self) -> None:
        self._seq, self._sid, self._target = 0, '', ''

    # ── 传输(子类实现)──────────────────────────────────────────────────────

    def _post(self, msg: dict, deadline: float) -> None:
        """把一条消息发出去,**不许超过 deadline**(见 `call` 那条注释)。"""
        raise NotImplementedError

    def _take(self, deadline: float) -> dict:
        raise NotImplementedError

    # ── 协议 ────────────────────────────────────────────────────────────────

    def _send(self, method: str, params: dict | None = None, sid: str = '',
              deadline: float = 0.0) -> int:
        self._seq += 1
        msg = {'id': self._seq, 'method': method, 'params': params or {}}
        if sid:
            msg['sessionId'] = sid
        self._post(msg, deadline or time.monotonic() + CALL_TIMEOUT)
        return self._seq

    def call(self, method: str, params: dict | None = None, sid: str = '',
             timeout: float = CALL_TIMEOUT) -> dict:
        """发一条命令,等它自己那条回复。期间的事件一律丢掉(我们不订阅事件)。

        **deadline 要在发出去之前就算出来,而且发送也受它约束。** 原来是先
        `_send` 再算 deadline,于是 `timeout` 只管收、不管发 —— 对端卡住不读
        数据的时候,一条命令会停在写上无限等:采集预算和用户按的那颗「停止」
        谁都轮不到执行(2026-10-09 审查实测 `timeout=0.05` 的一次 call 0.61 秒
        后仍阻塞着,只有强行关掉对端才解开)。两条传输的 `_post` 因此都收
        deadline,写不动就报错,而不是替调用方把时间花在那儿。
        """
        deadline = time.monotonic() + timeout
        want = self._send(method, params, sid, deadline)
        while True:
            msg = self._take(deadline)
            if msg.get('id') != want:
                continue                              # 别人的回复,或是事件
            if 'error' in msg:
                raise CdpError(f"{method} 失败:{msg['error'].get('message', '')}")
            return msg.get('result', {})

    # ── 页面 ────────────────────────────────────────────────────────────────

    def _adopt(self, target: str) -> None:
        """登记一个刚被创建出来的标签页。要记所有权的子类重写它。

        **必须在 `Target.createTarget` 刚回来那一刻就调,不能等整个 `open_page`
        返回。** 后面 `attachToTarget` / `Page.enable` 任何一步失败时,页面已经
        在浏览器里了 —— 而退出清理只认登记过的那几个,晚一步就是把一张空白页
        永久留在**用户自己的**浏览器里,反复重试会一直堆(2026-10-09 审查拿真
        无头浏览器实测:注入一次 attach 失败,退出之后那张 `review-leftover`
        还在)。
        """

    def open_page(self, url: str = 'about:blank') -> str:
        params: dict = {'url': url}
        if self._BACKGROUND:
            params['background'] = True
            if self._HIDDEN:
                params['hidden'] = True
        self._target = self._create(params)
        self._adopt(self._target)          # 先认下所有权,再做后面的初始化
        self._sid = self.call('Target.attachToTarget',
                              {'targetId': self._target, 'flatten': True})['sessionId']
        self.call('Page.enable', {}, self._sid)
        return self._sid

    def _create(self, params: dict) -> str:
        """建那张标签页。`hidden` 不被认时退一档,**但只退这一档**。

        `hidden` 是比较新的参数(老 Chrome 会把整条 `Target.createTarget` 判成
        参数错误)。认不出来的时候退回「后台标签页」是可以的 —— 它仍然不抢
        前台,只是会在标签栏里多一行。**绝不退到前台**:那是把用户正在看的
        东西顶掉,比标签栏多一行严重得多,宁可整件事失败。
        """
        try:
            return self.call('Target.createTarget', params)['targetId']
        except CdpError:
            if 'hidden' not in params:
                raise
            return self.call('Target.createTarget',
                             {k: v for k, v in params.items()
                              if k != 'hidden'})['targetId']

    def goto(self, url: str) -> dict:
        """导航,并把 `Page.navigate` 的回包**原样交回去**。

        **不在这里抛。** 导航失败的回包长这样:`{'frameId': …,
        'errorText': 'net::ERR_ABORTED'}` —— 一个 HTTP 204 就够(实测),而
        **页面停在原处**。时间线那两处调用(`x_browser` 的 profile 导航和收尾
        回 `/home`)不看它:它们随后各自按 `_STATE_JS` 判页面状态,在这里抛会把
        「登录失效 / 账号打不开 / 限流」那几句各自的话换成一句笼统的导航失败。
        所以交回回包、由调用方自己决定 —— 长文那条路把它当成**早一道**提示,
        真正承重的是落地之后那次身份核对(见 `x_article.browser_fetch`):老会话
        和别的实现完全可能什么都不回,而「页面没跳过去」必须拦得住。
        """
        return self.call('Page.navigate', {'url': url}, self._sid)

    def eval(self, expression: str, timeout: float = CALL_TIMEOUT):
        """求值并按值取回。JS 抛错翻成 CdpError,不把失败留成一个 None。"""
        result = self.call('Runtime.evaluate', {
            'expression': expression, 'returnByValue': True,
            'awaitPromise': True}, self._sid, timeout=timeout)
        if result.get('exceptionDetails'):
            text = result['exceptionDetails'].get('text', '页面脚本出错')
            raise CdpError(f'页面求值失败:{text}')
        return result.get('result', {}).get('value')


class Chrome(Session):
    """一个我们自己起的 Chrome + 一个页面会话。退出时一定收尾。"""

    def __init__(self, profile: str, *, headless: bool = True, chrome_path: str = '',
                 window: str = '1280,2000'):
        super().__init__()
        self.profile, self.headless = profile, headless
        self.chrome_path, self.window = chrome_path, window
        self.proc: subprocess.Popen | None = None
        self._buf = b''
        self._r = self._w = -1
        #: 收尾时那次「好好关一次」成了没有。`None` = 还没收尾。**只是一个
        #: 事实报告,不是异常**:收尾怎么失败都不许把后面几步跳过(见
        #: `__exit__`),可调用方有权知道 —— `Browser.close` 存在的理由就是让
        #: 浏览器把 profile 存盘,被 kill 掉的那一次写进去的东西可能一个都没
        #: 落盘,而那正是「导入登录态」要对外承诺的那件事
        #: (`x_login_import.install`)。
        self.closed_cleanly: bool | None = None

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def __enter__(self) -> 'Chrome':
        binary = find_chrome(self.chrome_path)
        os.makedirs(self.profile, exist_ok=True)
        # pr/pw 是我们这头;cr(chrome 读)/cw(chrome 写)要落到子进程的 fd 3/4。
        pr, cw = os.pipe()
        cr, pw = os.pipe()
        hi_r = hi_w = -1
        try:
            hi_r = fcntl.fcntl(cr, fcntl.F_DUPFD, _FD_FLOOR)
            hi_w = fcntl.fcntl(cw, fcntl.F_DUPFD, _FD_FLOOR)
            args = [binary, '--remote-debugging-pipe', f'--user-data-dir={self.profile}',
                    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
                    f'--window-size={self.window}']
            if self.headless:
                args.append('--headless=new')
            # 搬到 3/4 之后立刻关掉高位那两个:Chrome 自己还会 fork 渲染器和 GPU
            # 进程,让它们也继承着多余的写端,我们这头的 EOF 就不再由浏览器主
            # 进程的存活决定了。
            script = (f'exec 3<&{hi_r} 4>&{hi_w} {hi_r}<&- {hi_w}>&-; exec "$@"')
            self.proc = subprocess.Popen(
                ['/bin/sh', '-c', script, 'sh', *args], pass_fds=(hi_r, hi_w),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except BaseException:
            for fd in (pr, pw):
                os.close(fd)
            raise
        finally:
            for fd in (cr, cw, hi_r, hi_w):
                if fd >= 0:
                    os.close(fd)
        self._r, self._w = pr, pw
        self._nonblocking_write()
        try:
            self.call('Browser.getVersion', timeout=30)
        except (CdpError, EOFError, OSError) as e:
            self.__exit__(None, None, None)
            raise CdpError(_launch_hint(self.profile, e)) from None
        return self

    def __exit__(self, *_exc) -> None:
        """收尾一定做完:关浏览器这一步**怎么失败都不许把后面那几步跳过**。

        原来是「发 `Browser.close` → `wait`」裹在一个
        `except (OSError, subprocess.TimeoutExpired)` 里,而 `_post` 带上 deadline
        之后写不动报的是 `CdpError`(写出错的 `OSError` 也在那儿翻成了它)——
        于是异常从关闭路径冒出去,后面的 `kill`/`wait`、关那两个 fd、
        `self.proc = None` 一步都没执行。**留下的不是用户能看见并关掉的登录窗口,
        是一个还活着的无头浏览器**,而它攥着 profile 的 `SingletonLock`:下一次
        「检查连接」被那把锁挡住、报的是「登录窗口还开着?关掉它再试」,再起一次
        也被目录占用挡住 —— 这条通道从此用不了,而用户没有任何窗口可关
        (2026-10-09 第 2 轮审查拿真浏览器实测:`SIGSTOP` 停住它、灌满管道,
        `__exit__` 抛 `CdpError` 之后进程还活着、两个 fd 都还开着、锁还在,
        `SIGCONT` 恢复运行也不自愈)。`__enter__` 起不来时兜底走的也是这条路,
        所以同一个缺口在启动失败那一侧一模一样。
        """
        try:
            self._shutdown()
        finally:
            for fd in (self._r, self._w):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self._r = self._w = -1
            self._buf = b''
            self.proc = None

    def _shutdown(self) -> None:
        """先好好说一次,不行就 kill。**两步都有界,而且都不往外抛。**

        `Browser.close` 值得先试一次 —— 它会让浏览器把 profile 存盘,而登录态
        就在那里面;但一个卡住的浏览器连这条命令都收不下,那时唯一有意义的事是
        把它结束掉。`kill` 之后仍然等不到也不抛:我们这头的 fd 马上要关,它那端
        跟着收到 EOF,最坏留下一个正在退出的进程 —— 而抛出去的代价是连 fd 都
        不关、`proc` 也不复位(见 `__exit__` 那条注释)。
        """
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is not None:
            # 它自己先走了。**这不算「好好关了一次」** —— 崩溃或被别人杀掉的
            # 那一次 profile 里该存的东西可能一个都没存。
            self.closed_cleanly = False
            return
        try:
            self._send('Browser.close', deadline=time.monotonic() + 5)
            proc.wait(timeout=10)
            self.closed_cleanly = True
            return
        except (CdpError, EOFError, OSError, subprocess.TimeoutExpired):
            pass
        self.closed_cleanly = False
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # ── 传输 ────────────────────────────────────────────────────────────────

    def _nonblocking_write(self) -> None:
        """我们那个写端一律非阻塞。

        理由同 `ws_client.WebSocket.__init__`:**阻塞的 `write` 对一条大于剩余
        空间的载荷会一直等到全部写完**(POSIX 对管道就是这么规定的),select 在
        它前面挡不住 —— 于是 `_post` 那个「等可写再写一段」的循环只有在非阻塞
        之下才真的有上限。单拎成一个方法是为了让回归拿它摆局,不必真起一个
        Chrome。只动我们这一端:Chrome 继承的是管道的另一头,两个独立的 open
        file description,它那边的阻塞语义一个字没变。
        """
        os.set_blocking(self._w, False)

    def _post(self, msg: dict, deadline: float) -> None:
        """写也带 deadline(口径同 `ws_client._write`)。

        管道这条会以同样的方式阻塞:Chrome 卡住不读的时候,管道缓冲(64 KB)
        一满 `os.write` 就停在那儿,而那一下攥着的是整轮采集。健康的浏览器压根
        不会在这里停住 —— 一条命令只有几 KB。
        """
        view = memoryview(json.dumps(msg).encode('utf-8') + b'\0')
        limit = min(deadline, time.monotonic() + WRITE_TIMEOUT)
        while view:
            left = limit - time.monotonic()
            if left <= 0 or not select.select([], [self._w], [], left)[1]:
                raise CdpError('浏览器没有在预期时间内收下命令')
            try:
                sent = os.write(self._w, view)
            except BlockingIOError:
                continue                  # select 说可写、内核又改主意了
            except OSError as e:
                raise CdpError(f'写浏览器连接出错:{e}') from None
            view = view[sent:]

    def _take(self, deadline: float) -> dict:
        while b'\0' not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CdpError('浏览器没有在预期时间内响应')
            if not select.select([self._r], [], [], left)[0]:
                continue
            chunk = os.read(self._r, 1 << 16)
            if not chunk:
                raise EOFError('浏览器已退出')
            self._buf += chunk
        raw, self._buf = self._buf.split(b'\0', 1)
        return json.loads(raw)


def profile_busy(profile: str) -> bool:
    """这个 profile 目录此刻是不是**真的**被一个活着的浏览器占着。

    判据不是「`SingletonLock` 在不在」。那个链接是 Chrome 的 ProcessSingleton
    留下的,内容正是 `<主机名>-<pid>`;**它崩了、或者被我们 kill 掉,链接就留在
    原处**,而 Chrome 自己也是读这个 pid、发现死了就把锁抢过来(2026-10-09 实测:
    优雅退出之后链接没了;SIGKILL 之后链接还在、指着那个已经死掉的 pid,下一次
    启动照样成功并把链接改写成自己的 pid)。

    **只看文件在不在的后果是把用户堵死**:收尾哪一次没能优雅关闭(浏览器卡住,
    只剩 kill 这一条路 —— 见 `Chrome._shutdown`),「检查连接」和「打开登录窗口」
    就一起报「登录窗口还开着,关掉它再试」,而**根本没有窗口可关**,这条通道从此
    没有任何出路(同一轮审查实测)。采集那一侧压根不看这个文件,所以只有这两处
    被堵 —— 它们偏偏是用户唯一能自救的两个入口。

    规则只有一句:**链接指着一个活着的 pid 才叫占着**,别的形态(没有这个文件、
    它不是链接、链接内容看不懂)一律不是占用的证据,照常去启动 —— 真有另一个
    活着的实例时由 Chrome 自己仲裁,而那种情况下这个 pid 本来就是活的、上面
    那条规则已经答对了。不设「看不懂就当占着」的特例:那是在没有证据的时候
    堵用户唯一的两个自救入口,正是这条判据要改掉的毛病。
    """
    try:
        target = os.readlink(os.path.join(profile, 'SingletonLock'))
    except OSError:
        return False                       # 没有这个锁,或者它压根不是个链接
    pid = target.rpartition('-')[2]
    return pid.isdigit() and _alive(int(pid))


def _reap(pid: int) -> None:
    """收尸:它要是我们的子进程、而且已经死了,把它从进程表里摘掉。

    **不收尸的话 `_alive` 会把僵尸说成活着** —— `os.kill(pid, 0)` 对僵尸照样
    成功(进程表里那一格还在)。登录窗口正是我们用 `Popen` 起的子进程,于是:
    ①`_close_login_window` 发完 SIGTERM 之后白等满两段宽限期(实测 9 秒),最后
    还对一个已经死了的进程补一刀 SIGKILL;②更要命的是它被 SIGKILL 结束的那一档
    —— Chrome 那时来不及删 `SingletonLock`,链接留在原处指着这个僵尸,于是
    `profile_busy` **永远**答"被占着",而用户没有任何窗口可关(这正是
    `profile_busy` 那段注释里警告过的死局,原来只认为 kill 会造成它,没想到
    "没收尸"自己就够)。

    不是我们的子进程时 `waitpid` 抛 `ChildProcessError` —— 那一档本来也不需要
    收尸:进程重启之后那个窗口已经过继给了 init,死了就从进程表里消失。
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                        # 在,只是不归我们这个用户管
    return True


def _launch_hint(profile: str, err: Exception) -> str:
    """起不来的原因里,只有一种是用户当场能处置的:profile 正被别的窗口占着。"""
    if profile_busy(profile):
        return ('浏览器 profile 正被另一个窗口占用。请关掉之前打开的登录窗口再试'
                f'(目录:{profile})')
    return f'浏览器启动失败:{err}'


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


def _login_note(profile: str) -> str:
    return os.path.join(profile, _LOGIN_NOTE)


def remember_login_window(profile: str, pid: int) -> None:
    """记下「这个登录窗口是我们起的」。写不进去**不抛**。

    写不进去(满盘、权限)只意味着退回原来那个行为:窗口留着、要用户自己关,
    面板照旧报 profile 被占用。为它把「打开登录窗口」整个弄失败是反的 ——
    用户要的那件事(开窗口登录)已经成了。
    """
    try:
        with open(_login_note(profile), 'w', encoding='utf-8') as fh:
            json.dump({'pid': int(pid), 'at': time.time()}, fh)
    except OSError:
        pass


def forget_login_window(profile: str) -> None:
    try:
        os.unlink(_login_note(profile))
    except OSError:
        pass


def _read_login_note(profile: str) -> tuple[int, float]:
    """(pid, 起始时刻)。读不出、格式不对、pid 不是正整数一律返回 (0, 0.0)。

    **坏记录等于没有记录**,不是"当成占着"也不是"当成该关" —— 两个方向都会拿
    一份读不懂的文件去动别人的进程。
    """
    try:
        with open(_login_note(profile), encoding='utf-8') as fh:
            note = json.load(fh)
        pid, at = int(note['pid']), float(note['at'])
    except (OSError, ValueError, TypeError, KeyError):
        return 0, 0.0
    return (pid, at) if pid > 0 else (0, 0.0)


def login_window_pid(profile: str) -> int:
    """**我们起的**那个登录窗口此刻的 pid;没有就是 0。

    三个条件缺一不可:记录读得出、profile 真的被占着、占着的就是记录里那个 pid。
    最后一条是这整件事的安全边界 —— `SingletonLock` 指着别人时,那是用户自己开的
    窗口(或者另一个工具),一个字都不许碰。
    """
    pid, _ = _read_login_note(profile)
    if not pid:
        return 0
    try:
        target = os.readlink(os.path.join(profile, 'SingletonLock'))
    except OSError:
        return 0
    holder = target.rpartition('-')[2]
    if not (holder.isdigit() and int(holder) == pid and _alive(pid)):
        return 0
    return pid


def login_window_left(profile: str, *, ttl: float = LOGIN_WINDOW_TTL) -> float:
    """我们那个登录窗口还剩几秒自动关闭。0 = 没有我们的窗口,或者已经到点。

    **只读**:不杀进程、不删记录。面板那条路(`x_channels.readiness`)每次刷新都
    调它,而那个函数的契约是便宜且不改状态。
    """
    if not login_window_pid(profile):
        return 0.0
    _, at = _read_login_note(profile)
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
    pid = login_window_pid(profile)
    if pid:
        # **先收尸再往下判。** 窗口被别人结束掉(用户强退、系统 OOM)时,它在
        # 我们这儿留下一格僵尸,而 `SingletonLock` 可能来不及删 —— 那两件事凑一起
        # 就是「`profile_busy` 永远为真、却没有窗口可关」。收完尸再问一次:它要是
        # 真没了,下面那条走的就是"清陈旧记录"那一档。
        _reap(pid)
        pid = login_window_pid(profile)
    if not pid:
        # 窗口已经不在了(用户自己关的,或者上一次就被我们关掉):顺手把陈旧记录
        # 清掉。留着它的话,下一次同一个 pid 被系统分配给别的进程时,上面那道
        # 「pid 对得上」就会把一个无辜的进程认成我们的窗口。
        if _read_login_note(profile)[0]:
            forget_login_window(profile)
        return ''
    _, at = _read_login_note(profile)
    waited = time.time() - at
    if waited < ttl:
        return ''
    _close_login_window(pid)
    forget_login_window(profile)
    return (f'已自动关掉开了 {int(waited // 60)} 分钟的 X 登录窗口'
            f'(上限 {int(ttl // 60)} 分钟),它占着抓取要用的浏览器')


def _close_login_window(pid: int) -> None:
    """先好好说一次,不行就 kill。**两步都有界,而且都不往外抛。**

    理由同 `Chrome._shutdown`:`SIGTERM` 让 Chrome 把 profile 存盘(登录态就在
    那里面,而用户开这个窗口正是为了它);但一个卡住的 Chrome 连信号都不处理,
    那时唯一有意义的事是把它结束掉 —— 留着它等于这条通道永远用不了。

    **抛出去的代价是记录不被清掉**,而陈旧记录会让下一次把别人的 pid 认成我们的
    窗口,所以这里一个异常都不许漏出去。
    """
    import signal
    for sig, grace in ((signal.SIGTERM, 6.0), (signal.SIGKILL, 3.0)):
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


def watch_login_window(profile: str, *, ttl: float = LOGIN_WINDOW_TTL) -> None:
    """起一个守望线程:到点把登录窗口关掉。

    **daemon 线程**:它不该拦着进程退出 —— 进程退出时那个窗口归
    `release_login_window` 那一道管(下一次抓取或「检查连接」时)。

    轮询而不是 `waitpid`:窗口是 `start_new_session` 起的独立会话,而且我们关心的
    事件是"到点了",不是"它退出了"。它自己退出时循环下一跳就看到 pid 对不上,
    顺手把记录清掉收工。
    """
    import threading

    def _watch() -> None:
        while True:
            left = login_window_left(profile, ttl=ttl)
            if not login_window_pid(profile):
                release_login_window(profile, ttl=ttl)   # 清陈旧记录
                return
            if left <= 0:
                release_login_window(profile, ttl=ttl)
                return
            time.sleep(min(15.0, left))

    threading.Thread(target=_watch, name='x-login-window', daemon=True).start()
