# -*- coding: utf-8 -*-
"""沙箱服务那份临时根的**所有权口径,只有这一份**。

`tests/serve_*.py` 这些一次性全栈沙箱是被父进程 **SIGTERM** 收掉的,而 Python 对
没有 handler 的 SIGTERM 是直接结束 —— `with tempfile.TemporaryDirectory()` 的清理
一行都不跑,一份完整的沙箱库(几十 MB,带 vault)就留在盘上。实测:一轮定向回归
之后 TMPDIR 里仍有 `kb-source-qa-*`,全量回归留下两个;阅读页那条链留下的是
`kb-reading-qa-*`。

所以**根优先由起它的那一侧拥有**:`tests/headless.py` 的 `fixture_server()` 建根、
用 `KB_FIXTURE_ROOT` 传下来、在自己的 `finally` 里删。手动跑这些脚本时没有那个
变量,照旧自己建一个临时目录(那条路上收尾靠 Ctrl-C 的 KeyboardInterrupt,`with`
是跑得到的)。

回归 `tests/test_probe_contract.py`。
"""

from __future__ import annotations

import contextlib
import os
import tempfile


def fixture_root(stack: contextlib.ExitStack, prefix: str) -> str:
    """这次沙箱用的临时根:有 `KB_FIXTURE_ROOT` 就用父进程那份,否则自己建一个。

    自己建的那份挂在 `stack` 上(手动跑、Ctrl-C 收尾时会被删);父进程给的那份
    **不挂** —— 删它是父进程的事,我们这边一个 SIGTERM 就没了,删不动也不该删。
    """
    given = os.environ.get('KB_FIXTURE_ROOT', '').strip()
    if given:
        return given
    return stack.enter_context(tempfile.TemporaryDirectory(prefix=prefix))
