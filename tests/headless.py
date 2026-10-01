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
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import unittest
from pathlib import Path

#: 装一份无头壳的办法,skip 消息里原样带给用户。
INSTALL_HINT = ("装一份:`npx playwright install chromium-headless-shell`,"
                "或自己准备一个 chrome-headless-shell 并用 "
                "HEADLESS_SHELL=<可执行文件路径> 指定")

_GLOB = "chromium_headless_shell-*/*/chrome-headless-shell*"


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


def probe_env() -> dict[str, str]:
    """跑 .mjs 探针时要传给子进程的环境(探针按 HEADLESS_SHELL 找二进制)。"""
    assert SHELL is not None, "没有无头壳就不该走到这里,用 @requires_headless 拦住"
    return {**os.environ, "HEADLESS_SHELL": str(SHELL)}


if __name__ == "__main__":   # 排查用:python -m tests.headless
    print(f"node: {NODE or '(没找到)'}")
    print(f"chrome-headless-shell: {SHELL or '(没找到) ' + INSTALL_HINT}")
    sys.exit(0 if (NODE and SHELL) else 1)
