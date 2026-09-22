# -*- coding: utf-8 -*-
"""分类图谱(开源项目 / skill)的数据层与归类缓存。

钉住的几条,都是踩坏了不会报错、只会让页面悄悄变难看/变错的地方:

1. **分类表指纹变了,整份缓存必须作废**。类目是写死的,改一版类目(拆一个
   子类、改一个 code)之后老缓存里的 code 就对不上新表;不作废的话那些条目
   会挂在一个已经不存在的类目下,页面上直接消失——`Taxonomy.sig()` 进
   缓存文件、`load_raw` 比对不上就返回空,触发全量重判。
2. **没判到的一律落 fallback,不许瞎猜**。`--max-new` 截断和模型漏判都会留
   尾巴,这时候显示成「待归类」是对的,硬塞进某个类目就成了假信息。
3. **文章表按 url 去重**。一篇盘点文章能拆出几十个项目条目,标题+URL 内嵌
   到每个节点上等于复制几十遍;去重后节点只存下标。
4. **共现边跳过超大合集文章**。月度盘点一篇拆 40+ 个项目,它们两两相连既不
   是"相关"也会把边表撑爆。
5. **drop_codes 的类目不进图谱**(skill 图谱的 X = 判出来不是 skill),
   而且类目列表里也不能留着它,否则页面上会多一个永远空着的分区。
6. **指纹失效后的全量重判是另一条路**:不吃 `--max-new`,新结论攒在半成品
   文件里判完才换上正式缓存,没判完就既不动旧缓存也不覆盖旧图。混成一条的
   话改一句规则就能把几千条完整旧判定缩成几百条,页面大面积退化。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))

import build_catgraph as B  # noqa: E402
import graph_data as G  # noqa: E402
import taxonomy as T  # noqa: E402


def _group(norm, name, n=1, *, kind="project", url="", summary="s",
           topics=(), latest="2026-09-20", first="2026-09-01", arts=()):
    """一组条目;arts = [(文章 url, 文章标题, 发布日)],长度即提及次数。"""
    arts = list(arts) or [(f"https://x/{norm}/{i}", f"文章{i}", latest)
                          for i in range(n)]
    return {
        "norm": norm, "name": name, "kind": kind, "summary": summary,
        "url": url, "n": len(arts), "accounts": 1, "latest": latest,
        "first": first, "topics": list(topics), "all_topics": list(topics),
        "sources": ["dr-gzh-projects"],
        "entries": [{"url": u, "article_title": t, "account": "号",
                     "published": d} for u, t, d in arts],
    }


class TaxonomyCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = T.CACHE_DIR
        T.CACHE_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(T, "CACHE_DIR", self._old))

    def test_roundtrip(self):
        T.save_cache(T.SKILLS, {"a": "S1"}, {"a": "甲"})
        codes, names = T.load_raw(T.SKILLS)
        self.assertEqual(codes, {"a": "S1"})
        self.assertEqual(names["a"], "甲")

    def test_sig_change_invalidates(self):
        """类目改过之后老缓存整份作废,而不是让条目挂在不存在的类目下。"""
        T.save_cache(T.SKILLS, {"a": "S1"}, {"a": "甲"})
        changed = T.Taxonomy(
            key=T.SKILLS.key, item_zh=T.SKILLS.item_zh, fallback="S0",
            cats=T.SKILLS.cats[:-1] + (T.Cat("S99", "新类目"), T.Cat("S0", "其他")))
        self.assertNotEqual(changed.sig(), T.SKILLS.sig())
        self.assertEqual(T.load_cache(changed), {})

    def test_sig_covers_everything_that_enters_the_prompt(self):
        """只改 hint / extra_rules / fallback / item_zh 时也必须重判。

        这几样全都原样进 prompt(或改变「判不出来落哪」),改了判定就该变;
        签名不动的话旧结论被永久沿用——skill 的 X 档尤其明显:成员资格规则
        修正完,页面上还是老那批成员(2026-09-22 第 1 轮审查发现)。
        """
        base = T.SKILLS
        variants = {
            "extra_rules": T.Taxonomy(key=base.key, item_zh=base.item_zh,
                                      fallback=base.fallback, cats=base.cats,
                                      extra_rules="完全不同的成员资格规则"),
            "item_zh": T.Taxonomy(key=base.key, item_zh="别的东西",
                                  fallback=base.fallback, cats=base.cats,
                                  extra_rules=base.extra_rules),
            "hint": T.Taxonomy(
                key=base.key, item_zh=base.item_zh, fallback=base.fallback,
                extra_rules=base.extra_rules,
                cats=tuple(T.Cat(c.code, c.group, c.sub, c.hint + "。补一句说明")
                           for c in base.cats)),
            "fallback": T.Taxonomy(key=base.key, item_zh=base.item_zh,
                                   fallback="S1", cats=base.cats,
                                   extra_rules=base.extra_rules),
        }
        T.save_cache(base, {"a": "S1"}, {"a": "甲"})
        for what, v in variants.items():
            with self.subTest(改了=what):
                self.assertNotEqual(v.sig(), base.sig())
                self.assertEqual(T.load_cache(v), {})

    def test_unknown_code_not_invented(self):
        """模型给了表外的 code 时 classify 不能把它写进缓存。"""
        seen = {}

        def fake(prompt, model, **kw):
            seen["p"] = prompt
            return '[{"id":0,"c":"S1"},{"id":1,"c":"不存在的类目"}]'

        T._llm, old = fake, T._llm
        self.addCleanup(lambda: setattr(T, "_llm", old))
        got = T.classify(T.SKILLS, [("a", "甲", "x"), ("b", "乙", "y")],
                         model="stub", workers=1, log=lambda *_: None)
        self.assertIn("0. 甲 :: x", seen["p"])
        self.assertIn("1. 乙 :: y", seen["p"])
        self.assertEqual(got.codes, {"a": "S1"})

    def test_failed_round_does_not_wipe_the_cache(self):
        """一批都没判成(没配 key / 中转站挂了)时不能把旧判定覆盖成空。

        指纹刚变、老文件还攥着几千条结论的时候,那一下是不可逆的。
        """
        T.save_cache(T.SKILLS, {"a": "S1", "b": "S2"}, {"a": "甲", "b": "乙"})
        stale = T.cache_path(T.SKILLS).read_text(encoding="utf-8")

        def boom(prompt, model, **kw):
            raise RuntimeError("openai 未配置 API key")

        T._llm, old = boom, T._llm
        self.addCleanup(lambda: setattr(T, "_llm", old))
        got = T.classify(T.SKILLS, [("c", "丙", "x")], model="stub", workers=1,
                         log=lambda *_: None)
        self.assertEqual(got.codes, {"a": "S1", "b": "S2"})
        self.assertEqual(T.cache_path(T.SKILLS).read_text(encoding="utf-8"), stale)

    def test_fallback_must_exist(self):
        with self.assertRaises(AssertionError):
            T.Taxonomy(key="k", item_zh="x", fallback="没这个",
                       cats=(T.Cat("S0", "其他"),))


class ResignMigrationTest(unittest.TestCase):
    """分类表改过之后那一趟全量重判。

    这里钉的是 2026-09-22 第 2、3、4 轮审查发现的三次事故,形态一样:一份**不作数
    的判定**被换成正式缓存,把还能用的旧判定和好页面毁掉,不可逆。第 2 轮的触发
    条件是 `--max-new` 截断,第 3 轮是模型回了合法 JSON 却漏了一截(输出被截断、
    id 重复、code 在表外被过滤),第 4 轮是每条都回合法 fallback——三次都不需要
    任何故障。所以重判必须①不受 --max-new 限制、②攒在半成品文件里、③**每一条都
    拿到有效 code 才算判完**、④**新图验收通过、页面落盘之后才提交缓存**,
    没判完/不作数就既不换正式缓存也不覆盖页面。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = T.CACHE_DIR
        T.CACHE_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(T, "CACHE_DIR", self._old))
        # 101 条旧判定 + 一份改过规则(指纹变了)的分类表
        self.items = [(f"n{i}", f"名{i}", "简介") for i in range(101)]
        self.old = {n: "S1" for n, _, _ in self.items}
        T.save_cache(T.SKILLS, self.old, {n: nm for n, nm, _ in self.items})
        self.stale_bytes = T.cache_path(T.SKILLS).read_text(encoding="utf-8")
        self.taxo = T.Taxonomy(key=T.SKILLS.key, item_zh=T.SKILLS.item_zh,
                               fallback=T.SKILLS.fallback, cats=T.SKILLS.cats,
                               extra_rules="改过的成员资格规则")
        self.assertNotEqual(self.taxo.sig(), T.SKILLS.sig())

    def _stub(self, *, fail_names=(), omit=0, omit_until_call=10 ** 9,
              bad_code=False, code="S2"):
        """模型桩。

        fail_names:批次里含这些名字就抛异常——**按内容判,不按第几次调用**,
        这样补判轮再问一次照样失败(故障是持续的)。omit:每批少回几条,模拟
        「语法合法但漏项」;bad_code 则把漏的那条给个表外 code(会被过滤掉,
        效果一样)。omit_until_call:第几次调用之后不再漏(测补判补得上)。
        code:每条回哪个 code(拿 fallback 测「判了等于没判」那条路)。
        """
        self.calls = 0

        def fake(prompt, model, **kw):
            self.calls += 1
            rows = re.findall(r"^(\d+)\. (\S+) ::", prompt, re.M)
            if any(nm in fail_names for _i, nm in rows):
                raise RuntimeError("中转站 502")
            drop = omit if self.calls <= omit_until_call else 0
            out = []
            for k, (i, _nm) in enumerate(rows):
                if k >= len(rows) - drop:
                    if bad_code:                 # 表外 code:解析得出,但会被过滤
                        out.append({"id": int(i), "c": "表外的类目"})
                    continue                     # 不 bad_code 就是整条不回
                out.append({"id": int(i), "c": code})
            return json.dumps(out)

        T._llm, old = fake, T._llm
        self.addCleanup(lambda: setattr(T, "_llm", old))

    def _page_with_old_content(self):
        out_dir = Path(self.tmp.name) / "vault"
        out_dir.mkdir(exist_ok=True)
        page = out_dir / B.VIEWS["skills"]["out"]
        page.write_text("KNOWN-GOOD-OLD-PAGE", encoding="utf-8")
        old_taxo = B.VIEWS["skills"]["taxo"]
        B.VIEWS["skills"]["taxo"] = self.taxo
        self.addCleanup(lambda: B.VIEWS["skills"].__setitem__("taxo", old_taxo))
        groups = [_group(n, nm, topics=["skill工具"]) for n, nm, _ in self.items]
        return out_dir, page, groups

    def test_max_new_does_not_truncate_a_resign_rejudge(self):
        """重判不吃 --max-new:截断一次就等于把没轮到的那批旧判定扔了。"""
        self._stub()
        got = T.classify(self.taxo, self.items, model="stub", workers=1,
                         batch=40, max_new=40, log=lambda *_: None)
        self.assertTrue(got.complete)
        self.assertTrue(got.migrating)
        self.assertEqual(len(got.codes), 101)
        # 判完也只是「待提交」:正式缓存这会儿还是旧的那份
        self.assertEqual(T.cache_path(T.SKILLS).read_text(encoding="utf-8"),
                         self.stale_bytes)
        got.commit()
        self.assertEqual(T.load_cache(self.taxo), got.codes)
        self.assertFalse(T.migrate_path(self.taxo).exists())

    def test_incomplete_rejudge_keeps_the_old_cache(self):
        """有批次失败 → 正式缓存一个字节不动,进度攒在半成品文件里等下次。"""
        self._stub(fail_names={"名40"})
        got = T.classify(self.taxo, self.items, model="stub", workers=1,
                         batch=40, max_new=None, log=lambda *_: None)
        self.assertFalse(got.complete)
        self.assertEqual(got.pending, 40)
        self.assertEqual(T.cache_path(T.SKILLS).read_text(encoding="utf-8"),
                         self.stale_bytes)
        self.assertEqual(T.load_raw(self.taxo, T.migrate_path(self.taxo))[0],
                         got.codes)

        # 下一轮只补没判到的那 40 条,补齐后才换上正式缓存
        self._stub()
        again = T.classify(self.taxo, self.items, model="stub", workers=1,
                           batch=40, max_new=40, log=lambda *_: None)
        self.assertTrue(again.complete)
        self.assertEqual(len(again.codes), 101)
        self.assertEqual(self.calls, 1)
        again.commit()
        self.assertEqual(T.load_cache(self.taxo), again.codes)
        self.assertFalse(T.migrate_path(self.taxo).exists())

    def test_incomplete_rejudge_does_not_overwrite_the_page(self):
        """半份结果不许盖掉好页面(哪怕已经判出来几十条,classified > 0)。"""
        self._stub(fail_names={"名40"})
        _out_dir, page, groups = self._page_with_old_content()
        with self.assertRaises(SystemExit):
            B.build_one("skills", groups, use_llm=True, max_new=None,
                        model="stub", workers=1, out_dir=_out_dir)
        self.assertEqual(page.read_text(encoding="utf-8"), "KNOWN-GOOD-OLD-PAGE")

    def test_valid_but_short_response_is_not_a_finished_rejudge(self):
        """合法 JSON 但漏项 = 没判完。

        第 3 轮审查:`run()` 只在抛异常时算失败,于是「3 个批次各回 1 条」被当成
        三批全成功,3 条判定 promote 成正式缓存、101 条旧判定被覆盖、页面上 98 个
        节点变待归类。判据必须是「每一条都拿到有效 code」,不是「没有批次抛异常」。
        """
        self._stub(omit=39)              # 每批 40 条只回 1 条
        out_dir, page, groups = self._page_with_old_content()
        with self.assertRaises(SystemExit) as cm:
            B.build_one("skills", groups, use_llm=True, max_new=None,
                        model="stub", workers=1, out_dir=out_dir)
        self.assertIn("全量重判还没做完", str(cm.exception))
        self.assertEqual(page.read_text(encoding="utf-8"), "KNOWN-GOOD-OLD-PAGE")
        self.assertEqual(T.cache_path(T.SKILLS).read_text(encoding="utf-8"),
                         self.stale_bytes)
        # 判出来的那些没白跑,攒在半成品里下轮接着补
        part = T.load_raw(self.taxo, T.migrate_path(self.taxo))[0]
        self.assertTrue(0 < len(part) < 101)

    def test_out_of_table_code_counts_as_missing(self):
        """模型给的 code 在表外被过滤掉 = 这条没判到,同样不算判完。"""
        self._stub(omit=1, bad_code=True)
        got = T.classify(self.taxo, self.items, model="stub", workers=1,
                         batch=40, max_new=None, log=lambda *_: None)
        self.assertFalse(got.complete)
        self.assertGreater(got.pending, 0)
        self.assertEqual(T.cache_path(T.SKILLS).read_text(encoding="utf-8"),
                         self.stale_bytes)

    def test_retry_round_fills_model_omissions(self):
        """漏项会补判(只问缺的);补齐了才 promote——补判是为了别因为漏一条
        就整轮白等,不是放宽判据。"""
        self._stub(omit=2, omit_until_call=3)     # 第一轮 3 批各漏 2 条
        got = T.classify(self.taxo, self.items, model="stub", workers=1,
                         batch=40, max_new=None, log=lambda *_: None)
        self.assertTrue(got.complete)
        self.assertEqual(len(got.codes), 101)
        self.assertEqual(self.calls, 4)           # 3 批 + 1 轮补判(6 条一批)
        got.commit()
        self.assertEqual(T.load_cache(self.taxo), got.codes)
        self.assertFalse(T.migrate_path(self.taxo).exists())

    def test_cache_is_committed_only_after_the_page_is_written(self):
        """两阶段提交:页面落盘之前,正式缓存一个字节不能动。

        先换缓存再校验新图的话,任何一道拦截都只保住页面保不住缓存
        (2026-09-22 第 4 轮审查发现)。
        """
        self._stub()
        out_dir, page, groups = self._page_with_old_content()
        seen = {}
        real = B.P.render

        def spy(*a, **kw):
            # 渲染这一刻(页面还没落盘)正式缓存必须还是旧的那份
            seen["cache"] = T.cache_path(T.SKILLS).read_text(encoding="utf-8")
            return real(*a, **kw)

        B.P.render = spy
        self.addCleanup(lambda: setattr(B.P, "render", real))
        B.build_one("skills", groups, use_llm=True, max_new=None, model="stub",
                    workers=1, out_dir=out_dir)
        self.assertEqual(seen["cache"], self.stale_bytes)
        self.assertNotEqual(page.read_text(encoding="utf-8"), "KNOWN-GOOD-OLD-PAGE")
        self.assertEqual(len(T.load_cache(self.taxo)), 101)   # 页面写完才提交
        self.assertFalse(T.migrate_path(self.taxo).exists())

    def test_all_fallback_rejudge_never_replaces_the_cache(self):
        """每条都回合法 fallback:`left == 0` 可这一趟等于没判。

        第 4 轮审查:fallback 也是表内 code,于是 promote 先发生、`build_one`
        的「一个都没归类」才拦下——页面保住了,可那份还能用的旧缓存已经被换成
        一份全是「待归类」的新缓存;之后每轮全命中、不再问模型,页面永远更新
        不了。连跑两次必须都:正式缓存逐字节不变、旧页面不变、而且**第二次还会
        再问模型**(不能卡死)。
        """
        out_dir, page, groups = self._page_with_old_content()
        for run in (1, 2):
            with self.subTest(第几次=run):
                self._stub(code=T.SKILLS.fallback)
                with self.assertRaises(SystemExit):
                    B.build_one("skills", groups, use_llm=True, max_new=None,
                                model="stub", workers=1, out_dir=out_dir)
                self.assertGreater(self.calls, 0)      # 没卡在坏缓存上
                self.assertEqual(T.cache_path(T.SKILLS).read_text(encoding="utf-8"),
                                 self.stale_bytes)
                self.assertEqual(page.read_text(encoding="utf-8"),
                                 "KNOWN-GOOD-OLD-PAGE")
                self.assertFalse(T.migrate_path(self.taxo).exists())

        # 模型恢复正常之后能自己走出来
        self._stub()
        B.build_one("skills", groups, use_llm=True, max_new=None, model="stub",
                    workers=1, out_dir=out_dir)
        self.assertEqual(len(T.load_cache(self.taxo)), 101)
        self.assertNotEqual(page.read_text(encoding="utf-8"), "KNOWN-GOOD-OLD-PAGE")


class BuildDataTest(unittest.TestCase):
    def test_articles_deduped_and_referenced_by_index(self):
        a = _group("a", "A", arts=[("u1", "同一篇", "2026-09-10")])
        b = _group("b", "B", arts=[("u1", "同一篇", "2026-09-10")])
        d = G.build([a, b], {"a": "A1", "b": "A1"}, T.PROJECTS)
        self.assertEqual(len(d["arts"]), 1)
        self.assertEqual(d["nodes"][0]["ar"], [0])
        self.assertEqual(d["nodes"][1]["ar"], [0])

    def test_cooccurrence_edge_weight(self):
        arts = [("u1", "一", "2026-09-10"), ("u2", "二", "2026-09-11")]
        d = G.build([_group("a", "A", arts=arts), _group("b", "B", arts=arts)],
                    {"a": "A1", "b": "A1"}, T.PROJECTS)
        self.assertEqual(d["edges"], [[0, 1, 2]])

    def test_huge_roundup_article_makes_no_edges(self):
        """一篇拆出 41 个条目的月度盘点:这 41 个两两相连没有信息量。"""
        many = [_group(f"p{i}", f"P{i}", arts=[("u", "盘点", "2026-09-10")])
                for i in range(41)]
        d = G.build(many, {g["norm"]: "A1" for g in many}, T.PROJECTS)
        self.assertEqual(d["edges"], [])

    def test_unclassified_falls_back(self):
        d = G.build([_group("a", "A")], {}, T.PROJECTS)
        self.assertEqual(d["nodes"][0]["c"], T.PROJECTS.fallback)
        self.assertEqual(d["meta"]["classified"], 0)

    def test_drop_codes_removes_nodes_and_the_category(self):
        d = G.build([_group("a", "A"), _group("b", "B")],
                    {"a": "S1", "b": "X"}, T.SKILLS, drop_codes=frozenset({"X"}))
        self.assertEqual([n["l"] for n in d["nodes"]], ["A"])
        self.assertNotIn("X", [c["c"] for c in d["cats"]])

    def test_selectors(self):
        groups = [
            _group("p", "纯项目", kind="project"),
            _group("g", "挂着仓库的产品", kind="product", url="https://github.com/x/y"),
            _group("z", "gitee 上的产品", kind="product", url="https://gitee.com/a/b"),
            _group("o", "别的产品", kind="product", url="https://example.com"),
            _group("s", "技能包", kind="product", topics=["skill工具"]),
        ]
        self.assertEqual({g["norm"] for g in G.select_projects(groups)},
                         {"p", "g", "z"})
        self.assertEqual({g["norm"] for g in G.select_skills(groups)}, {"s"})

    def test_repo_host_is_matched_by_hostname_not_substring(self):
        """「只看有仓库」筛的必须是代码托管站,不是「有任意 http 网址」。

        project_url 里官网/文档站一大把(opencode.ai、prettier.io…),按有没有
        URL 筛的话这个开关名不副实(2026-09-22 第 1 轮审查发现);而按字符串
        包含判主机,`github.com.evil.cn` 也会蒙混过关。
        """
        self.assertEqual(G.repo_host("https://github.com/a/b"), "github.com")
        self.assertEqual(G.repo_host("https://WWW.Gitee.com/a"), "gitee.com")
        self.assertEqual(G.repo_host("https://raw.github.com/a"), "github.com")
        for bad in ("https://opencode.ai", "https://prettier.io",
                    "https://code.visualstudio.com/", "https://github.com.evil.cn/a",
                    "javascript:alert(1)", "", "not a url"):
            self.assertEqual(G.repo_host(bad), "", bad)

    def test_node_marks_repo_separately_from_url(self):
        d = G.build([_group("a", "A", url="https://prettier.io"),
                     _group("b", "B", url="https://github.com/x/y"),
                     _group("c", "C", url="javascript:alert(1)")],
                    {}, T.PROJECTS)
        a, b, c = d["nodes"]
        self.assertEqual((a["u"], a.get("rp")), ("https://prettier.io", None))
        self.assertEqual((b["u"], b["rp"]), ("https://github.com/x/y", "github.com"))
        self.assertEqual((c["u"], c.get("rp")), ("", None))

    def test_rank_puts_hot_first(self):
        groups = [_group("a", "A", n=1), _group("b", "B", n=5)]
        self.assertEqual([g["norm"] for g in G.rank(groups)], ["b", "a"])


if __name__ == "__main__":
    unittest.main()
