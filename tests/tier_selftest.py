# -*- coding: utf-8 -*-
"""测试设施自测那一组:什么时候可以不跑(`tests/tiers.py` 调这里)。

这一组只验**测试设施自己**(夹具什么时候退、探针起的浏览器收没收干净),一个
`kb` 模块都不 import,产品代码怎么改都动不了它们的结论,却占了全量里最贵的几个
文件。所以:**设施指纹跟最近一次全绿那轮一样、而且那轮还不太旧,就不跑。**

- **名单手写,但口径是结构性的**:列进来的文件不许 import `kb`(结构守卫在
  `tests/test_tier_selftest.py`)。漏登记的自测文件只是照常每轮跑,不会少验。
- **指纹现算**:名单里的文件,加上它们按名字引用到的 `tests/` 下支撑文件(传递
  闭包,docstring 里提一句也算 —— 宁可多圈;别的 `test_*.py` 不算支撑文件),加上 python / node / 无头壳的身份。
  `tests/__init__.py` 与 `tests/headless.py` 无条件算进去。
- **只有 full 落证**:它的两路合起来恰好是整套用例,fast/probes 只读证不写证。
- **证有保质期**(`MAX_AGE_SECONDS`):产品代码里的服务生命周期(信号处理之类)
  和机器环境不在指纹里,隔一阵无条件重验一次,把这两条漂移兜住。
- `KB_SELFTEST=always` 强制这一轮照跑。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"

SELFTEST_MODULES = (
    "tests.test_fixture_life",
    "tests.test_fixture_life_identity",
    "tests.test_fixture_life_nested",
    "tests.test_fixture_life_outcome",
    "tests.test_probe_leak",
)
#: 无条件进指纹的设施文件(每个测试都隐式经过它们)。
ALWAYS_IN = ("__init__.py", "headless.py")
FORCE_ENV = "KB_SELFTEST"
MAX_AGE_SECONDS = 7 * 24 * 3600

_NAME_RE = re.compile(r"\b([A-Za-z0-9_]+)\.(py|mjs)\b")
_MODULE_RE = re.compile(r"\btests\.([A-Za-z0-9_]+)\b")
_FROM_RE = re.compile(r"^\s*from tests import ([^\n#]+)", re.M)


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "knowbase" / "selftest-pass.json"


def module_file(module: str) -> Path:
    return TESTS_DIR / (module.split(".", 1)[1] + ".py")


def _referenced(text: str) -> set[str]:
    names = {f"{stem}.{ext}" for stem, ext in _NAME_RE.findall(text)}
    names |= {f"{stem}.py" for stem in _MODULE_RE.findall(text)}
    for group in _FROM_RE.findall(text):
        names |= {f"{n.strip()}.py" for n in group.strip("() ").split(",") if n.strip()}
    return names


def closure() -> list[Path]:
    """名单文件 + 它们按名字引用到的 `tests/` 下文件(传递)+ 无条件那两份。"""
    todo = [module_file(m) for m in SELFTEST_MODULES] + [TESTS_DIR / n for n in ALWAYS_IN]
    seen: set[Path] = set()
    while todo:
        path = todo.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for name in _referenced(path.read_text(encoding="utf-8", errors="replace")):
            # 别的测试文件不是设施:顺着它们走,闭包会把半个 tests/ 圈进来,指纹
            # 随每次产品改动都变,这一组就永远照跑。
            if not name.startswith("test_"):
                todo.append(TESTS_DIR / name)
    return sorted(seen)


def _tool_identity() -> list[str]:
    from tests import headless
    out = [sys.version, platform.platform(), str(headless.SHELL)]
    if headless.NODE:
        try:
            out.append(subprocess.run([headless.NODE, "--version"], capture_output=True,
                                      text=True, timeout=10).stdout.strip())
        except (OSError, subprocess.SubprocessError):
            out.append("node:?")   # 问不出版本就当它变了:下面那份证对不上,照跑
    return out


def fingerprint() -> str:
    h = hashlib.sha256()
    for path in closure():
        h.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
        h.update(path.read_bytes() + b"\0")
    for part in _tool_identity():
        h.update(part.encode() + b"\0")
    return h.hexdigest()


def decide(now: float | None = None) -> tuple[bool, str]:
    """(这一轮跳不跳, 说给人听的一句)。拿不准一律照跑。"""
    if os.environ.get(FORCE_ENV, "").strip() == "always":
        return False, f"{FORCE_ENV}=always,照跑"
    try:
        record = json.loads(cache_path().read_text(encoding="utf-8"))
        passed_at = float(record["passed_at"])
        stamp = str(record["fingerprint"])
    except (OSError, ValueError, KeyError, TypeError):
        return False, "没有全绿记录,照跑"
    now = time.time() if now is None else now
    if not 0 <= now - passed_at <= MAX_AGE_SECONDS:
        return False, "上次全绿已超过保质期,照跑"
    if stamp != fingerprint():
        return False, "设施或工具变了,照跑"
    when = time.strftime("%m-%d %H:%M", time.localtime(passed_at))
    return True, (f"设施指纹与 {when} 全绿那轮一致,这一轮不跑"
                  f"(强制跑:{FORCE_ENV}=always)")


def record_pass(now: float | None = None) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fingerprint": fingerprint(),
                               "passed_at": time.time() if now is None else now}),
                   encoding="utf-8")
    os.replace(tmp, path)


def is_selftest(test) -> bool:
    return type(test).__module__ in SELFTEST_MODULES
