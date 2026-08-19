#!/usr/bin/env python3
"""重跑 pre_tagged 来源的 entity/topic/summary 入库(不动 chunks/向量)。

场景:parse_pretagged 逻辑升级(如主实体 kind、summary 改从 frontmatter 读)后,
文件内容没变、kb index 的增量判断不会触发重解析,用这个脚本全量刷一遍。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import store
from kb.config import SOURCES
from kb.extract import parse_pretagged, save_extraction
from kb.scanner import scan_all


def main() -> int:
    pre = {s.name for s in SOURCES if s.pre_tagged}
    conn = store.connect()
    known = {r["path"] for r in conn.execute("SELECT path FROM documents")}
    n = skipped = 0
    for doc in scan_all():
        if doc.source not in pre:
            continue
        if doc.path not in known:  # 还没 index 的新文件,轮不到刷
            skipped += 1
            continue
        data = parse_pretagged(doc)
        if data is None:
            continue
        save_extraction(conn, doc.path, data)
        n += 1
        if n % 200 == 0:
            conn.commit()
            print(f"[pretagged-refresh] {n} 篇...", flush=True)
    conn.commit()
    conn.close()
    print(f"[pretagged-refresh] 完成: 刷新 {n} 篇, 未入索引跳过 {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
