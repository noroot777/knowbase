# -*- coding: utf-8 -*-
"""来源别名是对外的查询参数,一个键都不许删。

`SOURCE_ALIASES` 里的「绿泡泡」「微信」「公众号」「wechat」不是文案,是**行为字符串**:
`expand_sources()` 认不出一个名字时不报错,而是把它当成具体来源名原样传进 SQL,
于是精确匹配**静默得到空集** —— 用词统一那一轮把「绿泡泡」这个键删掉之后,同一批
已收录的公众号文章用「公众号」查得到、换成「绿泡泡」查出来零条(2026-10-02 审查实测)。
零结果和"这个来源下确实没有文章"长得一模一样,页面上看不出发生了什么。

所以这里不验别名表长什么样,而是**真建一张 documents 表、真塞一篇 dr-gzh 的文章**,
走真的 `enumerate_documents` 和真的 `search._filter_sql`,看几种写法是不是都把它找回来。
"""

import sqlite3
import unittest

from kb import store
from kb.aggregate import enumerate_documents
from kb.config import SOURCE_ALIASES, SOURCE_GROUPS, expand_sources
from kb.search import Filters, _filter_sql

# 用户/模型在问答里给公众号这批内容时可能写的几种写法,全都得指向同一批文章
GZH_SPELLINGS = ["公众号", "绿泡泡", "微信", "wechat", "公众号系"]

# 「简报」这一类改过名(原来叫「邮件简报」—— 那个「邮件」说的是发行方怎么发给它
# 自己的订阅者,而这条通道只读 RSS 的期号清单和每期落地页,压根不碰邮箱)。别名表
# 里有一行是从 `label` **派生**的,所以改标签这个动作**天然就把老键删掉了**:
# 下面这几种写法必须都还认得出来,否则老写法走的正是那条静默空集的路。
DIGEST_SPELLINGS = ["简报", "邮件简报", "newsletter"]


class SourceAliasTests(unittest.TestCase):
    def setUp(self):
        # 不碰开发机上真实的 data/kb.db:内存库 + 仓库真实 schema
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(store._SCHEMA)
        self.conn.execute(
            "INSERT INTO documents(path, source, title) VALUES (?,?,?)",
            ("article.md", "dr-gzh", "别名回归用文章"),
        )
        self.conn.execute(
            "INSERT INTO documents(path, source, title) VALUES (?,?,?)",
            ("issue.md", "digest", "别名回归用简讯"),
        )
        self.addCleanup(self.conn.close)

    def _enumerated(self, source) -> list[str]:
        return [d["title"] for d in
                enumerate_documents(self.conn, source=source, exclude_promo=False)]

    def _filtered(self, source) -> int:
        where, params = _filter_sql(Filters(source=source, exclude_promo=False))
        sql = "SELECT COUNT(*) FROM documents d WHERE 1=1" + where
        return self.conn.execute(sql, params).fetchone()[0]

    def test_every_spelling_finds_the_same_article(self):
        """几种写法各查一次:枚举和检索过滤都得返回那一篇。"""
        for spelling in GZH_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(["别名回归用文章"], self._enumerated(spelling))
                self.assertEqual(1, self._filtered(spelling))

    def test_every_spelling_expands_to_the_whole_group(self):
        """展开结果是整个组,不是把那个名字原样传下去。"""
        for spelling in GZH_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(SOURCE_GROUPS["gzh"], expand_sources(spelling))

    def test_mixed_list_expands_and_dedupes(self):
        """列表里混着别名和具体来源名:逐项展开、去重、保序。"""
        self.assertEqual(
            ["dr-gzh", "dr-gzh-projects", "gzh", "youtube", "dr-youtube-dig", "dr-video-projects"],
            expand_sources(["绿泡泡", "dr-gzh", "视频", "公众号"]),
        )
        # 同一批内容的两种写法混在一起,不该把那一篇数两遍
        self.assertEqual(["别名回归用文章"], self._enumerated(["绿泡泡", "微信"]))

    def test_unknown_source_really_is_an_empty_set(self):
        """反面:认不出的名字确实静默得到空集 —— 这就是删掉一个键的后果。"""
        self.assertEqual(["不存在的来源"], expand_sources("不存在的来源"))
        self.assertEqual([], self._enumerated("不存在的来源"))

    def test_renamed_type_keeps_its_old_spelling(self):
        """改过名的那一类:新名字和老名字都得查回同一条。

        反面是 `test_unknown_source_really_is_an_empty_set` —— 改名时漏留老键,
        老写法走的就是那条路:不报错,静默零条。
        """
        for spelling in DIGEST_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(["别名回归用简讯"], self._enumerated(spelling))
                self.assertEqual(1, self._filtered(spelling))
                self.assertEqual(["digest"], expand_sources(spelling))

    def test_no_alias_points_at_nothing(self):
        """每个别名都得指到一个真实的组上,不许留下指空的键。"""
        for key, group in SOURCE_ALIASES.items():
            with self.subTest(alias=key):
                self.assertIn(group, SOURCE_GROUPS)


if __name__ == "__main__":
    unittest.main()
