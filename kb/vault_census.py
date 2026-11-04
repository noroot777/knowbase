# -*- coding: utf-8 -*-
"""给待处理数用的 vault 清点:现在有哪几篇、各是哪个来源、指纹是多少。只读。

同步中心每次打开(切到那个标签、一轮跑完、点「重新读取」)都要一份待处理数,
而它要知道 vault 里每一篇的来源和指纹 —— 原来每问一次就 `scan_all()` 一遍全库,
一次查询还问两遍(索引那一项一遍、采集器数「已导出多少篇」又一遍)。两次打开
之间多半一篇都没变,读三万篇只是为了得出和上次一样的数。

所以**按文件的 stat 记住上一次读出来的结果**:签名(`scan_status.stat_signature`,
含 inode、大小、mtime_ns、ctime_ns)没变的那篇直接用上次那一条,变了的、新出现
的才重新读。ctime 用户改不回去,任何一次写都会动它,所以「签名相同」可以当作
「内容相同」。读的那一步就是 `scanner._doc_from_file`,读不出来的那篇跟 `scan_all`
一样当作不在(`scanner.READ_ERRORS`),而且**不记住**,下次照样重读。

记忆只在进程内,重启就从头读一遍。签名取在读之前:读的期间文件又被改了的话,
记下的是旧签名配新内容,下一次签名对不上、照样重读,不会把旧内容留住。

**只给计数用。** 索引、清理要的是带完整凭据的那份扫描(`scan_all` 的 report),
别拿这一份去代替。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from kb import scanner
from kb.scan_status import stat_signature


@dataclass(frozen=True)
class Entry:
    path: str            # 相对 VAULT_ROOT,同 `Doc.path`
    source: str          # 同 `Doc.source`
    content_hash: str    # 同 `Doc.content_hash`


_lock = threading.Lock()
_memo: dict[tuple[str, str], tuple[tuple[int, ...], Entry]] = {}


def census() -> list[Entry]:
    """vault 里此刻读得出来的每一篇。并发调用排队:后来的那次基本全是命中。"""
    with _lock:
        seen: dict[tuple[str, str], tuple[tuple[int, ...], Entry]] = {}
        for src, md_path in scanner.iter_md():
            key = (str(md_path), src.name)
            try:
                signature = stat_signature(md_path.stat())
            except OSError:
                continue
            hit = _memo.get(key)
            if hit is not None and hit[0] == signature:
                seen[key] = hit
                continue
            try:
                doc = scanner._doc_from_file(md_path, src)
            except scanner.READ_ERRORS:
                continue
            seen[key] = (signature, Entry(doc.path, doc.source, doc.content_hash))
        # 整份换掉:已经不在的那些篇一并忘掉
        _memo.clear()
        _memo.update(seen)
        return [entry for _signature, entry in seen.values()]
