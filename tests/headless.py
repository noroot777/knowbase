# -*- coding: utf-8 -*-
"""找 chrome-headless-shell 的唯一口径(三个浏览器回归共用)。

原来三处各写死一份
`~/Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell`
—— 锁死 macOS + arm64 + playwright 构建号 1243。别的机器上 `skipUnless` 静默跳过,
贡献者改了前端得不到任何反馈,也看不出原因;本机的 playwright 缓存一升版同样整组
不再跑。

口径按这个顺序:
1. `HEADLESS_SHELL` 环境变量(自己装的、系统包管理器装的、CI 里下的都走它);
2. playwright 缓存目录下 glob 探测 —— **构建号和平台目录名都不写死**,
   按构建号取最新的那一份;缓存根目录本身也认 `PLAYWRIGHT_BROWSERS_PATH`,
   并把三个平台的默认位置都试一遍;
3. 都找不到才 skip,而且 skip 消息要写清怎么装(静默跳过是上面那个问题的根)。

口径只写一份:三个测试都 `from tests.headless import ...`,别再各自探测。

跑探针走 `run_probe()` —— **不要再直接 `subprocess.run([node, probe])`**,理由见
那个函数自己的说明(一句话:它的 timeout 是 SIGKILL,探针起的浏览器会被漏在外面)。
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

#: 装一份无头壳的办法,skip 消息里原样带给用户。
INSTALL_HINT = ("装一份:`npx playwright install chromium-headless-shell`,"
                "或自己准备一个 chrome-headless-shell 并用 "
                "HEADLESS_SHELL=<可执行文件路径> 指定")

_GLOB = "chromium_headless_shell-*/*/chrome-headless-shell*"

#: 把「原调用方是谁」告诉探针的环境变量;口径在 `tests/probe_shell.mjs` 的 CALLER。
#: 唯一的写入方是 `run_probe`,别在调用点自己设。
PARENT_ENV = "KB_PROBE_PARENT"
# 临时 profile 建在这个**父进程拥有**的根里面(见 run_probe 的 docstring)。
# 跟沙箱那份库同一条口径(`tests/fixture_root.py`):建在起它的这一侧、这一侧删。
PROFILE_ROOT_ENV = "KB_PROBE_PROFILE_ROOT"


def _cache_roots() -> list[Path]:
    """可能放着 playwright 浏览器的目录。

    `PLAYWRIGHT_BROWSERS_PATH` 优先(CI 常把它指到可缓存的路径);其余是
    playwright 在三个平台上的默认位置 —— 不按 sys.platform 只挑一个,是因为
    容器里跑 linux 构建、宿主是 mac 这类混合情况下多试一个目录不花什么钱。
    """
    roots: list[Path] = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env and env != "0":
        roots.append(Path(env).expanduser())
    home = Path.home()
    roots += [home / "Library/Caches/ms-playwright",          # macOS
              home / ".cache/ms-playwright",                   # Linux
              Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "ms-playwright"]
    seen, out = set(), []
    for r in roots:
        if str(r) not in seen:
            seen.add(str(r))
            out.append(r)
    return out


def _build_number(path: Path) -> int:
    """从 `chromium_headless_shell-1243` 这层目录名里取构建号,取不到算 0。"""
    for part in path.parts:
        m = re.fullmatch(r"chromium_headless_shell-(\d+)", part)
        if m:
            return int(m.group(1))
    return 0


def headless_shell() -> Path | None:
    """chrome-headless-shell 的路径;找不到返回 None。"""
    env = os.environ.get("HEADLESS_SHELL", "").strip()
    if env:
        candidate = Path(env).expanduser()
        return candidate if candidate.is_file() else None
    # 也认 PATH 上的(发行版包、自己编的)
    on_path = shutil.which("chrome-headless-shell")
    if on_path:
        return Path(on_path)
    found: list[Path] = []
    for root in _cache_roots():
        if root.is_dir():
            found += [p for p in root.glob(_GLOB) if p.is_file() and os.access(p, os.X_OK)]
    if not found:
        return None
    return sorted(found, key=lambda p: (_build_number(p), str(p)))[-1]


SHELL = headless_shell()
NODE = shutil.which("node")


#: 显式关掉浏览器回归那一组(fast 层设它;手动跑 README 那条 discover 想省时间
#: 也可以自己设)。
#:
#: **为什么要一个专门的开关**:不给 `HEADLESS_SHELL` 并**不**等于不跑 —— 上面那条
#: 口径会自己去 playwright 缓存里找一份(本机实测就找到了),所以「我没给路径」
#: 关不掉它。认 `1/true/yes/on`(不分大小写、容前后空白),**其余一律当没关** ——
#: 方向是不对称的:误判成「人关了」的后果是一轮界面都没验,而收尾那行照旧写 OK。
SKIP_ENV = "KB_SKIP_BROWSER_TESTS"
_ON = frozenset({"1", "true", "yes", "on"})


def browser_tests_off() -> bool:
    """这一轮是不是被显式关掉了浏览器回归(人手动设,或 fast 层替你设)。"""
    return os.environ.get(SKIP_ENV, "").strip().lower() in _ON


def browser_gate(missing=()):
    """真实浏览器测试类的统一闸门:**会起浏览器的类一律挂它**(或下面两个现成
    包装),不许在测试文件里自己写 `skipUnless` 按工具可用性做门 —— 自己写的那份
    不认上面的开关(fast 层靠它拦,手动关也靠它),照样起浏览器;也不带
    `kb_browser` 标记,probes 层统计不到它,「浏览器零覆盖」就看不出来。结构守卫
    两处、判的不是同一件事:按 skipUnless/skipIf 条件源码判的在
    `tests/test_tiers.py`,按「导入了哪些名字 / 隔着别的模块取闸件」判的在
    `tests/test_browser_switch.py`。完整语境见 docs/decisions/test-tiers.md 与
    probe-shell.md。

    `missing` 由调用点按**自己**的工具要求算:.mjs 探针要 node+壳
    (`requires_headless`),Python 直驱 CDP 的 DOM 组只要壳(`requires_shell`),
    SingletonLock 那组要原生 Chrome(无头壳压根不建那把锁)。这里不强加统一依赖。

    做三件事,顺序有含义:先打 `kb_browser` 标记(分层统计的唯一口径,**缺工具
    被 skip 的也得带着**,不然 probes 层的覆盖账少记它);再按开关整类拦下 ——
    **「人关的」和「这台机器缺东西」必须说成两句话**,一句 `OK (skipped=30)` 在
    两种情形下长得一模一样,而含义差很远;最后才轮到缺工具,按调用点给的说法
    skip(消息写清怎么补 —— 静默跳过是这套口径当年要修的事故形态)。
    """
    missing = [m for m in missing if m]

    def mark(cls):
        cls.kb_browser = True
        if browser_tests_off():
            return unittest.skip(
                f"{SKIP_ENV} 已显式关掉浏览器回归:这一轮没有验过界面"
                "(要验:./kb.sh test probes 或 ./kb.sh test full)")(cls)
        if missing:
            return unittest.skip("缺 " + ";".join(missing))(cls)
        return cls

    return mark


def requires_headless(cls):
    """node 驱动的浏览器回归(.mjs 探针)用这个:node 和无头壳缺哪个都说清楚。"""
    missing = []
    if NODE is None:
        missing.append("node(驱动 CDP 的那一侧)")
    if SHELL is None:
        missing.append(f"chrome-headless-shell —— {INSTALL_HINT}")
    return browser_gate(missing)(cls)


def requires_shell(cls):
    """Python 直驱 CDP 的浏览器回归用这个:只要无头壳,不额外强加 node。

    原来五处各写一份 `skipUnless(SHELL is not None, ...)` —— 那个写法让上面那个
    开关**关不住它们**:开关只认得过这里的那几组,而它们起的也是真浏览器。
    """
    missing = [] if SHELL is not None else [f"chrome-headless-shell —— {INSTALL_HINT}"]
    return browser_gate(missing)(cls)


def run_probe(argv, *, timeout: int = 300, env=None, cwd=None,
              ) -> subprocess.CompletedProcess:
    """跑一个 .mjs 探针,**保证它起的浏览器一定被收掉**。

    探针自己在 `tests/probe_shell.mjs` 里挂了 `process.on('exit')` 回收,那一层
    覆盖「探针抛异常」这条最常见的路。**但它挡不住 SIGKILL,而这里的 timeout
    发的正是 SIGKILL** —— `subprocess.run(timeout=)` 超时后调的是 `kill()`,
    node 连 'exit' 事件都来不及触发,它起的 chrome 就留在外面了(ppid=1,永久)。

    所以这一层按**进程组**收:`start_new_session=True` 让 node 成为新会话的首进程
    (pgid = node 的 pid),chrome 是它的后代、自动在同一个组里,`killpg` 一起带走。
    新开会话同时保证这个组里**只有** node 和它的后代,不会误伤跑测试的那个 shell。

    收尾在 `finally` 里无条件做一次:正常跑完那一轮探针已经自己 kill 过浏览器了,
    对已经退出的组成员再发一次信号是空操作(ProcessLookupError 吞掉);而探针**没有**
    收干净的那些情况,这一次就是最后一道。

    **独立会话反过来又开出第三条泄漏路,这里管不了,由探针自己管。** node 不在本进程
    的进程组里了,于是取消**调用方**(CI 取消作业、`kill` 这个 pytest、
    `kill -- -<pgid>`)再也波及不到它 —— 而 Python 对**没有 handler** 的 SIGTERM 是
    直接结束,下面这个 `finally` 一行都不跑。实测:调用方以 -15 退出,node 被过继给
    init 之后照旧活着、chrome 挂在它下面、DevTools 端口继续监听。
    **没有在这里装 SIGTERM handler 去接它**:那会把跑测试那个进程自己的信号行为
    静默换掉(pytest/unittest 各有一套),而且照样盖不住调用方被 SIGKILL 那一档。
    判据只能待在子进程里 —— `tests/probe_shell.mjs` 的 `watchParent()` 盯着
    `process.ppid`,调用方一死它自己退出,'exit' 钩子接着把浏览器收掉。

    **临时 profile 的那个根也归这一侧**(`KB_PROBE_PROFILE_ROOT`)。探针那边只在
    node 的 `'exit'` 钩子里删它自己那个目录,而上面说了:超时发的是 SIGKILL,钩子
    一行都不跑 —— 浏览器确实被组杀收掉了,**目录留在盘上**(第 6 轮审查实测:真跑
    一次共享 `launchShell` + `run_probe(timeout=6)`,壳死了、profile 还在)。所以这里
    建一个根传下去、在自己的 `finally` 里整根删掉:SIGKILL 杀不动的是子进程,删不掉
    的不是我们。**不要改成在这一层也挂钩子** —— 同一条路上"谁拥有、谁回收"只许有
    一个答案,两边都删就又回到了"目录的所有权在一个收不到清理机会的进程手上"。

    **这里要做的那一半是:把「原调用方是谁」明确告诉它**(`KB_PROBE_PARENT`)。
    让它自己现读 `process.ppid` 不够 —— 探针在起浏览器之前本来就有一段异步(等沙箱
    后端监听、摆局),调用方死在那一段里时它读到的已经是 1,于是「本来就没有调用方,
    不用盯」,检查**永久不装**,随后照旧起一个浏览器留在后台(实测:取消之后 node
    ppid=1 仍创建 chrome、DevTools 端口持续监听,3 秒后仍都在)。pid 在这里是现成的,
    传过去它就能回答「我还是不是那个人的子进程」。**无条件覆盖**调用方给的 env:
    这个值只有一个写入方,`probe_env()` 顺手把继承来的旧值摘掉。

    返回值和 `subprocess.run` 的形状一样,调用点照旧读 `.returncode` / `.stdout`。
    """
    child_env = dict(os.environ if env is None else env)
    child_env[PARENT_ENV] = str(os.getpid())
    # **`mkdtemp` + 自己 `rmtree`,不用 `TemporaryDirectory`**:后者的 GC finalizer 会
    # 把这份目录也兜掉,于是"这一层到底有没有收"就验不出来了(定点注入去掉下面那句
    # 收尾,回归红 0 条 —— 实测)。所有权要能被回归看见,只能由这一处独占。
    profiles = tempfile.mkdtemp(prefix="kb-probe-profiles-")
    child_env[PROFILE_ROOT_ENV] = profiles
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=child_env, cwd=cwd, start_new_session=True)
        # pgid 现在就记下来:进程退出之后 os.getpgid() 会抛 ProcessLookupError,
        # 而 start_new_session 保证了它恒等于 proc.pid,不必去问系统。
        pgid = proc.pid
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _killpg(pgid)
            # 组杀完再收一次输出:探针可能已经打印了一半结果,那是排查超时的线索。
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:      # pragma: no cover - 组都杀了还收不到
                proc.kill()
                out, err = proc.communicate()
        finally:
            _killpg(pgid)
    finally:
        # 组杀完才轮到这一句(内层 finally 先跑):先把浏览器收掉,再删它的 profile,
        # 反过来删的是一个**正在被写**的目录。`Popen` 自己抛出来那一下也走这里。
        shutil.rmtree(profiles, ignore_errors=True)
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def reap(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """收掉一个由测试自己起的子进程(谁起的谁收)。

    **起完立刻 `cls.addClassCleanup(reap, proc)`**,不要只写在 `tearDownClass` 里:
    `setUpClass` 抛异常之后 unittest **不调** `tearDownClass`,只跑 `doClassCleanups`
    —— 那个服务连同它监听的端口会留在后台(第 1 轮审查注入 `TimeoutExpired` 实测:
    服务还活着、端口还在监听)。同一条口径的另一半是目录:`tests/fixture_root.py`。

    已经退出的直接返回;`terminate` 收不动再 `kill`。
    """
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:          # pragma: no cover - 收不掉才走这条
        proc.kill()
        proc.wait(timeout=timeout)


def _killpg(pgid: int) -> None:
    """把整个进程组收掉;组已经空了是正常末态,不是错误。"""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class SandboxBase(str):
    """沙箱的 base URL,**顺带带着那份临时根的路径**。

    是 `str` 的子类,所以既有调用点照旧把它当 URL 用(拼接、urlopen、传给探针都
    不用改)。多出来的 `.root` 给要**查盘**的用例:界面上说「已经写进知识库了」
    而 vault 里什么都没有,是这条链最难看出来的一种谎报 —— 验它只能去看文件
    (见 `tests/test_grab_probe.py`)。
    """

    def __new__(cls, base: str, root: str):
        self = super().__new__(cls, base)
        self.root = root
        return self


@contextlib.contextmanager
def fixture_server(*extra_args: str, script: str = "serve_sources.py",
                   ready: str = "/", timeout: float = 30):
    """起 `tests/serve_*.py` 里某个一次性全栈沙箱(默认来源页那个),`yield` base URL。

    **临时库由这一侧拥有。** 服务进程是被 SIGTERM 收掉的,而 Python 对没有 handler
    的 SIGTERM 是直接结束 —— 它那边 `with tempfile.TemporaryDirectory()` 的清理一行
    都不跑,一份完整的沙箱库就留在盘上(第 5 轮实测:定向回归跑完仍残留
    `kb-source-qa-*`,全量回归留下两个;阅读页那条链留的是 `kb-reading-qa-*`)。
    所以这里建根、用 `KB_FIXTURE_ROOT` 告诉它、在 `finally` 里删;子进程那一侧认这个
    变量的口径只有一份,在 `tests/fixture_root.py`。**不要再在调用点各自 `Popen` +
    `terminate`** —— 那个写法里目录的所有权在一个收不到清理机会的进程手上,而且端口
    也得各挑一次。结构守卫在 `tests/test_probe_contract.py`。

    `script` 给的是 `tests/` 下那个沙箱脚本名(默认来源页那个);`ready` 是判「起来了」
    用的路径;`timeout` 要按它摆盘的工夫给(阅读页那个先跑完导出和建索引才监听)。

    端口也在这里挑(绑 0 让内核给一个空闲的,随即关掉再交给子进程):调用点各写一份
    的话两个测试同跑会撞端口,而撞上之后的症状是探针等不到页面,看不出真因。
    """
    assert NODE is not None, "没有 node 就不该走到这里,用 @requires_headless 拦住"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = tempfile.TemporaryDirectory(prefix="kb-fixture-")
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "KB_FIXTURE_ROOT": root.name}
    server = subprocess.Popen(
        [sys.executable, f"tests/{script}", "--port", str(port), *extra_args],
        cwd=Path(__file__).resolve().parents[1], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise AssertionError("隔离沙箱服务启动失败(进程已退出)")
            try:
                urllib.request.urlopen(base + ready, timeout=1).read()
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        else:
            raise AssertionError("隔离沙箱服务没有响应")
        yield SandboxBase(base, root.name)
    finally:
        reap(server)                 # 收子进程的口径只有一份(见 reap)
        root.cleanup()


def probe_env() -> dict[str, str]:
    """跑 .mjs 探针时要传给子进程的环境(探针按 HEADLESS_SHELL 找二进制)。"""
    assert SHELL is not None, "没有无头壳就不该走到这里,用 @requires_headless 拦住"
    out = {**os.environ, "HEADLESS_SHELL": str(SHELL)}
    # 继承来的旧值一律摘掉:那个 pid 说的是别人的调用方,留着会让探针一启动就
    # 判成「我已经没有调用方了」当场退出。唯一的写入方是 run_probe。
    out.pop(PARENT_ENV, None)
    return out


if __name__ == "__main__":   # 排查用:python -m tests.headless
    print(f"node: {NODE or '(没找到)'}")
    print(f"chrome-headless-shell: {SHELL or '(没找到) ' + INSTALL_HINT}")
    sys.exit(0 if (NODE and SHELL) else 1)
