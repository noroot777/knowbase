# -*- coding: utf-8 -*-
"""发布这一步:不作数的判定不许发出去,页面落盘必须是原子的。

两条都是「没有旧页面可保」时暴露出来的(2026-09-22 第 1 轮审查发现):

1. 拦截原来写成 `not res.complete and out.exists()`——首次生成、页面被人删了或
   产物丢了的时候,一份已经判定「不作数」的结果照样会被写成 0 节点的新页面,
   还以退出码 0 收场,`wiki_daily.run_step()` 看到的是成功,管道面板全绿。
   没有旧页面可保 ≠ 坏结果可以发。
2. 页面是直接 `write_text` 到正式产物上的。磁盘满、进程被杀、写到一半抛异常,
   都会先把好页面截成半截再报错——缓存那头的两阶段提交完好,kb-web 却已经在
   拿一份坏 HTML 对外服务。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))

import build_catgraph as B  # noqa: E402
import taxonomy as T  # noqa: E402

from tests.test_catgraph import _group  # noqa: E402


class PublishGateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = T.CACHE_DIR
        T.CACHE_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(T, "CACHE_DIR", old))
        self.out_dir = Path(self.tmp.name) / "vault"
        self.out_dir.mkdir()
        self.page = self.out_dir / B.VIEWS["skills"]["out"]
        self.groups = [_group(f"n{i}", f"名{i}", topics=["skill工具"])
                       for i in range(3)]

    def _stub(self, code: str):
        """模型桩:每条都回同一个 code。"""
        def fake(prompt, model, **kw):
            rows = re.findall(r"^(\d+)\. (\S+) ::", prompt, re.M)
            return json.dumps([{"id": int(i), "c": code} for i, _nm in rows])

        T._llm, old = fake, T._llm
        self.addCleanup(lambda: setattr(T, "_llm", old))

    def _build(self, **kw):
        return B.build_one("skills", self.groups, use_llm=True, max_new=None,
                           model="stub", workers=1, out_dir=self.out_dir, **kw)

    # ── ① 不作数的判定不许发 ────────────────────────────────────────────────
    def test_void_verdict_never_publishes_a_first_page(self):
        """每条都判成 X(不是 skill)= 图上一个节点都不剩,没有旧页也不许发。

        条数少(3 条)时归类层不按「void 占比」判坏——skill 候选里本来就有近
        四分之一是 X;但问过模型、最后一个节点都没剩,这一页就是废的,页面这道
        验收即使没有旧图也要拦。
        """
        self._stub("X")
        with self.assertRaises(SystemExit) as cm:
            self._build()
        self.assertIn("0 个节点", str(cm.exception))
        self.assertFalse(self.page.exists(), "不作数的判定被写成新页面了")
        self.assertFalse(T.cache_path(T.SKILLS).exists())
        self.assertFalse(T.staging_path(T.SKILLS).exists())

    def test_all_fallback_verdict_never_publishes_a_first_page(self):
        """全落「待归类」也是同一回事(fallback 是合法 code,更容易漏)。"""
        self._stub(T.SKILLS.fallback)
        with self.assertRaises(SystemExit):
            self._build()
        self.assertFalse(self.page.exists())

    def test_good_first_run_does_publish(self):
        """正常判定当然要发出来:上面那道闸不能把首次生成一起挡了。"""
        self._stub("S1")
        self._build()
        self.assertTrue(self.page.exists())
        self.assertEqual(B.P.page_stats(self.page),
                         {"nodes": 3, "classified": 3})

    # ── ①b 首发也有质量下限:不是只有 100% 待归类才算坏 ──────────────────────
    def test_one_good_plus_a_hundred_fallback_never_publishes(self):
        """101 条里 1 条正常、100 条待归类:首次生成同样不许发、不许进缓存。

        复审第 2 轮:`complete` 原来只看「是不是**全部**落在 void 上」,有一条
        正常 code 就算判完。于是这份 99% 待归类的结果被发成正式页面、100 条
        fallback 进正式缓存,第二次运行全命中缓存、再也不问模型,自己走不出来。
        """
        many = [_group(f"n{i}", f"名{i}", topics=["skill工具"]) for i in range(101)]
        self.groups = many
        good_one = "名0"

        def fake(prompt, model, **kw):
            rows = re.findall(r"^(\d+)\. (\S+) ::", prompt, re.M)
            return json.dumps([{"id": int(i),
                                "c": "S1" if nm == good_one else T.SKILLS.fallback}
                               for i, nm in rows])

        T._llm, old = fake, T._llm
        self.addCleanup(lambda: setattr(T, "_llm", old))

        for run in (1, 2):                       # 第二次仍要问模型,不能卡死
            with self.subTest(第几次=run):
                self.calls = 0
                with self.assertRaises(SystemExit) as cm:
                    self._build()
                self.assertIn("不作数", str(cm.exception))
                self.assertFalse(self.page.exists())
                self.assertFalse(T.cache_path(T.SKILLS).exists())
                self.assertFalse(T.staging_path(T.SKILLS).exists())

        # 模型恢复正常之后自己走出来
        self._stub("S1")
        self._build()
        self.assertEqual(B.P.page_stats(self.page)["classified"], 101)

    def test_a_whole_bad_increment_is_rejected_even_with_a_healthy_cache(self):
        """已有 100 条正常缓存,这天新增的 20 条全部退化:同样不许入库。

        复审第 3 轮:void 占比原来拿**整份缓存**当分母,缓存越大越掩盖问题——
        20/120 = 16.7% 远够不着阈值,于是整批失败的增量照样提交,下一轮全命中
        缓存、不再问模型,坏结果永久固化。分母得是这一趟新拿到的判定。
        """
        base = [_group(f"n{i}", f"名{i}", topics=["skill工具"]) for i in range(100)]
        self.groups = base
        self._stub("S1")
        self._build()                                   # 100 条正常,先有一版
        good_page = self.page.read_bytes()
        good_cache = T.cache_path(T.SKILLS).read_bytes()

        self.groups = base + [_group(f"m{i}", f"新{i}", topics=["skill工具"])
                              for i in range(20)]
        for run in (1, 2):                              # 第二次仍要问模型
            with self.subTest(第几次=run):
                self._stub(T.SKILLS.fallback)           # 新增那 20 条全废
                with self.assertRaises(SystemExit) as cm:
                    self._build()
                self.assertIn("不作数", str(cm.exception))
                self.assertEqual(self.page.read_bytes(), good_page)
                self.assertEqual(T.cache_path(T.SKILLS).read_bytes(), good_cache)
                self.assertFalse(T.staging_path(T.SKILLS).exists())

        self._stub("S2")                                # 模型恢复 → 自己走出来
        self._build()
        self.assertEqual(B.P.page_stats(self.page),
                         {"nodes": 120, "classified": 120})

    def test_a_few_fallbacks_are_normal(self):
        """条数少的时候连 100% 也不算坏:新来两三条确实判不出来,不该拦住整张图。"""
        self._stub("S1")
        self._build()                             # 3 条正常,先有一版
        self.groups = self.groups + [_group("新来的", "新来的", topics=["skill工具"])]
        self._stub(T.SKILLS.fallback)             # 新来的这一条判不出来
        self._build()
        self.assertEqual(B.P.page_stats(self.page),
                         {"nodes": 4, "classified": 3})

    def test_allow_shrink_lets_a_deliberate_collapse_through(self):
        """确实是维护者自己收紧了规则:--allow-shrink 得放行。"""
        many = [_group(f"n{i}", f"名{i}", topics=["skill工具"]) for i in range(101)]
        self.groups = many
        self._stub(T.SKILLS.fallback)
        with self.assertRaises(SystemExit):
            self._build()
        self._stub(T.SKILLS.fallback)
        self._build(allow_shrink=True)
        self.assertEqual(B.P.page_stats(self.page)["nodes"], 101)

    # ── ② 一个候选都没扫到,不许盖掉好页面 ──────────────────────────────────
    def test_empty_scan_does_not_replace_a_good_page(self):
        """上游扫描瞬时为空(vault 没挂上之类):旧图还在就必须拒绝。"""
        self._stub("S1")
        self._build()
        good = self.page.read_bytes()
        self.groups = []
        with self.assertRaises(SystemExit) as cm:
            B.build_one("skills", [], use_llm=False, max_new=None, model=None,
                        workers=1, out_dir=self.out_dir)
        self.assertIn("一个候选都没扫到", str(cm.exception))
        self.assertEqual(self.page.read_bytes(), good)

    def test_empty_scan_is_fine_when_there_is_no_old_page(self):
        """首次生成时确实没东西:空图是如实反映,照常发。"""
        B.build_one("skills", [], use_llm=False, max_new=None, model=None,
                    workers=1, out_dir=self.out_dir)
        self.assertEqual(B.P.page_stats(self.page), {"nodes": 0, "classified": 0})

    def test_empty_scan_passes_with_allow_shrink(self):
        """确认就是要清空:--allow-shrink 放行。"""
        self._stub("S1")
        self._build()
        B.build_one("skills", [], use_llm=False, max_new=None, model=None,
                    workers=1, out_dir=self.out_dir, allow_shrink=True)
        self.assertEqual(B.P.page_stats(self.page), {"nodes": 0, "classified": 0})

    # ── ② 页面原子落盘 ─────────────────────────────────────────────────────
    def _boom_on_page(self):
        """把「写页面的临时文件」做成写一截就炸,其它写盘(缓存)照常。"""
        real = Path.write_text

        def boom(self_, text, *a, **kw):
            if ".html." in self_.name and self_.name.endswith(".tmp"):
                real(self_, "PARTIAL")            # 先落一截,模拟磁盘满
                raise OSError("simulated disk full")
            return real(self_, text, *a, **kw)

        return mock.patch.object(Path, "write_text", boom)

    def test_write_failure_leaves_the_old_page_byte_identical(self):
        self._stub("S1")
        self._build()
        good = self.page.read_bytes()
        T.cache_path(T.SKILLS).unlink()          # 丢缓存,逼它重判出不同的一版
        self._stub("S2")
        with self._boom_on_page(), self.assertRaises(OSError):
            self._build()
        self.assertEqual(self.page.read_bytes(), good, "旧页面被写坏了")
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()),
                         [self.page.name], "临时文件没清掉")

    def test_write_failure_does_not_commit_the_cache(self):
        """页面没落盘 = 这一轮不算数,正式缓存也不能提交(下轮重来)。"""
        self._stub("S1")
        with self._boom_on_page(), self.assertRaises(OSError):
            self._build()
        self.assertFalse(self.page.exists())
        self.assertFalse(T.cache_path(T.SKILLS).exists(),
                         "页面没写成却把判定提交进正式缓存了")

    # ── ③ 反过来:页面写成了、缓存提交失败 ──────────────────────────────────
    def _boom_on_cache(self):
        """只让**正式缓存**那次写盘炸,暂存照常落(磁盘满/权限变化的形态)。"""
        real = T.save_cache

        def boom(taxo, codes, names, path=None, **kw):
            if path is None or path == T.cache_path(taxo):
                raise OSError("simulated cache disk full")
            return real(taxo, codes, names, path, **kw)

        return mock.patch.object(T, "save_cache", boom)

    def test_cache_commit_failure_rolls_the_page_back(self):
        """失败的发布不许改变正式产物:页面得原子回滚成旧的那一版。

        复审第 4 轮:跨进程锁只挡 writer 交错,给不了跨文件的失败原子性。页面
        换好了、缓存提交不上时任务是报错退出了,可 Vault 里已经是新页面、缓存
        还停在旧的——没人重试的话这个不一致会一直挂着。
        """
        base = [_group(f"n{i}", f"名{i}", topics=["skill工具"]) for i in range(100)]
        self.groups = base
        self._stub("S1")
        self._build()
        good_page = self.page.read_bytes()
        good_cache = T.cache_path(T.SKILLS).read_bytes()

        self.groups = base + [_group(f"m{i}", f"新{i}", topics=["skill工具"])
                              for i in range(20)]
        self._stub("S2")
        with self._boom_on_cache(), self.assertRaises(OSError):
            self._build()
        self.assertEqual(self.page.read_bytes(), good_page, "页面没回滚")
        self.assertEqual(T.cache_path(T.SKILLS).read_bytes(), good_cache)
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()),
                         [self.page.name], "临时文件没清掉")
        self.assertTrue(T.staging_path(T.SKILLS).exists(),
                        "这一轮判出来的东西是好的,别连进度一起丢了")

        self._stub("S2")                       # 重试一次就该好
        self._build()
        self.assertEqual(B.P.page_stats(self.page),
                         {"nodes": 120, "classified": 120})

    def test_cache_commit_failure_on_a_first_publish_leaves_no_page(self):
        """首次生成时提交失败:回到「什么都没有」,不留一张没有缓存的页面。"""
        self._stub("S1")
        with self._boom_on_cache(), self.assertRaises(OSError):
            self._build()
        self.assertFalse(self.page.exists())
        self.assertFalse(T.cache_path(T.SKILLS).exists())
        self.assertEqual(list(self.out_dir.iterdir()), [])

    def test_staging_cleanup_failure_does_not_roll_the_page_back(self):
        """正式缓存换成功了,只是暂存没删掉:这叫提交完成,页面不许回滚。

        复审第 5 轮:`_promote` 里两步共用一个异常边界,`_publish` 又把
        `commit()` 的任何异常都当成「缓存没提交」——于是缓存已经是新的 120 条、
        页面被换回旧的 100 节点,反方向的半提交是自己制造出来的。
        """
        base = [_group(f"n{i}", f"名{i}", topics=["skill工具"]) for i in range(100)]
        self.groups = base
        self._stub("S1")
        self._build()

        self.groups = base + [_group(f"m{i}", f"新{i}", topics=["skill工具"])
                              for i in range(20)]
        self._stub("S2")
        real = Path.unlink

        def denied(self_, *a, **kw):
            if self_.name.endswith(".staging.json"):
                raise PermissionError("simulated staging unlink denied")
            return real(self_, *a, **kw)

        with mock.patch.object(Path, "unlink", denied):
            self._build()                       # 不该抛:缓存已经提交完成了
        self.assertEqual(B.P.page_stats(self.page),
                         {"nodes": 120, "classified": 120}, "页面被错误回滚了")
        cache = json.loads(T.cache_path(T.SKILLS).read_text("utf-8"))["items"]
        self.assertEqual(len(cache), 120)
        self.assertTrue(T.staging_path(T.SKILLS).exists())   # 没删掉是既定事实

        # 下一轮自己收尾:留在盘上的那份全是正式缓存已有的,丢掉并清理
        self._stub("S3")
        self._build()
        self.assertFalse(T.staging_path(T.SKILLS).exists(), "残留暂存没被清理")
        self.assertEqual(B.P.page_stats(self.page),
                         {"nodes": 120, "classified": 120})


if __name__ == "__main__":
    unittest.main()
