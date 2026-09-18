# -*- coding: utf-8 -*-
"""存为笔记时,正文里的 [n] 要变成 Obsidian 点得动的内部链接。

原来只有末尾「## 引用」列表是双链,正文的 [1][2][3] 是纯文本——网页端那些
引用链接是 chat.js 渲染时现拼的(docA),压根没进落盘的 markdown,所以笔记
在 Obsidian 里打开,满屏引用一个都点不动。

钉住的三条:
1. **只能用 markdown 链接,不能用双链**。wikilink 的 alias 不允许方括号
   (Obsidian 官方把 `[`/`]` 列为让链接失效的字符),`[[路径|[1]]]` 会被吃掉;
   写成 `[[路径|1]]` 又会让连着的几个引用糊成「123」,认不出边界。
2. **路径整体 URL 编码**。vault 里带空格、括号的文件名很常见,`)` 会把
   markdown 链接提前截断。
3. **只换 refs 里真有的号**,正文里的 `2026` 之类不能被误伤。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.web.chat_api import linkify_citations       # noqa: E402

REFS = [
    {"n": 1, "path": "source/dr-gzh/2026-09-10/号-标 题.md", "title": "标 题"},
    {"n": 2, "path": "source/dr-gzh-projects/2026-09-17/ab (2).md", "title": 'Skill "X"'},
    {"n": 9, "path": "", "title": "没有路径"},
]

LINK1 = "[\\[1\\]](source/dr-gzh/2026-09-10/%E5%8F%B7-%E6%A0%87%20%E9%A2%98.md \"标 题\")"
LINK2 = "[\\[2\\]](source/dr-gzh-projects/2026-09-17/ab%20%282%29.md \"Skill 'X'\")"


class LinkifyCitationsTest(unittest.TestCase):
    def test_cited_numbers_become_links(self):
        self.assertEqual(linkify_citations("SkillUI [1][2]:说明。", REFS),
                         f"SkillUI {LINK1}{LINK2}:说明。")

    def test_path_is_url_encoded(self):
        # 括号不编码的话 markdown 链接会在 ')' 处提前截断
        out = linkify_citations("[2]", REFS)
        self.assertIn("%282%29", out)
        self.assertNotIn("(2)", out)

    def test_no_wikilink_syntax(self):
        # 关键是不出现 `[[`:没有起点,后面那个 `\]]` 就不会被当成双链收尾
        out = linkify_citations("[1]", REFS)
        self.assertNotIn("[[", out)
        self.assertTrue(out.startswith("[\\[1\\]]("))

    def test_uncited_or_pathless_numbers_stay_plain(self):
        text = "2026 年 [7] 与 [9] 都不该变。"
        self.assertEqual(linkify_citations(text, REFS), text)

    def test_title_quotes_do_not_break_the_link(self):
        self.assertNotIn('""', linkify_citations("[2]", REFS))

    def test_renders_as_bracketed_label(self):
        try:
            import markdown_it
        except ImportError:                       # 环境没装就跳过,不影响其余断言
            self.skipTest("markdown_it 未安装")
        html = markdown_it.MarkdownIt().render(linkify_citations("A [1] B", REFS))
        self.assertIn(">[1]</a>", html)
        self.assertIn("%E6%A0%87%20%E9%A2%98.md", html)


if __name__ == "__main__":
    unittest.main()
