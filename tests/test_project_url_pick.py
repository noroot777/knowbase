# -*- coding: utf-8 -*-
"""聚合 project_url 时挑哪一条:仓库优先,且和图谱的「有没有仓库」同一套口径。

同一个工具在不同文章里给的 project_url 常常不一样:A 文给官网、B 文给仓库。
图鉴聚合时原来只认字面量 `github.com`,于是 Hugging Face / Gitee / GitLab 上的
仓库会输给官网——而图谱那头的「只看有仓库」是按聚合后这一个 url 判的,这个条目
就被当成「没有仓库」筛掉,open-weight 模型那几个连开源项目图谱都进不去
(2026-09-22 第 1 轮审查发现,生产数据里实测 7 个条目受影响)。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))

import graph_data as G  # noqa: E402

from kb.projects import _pick_url  # noqa: E402
from kb.repo_url import repo_host  # noqa: E402

HF = "https://huggingface.co/zai-org/GLM-4.7"
GH = "https://github.com/acme/tool"
SITE = "https://open.bigmodel.cn"
DOCS = "https://prettier.io/docs"


class PickUrlTest(unittest.TestCase):

    def test_repo_beats_homepage_whatever_the_host(self):
        for repo in (HF, GH, "https://gitee.com/a/b", "https://gitlab.com/a/b",
                     "https://modelscope.cn/models/a/b"):
            with self.subTest(repo=repo):
                self.assertEqual(_pick_url([SITE, repo]), repo)
                self.assertEqual(_pick_url([repo, SITE]), repo)

    def test_github_still_wins_among_repos(self):
        self.assertEqual(_pick_url([HF, GH]), GH)
        self.assertEqual(_pick_url([GH, HF]), GH)

    def test_no_repo_keeps_the_first_non_empty(self):
        self.assertEqual(_pick_url([SITE, DOCS]), SITE)
        self.assertEqual(_pick_url([]), "")

    def test_lookalike_host_is_not_a_repo(self):
        """github.com.evil.cn 不是仓库,不能靠它插队。"""
        evil = "https://github.com.evil.cn/x"
        self.assertEqual(repo_host(evil), "")
        self.assertEqual(_pick_url([SITE, evil]), SITE)

    def test_picked_url_agrees_with_the_graph_filter(self):
        """挑出来的那条要能让图谱认出「有仓库」——两层口径必须是同一份。"""
        g = {"norm": "glm", "name": "GLM", "kind": "model",
             "url": _pick_url([SITE, HF]), "all_topics": []}
        self.assertTrue(G.repo_host(g["url"]))
        self.assertEqual(G.select_projects([g]), [g])   # 并集口径收得进来


if __name__ == "__main__":
    unittest.main()
