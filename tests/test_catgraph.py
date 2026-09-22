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

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))

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
        self.assertEqual(got, {"a": "S1"})

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
        self.assertEqual(got, {"a": "S1", "b": "S2"})
        self.assertEqual(T.cache_path(T.SKILLS).read_text(encoding="utf-8"), stale)

    def test_fallback_must_exist(self):
        with self.assertRaises(AssertionError):
            T.Taxonomy(key="k", item_zh="x", fallback="没这个",
                       cats=(T.Cat("S0", "其他"),))


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
