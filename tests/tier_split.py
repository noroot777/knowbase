# -*- coding: utf-8 -*-
"""full 层的两路并行(`tests/tiers.py` 的 `main(["full"])` 调这里)。

整套按 `kb_browser` 标记切成两路,各起一个子进程同时跑:逻辑那一路吃 CPU,浏览器
那一路大半时间在等真浏览器,串起来跑是白白把两段时间相加。**两路不重不漏,合起
来就是整套**(切法在 `tiers.build_suite`)。代价是两路之间的同跑污染看不见了 ——
同一路里的照旧看得见;这个取舍是仓库主人拍板的,见 docs/decisions/test-tiers.md。

- **两路各用自己的数据目录**(`$KB_DATA_DIR/<路名>`):两个进程同时写一个 kb.db,
  互相踩出来的红指不到任何代码。
- **输出各落一份,跑完按顺序整段打出**:交错在一起的点和回溯没人读得懂。
- **任一路红就红**;子进程被信号打断、没交回账本,同样按红算。
- **收尾归这一侧**:SIGTERM/SIGINT 进来先收两路再退。父进程被 SIGKILL 时收不到
  任何机会,所以每一路自己盯着父进程(`watch_parent`),父进程一没就退。
- 自测那一组(`tests/tier_selftest.py`)跳不跳由这里替两路一次定好;两路的账合
  起来覆盖了整套,所以**只有这里落「全绿」的证**。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from tests import tier_selftest
from tests.tiers import PARTS, ROOT

_POLL_SECONDS = 1.0


def child_argv(part: str, selftest: str, report: str) -> list[str]:
    return [sys.executable, "-m", "tests.tiers", part, f"--selftest={selftest}",
            f"--report={report}", f"--parent={os.getpid()}"]


def watch_parent(parent: int) -> None:
    """父进程没了(被过继给别人)就自己退:SIGTERM 自己,走正常的收尾路径。"""
    def loop():
        while True:
            if os.getppid() != parent:
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(_POLL_SECONDS)
    threading.Thread(target=loop, daemon=True, name="kb-tier-parent").start()


class _Stop(Exception):
    pass


def _raise_stop(signum, frame):
    raise _Stop(signum)


def run_full(status=None) -> int:
    status = status or sys.stderr
    skip, why = tier_selftest.decide()
    print(f"[selftest] {why}", file=status, flush=True)
    selftest = "skip" if skip else "run"
    owned = None if os.environ.get("KB_DATA_DIR") else \
        tempfile.TemporaryDirectory(prefix="kb-test-data-")
    base = os.environ.get("KB_DATA_DIR") or owned.name
    work = tempfile.TemporaryDirectory(prefix="kb-tier-split-")
    procs: dict[str, subprocess.Popen] = {}
    logs: dict = {}
    old = {s: signal.signal(s, _raise_stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            walls, wall = _launch_and_wait(procs, logs, base, work.name, selftest, status)
        except _Stop as stop:
            return 128 + stop.args[0]
        finally:
            _reap(procs)      # 正常跑完是空操作;被打断时先收两路,再删它们的目录
            for s, h in old.items():
                signal.signal(s, h)
        return _summarize(procs, logs, work.name, walls, wall, selftest, status)
    finally:
        for f in logs.values():
            f.close()
        work.cleanup()
        if owned:
            owned.cleanup()


def _launch_and_wait(procs, logs, base, work, selftest, status):
    t0 = time.monotonic()
    for part in PARTS:
        data = Path(base) / part
        data.mkdir(parents=True, exist_ok=True)
        logs[part] = open(Path(work) / f"{part}.log", "w+", encoding="utf-8")
        procs[part] = subprocess.Popen(
            child_argv(part, selftest, str(Path(work) / f"{part}.json")),
            cwd=ROOT, env={**os.environ, "KB_DATA_DIR": str(data)},
            stdout=logs[part], stderr=subprocess.STDOUT)
    print(f"[full] {'、'.join(PARTS)} 两路并行开跑,输出跑完整段打出",
          file=status, flush=True)
    walls: dict[str, float] = {}
    while len(walls) < len(PARTS):
        for part in PARTS:
            code = procs[part].poll()
            if code is not None and part not in walls:
                walls[part] = time.monotonic() - t0
                print(f"[full] {part} 跑完(rc={code},{walls[part]:.0f} 秒)",
                      file=status, flush=True)
        time.sleep(0.2)
    return walls, time.monotonic() - t0


def _reap(procs) -> None:
    for p in procs.values():
        if p.poll() is None:
            p.terminate()
    for p in procs.values():
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:      # pragma: no cover - 收不动才走这条
            p.kill()
            p.wait()


def _summarize(procs, logs, work, walls, wall, selftest, status) -> int:
    reports, rc = {}, 0
    for part in PARTS:
        logs[part].seek(0)
        print(f"\n======== [{part}] ========", file=status)
        status.write(logs[part].read())
        status.flush()
        try:
            reports[part] = json.loads((Path(work) / f"{part}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"[full] {part} 没交回账本(rc={procs[part].returncode})—— 按红算",
                  file=status, flush=True)
            rc = 1
            continue
        rc = rc or (reports[part]["rc"] or procs[part].returncode)
    if len(reports) == len(PARTS):
        executed = sum(r["executed"] for r in reports.values())
        total = sum(r["browser_total"] for r in reports.values())
        ran = sum(r["browser_ran"] for r in reports.values())
        lanes = "、".join(f"{p} {walls.get(p, 0):.0f} 秒" for p in PARTS)
        print(f"\n[full] 两路合计跑了 {executed} 条,墙钟 {wall:.0f} 秒({lanes});"
              f"浏览器用例真跑 {ran}/{total} 条", file=status, flush=True)
        planned = set().union(*(r["selftest_planned"] for r in reports.values()))
        passed = set().union(*(r["selftest_passed"] for r in reports.values()))
        if selftest == "run" and planned and planned <= passed:
            tier_selftest.record_pass()
            print(f"[selftest] {len(planned)} 条全绿,记下设施指纹", file=status, flush=True)
    print(f"[full] {'通过' if rc == 0 else '没通过'}", file=status, flush=True)
    return 1 if rc else 0
