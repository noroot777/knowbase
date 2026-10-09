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

import os
import re
import shutil
import signal
import subprocess
import sys
import unittest
from pathlib import Path

#: 装一份无头壳的办法,skip 消息里原样带给用户。
INSTALL_HINT = ("装一份:`npx playwright install chromium-headless-shell`,"
                "或自己准备一个 chrome-headless-shell 并用 "
                "HEADLESS_SHELL=<可执行文件路径> 指定")

_GLOB = "chromium_headless_shell-*/*/chrome-headless-shell*"

#: 把「原调用方是谁」告诉探针的环境变量;口径在 `tests/probe_shell.mjs` 的 CALLER。
#: 唯一的写入方是 `run_probe`,别在调用点自己设。
PARENT_ENV = "KB_PROBE_PARENT"


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


def requires_headless(cls):
    """给浏览器回归用的装饰器:node 和无头壳缺哪个都说清楚,外加怎么补。"""
    missing = []
    if NODE is None:
        missing.append("node(驱动 CDP 的那一侧)")
    if SHELL is None:
        missing.append(f"chrome-headless-shell —— {INSTALL_HINT}")
    if missing:
        return unittest.skip("缺 " + ";".join(missing))(cls)
    return cls


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
        except subprocess.TimeoutExpired:          # pragma: no cover - 组都杀了还收不到
            proc.kill()
            out, err = proc.communicate()
    finally:
        _killpg(pgid)
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _killpg(pgid: int) -> None:
    """把整个进程组收掉;组已经空了是正常末态,不是错误。"""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


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
