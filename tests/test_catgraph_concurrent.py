# -*- coding: utf-8 -*-
"""两个生成进程撞在一起时,正式页面和正式缓存必须是同一份结论。

页面和缓存是两个文件、两次 rename,各自原子挡不住两个 writer 交错(2026-09-22
复审第 3 轮发现):A 写完页面 → B 写页面并提交 B 的缓存 → A 再提交 A 的缓存,
两个进程都以退出码 0 收场,最终页面来自 B、缓存却来自 A;下一轮又拿 A 的缓存
把页面刷回去,一路没有任何告警。CLI、wiki_daily 子进程、手动重跑都是独立进程,
进程内的状态管不着它们,所以 `taxonomy.publish_lock` 锁在文件上,把「读验收
基线 → 落页面 → 提交缓存」整段圈起来。

这里用两个**真子进程**做受控交错:A(30 个候选)写完页面就停住,B(40 个候选,
多出来的 10 条判成另一个 code)整轮跑完,再放 A 走完。验的是最终页面上每个
节点的 code 都能在正式缓存里找到同样的一条——对不上就意味着下一轮会拿另一份
结论把页面刷回去。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

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


class ConcurrentPublishTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cache_dir, self.out_dir = root / "data", root / "vault"
        self.cache_dir.mkdir()
        self.out_dir.mkdir()
        self.child = root / "child.py"
        self.child.write_text(CHILD.format(repo=str(REPO)), encoding="utf-8")
        self.paused, self.go = root / "paused", root / "go"

    def _spawn(self, code: str, n: int, *, pause: bool):
        return subprocess.Popen(
            [sys.executable, str(self.child), code, str(n), str(self.cache_dir),
             str(self.out_dir), str(self.paused if pause else Path("-")),
             str(self.go)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_page_and_cache_never_come_from_different_writers(self):
        a = self._spawn("S1", 30, pause=True)
        for _ in range(400):                       # 等 A 写完页面、停在提交前
            if self.paused.exists() or a.poll() is not None:
                break
            time.sleep(0.05)
        self.assertTrue(self.paused.exists(), "A 没走到暂停点")

        b = self._spawn("S2", 40, pause=False)     # 候选多了 10 条的第二个 writer
        time.sleep(1.5)
        b_jumped_the_queue = b.poll() is not None
        self.go.touch()
        a_out, a_err = a.communicate(timeout=120)
        b_out, b_err = b.communicate(timeout=120)
        self.assertEqual((a.returncode, b.returncode), (0, 0),
                         f"A:\n{a_err}\nB:\n{b_err}")

        page = (self.out_dir / "graph-skills.html").read_text(encoding="utf-8")
        cache = json.loads((self.cache_dir / "taxonomy_skills.json")
                           .read_text(encoding="utf-8"))["items"]
        on_page = dict(re.findall(r'"q": "(\w+)", "c": "(\w+)"', page))
        self.assertTrue(on_page, "没从页面里读出节点")
        mismatch = {q: (c, cache.get(q, {}).get("c"))
                    for q, c in on_page.items() if cache.get(q, {}).get("c") != c}
        self.assertFalse(
            mismatch,
            f"正式页面和正式缓存不是同一份结论(页面 {len(on_page)} 个节点,"
            f"缓存 {len(cache)} 条,对不上 {len(mismatch)} 个):"
            f"{dict(list(mismatch.items())[:3])}")
        self.assertFalse(b_jumped_the_queue,
                         "B 没等 A 交完班就自己发布了(发布锁没起作用)")

    def test_a_crashed_stale_writer_does_not_resurrect_its_verdict(self):
        """旧基线上崩掉的 writer,不许在恢复轮把已提交的结论换掉。

        复审第 5 轮:暂存不记它基于哪一版正式缓存,`codes.update(part)` 拿旧结论
        盖掉新提交的那条;条数还一样,`len(codes) == n_official` 又判成「无事可
        提交」——恢复轮以成功退出收场,却把页面改成 S2、缓存留在 S1,而且暂存
        永远留在盘上,以后每轮都这样。
        """
        root = Path(self.tmp.name)
        stale = root / "stale.py"
        stale.write_text(STALE_CHILD.format(repo=str(REPO)), encoding="utf-8")
        ready, go, asked = root / "ready", root / "go2", root / "asked"

        def spawn(code, *, wait, crash, asked_at=asked):
            return subprocess.Popen(
                [sys.executable, str(stale), code, str(self.cache_dir),
                 str(self.out_dir), str(ready if wait else Path("-")), str(go),
                 str(asked_at), "1" if crash else "0"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        b = spawn("S2", wait=True, crash=True)     # ① B 读完空基线,卡在模型那儿
        for _ in range(400):
            if ready.exists() or b.poll() is not None:
                break
            time.sleep(0.05)
        self.assertTrue(ready.exists(), "B 没走到等待点")

        a = spawn("S1", wait=False, crash=False)   # ② A 整轮跑完,发布 S1
        self.assertEqual(a.wait(timeout=120), 0, a.communicate()[1])
        go.touch()                                 # ③ B 醒来,写完暂存就崩
        self.assertEqual(b.wait(timeout=120), 99, b.communicate()[1])
        self.assertEqual(T_load(self.cache_dir / "taxonomy_skills.staging.json"),
                         {"n0": "S2"}, "没留下旧基线上的暂存")

        asked.unlink()                             # ④ 恢复轮:不该再问模型
        c = spawn("S3", wait=False, crash=False)
        self.assertEqual(c.wait(timeout=120), 0, c.communicate()[1])
        self.assertFalse(asked.exists(), "恢复轮又问了一遍模型")

        page = (self.out_dir / "graph-skills.html").read_text(encoding="utf-8")
        cache = T_load(self.cache_dir / "taxonomy_skills.json")
        self.assertEqual(cache, {"n0": "S1"}, "崩掉的旧 writer 盖掉了正式缓存")
        self.assertIn('"q": "n0", "c": "S1"', page, "页面用了作废的旧结论")
        self.assertFalse((self.cache_dir / "taxonomy_skills.staging.json").exists(),
                         "作废的暂存一直留在盘上")


def T_load(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v["c"] for k, v in raw["items"].items()}


if __name__ == "__main__":
    unittest.main()
