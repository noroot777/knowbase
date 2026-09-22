# -*- coding: utf-8 -*-
"""暂存里的东西**都是没验过的半成品**,续用之前得按原批次各验各的。

质量判定(「这一趟是不是等于什么也没判出来」)跑在 `finish()` 里,而判定结果
在那之前就已经落到暂存文件上了。中间这段窗口里进程被杀、或者另一个 writer
抢先读走,这批本该判「不作数」的结果就留在盘上——下一轮把它和更多正常结果合
在一起算占比,20 条全废的被 180 条正常的稀释成 10%,当场随整份缓存提交,再下
一轮全命中缓存、不再问模型,坏结果永久固化(2026-09-22 复审第 4 轮发现)。

所以暂存记下「哪几条是同一趟判出来的」(`taxonomy.load_runs`),恢复时按批次
各验各的,这一趟自己判的那批也只跟自己比。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))

import build_catgraph as B  # noqa: E402
import taxonomy as T  # noqa: E402

from tests.test_catgraph import _group  # noqa: E402

# 子进程:判完 20 条全 X,暂存刚落盘就被「杀掉」(还没跑到 finish() 的质量判定)
CRASH_CHILD = '''\
import json, os, re, sys
from pathlib import Path
sys.path.insert(0, {repo!r})
sys.path.insert(0, {wiki!r})
import build_catgraph as B, taxonomy as T
from tests.test_catgraph import _group
T.CACHE_DIR = Path(sys.argv[1])

def fake(prompt, model, **kw):
    rows = re.findall(r"^(\\d+)\\. (\\S+) ::", prompt, re.M)
    return json.dumps([{{"id": int(i), "c": "X"}} for i, _nm in rows])
T._llm = fake

real = T.save_cache
def hooked(taxo, codes, names, path=None, **kw):
    real(taxo, codes, names, path, **kw)
    if path is not None:                      # 暂存刚落盘 → 进程没了
        sys.stdout.flush()
        os._exit(99)
T.save_cache = hooked

gs = [_group(f"n{{i}}", f"名{{i}}", topics=["skill工具"]) for i in range(20)]
B.build_one("skills", gs, use_llm=True, max_new=None, model="stub",
            workers=1, out_dir=Path(sys.argv[2]))
'''


class StagingIdentityTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        old = T.CACHE_DIR
        T.CACHE_DIR = root / "data"
        self.addCleanup(lambda: setattr(T, "CACHE_DIR", old))
        self.out_dir = root / "vault"
        self.out_dir.mkdir()
        self.page = self.out_dir / B.VIEWS["skills"]["out"]
        self.stage = T.staging_path(T.SKILLS)
        self.judged = 0

    def _stub(self, code: str):
        def fake(prompt, model, **kw):
            rows = re.findall(r"^(\d+)\. (\S+) ::", prompt, re.M)
            self.judged += len(rows)
            return json.dumps([{"id": int(i), "c": code} for i, _nm in rows])

        T._llm, old = fake, T._llm
        self.addCleanup(lambda: setattr(T, "_llm", old))

    def _build(self, n, **kw):
        gs = [_group(f"n{i}", f"名{i}", topics=["skill工具"]) for i in range(n)]
        return B.build_one("skills", gs, use_llm=True, max_new=None,
                           model="stub", workers=1, out_dir=self.out_dir, **kw)

    def _seed_staging(self, codes: dict[str, str], runs=None):
        """直接写一份暂存(模拟上一趟留下的),不经过 classify。"""
        body = {"sig": T.SKILLS.sig(),
                "items": {k: {"c": v, "n": k} for k, v in codes.items()}}
        if runs is not None:
            body["runs"] = runs
        T.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.stage.write_text(json.dumps(body, ensure_ascii=False),
                              encoding="utf-8")

    def test_crashed_bad_staging_is_not_laundered_by_the_next_run(self):
        """进程在质量判定前被杀留下 20 条全 X 的暂存,下一轮不许把它洗白。"""
        child = Path(self.tmp.name) / "crash.py"
        child.write_text(
            CRASH_CHILD.format(repo=str(REPO), wiki=str(REPO / "scripts" / "wiki")),
            encoding="utf-8")
        r = subprocess.run([sys.executable, str(child), str(T.CACHE_DIR),
                            str(self.out_dir)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 99, r.stderr)        # 确实是「被杀」走的
        staged = T.load_raw(T.SKILLS, self.stage)[0]
        self.assertEqual(sorted(staged.values()), ["X"] * 20, "没留下坏暂存")
        self.assertFalse(T.cache_path(T.SKILLS).exists())
        self.assertFalse(self.page.exists())

        self._stub("S1")                                    # 这一轮模型正常
        self._build(200)
        cache = json.loads(T.cache_path(T.SKILLS).read_text("utf-8"))["items"]
        self.assertEqual(len(cache), 200)
        self.assertEqual([k for k, v in cache.items() if v["c"] == "X"], [],
                         "崩溃前那批没验过的 X 被稀释着提交进正式缓存了")
        self.assertEqual(self.judged, 200, "那 20 条没被重判,直接当判定用了")
        self.assertEqual(B.P.page_stats(self.page),
                         {"nodes": 200, "classified": 200})

    def test_a_good_resumed_batch_does_not_dilute_a_bad_new_batch(self):
        """续上的 180 条正常判定,不能把这一趟全废的 20 条稀释过关。"""
        self._seed_staging({f"n{i}": "S1" for i in range(180)},
                           runs=[[f"n{i}" for i in range(180)]])
        self._stub(T.SKILLS.fallback)                       # 新增 20 条全废
        with self.assertRaises(SystemExit) as cm:
            self._build(200)
        self.assertIn("不作数", str(cm.exception))
        self.assertFalse(self.page.exists())
        self.assertFalse(T.cache_path(T.SKILLS).exists())
        # 验过的那 180 条是别的趟的进度,不该被这一趟的失败连坐
        left = T.load_raw(T.SKILLS, self.stage)[0]
        self.assertEqual(len(left), 180)
        self.assertEqual(set(left.values()), {"S1"})

    def test_a_good_resumed_batch_is_still_resumed(self):
        """验过的暂存照常续用:上面那道闸不能把正常的续跑一起拦了。"""
        self._seed_staging({f"n{i}": "S1" for i in range(180)},
                           runs=[[f"n{i}" for i in range(180)]])
        self._stub("S2")
        self._build(200)
        self.assertEqual(self.judged, 20, "已经判过的那 180 条又问了一遍模型")
        self.assertEqual(B.P.page_stats(self.page),
                         {"nodes": 200, "classified": 200})

    def test_old_staging_without_batch_marks_is_verified_as_one_batch(self):
        """老格式暂存(没记批次)整份当一批验——读不到分组不等于验过了。"""
        self._seed_staging({f"n{i}": "X" for i in range(20)})   # 没有 runs
        self._stub("S1")
        self._build(200)
        cache = json.loads(T.cache_path(T.SKILLS).read_text("utf-8"))["items"]
        self.assertEqual([k for k, v in cache.items() if v["c"] == "X"], [])
        self.assertEqual(self.judged, 200)

    def test_allow_shrink_still_takes_the_staged_batch(self):
        """维护者确认这就是预期结果时,--allow-shrink 照样放行续上的那批。"""
        self._seed_staging({f"n{i}": "X" for i in range(20)},
                           runs=[[f"n{i}" for i in range(20)]])
        self._stub("S1")
        self._build(200, allow_shrink=True)
        cache = json.loads(T.cache_path(T.SKILLS).read_text("utf-8"))["items"]
        self.assertEqual(sum(1 for v in cache.values() if v["c"] == "X"), 20)
        self.assertEqual(self.judged, 180)

    # ── 暂存不记「基于哪一版正式缓存」,同名 key 一律以正式缓存为准 ──────────
    def test_real_legacy_staging_carries_the_official_baseline(self):
        """上一版写暂存落的是**整份 codes**(含正式基线),不是这一趟新判的那些。

        复审第 5 轮:把这种文件「整份当一批验」,正好把第 3 轮那个错分母请回来
        ——20 条全废的新增混进 100 条正式基线,占比 16.7%,照样提交。基线得先按
        正式缓存摘掉,只拿剩下的当上一趟的批次验。
        """
        self._stub("S1")
        self._build(100)                       # 正式缓存:100 条正常
        good_page = self.page.read_bytes()
        good_cache = T.cache_path(T.SKILLS).read_bytes()

        self._seed_staging({**{f"n{i}": "S1" for i in range(100)},       # 正式基线
                            **{f"n{i}": T.SKILLS.fallback                # 本轮新增
                               for i in range(100, 120)}})               # 没有 runs
        self._stub(T.SKILLS.fallback)          # 重判出来还是全废
        self.judged = 0
        with self.assertRaises(SystemExit) as cm:
            self._build(120)
        self.assertIn("不作数", str(cm.exception))
        self.assertEqual(self.judged, 20, "摘掉基线后该重判的只有新增那 20 条")
        self.assertEqual(self.page.read_bytes(), good_page)
        self.assertEqual(T.cache_path(T.SKILLS).read_bytes(), good_cache)

    def test_stale_staging_never_overrides_the_official_cache(self):
        """旧 writer 留在暂存里的同名判定,不许盖掉已经提交的正式结论。"""
        self._stub("S1")
        self._build(1)                         # 正式缓存与页面:n0 = S1
        self._seed_staging({"n0": "S2"}, runs=[["n0"]])   # 旧 writer 崩在盘上的

        self._stub("S3")                       # 这一轮不该问模型
        self.judged = 0
        self._build(1)
        self.assertEqual(self.judged, 0)
        cache = json.loads(T.cache_path(T.SKILLS).read_text("utf-8"))["items"]
        self.assertEqual(cache["n0"]["c"], "S1")
        page = self.page.read_text(encoding="utf-8")
        self.assertIn('"q": "n0", "c": "S1"', page, "页面用了暂存里的旧结论")
        self.assertFalse(self.stage.exists(), "作废的暂存留在盘上了")

    # ── 不发布时把自己判的并回暂存(_restash) ──────────────────────────────
    def test_restash_merges_instead_of_overwriting(self):
        """并回暂存是读-改-写:盘上别人留下的批次原样保住,自己的单独记一批。"""
        self._seed_staging({"a": "S1"}, runs=[["a"]])
        T._restash(T.SKILLS, {"b": "S2"}, {"b": "名B"})
        self.assertEqual(T.load_raw(T.SKILLS, self.stage)[0],
                         {"a": "S1", "b": "S2"})
        self.assertEqual([sorted(g) for g in
                          T.load_runs(self.stage, {"a": "S1", "b": "S2"})],
                         [["a"], ["b"]], "两趟判的被揉成一批了(质量判定会互相稀释)")

    def test_restash_never_shadows_the_official_cache(self):
        """正式缓存已经有的 key 一律不并回去——同名以正式缓存为准(复审第 5 轮)。"""
        self._stub("S1")
        self._build(1)                                  # 正式缓存:n0 = S1
        T._restash(T.SKILLS, {"n0": "S9", "n1": "S2"}, {})
        self.assertEqual(T.load_raw(T.SKILLS, self.stage)[0], {"n1": "S2"})

    def test_restash_on_an_empty_disk_writes_a_fresh_staging(self):
        """赢家把共享暂存删掉之后,输家并回去的就是一份全新的暂存。"""
        self.assertFalse(self.stage.exists())
        T._restash(T.SKILLS, {"x": "S1"}, {"x": "名X"})
        self.assertEqual(T.load_raw(T.SKILLS, self.stage)[0], {"x": "S1"})
        self.assertEqual(T.load_runs(self.stage, {"x": "S1"}), [["x"]])

    def test_restash_write_failure_only_warns(self):
        """并不回去顶多是下一轮重判一遍,不该让「不发布」这条安全路径异常退出。"""
        real = T.save_cache

        def denied(taxo, codes, names, path=None, **kw):
            if path is not None:
                raise OSError("simulated staging write denied")
            return real(taxo, codes, names, path, **kw)

        T.save_cache = denied
        self.addCleanup(lambda: setattr(T, "save_cache", real))
        T._restash(T.SKILLS, {"x": "S1"}, {})           # 不抛就是通过
        self.assertFalse(self.stage.exists())

    # ── 共享暂存:各人只改自己那一批 ────────────────────────────────────────
    def test_commit_keeps_other_writers_batches(self):
        """提交完只摘掉**自己刚提交的那些** key,不是把整个暂存文件删掉。

        复审第 8 轮:暂存是所有 writer 共用的,整份删会把别人还没提交的进度一起
        带走——被 CAS 拒绝的那一轮刚把自己那条并回去,转头就被下一个发布成功的
        writer 删掉了。这里让「别人」在我们读完暂存**之后**才并回去(页面刚落盘、
        缓存还没提交那一刻),我们这一趟根本没见过它。
        """
        real = B._atomic_write

        def hooked(out, data):
            real(out, data)
            T._restash(T.SKILLS, {"other": "S9"}, {"other": "别人"}, "otherrun")

        B._atomic_write = hooked
        self.addCleanup(lambda: setattr(B, "_atomic_write", real))
        self._stub("S1")
        self._build(1)                                  # 我们提交 n0
        self.assertEqual(T_load(T.cache_path(T.SKILLS)), {"n0": "S1"})
        self.assertEqual(T_load(self.stage), {"other": "S9"},
                         "提交时把别人还没提交的进度一起删了")

    def test_discard_only_drops_its_own_batch(self):
        """「这一趟不作数」也只摘自己那一批,别人的留着。"""
        T._restash(T.SKILLS, {"other": "S9"}, {"other": "别人"})
        self._stub(T.SKILLS.fallback)                   # 这一趟 200 条全废
        with self.assertRaises(SystemExit):
            self._build(200)
        self.assertEqual(T_load(self.stage), {"other": "S9"},
                         "作废自己这一批时把别人的也扫了")

    def test_a_second_stash_replaces_its_own_batch_instead_of_splitting_it(self):
        """同一趟多次落盘写的是同一个批次身份,不该把自己拆成好几批。"""
        T._edit_stage(T.SKILLS, put={"a": "S1"}, put_id="run1", names={})
        T._edit_stage(T.SKILLS, put={"a": "S1", "b": "S1"}, put_id="run1", names={})
        self.assertEqual(T_load(self.stage), {"a": "S1", "b": "S1"})
        self.assertEqual(T.load_runs(self.stage, {"a": "S1", "b": "S1"}),
                         [["a", "b"]])

    # ── 发布序号:内容一样不等于没人发布过 ──────────────────────────────────
    def test_publication_generation_moves_on_every_publish(self):
        """两次发布同样的内容,序号也得往前走(ABA 只能靠它认出来)。"""
        self._stub("S1")
        self._build(1)
        first = T.read_gen(T.SKILLS)
        page = self.page.read_bytes()
        self._build(1)                                  # 逐字相同的一次重发
        self.assertEqual(self.page.read_bytes(), page)
        self.assertNotEqual(T.read_gen(T.SKILLS), first, "发布序号没动")
        self.assertTrue(first.split("-")[0].isdigit())

    def test_a_failed_publish_does_not_move_the_generation(self):
        """被验收拦下(压根没走到发布动作)时,序号不动。"""
        self._stub("S1")
        self._build(200)
        before = T.read_gen(T.SKILLS)
        self._stub(T.SKILLS.fallback)                   # 新增那 200 条全废
        with self.assertRaises(SystemExit):
            self._build(400)
        self.assertEqual(T.read_gen(T.SKILLS), before)

    def test_a_generation_write_failure_is_fail_closed(self):
        """序号写不进去 → 这一轮连页面都不换(不是打行告警照常发)。"""
        self._stub("S1")
        self._build(1)
        page, cache = self.page.read_bytes(), T.cache_path(T.SKILLS).read_bytes()
        real = T.bump_gen

        def boom(taxo):
            raise OSError("simulated gen sidecar write denied")

        T.bump_gen = boom
        self.addCleanup(lambda: setattr(T, "bump_gen", real))
        self._stub("S1")
        with self.assertRaises(SystemExit) as cm:
            self._build(2)
        self.assertIn("序号", str(cm.exception))
        self.assertEqual(self.page.read_bytes(), page, "fail-closed 没兜住")
        self.assertEqual(T.cache_path(T.SKILLS).read_bytes(), cache)

    def test_a_later_failure_leaves_the_generation_bumped_on_purpose(self):
        """序号先推进:后面页面写失败时它多走一步——**安全的假阳性**。

        别的任务看到基线变了会多拒一轮(退出码 0,下一轮自己就好了),而正式产物
        一个字节都没动。反过来「发布完才推进」留下的是危险的假阴性:产物变了、
        序号没变,A→B→A 之后旧任务认不出期间发生过发布(复审第 9 轮发现)。
        """
        self._stub("S1")
        self._build(1)
        before, page = T.read_gen(T.SKILLS), self.page.read_bytes()
        real = B._atomic_write

        def boom(out, data):
            raise OSError("simulated disk full")

        B._atomic_write = boom
        self.addCleanup(lambda: setattr(B, "_atomic_write", real))
        self._stub("S1")
        with self.assertRaises(OSError):
            self._build(2)
        self.assertNotEqual(T.read_gen(T.SKILLS), before)
        self.assertEqual(self.page.read_bytes(), page, "正式页面不该被动过")
        self.assertEqual(T_load(T.cache_path(T.SKILLS)), {"n0": "S1"})


def T_load(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v["c"] for k, v in raw["items"].items()}


if __name__ == "__main__":
    unittest.main()
