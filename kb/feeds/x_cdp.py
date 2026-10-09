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

    def __init__(self) -> None:
        self._seq, self._sid, self._target = 0, '', ''

    # ── 传输(子类实现)──────────────────────────────────────────────────────

    def _post(self, msg: dict) -> None:
        raise NotImplementedError

    def _take(self, deadline: float) -> dict:
        raise NotImplementedError

    # ── 协议 ────────────────────────────────────────────────────────────────

    def _send(self, method: str, params: dict | None = None, sid: str = '') -> int:
        self._seq += 1
        msg = {'id': self._seq, 'method': method, 'params': params or {}}
        if sid:
            msg['sessionId'] = sid
        self._post(msg)
        return self._seq

    def call(self, method: str, params: dict | None = None, sid: str = '',
             timeout: float = CALL_TIMEOUT) -> dict:
        """发一条命令,等它自己那条回复。期间的事件一律丢掉(我们不订阅事件)。"""
        want = self._send(method, params, sid)
        deadline = time.monotonic() + timeout
        while True:
            msg = self._take(deadline)
            if msg.get('id') != want:
                continue                              # 别人的回复,或是事件
            if 'error' in msg:
                raise CdpError(f"{method} 失败:{msg['error'].get('message', '')}")
            return msg.get('result', {})

    # ── 页面 ────────────────────────────────────────────────────────────────

    def open_page(self, url: str = 'about:blank') -> str:
        params: dict = {'url': url}
        if self._BACKGROUND:
            params['background'] = True
        self._target = self.call('Target.createTarget', params)['targetId']
        self._sid = self.call('Target.attachToTarget',
                              {'targetId': self._target, 'flatten': True})['sessionId']
        self.call('Page.enable', {}, self._sid)
        return self._sid

    def goto(self, url: str) -> None:
        self.call('Page.navigate', {'url': url}, self._sid)

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
        try:
            self.call('Browser.getVersion', timeout=30)
        except (CdpError, EOFError, OSError) as e:
            self.__exit__(None, None, None)
            raise CdpError(_launch_hint(self.profile, e)) from None
        return self

    def __exit__(self, *_exc) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self._send('Browser.close')          # 让它自己把 profile 存盘
                self.proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait(timeout=5)
        for fd in (self._r, self._w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._r = self._w = -1
        self.proc = None

    # ── 传输 ────────────────────────────────────────────────────────────────

    def _post(self, msg: dict) -> None:
        os.write(self._w, json.dumps(msg).encode('utf-8') + b'\0')

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


def _launch_hint(profile: str, err: Exception) -> str:
    """起不来的原因里,只有一种是用户当场能处置的:profile 正被别的窗口占着。"""
    if os.path.lexists(os.path.join(profile, 'SingletonLock')):
        return ('浏览器 profile 正被另一个窗口占用。请关掉之前打开的登录窗口再试'
                f'(目录:{profile})')
    return f'浏览器启动失败:{err}'
