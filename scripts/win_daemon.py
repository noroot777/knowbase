# -*- coding: utf-8 -*-
"""Windows 上「起一个真正脱离终端的后台服务」和「先好好跟它说一句」这两件事。

`kb.sh` 的 `daemonize()` 是一段内联的 python(fork + setsid);Windows 没有 fork,
这两件事也都要绕 Win32 API,写不进一行 shell,所以单独成文件。`kb.ps1` 调它。

两个子命令:

  spawn <pidfile> <logfile> <workdir> [--env NAME=VALUE]... -- <cmd...>
      起服务。写进 pidfile 的是**真正那个服务进程的 pid**(不是什么包装进程的)。
      给了 `--env` 就**只用**给的那些组成子进程的环境(一个都不继承)—— 那是
      `kb.sh` 里 `env -i` 的对等物,名单和理由在 `scripts/win_env.ps1`。
      一个 `--env` 都没给时照常继承本进程的环境。

  ctrlbreak <pid>
      给那个服务的控制台送一次 CTRL_BREAK —— Windows 上最接近 `kill -INT` 的东西。

## 为什么用 CREATE_NEW_CONSOLE 而不是 DETACHED_PROCESS

两个都能让进程活过终端关闭(DETACHED_PROCESS 更像 `setsid`)。但 **DETACHED_PROCESS
的进程压根没有控制台,于是再也没法给它送控制台事件** —— 那就等于 Windows 上只剩
TerminateProcess 一条路,而 `kb.sh` 专门写过为什么不能只有那一条:kb-web 进程里可能
正跑着同步,握着 kb.db 的写事务、一个采集器子进程、一个向量编码子进程,直接硬杀会把
这些甩在那儿。所以给它一个**自己的控制台、但窗口隐藏**(STARTF_USESHOWWINDOW +
SW_HIDE):看不见,却收得到 CTRL_BREAK。

uvicorn 在 Windows 上确实装了 SIGBREAK 的处理(CTRL_BREAK 到 python 就是 SIGBREAK),
所以这一下是真能让它走正常的 shutdown 流程,而不是被砍掉。

## ctrlbreak 为什么要先 FreeConsole

`GenerateConsoleCtrlEvent` 只能送给**和调用方共用同一个控制台**的进程组。服务有它
自己那个控制台,所以要先 `FreeConsole()` 脱开我们自己的,再 `AttachConsole(pid)`
贴到它那个上去。这也正是**必须放在一个一次性子进程里做**的原因:在 kb.ps1 自己的
进程里 FreeConsole 会把用户的终端搞坏。

贴上去之后立刻 `SetConsoleCtrlHandler(NULL, TRUE)` 把自己对 CTRL 事件设成忽略 ——
不然我们送出去的那一下会把自己也打死,于是退出码变成「失败」,调用方以为没送到,
直接升级到硬杀,那就白做了。

送的是 group 0 = 「这个控制台上的所有进程」。这正好对应 `kb.sh` 那边
`collect_tree` 之后逐个 `kill -INT`:服务和它所有子进程一起收到。
"""

from __future__ import annotations

import os
import subprocess
import sys

CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CTRL_BREAK_EVENT = 1


def spawn(argv: list[str]) -> int:
    if len(argv) < 3:
        print('spawn 要有 pidfile / logfile / workdir', file=sys.stderr)
        return 2
    pidfile, logfile, workdir = argv[0], argv[1], argv[2]
    rest = argv[3:]
    env: dict[str, str] | None = None
    while rest and rest[0] == '--env':
        if len(rest) < 2:
            print('--env 后面要跟 NAME=VALUE', file=sys.stderr)
            return 2
        name, _, value = rest[1].partition('=')
        if env is None:
            env = {}
        env[name] = value
        rest = rest[2:]
    if rest and rest[0] == '--':
        rest = rest[1:]
    cmd = rest
    if not cmd:
        print('spawn 要有命令', file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(pidfile)) or '.', exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(logfile)) or '.', exist_ok=True)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    # 追加打开:日志是跨多次重启累积的一份(`kb.ps1 logs` 读它的尾巴)
    log = open(logfile, 'ab', buffering=0)
    try:
        proc = subprocess.Popen(
            cmd, cwd=workdir, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            startupinfo=si,
            creationflags=CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP)
    finally:
        log.close()          # 句柄已经复制给子进程了,我们这头不用留着
    # **先落盘再返回。** kb.ps1 起完要靠 pidfile 和端口两样来判「起没起来」,
    # 而 code_stale 的判据正是「pidfile 比 kb/ 下的 .py 旧」—— 写 pidfile 的
    # 这一刻就是进程启动那一刻,这个含义不能漂。
    with open(pidfile, 'w', encoding='ascii') as fh:
        fh.write(str(proc.pid))
    return 0


def ctrlbreak(argv: list[str]) -> int:
    import ctypes
    if not argv:
        return 2
    pid = int(argv[0])
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.FreeConsole()                      # 脱开自己那个(本进程是一次性的)
    if not k32.AttachConsole(pid):
        # 它没有控制台、或者已经没了。这不是错误,只是「这一下没送出去」——
        # 调用方据此升级到硬杀。
        return 1
    # 先把自己对 CTRL 事件设成忽略,再送:不然这一下会把自己也打死
    k32.SetConsoleCtrlHandler(None, True)
    ok = k32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0)
    k32.FreeConsole()
    return 0 if ok else 1


def main() -> int:
    if os.name != 'nt':
        print('这个脚本只在 Windows 上有意义(POSIX 上走 kb.sh 的 daemonize)',
              file=sys.stderr)
        return 2
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    action, rest = sys.argv[1], sys.argv[2:]
    if action == 'spawn':
        return spawn(rest)
    if action == 'ctrlbreak':
        return ctrlbreak(rest)
    print(f'不认识的子命令 {action}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
