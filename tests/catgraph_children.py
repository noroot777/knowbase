# -*- coding: utf-8 -*-
"""`test_catgraph_concurrent` 用来做受控交错的四个**真子进程**模板。

拆出来只因为那个测试文件越过了 700 行上限,而这一块最独立:纯模板字符串,
没有模块级可变状态,也没有被测试改写的模块变量(那种搬走之后
`X.CACHE_DIR = tmp` 只改到一份副本,理由同 `scripts/wiki/taxo_tables.py`)。
每个模板都用 `.format(repo=...)` 填仓库路径,所以正文里的花括号都是双写的。

- `CHILD`        普通 writer:写完页面停在标记文件上,等 go 才继续。
- `STALE_CHILD`  陈旧 writer:先取基线快照,再等别人发布完才去发布。
- `LATE_CHILD`   后到的 writer:可注入 `gen_oserror` / `crash_after_page` 故障。
- `KEEP_CHILD`   被拒的 writer:验它把自己判的判定并回暂存。
"""

from __future__ import annotations

CHILD = '''\
import json, re, sys, time
from pathlib import Path
REPO = Path({repo!r})
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))
import build_catgraph as B, taxonomy as T
T.CACHE_DIR = Path(sys.argv[3])
code, n, out_dir = sys.argv[1], int(sys.argv[2]), Path(sys.argv[4])
pause_at, go = Path(sys.argv[5]), Path(sys.argv[6])

def fake(prompt, model, **kw):
    rows = re.findall(r"^(\\d+)\\. (\\S+) ::", prompt, re.M)
    return json.dumps([{{"id": int(i), "c": code}} for i, _nm in rows])
T._llm = fake

if pause_at.name != "-":                 # A:页面落盘后停住,还没提交缓存
    real = B._atomic_write
    def hooked(out, text):
        real(out, text)
        pause_at.touch()
        for _ in range(600):
            if go.exists():
                return
            time.sleep(0.05)
    B._atomic_write = hooked

def group(i):
    q = f"n{{i}}"
    return {{"norm": q, "name": f"名{{i}}", "kind": "product", "summary": "简介",
            "url": "", "n": 1, "accounts": 1, "latest": "2026-09-20",
            "first": "2026-09-01", "topics": ["skill工具"],
            "all_topics": ["skill工具"], "sources": ["dr-gzh-projects"],
            "entries": [{{"url": f"https://x/{{q}}", "article_title": f"文{{i}}",
                         "account": "号", "published": "2026-09-20"}}]}}

B.build_one("skills", [group(i) for i in range(n)], use_llm=True, max_new=None,
            model="stub", workers=1, out_dir=out_dir)
'''

# 另一种交错:B 读完「还没有正式缓存」这个基线就卡在模型那儿,等 A 发布完才醒,
# 判出同一条的另一个结论、写完暂存就崩。下一轮必须以正式缓存为准。
STALE_CHILD = '''\
import json, os, re, sys, time
from pathlib import Path
REPO = Path({repo!r})
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))
import build_catgraph as B, taxonomy as T
T.CACHE_DIR = Path(sys.argv[2])
code, out_dir = sys.argv[1], Path(sys.argv[3])
ready, go, asked = Path(sys.argv[4]), Path(sys.argv[5]), Path(sys.argv[6])
crash = sys.argv[7] == "1"

def fake(prompt, model, **kw):
    asked.touch()
    if ready.name != "-":                # B:读完基线就停在这儿,等 A 发布完
        ready.touch()
        for _ in range(600):
            if go.exists():
                break
            time.sleep(0.05)
    rows = re.findall(r"^(\\d+)\\. (\\S+) ::", prompt, re.M)
    return json.dumps([{{"id": int(i), "c": code}} for i, _nm in rows])
T._llm = fake

if crash:                                # 暂存刚落盘 → 进程没了
    real = T.save_cache
    def hooked(taxo, codes, names, path=None, **kw):
        real(taxo, codes, names, path, **kw)
        if path is not None:
            os._exit(99)
    T.save_cache = hooked

g = {{"norm": "n0", "name": "名0", "kind": "product", "summary": "简介",
     "url": "", "n": 1, "accounts": 1, "latest": "2026-09-20",
     "first": "2026-09-01", "topics": ["skill工具"],
     "all_topics": ["skill工具"], "sources": ["dr-gzh-projects"],
     "entries": [{{"url": "https://x/n0", "article_title": "文0",
                  "account": "号", "published": "2026-09-20"}}]}}
B.build_one("skills", [g], use_llm=True, max_new=None,
            model="stub", workers=1, out_dir=out_dir)
'''

# 第三种交错:整轮跑完(判定、建图、渲染都在锁外做完)才停在**取发布锁之前**,
# 等另一个 writer 发布完再进锁。基于陈旧基线的这一份不许发出去。
LATE_CHILD = '''\
import contextlib, json, os, re, sys, time
from pathlib import Path
REPO = Path({repo!r})
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))
import build_catgraph as B, taxonomy as T
T.CACHE_DIR = Path(sys.argv[3])
code, n, out_dir = sys.argv[1], int(sys.argv[2]), Path(sys.argv[4])
ready, go = Path(sys.argv[5]), Path(sys.argv[6])
use_llm = sys.argv[7] == "1"
summary = sys.argv[8] if len(sys.argv) > 8 else "简介"
fail = sys.argv[9] if len(sys.argv) > 9 else "-"

if fail == "gen_oserror":                # 序号写不进去(权限/磁盘满)
    def boom(taxo):
        raise OSError("simulated gen sidecar write denied")
    T.bump_gen = boom
elif fail == "crash_after_page":         # 正式页面刚可见,进程就被杀
    _real_write = B._atomic_write
    def _hooked(out, data):
        _real_write(out, data)
        sys.stdout.flush()
        os._exit(99)
    B._atomic_write = _hooked

def fake(prompt, model, **kw):
    rows = re.findall(r"^(\\d+)\\. (\\S+) ::", prompt, re.M)
    return json.dumps([{{"id": int(i), "c": code}} for i, _nm in rows])
T._llm = fake

if ready.name != "-":                    # 判完、渲染完,停在取锁之前
    real_lock = T.publish_lock
    @contextlib.contextmanager
    def slow(taxo):
        ready.touch()
        for _ in range(600):
            if go.exists():
                break
            time.sleep(0.05)
        with real_lock(taxo):
            yield
    T.publish_lock = slow

def group(i):
    q = f"n{{i}}"
    return {{"norm": q, "name": f"名{{i}}", "kind": "product", "summary": summary,
            "url": "", "n": 1, "accounts": 1, "latest": "2026-09-20",
            "first": "2026-09-01", "topics": ["skill工具"],
            "all_topics": ["skill工具"], "sources": ["dr-gzh-projects"],
            "entries": [{{"url": f"https://x/{{q}}", "article_title": f"文{{i}}",
                         "account": "号", "published": "2026-09-20"}}]}}

B.build_one("skills", [group(i) for i in range(n)], use_llm=use_llm,
            max_new=None, model="stub" if use_llm else None,
            workers=1, out_dir=out_dir)
'''

# 第四种交错:两个进程各判**自己独有**的一条。赢的那个把共享暂存整份覆盖、发布
# 完又把它删掉;输的那个被 CAS 拒绝时,自己那条判定必须还在盘上(正式缓存或暂存)。
KEEP_CHILD = '''\
import contextlib, json, re, sys, time
from pathlib import Path
REPO = Path({repo!r})
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))
import build_catgraph as B, taxonomy as T
T.CACHE_DIR = Path(sys.argv[3])
norm, code, out_dir = sys.argv[1], sys.argv[2], Path(sys.argv[4])
ready, go = Path(sys.argv[5]), Path(sys.argv[6])
hold, hold_go = Path(sys.argv[7]), Path(sys.argv[8])

def fake(prompt, model, **kw):
    if hold.name != "-":                 # 读完基线就停在模型调用里
        hold.touch()
        for _ in range(600):
            if hold_go.exists():
                break
            time.sleep(0.05)
    rows = re.findall(r"^(\\d+)\\. (\\S+) ::", prompt, re.M)
    return json.dumps([{{"id": int(i), "c": code}} for i, _nm in rows])
T._llm = fake

if ready.name != "-":                    # 判完、渲染完,停在取锁之前
    real_lock = T.publish_lock
    @contextlib.contextmanager
    def slow(taxo):
        ready.touch()
        for _ in range(600):
            if go.exists():
                break
            time.sleep(0.05)
        with real_lock(taxo):
            yield
    T.publish_lock = slow

g = {{"norm": norm, "name": norm, "kind": "product", "summary": "简介",
     "url": "", "n": 1, "accounts": 1, "latest": "2026-09-20",
     "first": "2026-09-01", "topics": ["skill工具"],
     "all_topics": ["skill工具"], "sources": ["dr-gzh-projects"],
     "entries": [{{"url": f"https://x/{{norm}}", "article_title": "文",
                  "account": "号", "published": "2026-09-20"}}]}}
B.build_one("skills", [g], use_llm=True, max_new=None,
            model="stub", workers=1, out_dir=out_dir)
'''
