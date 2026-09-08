# -*- coding: utf-8 -*-
"""测试用的假 worker:说与 kb/embed_worker.py 完全相同的协议,但不加载模型。

帧的读写直接复用 embed_worker 里那两个函数,不另抄一份——这样它和 embed_pool
之间就是真正的契约测试:哪天有人改了真 worker 的帧格式而忘了改 pool(或反过来),
测试会当场红,而不是等到线上向量整体平移 4 字节才发现。

产向量规则:第 i 维 = len(text) + i,长度固定 DIM=8。几个魔法文本触发故障:
  __BOOM__      → 回错误帧(worker 侧编码异常)
  __HUGEFRAME__ → 回一个声称 4 GB 的帧头(模拟管道错位)
  __SILENT__    → 收了请求就装死(模拟 worker 卡死)
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.embed_worker import _read_frame, _write_frame  # noqa: E402

DIM = 8


def main() -> int:
    import numpy as np

    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    inp = sys.stdin.buffer

    while True:
        raw = _read_frame(inp)
        if raw is None:
            return 0
        req = json.loads(raw)
        if req.get("op") == "shutdown":
            return 0
        texts = req["texts"]
        if any(t == "__SILENT__" for t in texts):
            time.sleep(600)             # 装死:什么都不回
            continue
        if any(t == "__HUGEFRAME__" for t in texts):
            # 只写帧头,声称后面有 4 GB。父进程必须当场判错位而不是等下去。
            out.write(struct.pack("<I", 4 * 1024 * 1024 * 1024 - 1))
            out.flush()
            continue
        try:
            if any(t == "__BOOM__" for t in texts):
                raise ValueError("stub 故意炸")
            arr = np.asarray([[len(t) + i for i in range(DIM)] for t in texts],
                             dtype=np.float32)
        except Exception as e:
            _write_frame(out, json.dumps(
                {"ok": False, "error": f"{type(e).__name__}: {e}"}).encode())
            continue
        _write_frame(out, json.dumps(
            {"ok": True, "n": arr.shape[0], "dim": arr.shape[1]}).encode())
        _write_frame(out, arr.tobytes())


if __name__ == "__main__":
    sys.exit(main())
