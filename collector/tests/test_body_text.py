"""`body_text` 那一列的规范化:段落列表 → 单段 TEXT。

生产入参是 `fetch_article_body_full` 返回的 `list[str]`;`None` 那一档
(「这篇没正文」)原来一条测试都没有。
"""

from __future__ import annotations

import unittest

from src.db.body_text import normalize_body_text


class NormalizeBodyTextTests(unittest.TestCase):
    def test_paragraphs_are_joined_with_newlines(self):
        self.assertEqual("第一段\n第二段", normalize_body_text(["第一段", "第二段"]))

    def test_no_body_becomes_an_empty_string_not_none(self):
        """存空串而不是 NULL:「有没有这一行」和「这一行有没有内容」是两个问题。"""
        self.assertEqual("", normalize_body_text(None))
        self.assertEqual("", normalize_body_text([]))

    def test_a_ragged_list_still_lands_instead_of_raising(self):
        """半截数据也该落库——抓取那头已经判过「有没有可读正文」了。"""
        self.assertEqual("正文\nNone\n3", normalize_body_text(["正文", None, 3]))

    def test_a_plain_string_passes_through_without_being_re_encoded(self):
        """别在这里 json.dumps:库里存量已经有一种 JSON 格式了,读回那头要试解。"""
        self.assertEqual('["已经是 JSON 的存量行"]',
                         normalize_body_text('["已经是 JSON 的存量行"]'))
