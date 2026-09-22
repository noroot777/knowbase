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
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 同目录的子进程模板

from catgraph_children import (CHILD, KEEP_CHILD, LATE_CHILD,  # noqa: E402
                               STALE_CHILD)



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
        self.ready, self.go2 = root / "ready2", root / "go3"

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
        """旧基线上的 writer,不许在恢复轮把已提交的结论换掉。

        复审第 5 轮:暂存不记它基于哪一版正式缓存,`codes.update(part)` 拿旧结论
        盖掉新提交的那条;条数还一样,`len(codes) == n_official` 又判成「无事可
        提交」——恢复轮以成功退出收场,却把页面改成 S2、缓存留在 S1,而且暂存
        永远留在盘上,以后每轮都这样。

        第 8 轮把「以正式缓存为准」提到了每一次写暂存上(`_edit_stage`),所以
        这条旧判定现在连盘都上不去:B 醒来时 n0 已经是 A 提交过的了。
        """
        root = Path(self.tmp.name)
        stale = root / "stale.py"
        stale.write_text(STALE_CHILD.format(repo=str(REPO)), encoding="utf-8")
        ready, go, asked = root / "ready", root / "go2", root / "asked"
        stage = self.cache_dir / "taxonomy_skills.staging.json"

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
        go.touch()                                 # ③ B 醒来:它那条已经作废了
        b_out, b_err = b.communicate(timeout=120)
        self.assertEqual(b.returncode, 0, b_err)
        self.assertIn("不发布", b_out, "基于陈旧基线的那一份还是发出去了")
        self.assertFalse(stage.exists(), "作废的旧判定被写回了暂存")

        asked.unlink()                             # ④ 恢复轮:不该再问模型
        c = spawn("S3", wait=False, crash=False)
        self.assertEqual(c.wait(timeout=120), 0, c.communicate()[1])
        self.assertFalse(asked.exists(), "恢复轮又问了一遍模型")

        page = (self.out_dir / "graph-skills.html").read_text(encoding="utf-8")
        cache = T_load(self.cache_dir / "taxonomy_skills.json")
        self.assertEqual(cache, {"n0": "S1"}, "旧 writer 盖掉了正式缓存")
        self.assertIn('"q": "n0", "c": "S1"', page, "页面用了作废的旧结论")
        self.assertFalse(stage.exists(), "作废的暂存一直留在盘上")

    def test_a_crashed_writer_leaves_its_progress_for_the_next_run(self):
        """写完暂存就被杀的 writer:它那批判定下一轮接着用,不用再买一遍。"""
        root = Path(self.tmp.name)
        stale = root / "stale.py"
        stale.write_text(STALE_CHILD.format(repo=str(REPO)), encoding="utf-8")
        go, asked = root / "go2", root / "asked"
        stage = self.cache_dir / "taxonomy_skills.staging.json"

        def spawn(code, *, crash):
            return subprocess.Popen(
                [sys.executable, str(stale), code, str(self.cache_dir),
                 str(self.out_dir), "-", str(go), str(asked), "1" if crash else "0"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        b = spawn("S2", crash=True)                # 判完 n0,暂存刚落盘就没了
        self.assertEqual(b.wait(timeout=120), 99, b.communicate()[1])
        self.assertEqual(T_load(stage), {"n0": "S2"}, "崩掉的那趟没留下进度")
        self.assertFalse((self.cache_dir / "taxonomy_skills.json").exists())

        asked.unlink()
        c = spawn("S3", crash=False)               # 恢复轮:续上,不再问模型
        self.assertEqual(c.wait(timeout=120), 0, c.communicate()[1])
        self.assertFalse(asked.exists(), "恢复轮又问了一遍模型")
        self.assertEqual(T_load(self.cache_dir / "taxonomy_skills.json"),
                         {"n0": "S2"})
        self.assertFalse(stage.exists(), "提交完的那条还留在暂存里")

    # ── 基于陈旧基线的那一份不许发布(判定/建图/渲染都在锁外做完) ────────────
    def _late(self, code, n, *, wait, llm=True, summary="简介", fail="-"):
        late = Path(self.tmp.name) / "late.py"
        if not late.exists():
            late.write_text(LATE_CHILD.format(repo=str(REPO)), encoding="utf-8")
        return subprocess.Popen(
            [sys.executable, str(late), code, str(n), str(self.cache_dir),
             str(self.out_dir), str(self.ready if wait else Path("-")),
             str(self.go2), "1" if llm else "0", summary, fail],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _wait_ready(self, proc):
        for _ in range(400):
            if self.ready.exists() or proc.poll() is not None:
                break
            time.sleep(0.05)
        self.assertTrue(self.ready.exists(), "没走到取锁前的暂停点")

    def test_a_stale_llm_run_never_republishes_over_a_newer_one(self):
        """旧任务判完 30 条停在锁外,新任务发布了 40 条:旧的那份不许再发。

        复审第 6 轮:发布锁只保证两次发布动作不交错,认不出后拿到锁的是基于陈旧
        基线跑出来的——两个进程都退出 0,可页面被刷回 30 节点,新增的 10 条缓存
        一起蒸发。
        """
        stale = self._late("S1", 30, wait=True)
        self._wait_ready(stale)
        cur = self._late("S2", 40, wait=False)
        self.assertEqual(cur.wait(timeout=120), 0, cur.communicate()[1])
        page_now = (self.out_dir / "graph-skills.html").read_bytes()
        cache_now = T_load(self.cache_dir / "taxonomy_skills.json")
        self.assertEqual(len(cache_now), 40)

        self.go2.touch()
        out, err = stale.communicate(timeout=120)
        self.assertEqual(stale.returncode, 0, err)
        self.assertIn("不发布", out, "旧基线上的那一份还是发出去了")
        self.assertEqual((self.out_dir / "graph-skills.html").read_bytes(),
                         page_now, "新发布的页面被旧任务刷回去了")
        self.assertEqual(T_load(self.cache_dir / "taxonomy_skills.json"),
                         cache_now, "新提交的判定被旧任务抹掉了")

    def test_a_stale_no_llm_run_does_not_split_page_and_cache(self):
        """`--no-llm` 那条路的 commit 是空操作,更隐蔽:它只把旧页面重发一遍。"""
        seed = self._late("S1", 30, wait=False)
        self.assertEqual(seed.wait(timeout=120), 0, seed.communicate()[1])

        stale = self._late("-", 30, wait=True, llm=False)
        self._wait_ready(stale)
        cur = self._late("S2", 40, wait=False)
        self.assertEqual(cur.wait(timeout=120), 0, cur.communicate()[1])
        page_now = (self.out_dir / "graph-skills.html").read_bytes()
        cache_now = T_load(self.cache_dir / "taxonomy_skills.json")
        self.assertEqual(len(cache_now), 40)

        self.go2.touch()
        out, err = stale.communicate(timeout=120)
        self.assertEqual(stale.returncode, 0, err)
        page = (self.out_dir / "graph-skills.html")
        self.assertEqual(page.read_bytes(), page_now, "页面被旧任务刷回 30 节点了")
        self.assertEqual(T_load(self.cache_dir / "taxonomy_skills.json"), cache_now)
        on_page = dict(re.findall(r'"q": "(\w+)", "c": "(\w+)"',
                                  page.read_text(encoding="utf-8")))
        self.assertEqual(len(on_page), 40)
        self.assertFalse({q: c for q, c in on_page.items()
                          if cache_now.get(q) != c}, "页面和缓存对不上")

    # ── 基线只比分类缓存不够:缓存一个字节没动,页面照样能被刷回去 ────────────
    def _cache_fp(self) -> str:
        sys.path.insert(0, str(REPO / "scripts" / "wiki"))
        import taxonomy as T
        old, T.CACHE_DIR = T.CACHE_DIR, self.cache_dir
        try:
            return T.cache_fingerprint(T.SKILLS)
        finally:
            T.CACHE_DIR = old

    def test_a_stale_run_does_not_hide_candidates_the_cache_never_saw(self):
        """新候选只在页面上(显示成待归类),分类缓存一条都没变:旧任务照样不许发。

        复审第 7 轮:CAS 只比 `norm → code` 的话,`--no-llm` 撞上新候选、或者默认
        构建全部命中缓存时,新任务发布的页面对缓存指纹毫无影响——旧任务两次读到
        的指纹一模一样,以退出码 0 把 40 节点的页面刷回 30 节点,新出现的 10 个
        候选从正式页面上静默消失。
        """
        seed = self._late("S1", 30, wait=False)      # 30 条判定 + 30 节点页面
        self.assertEqual(seed.wait(timeout=120), 0, seed.communicate()[1])

        stale = self._late("-", 30, wait=True, llm=False)
        self._wait_ready(stale)
        fp0 = self._cache_fp()
        cur = self._late("-", 40, wait=False, llm=False)   # 多出来的 10 条待归类
        self.assertEqual(cur.wait(timeout=120), 0, cur.communicate()[1])
        self.assertEqual(self._cache_fp(), fp0,
                         "这一轮的前提没了:新任务本来就不该动分类缓存")
        page_now = (self.out_dir / "graph-skills.html").read_bytes()

        self.go2.touch()
        out, err = stale.communicate(timeout=120)
        self.assertEqual(stale.returncode, 0, err)
        self.assertIn("不发布", out, "缓存没变,旧任务就把新页面盖掉了")
        page = self.out_dir / "graph-skills.html"
        self.assertEqual(page.read_bytes(), page_now, "页面被旧任务刷回去了")
        on_page = set(re.findall(r'"q": "(\w+)"', page.read_text(encoding="utf-8")))
        missing = [f"n{i}" for i in range(30, 40) if f"n{i}" not in on_page]
        self.assertFalse(missing, f"新候选从正式页面上消失了:{missing}")

    def test_a_stale_run_does_not_roll_back_a_cache_hit_only_page(self):
        """全部命中缓存的日常构建同样有这个问题:刷回去的是简介/热度这些可见内容。

        两个任务判的是同一条 n0(缓存里已经有了,谁都不会再问模型),新任务手上
        的是新版简介。缓存自始至终一条 `n0 → S1`,只有页面变了。
        """
        seed = self._late("S1", 1, wait=False, summary="旧简介")
        self.assertEqual(seed.wait(timeout=120), 0, seed.communicate()[1])

        stale = self._late("S1", 1, wait=True, summary="旧简介")
        self._wait_ready(stale)
        fp0 = self._cache_fp()
        cur = self._late("S1", 1, wait=False, summary="新简介")
        self.assertEqual(cur.wait(timeout=120), 0, cur.communicate()[1])
        page = self.out_dir / "graph-skills.html"
        self.assertEqual(self._cache_fp(), fp0, "这一轮的前提没了:缓存不该变")
        self.assertIn("新简介", page.read_text(encoding="utf-8"))

        self.go2.touch()
        out, err = stale.communicate(timeout=120)
        self.assertEqual(stale.returncode, 0, err)
        self.assertIn("不发布", out, "旧快照又被发出去了")
        text = page.read_text(encoding="utf-8")
        self.assertIn("新简介", text, "正式页面被刷回旧简介了")
        self.assertNotIn("旧简介", text)

    # ── 被 CAS 拒绝时,自己判出来的那些必须还在盘上 ──────────────────────────
    def test_a_rejected_stale_run_keeps_its_own_verdicts(self):
        """赢的那个把共享暂存整份覆盖、发布完又删掉;输的那个不能假定自己写过的
        还在盘上。

        复审第 7 轮:所有进程共用一个 `taxonomy_skills.staging.json`,分类阶段是
        整份覆盖式地写。CAS 拒绝发布时那句「判出来的判定留在暂存里,一条都不丢」
        因此是假的——`stale_only` 既不在正式缓存也不在暂存,模型钱白花,日志还说
        留下了。
        """
        root = Path(self.tmp.name)
        keep = root / "keep.py"
        keep.write_text(KEEP_CHILD.format(repo=str(REPO)), encoding="utf-8")
        s_ready, s_go = root / "s_ready", root / "s_go"
        c_hold, c_go = root / "c_hold", root / "c_go"

        def spawn(norm, code, ready, go, hold, hold_go):
            return subprocess.Popen(
                [sys.executable, str(keep), norm, code, str(self.cache_dir),
                 str(self.out_dir), str(ready), str(go), str(hold), str(hold_go)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # ① 赢家读完空基线,卡在模型调用里
        cur = spawn("current_only", "S2", Path("-"), Path("-"), c_hold, c_go)
        for _ in range(400):
            if c_hold.exists() or cur.poll() is not None:
                break
            time.sleep(0.05)
        self.assertTrue(c_hold.exists(), "赢家没走到等待点")
        # ② 输家也读空基线,判出唯一的 stale_only、写完暂存,停在取锁之前
        stale = spawn("stale_only", "S1", s_ready, s_go, Path("-"), Path("-"))
        for _ in range(400):
            if s_ready.exists() or stale.poll() is not None:
                break
            time.sleep(0.05)
        self.assertTrue(s_ready.exists(), "输家没走到取锁前的暂停点")
        stage = self.cache_dir / "taxonomy_skills.staging.json"
        self.assertEqual(T_load(stage), {"stale_only": "S1"})
        # ③ 赢家醒来:它只把**自己刚提交的那些**从共享暂存里摘掉,别人的进度留着
        c_go.touch()
        self.assertEqual(cur.wait(timeout=120), 0, cur.communicate()[1])
        self.assertEqual(T_load(self.cache_dir / "taxonomy_skills.json"),
                         {"current_only": "S2"})
        self.assertEqual(T_load(stage) if stage.exists() else {},
                         {"stale_only": "S1"},
                         "赢家提交时把别人还没提交的进度一起删了")
        # ④ 输家醒来:CAS 拒绝发布,但它那条判定不能就这么没了
        s_go.touch()
        out, err = stale.communicate(timeout=120)
        self.assertEqual(stale.returncode, 0, err)
        self.assertIn("不发布", out)
        cache = T_load(self.cache_dir / "taxonomy_skills.json")
        left = T_load(stage) if stage.exists() else {}
        self.assertEqual(cache, {"current_only": "S2"}, "赢家的结论被动了")
        self.assertEqual(left.get("stale_only"), "S1",
                         f"被拒的那一轮把自己判的弄丢了(暂存 {left});"
                         f"下一轮只能再花一次模型钱")
        self.assertNotIn("current_only", left,
                         "正式缓存已经有的 key 不该再并回暂存")

    def test_a_third_writer_does_not_wipe_the_restashed_verdict(self):
        """并回暂存的那条,不许被「基线仍然合法」的第三个 writer 盖掉再删掉。

        复审第 8 轮:`_restash` 自己是安全的读-改-写,可普通分类阶段的落盘仍是
        **整份覆盖**、而且不持任何锁,提交时又是**整份删除**。于是一个在 A 发布
        之后才开工、正式基线完全合法的 D,能把 B 刚并回去的唯一判定盖掉、发布
        成功后再把整个暂存删掉——B 明明返回成功并打印「判定已并回暂存」,那条判定
        最后既不在正式缓存也不在暂存,下一轮还得再买一次模型。
        """
        root = Path(self.tmp.name)
        keep = root / "keep.py"
        keep.write_text(KEEP_CHILD.format(repo=str(REPO)), encoding="utf-8")
        stage = self.cache_dir / "taxonomy_skills.staging.json"
        cache = self.cache_dir / "taxonomy_skills.json"
        b_ready, b_go = root / "b_ready", root / "b_go"
        a_hold, a_go = root / "a_hold", root / "a_go"
        d_hold, d_go = root / "d_hold", root / "d_go"

        def spawn(norm, code, ready, go, hold, hold_go):
            return subprocess.Popen(
                [sys.executable, str(keep), norm, code, str(self.cache_dir),
                 str(self.out_dir), str(ready), str(go), str(hold), str(hold_go)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def wait_for(flag, proc, what):
            for _ in range(400):
                if flag.exists() or proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(flag.exists(), f"{what} 没走到等待点")

        # ① A 读空基线,卡在模型里;B 也读空基线,判完写进暂存、停在取锁之前
        a = spawn("winner_a", "S1", Path("-"), Path("-"), a_hold, a_go)
        wait_for(a_hold, a, "A")
        b = spawn("restashed_b", "S2", b_ready, b_go, Path("-"), Path("-"))
        wait_for(b_ready, b, "B")
        self.assertEqual(T_load(stage), {"restashed_b": "S2"})
        # ② A 恢复:发布 winner_a
        a_go.touch()
        self.assertEqual(a.wait(timeout=120), 0, a.communicate()[1])
        self.assertEqual(T_load(cache), {"winner_a": "S1"})
        # ③ D 在 A 发布之后开工(正式基线对它完全合法),卡在模型里
        d = spawn("later_d", "S3", Path("-"), Path("-"), d_hold, d_go)
        wait_for(d_hold, d, "D")
        # ④ B 恢复:被 CAS 拒绝,把自己那条并回暂存
        b_go.touch()
        b_out, b_err = b.communicate(timeout=120)
        self.assertEqual(b.returncode, 0, b_err)
        self.assertIn("不发布", b_out)
        self.assertEqual(T_load(stage).get("restashed_b"), "S2", "并回暂存没成")
        # ⑤ D 恢复:它的发布是合法的,但不能顺手把 B 的进度抹掉
        d_go.touch()
        self.assertEqual(d.wait(timeout=120), 0, d.communicate()[1])
        final_cache = T_load(cache)
        final_stage = T_load(stage) if stage.exists() else {}
        self.assertEqual(final_cache.get("winner_a"), "S1")
        self.assertEqual(final_cache.get("later_d"), "S3")
        # B 那条要么被 D 顺路提交进了正式缓存,要么还在暂存里等下一轮——就是不能没了
        self.assertEqual(final_cache.get("restashed_b",
                                         final_stage.get("restashed_b")), "S2",
                         f"B 的唯一判定被第三个 writer 弄丢了(暂存 {final_stage},"
                         f"缓存 {final_cache});下一轮只能再买一次模型")

    # ── 内容一样不等于没人发布过(ABA) ──────────────────────────────────────
    def test_a_stale_run_detects_a_publish_cycle_that_returns_to_the_same_bytes(self):
        """产物 A→B→A 转了一圈:内容和开工时逐字相同,但发布序列已经走了两步。

        复审第 8 轮:内容签名回答的是「东西一不一样」,回答不了「发布序列有没有
        往前走」。定时任务、手动重跑和慢模型任务重叠时,只要页面内容发生回退再
        恢复(或者同样输入重发一次),旧任务就能绕过 CAS 静默覆盖——它还打印
        正常的发布结果、退出码 0。
        """
        seed = self._late("S1", 1, wait=False, summary="BASE_A")
        self.assertEqual(seed.wait(timeout=120), 0, seed.communicate()[1])
        page = self.out_dir / "graph-skills.html"
        base_a = page.read_bytes()

        stale = self._late("S1", 1, wait=True, summary="STALE_SNAPSHOT")
        self._wait_ready(stale)                      # 以 A 为基线,停在取锁之前

        b = self._late("S1", 1, wait=False, summary="NEW_B")   # A → B
        self.assertEqual(b.wait(timeout=120), 0, b.communicate()[1])
        self.assertNotEqual(page.read_bytes(), base_a)
        a2 = self._late("S1", 1, wait=False, summary="BASE_A")  # B → A(逐字相同)
        self.assertEqual(a2.wait(timeout=120), 0, a2.communicate()[1])
        self.assertEqual(page.read_bytes(), base_a,
                         "这一轮的前提没了:第二次发布该把页面还原成逐字相同的 A")

        self.go2.touch()
        out, err = stale.communicate(timeout=120)
        self.assertEqual(stale.returncode, 0, err)
        self.assertIn("不发布", out, "期间发布过两次,旧任务却没认出来(ABA)")
        self.assertEqual(page.read_bytes(), base_a, "旧快照盖掉了最后完成的那一版")
        self.assertNotIn("STALE_SNAPSHOT", page.read_text(encoding="utf-8"))

    def test_a_writer_killed_right_after_the_page_still_leaves_a_trace(self):
        """进程死在「页面已经可见、序号还没推进」那个窗口里 → ABA 又回来了。

        复审第 9 轮:序号原来是**发布完才推进**的,于是普通的 kill、宿主机重启
        就能留下「正式产物已经变了、序号没变」。这里让两个 writer 都死在页面刚
        落盘的那一刻,页面照样走完 A→B→A,签名逐字回到开工那一版——旧任务认不出
        期间有两次可见的发布,静默覆盖最终页面。序号改成**动正式产物之前就推进**
        之后,这两次都留下了痕迹。
        """
        seed = self._late("S1", 1, wait=False, summary="BASE_A")
        self.assertEqual(seed.wait(timeout=120), 0, seed.communicate()[1])
        page = self.out_dir / "graph-skills.html"
        base_a = page.read_bytes()

        stale = self._late("S1", 1, wait=True, summary="STALE_SNAPSHOT")
        self._wait_ready(stale)

        b = self._late("S1", 1, wait=False, summary="NEW_B", fail="crash_after_page")
        self.assertEqual(b.wait(timeout=120), 99, b.communicate()[1])
        self.assertNotEqual(page.read_bytes(), base_a, "B 的页面没落盘,交错没构造成")
        a2 = self._late("S1", 1, wait=False, summary="BASE_A", fail="crash_after_page")
        self.assertEqual(a2.wait(timeout=120), 99, a2.communicate()[1])
        self.assertEqual(page.read_bytes(), base_a,
                         "这一轮的前提没了:页面该被还原成逐字相同的 A")

        self.go2.touch()
        out, err = stale.communicate(timeout=120)
        self.assertEqual(stale.returncode, 0, err)
        self.assertIn("不发布", out,
                      "期间有两次可见的发布,旧任务却没认出来(序号推进得太晚)")
        self.assertEqual(page.read_bytes(), base_a)
        self.assertNotIn("STALE_SNAPSHOT", page.read_text(encoding="utf-8"))

    def test_a_generation_write_failure_does_not_touch_the_artifacts(self):
        """序号写不进去就 fail-closed:这一轮连页面都不换,报错退出。

        复审第 9 轮:原来那句「这一次发布本身是成功的」只打了行告警就返回 0——
        sidecar 的一次短暂 I/O 故障,就把并发保护静默降回上一轮已经证明存在 ABA
        的内容签名。fail-closed 之后最坏也只是这一轮不发,产物一个字节没动。
        """
        seed = self._late("S1", 1, wait=False, summary="BASE_A")
        self.assertEqual(seed.wait(timeout=120), 0, seed.communicate()[1])
        page = self.out_dir / "graph-skills.html"
        cache = self.cache_dir / "taxonomy_skills.json"
        base_a, base_cache = page.read_bytes(), cache.read_bytes()
        gen = (self.cache_dir / "taxonomy_skills.gen").read_text(encoding="utf-8")

        b = self._late("S1", 2, wait=False, summary="NEW_B", fail="gen_oserror")
        out, err = b.communicate(timeout=120)
        self.assertNotEqual(b.returncode, 0, "序号写失败还当成功发布返回了")
        self.assertIn("序号", out + err)
        self.assertEqual(page.read_bytes(), base_a, "fail-closed 没兜住:页面被换了")
        self.assertEqual(cache.read_bytes(), base_cache, "缓存被换了")
        self.assertEqual((self.cache_dir / "taxonomy_skills.gen")
                         .read_text(encoding="utf-8"), gen)


def T_load(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v["c"] for k, v in raw["items"].items()}


if __name__ == "__main__":
    unittest.main()
