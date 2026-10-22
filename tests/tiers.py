# -*- coding: utf-8 -*-
"""回归分层入口(`./kb.sh test` 调的就是这里;直接 `python -m tests.tiers` 也行)。

三层的分工(完整语境、实测耗时见 docs/decisions/test-tiers.md):

- **fast(缺省)**:discover 全部,但先设 `KB_SKIP_BROWSER_TESTS` —— 所有真实浏览器
  测试类在 `tests.headless.browser_gate` 那一个闸门上被显式拦下(skip 消息写清
  出路),同文件里的纯逻辑用例照常跑。这是提交前的门禁层。
- **probes**:只跑引用 `tests.headless` 的那些测试文件(浏览器回归和它们的邻居)。
  改了 `kb/static/`、`kb/web/pages.py` 这类前端产物之后跑这层。**浏览器用例没验全
  (有被工具缺席拦下的,或一条都没发现)就按失败退出** —— 纯逻辑邻居的通过不许
  充当浏览器覆盖(实测事故形态:缺壳时浏览器用例全 skip、几百条邻居通过,命令照
  样报成功)。
- **full**:全部,等同 README 里那条 discover 命令(缺工具自然 skip,跟 CI 同一
  态度,但把浏览器覆盖数打出来)。push 前至少跑一次 —— 共享 `KB_DATA_DIR` 的
  「只在同跑时红、单跑永远绿」那类问题,只有全量同跑看得见。

**所选层对快速层开关的态度必须显式**(`_apply_tier_env`):fast 设上,probes/full
把继承值清掉。不清的话,外头 export 过一次的壳里 full 就再也不是「一条不拦」。

**probes 的名单是现算的,不许改成手工维护的文件列表**:列表会静默过期 —— 新探针
文件没人记得登记,它就落进 fast 层,快速层又慢回去,而且没有任何一处会红。现算的
口径宁可多圈(多跑几条毫秒级用例)不可少圈;「引用 tests.headless」能圈全,靠的是
所有真实浏览器类都必须挂那里的共享闸门(结构守卫在 tests/test_tiers.py)。
"""

from __future__ import annotations

import os
import re
import sys
import time
import unittest
from collections import Counter
from pathlib import Path

from tests.headless import SKIP_ENV

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
TIERS = ("fast", "probes", "full")

#: probes 层的口径:测试文件的源码里引用了 tests.headless(共享闸门、run_probe、
#: fixture_server 都在这一个模块里)。docstring 里偶尔提一句也会被圈进来 ——
#: 多跑几条毫秒级用例无害,漏一个真浏览器文件才是要防的那头。
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


def _apply_tier_env(tier: str) -> None:
    """在 discover **之前**对快速层开关表态(闸门是类装饰器,import 那一刻就定了
    拦不拦):fast 设上;probes/full **清掉继承值** —— 不清的话,外头 export 过
    一次(或嵌套跑过 fast)的壳里,工具齐备、显式全量照样把浏览器回归整层拦掉,
    而且一切显示正常。"""
    if tier == "fast":
        os.environ[SKIP_ENV] = "1"
    else:
        os.environ.pop(SKIP_ENV, None)


def _iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def browser_case_ids(suite) -> set[str]:
    """浏览器用例的账本:`kb_browser` 标记(tests.headless.browser_gate 打的)是
    「这条要起真实浏览器」的唯一口径。要在 run **之前**收集 —— unittest 跑完
    会把 suite 里的条目逐个释放掉。"""
    return {t.id() for t in _iter_tests(suite) if getattr(t, "kb_browser", False)}


class TierResult(unittest.TextTestResult):
    """给覆盖账记真实执行事件的 result。

    「真跑了没有」只能从**正向事件**建账:哪条 `startTest` 了、哪条被整条 skip、
    哪条的子例 skip 了。不许用「计划的 id 集合减 skipped 的 id 集合」反推 ——
    `setUpClass`/`setUpModule` 抛 `SkipTest` 时 unittest 记的是
    `setUpClass (模块.类)` 这种 holder,逐条方法的 id 根本不出现在 skipped 里,
    反推会把一条都没开始的整组算成「全跑了」;`subTest` 里的 skip 记的是带参数
    的子例 id,同样对不上方法 id。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kb_started: set[str] = set()        # 真正开始过的方法 id
        self.kb_whole_skips: set[str] = set()    # 整条 skip(方法级 + holder)
        self.kb_partial_skips: set[str] = set()  # 子例 skip 的**父方法** id

    def startTest(self, test):
        self.kb_started.add(test.id())
        super().startTest(test)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        parent = getattr(test, "test_case", None)   # _SubTest 才有这个指回父方法
        if parent is not None:
            self.kb_partial_skips.add(parent.id())
        else:
            self.kb_whole_skips.add(test.id())


def browser_coverage(browser_ids: set[str], result: TierResult) -> tuple[int, int]:
    """(浏览器用例总数, 真跑了几条)。

    「真跑」= 这条方法真的开始了,整条没被 skip,子例也一个都没 skip ——
    **没开始的和跑了一半的都不算验全**;红的算跑(它起码验了)。判据全部来自
    TierResult 的正向事件,理由见它的 docstring。
    """
    ran = (result.kb_started & browser_ids) \
        - result.kb_whole_skips - result.kb_partial_skips
    return len(browser_ids), len(ran)


def main(argv: list[str]) -> int:
    tier = argv[0] if argv else "fast"
    if tier not in TIERS:
        print(f"不认识的层 {tier!r},只有 {'|'.join(TIERS)}(缺省 fast)", file=sys.stderr)
        return 2
    _apply_tier_env(tier)
    # 汇总要打的流,**现在就存一份引用**:有的用例会临时把 sys.stdout/sys.stderr
    # 换成 StringIO,泄漏(多线程、失败路径)时跑完再取就拿到它那份替身,汇总行
    # 一声不响地丢掉 —— 实测丢过。unittest 自己的结果块没丢,靠的就是 runner 在
    # 开跑前解析了一次流;这里同一个办法。
    status = sys.stderr
    suite = build_suite(tier)
    browser_ids = browser_case_ids(suite)
    t0 = time.monotonic()
    result = unittest.TextTestRunner(verbosity=1, resultclass=TierResult).run(suite)
    wall = time.monotonic() - t0

    # 执行条数同样从正向事件算:开始了且整条没被 skip 的才算。testsRun 减
    # len(skipped) 在 setUpClass/setUpModule skip 时会出负数(holder 记进了
    # skipped,方法却一条没开始、testsRun 为 0)。
    executed = len(result.kb_started - result.kb_whole_skips)
    total, ran = browser_coverage(browser_ids, result)
    line = f"[{tier}] 跑了 {executed} 条,用时 {wall:.0f} 秒"
    if tier == "fast":
        line += (f";浏览器回归 {total - ran} 条被拦"
                 "(改了前端就跑 ./kb.sh test probes,push 前跑一次 full)")
    else:
        line += f";浏览器用例真跑 {ran}/{total} 条"
    print(line, file=status, flush=True)
    # skip 理由按原因去重打出来:verbosity=1 只显示 's',缺了工具的人在默认输出里
    # 得看得到装哪个、怎么装,不能让「安装提示写在装饰器里」停在装饰器里。
    for reason, n in Counter(r for _, r in result.skipped).most_common():
        print(f"  skip ×{n}:{reason}", file=status, flush=True)
    if tier == "probes":
        # 这一层的意义就是「现在就验浏览器」,没验全不许绿:缺壳时浏览器用例全
        # skip、几百条纯逻辑邻居通过、命令报成功 —— 那正是要修的事故形态。空集
        # 合同罪:一条浏览器用例都没发现(名单口径坏了、标记丢了),这层就什么
        # 都没验,不许报成功。full 不在此列(它沿用 README/CI 那条 discover 的
        # 「缺工具自然 skip」政策,覆盖数如实打印)。
        if not total:
            print("[probes] 一条浏览器用例都没发现 —— 这层等于什么都没验,"
                  "按失败算(名单或 kb_browser 标记的口径坏了)",
                  file=status, flush=True)
            return 1
        if ran < total:
            print(f"[probes] 浏览器用例 {total - ran} 条没真跑 —— 这层等于没验全,"
                  "按失败算(纯逻辑邻居的通过不作数);缺什么看上面的 skip 分类",
                  file=status, flush=True)
            return 1
    if tier == "fast" and ran:
        # 快速层不许起任何真实浏览器。标记类真跑了,说明闸门/层表态坏了。
        print(f"[fast] 有 {ran} 条浏览器用例真跑了 —— 快速层的闸门失效,按失败算",
              file=status, flush=True)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
