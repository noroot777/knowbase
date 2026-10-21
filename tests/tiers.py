# -*- coding: utf-8 -*-
"""回归分层入口(`./kb.sh test` 调的就是这里;直接 `python -m tests.tiers` 也行)。

三层的分工(完整语境、实测耗时见 docs/decisions/test-tiers.md):

- **fast(缺省)**:discover 全部,但先设 `KB_TEST_NO_BROWSER` —— 浏览器回归在
  `tests.headless.requires_headless` 那一个闸门上被显式拦下(skip 消息写清出路),
  同文件里的纯逻辑用例照常跑。这是提交前的门禁层。
- **probes**:只跑引用 `tests.headless` 的那些测试文件(浏览器回归和它们的邻居)。
  改了 `kb/static/`、`kb/web/pages.py` 这类前端产物之后跑这层。
- **full**:全部,等同 README 里那条 discover 命令。push 前至少跑一次 ——
  共享 `KB_DATA_DIR` 的「只在同跑时红、单跑永远绿」那类问题,只有全量同跑看得见,
  分层和选测都替代不了它。

**probes 的名单是现算的,不许改成手工维护的文件列表**:列表会静默过期 —— 新探针
文件没人记得登记,它就落进 fast 层,快速层又慢回去,而且没有任何一处会红。现算的
口径宁可多圈(多跑几条毫秒级用例)不可少圈。
"""

from __future__ import annotations

import os
import re
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
TIERS = ("fast", "probes", "full")

#: probes 层的口径:测试文件的源码里引用了 tests.headless(import 它的 run_probe /
#: requires_headless / fixture_server,都是这一个模块)。docstring 里偶尔提一句也会
#: 被圈进来 —— 多跑几条毫秒级用例无害,漏一个真探针文件才是要防的那头。
_PROBE_RE = re.compile(r"\btests\.headless\b")


def probe_modules() -> list[str]:
    """引用 tests.headless 的测试模块名(``tests.test_xxx``),按文件名排序。"""
    out = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if _PROBE_RE.search(path.read_text(encoding="utf-8")):
            out.append(f"tests.{path.stem}")
    return out


def build_suite(tier: str) -> unittest.TestSuite:
    """按层取用例。fast 的闸门不在这里 —— 在 tests.headless(见模块 docstring)。"""
    loader = unittest.defaultTestLoader
    if tier == "probes":
        return loader.loadTestsFromNames(probe_modules())
    return loader.discover(str(TESTS_DIR), top_level_dir=str(ROOT))


def main(argv: list[str]) -> int:
    tier = argv[0] if argv else "fast"
    if tier not in TIERS:
        print(f"不认识的层 {tier!r},只有 {'|'.join(TIERS)}(缺省 fast)", file=sys.stderr)
        return 2
    # 环境变量要在 discover **之前**设:requires_headless 是类装饰器,import 测试
    # 模块那一刻就定了拦不拦。
    from tests.headless import NO_BROWSER_ENV
    if tier == "fast":
        os.environ[NO_BROWSER_ENV] = "1"

    # 汇总要打的流,**现在就存一份引用**:有的用例会临时把 sys.stdout/sys.stderr
    # 换成 StringIO,泄漏(多线程、失败路径)时跑完再取就拿到它那份替身,汇总行
    # 一声不响地丢掉 —— 实测丢过。unittest 自己的结果块没丢,靠的就是 runner 在
    # 开跑前解析了一次流;这里同一个办法。
    status = sys.stderr
    t0 = time.monotonic()
    result = unittest.TextTestRunner(verbosity=1).run(build_suite(tier))
    wall = time.monotonic() - t0

    gated = sum(1 for _, reason in result.skipped if NO_BROWSER_ENV in reason)
    other_skips = len(result.skipped) - gated
    line = (f"[{tier}] 跑了 {result.testsRun - len(result.skipped)} 条,"
            f"用时 {wall:.0f} 秒")
    if gated:
        line += (f";浏览器回归 {gated} 条被拦(改了前端就跑 ./kb.sh test probes,"
                 "push 前跑一次 full)")
    if other_skips:
        line += f";另有 {other_skips} 条各自原因的 skip"
    print(line, file=status, flush=True)
    if tier == "probes" and result.testsRun == len(result.skipped):
        # 一条都没真跑,这层等于没验 —— 说清楚,别让全绿冒充通过。
        print("[probes] 全部被 skip:这一层一条都没真跑,理由见上面的 skip 消息",
              file=status, flush=True)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
