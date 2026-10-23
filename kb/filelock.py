# -*- coding: utf-8 -*-
"""跨平台的文件锁:`fcntl.flock` 那四个常量加一个同名函数,Windows 上用 LockFileEx 实现。

**为什么要有这一份。** `import fcntl` 是 POSIX 专有的,而它在 `kb.server` 的 import 链上
出现了六次(`maintenance`、`pipeline_failures`、`fetch_rounds`、`daily_carry`、
`feeds.x_cdp`、`wechat_setup`)—— 都在模块顶层。于是 Windows 上 kb-web **压根起不来**,
报的是一句 `ModuleNotFoundError: No module named 'fcntl'`:它指不到任何跟知识库有关的
东西,人只会去怀疑依赖装坏了。所以这一层的存在不是为了「兼容更多平台」这种泛泛的好,
而是为了让那条 import 链在 Windows 上走得通。

**口径只有一份:所有取文件锁的地方都过这里**,不许哪一处自己再 `import fcntl`
(那正是本轮之前的形态 —— 同一件事在十一处各写了一遍,于是加一个平台要改十一处,
而漏掉的那几处在别的平台上是**静默**的 import 失败)。回归 `tests/test_filelock.py`。

**POSIX 上四个常量直接转发平台自己的值**,不自己写死 1/2/4/8:数值在各家 libc 上不
保证一样,写死之后「拿到的锁跟以为的不是同一种」是查不出来的那类错。

## Windows 这一侧的语义,跟 flock 对得上和对不上的地方

对得上(这几条是现有调用方依赖的):
  - **LOCK_SH / LOCK_EX 都是真的**(LockFileEx 不带 `EXCLUSIVE` 就是共享锁),
    所以 `fetch_rounds` 那个「读用共享锁、写用独占锁」照旧成立。
  - **LOCK_NB 拿不到锁时抛 `OSError`**(`ctypes.WinError` 造出来的就是 OSError),
    所以一圈 `except OSError:` 的轮询和降级路径一个字都不用改。
  - **句柄关掉或进程死掉,锁自动释放。** 这是 `maintenance` 那句「持锁者崩了也不会把
    别人堵死」的前提,Windows 上同样成立。

对不上(所以下面这两件事不许做,现在也没人做):
  - **同一个句柄对同一段再锁一次不会「转换」,而是自己把自己挡住**(POSIX flock 会把
    LOCK_SH 升级成 LOCK_EX)。重入要在**上层**放行 —— `maintenance` 的 `_held`
    (threading.local)、`llm` 的 `_lock_depth` 就是在干这件事,别把它们删掉。
  - **Windows 的字节段锁是强制锁**:锁住的那一段,别的进程连读写都会失败(POSIX flock
    只是劝告性的)。本仓库全部锁在**专门的 lockfile** 上(`feeds-trim.lock`、
    `secrets.json.lock` 这一族,只创建不写内容),所以这条差别碰不到;**谁要是把锁改
    成直接锁数据文件,Windows 上就会让「不持锁的旁路读取」从成功变成报错**。

**锁的是第 0 个字节这一个字节,不是整个文件。** 范围只要上锁和解锁两边一致就行,
而「1 个字节」比 `0xFFFFFFFF` 那种写法少一类溢出口;超出文件末尾加锁在 Windows 上是
合法的,所以空 lockfile 也锁得住(实测,`tests/test_filelock.py` 钉着)。
"""

from __future__ import annotations

import os

try:                                     # POSIX
    import fcntl as _fcntl
except ImportError:                      # pragma: no cover - Windows 走下面那一支
    _fcntl = None


def _fd_of(fd) -> int:
    """调用方给的可能是 fd(`fh.fileno()`、`os.open` 的返回值),也可能是文件对象。"""
    return fd.fileno() if hasattr(fd, 'fileno') else int(fd)


if _fcntl is not None:
    LOCK_SH = _fcntl.LOCK_SH
    LOCK_EX = _fcntl.LOCK_EX
    LOCK_NB = _fcntl.LOCK_NB
    LOCK_UN = _fcntl.LOCK_UN
    HAVE_FLOCK = True
    BACKEND = 'fcntl'

    def flock(fd, operation: int) -> None:
        """`fcntl.flock` 原样转发。"""
        _fcntl.flock(_fd_of(fd), operation)

elif os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt

    #: 这四个值在 Windows 上由我们自己定(没有 libc 的 flock 可以抄),只要
    #: **本模块内自洽**就行 —— 调用方一律用这里导出的名字,不许写字面量。
    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8
    HAVE_FLOCK = True
    BACKEND = 'LockFileEx'

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _RANGE = 1                           # 锁第 0 个字节;见模块说明

    class _Overlapped(ctypes.Structure):
        # Internal / InternalHigh 是 ULONG_PTR:用 LPVOID 才能在 32 位和 64 位上
        # 都拿到正确的结构大小(写成 DWORD 的话 64 位上 Offset 会错位,
        # 于是锁加到了一个随机偏移上 —— 两个进程各锁一段,互斥静默失效)。
        _fields_ = [('Internal', wintypes.LPVOID),
                    ('InternalHigh', wintypes.LPVOID),
                    ('Offset', wintypes.DWORD),
                    ('OffsetHigh', wintypes.DWORD),
                    ('hEvent', wintypes.HANDLE)]

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                wintypes.DWORD, wintypes.DWORD,
                                ctypes.POINTER(_Overlapped)]
    _k32.LockFileEx.restype = wintypes.BOOL
    _k32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _k32.UnlockFileEx.restype = wintypes.BOOL

    def flock(fd, operation: int) -> None:
        """`fcntl.flock` 的 Windows 版。拿不到锁、解锁失败都抛 `OSError`。"""
        handle = msvcrt.get_osfhandle(_fd_of(fd))
        over = _Overlapped()
        if operation & LOCK_UN:
            ok = _k32.UnlockFileEx(handle, 0, _RANGE, 0, ctypes.byref(over))
        else:
            flags = 0
            if operation & LOCK_EX:
                flags |= _LOCKFILE_EXCLUSIVE_LOCK
            if operation & LOCK_NB:
                flags |= _LOCKFILE_FAIL_IMMEDIATELY
            ok = _k32.LockFileEx(handle, flags, 0, _RANGE, 0, ctypes.byref(over))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

else:                                    # pragma: no cover - 既不是 POSIX 也不是 NT
    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8
    HAVE_FLOCK = False
    BACKEND = 'none'

    def flock(fd, operation: int) -> None:
        """没有文件锁可用。**抛 OSError 而不是安静返回** —— 安静返回等于骗调用方
        「锁住了」,而它们据此整份覆盖写回;抛出来的话,写 kb.db 那一族会按
        「等不到锁 → 这一趟什么都不动」收场,那是安全的那一侧。"""
        raise OSError('这个平台没有可用的文件锁(既没有 fcntl 也不是 Windows)')
