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
  任何机会,所以每一路自己盯着父进程(`watch_parent`),父进程一没就退。盯法是
  **一根只有父进程握着写端的管道**:写端随父进程一起被内核关掉,读端立刻读到 EOF。
  不许改回「睡一秒、看一眼 getppid」—— 那个线程睡的是全局 `time.sleep`,而有的用例
  正是打桩它来记节拍(`tests/test_youtube_pacing.py`),打桩期间这根线程空转着往
  桩里灌了上百万拍,把那条用例带红、也把整一路拖慢。
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


def child_argv(part: str, selftest: str, report: str, parent_fd: int) -> list[str]:
    return [sys.executable, "-m", "tests.tiers", part, f"--selftest={selftest}",
            f"--report={report}", f"--parent-fd={parent_fd}"]


def watch_parent(fd: int) -> None:
    """fd 是父进程那根管道的读端:读到 EOF = 父进程没了,SIGTERM 自己走正常收尾。

    线程一直阻塞在 read 上,不睡、不轮询。读端不往孙进程传(后者多半是
    `close_fds` 的 Popen,这里再保一道),写端从来只在父进程手里。"""
    os.set_inheritable(fd, False)

    def wait():
        try:
            while os.read(fd, 1):
                pass
        except OSError:
            pass
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=wait, daemon=True, name="kb-tier-parent").start()


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
    read_fd, write_fd = os.pipe()   # 写端只在这里,两路各拿一份读端:见 watch_parent
    old = {s: signal.signal(s, _raise_stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            walls, wall = _launch_and_wait(procs, logs, base, work.name, selftest,
                                           status, read_fd)
        except _Stop as stop:
            return 128 + stop.args[0]
        finally:
            _reap(procs)      # 正常跑完是空操作;被打断时先收两路,再删它们的目录
            os.close(write_fd)   # 读端在 _launch_and_wait 里关,两头各关一次、不重关
            for s, h in old.items():
                signal.signal(s, h)
        return _summarize(procs, logs, work.name, walls, wall, selftest, status)
    finally:
        for f in logs.values():
            f.close()
        work.cleanup()
        if owned:
            owned.cleanup()


def _launch_and_wait(procs, logs, base, work, selftest, status, read_fd):
    t0 = time.monotonic()
    try:
        for part in PARTS:
            data = Path(base) / part
            data.mkdir(parents=True, exist_ok=True)
            logs[part] = open(Path(work) / f"{part}.log", "w+", encoding="utf-8")
            procs[part] = subprocess.Popen(
                child_argv(part, selftest, str(Path(work) / f"{part}.json"), read_fd),
                cwd=ROOT, env={**os.environ, "KB_DATA_DIR": str(data)},
                stdout=logs[part], stderr=subprocess.STDOUT, pass_fds=(read_fd,))
    finally:
        os.close(read_fd)     # 父进程自己不读,两路各自继承了一份
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
