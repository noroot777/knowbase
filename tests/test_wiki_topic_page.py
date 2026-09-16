# -*- coding: utf-8 -*-
"""主题标签 → 主题页文件名这条路的回归测试。

2026-09-15 那轮管道的 wiki 阶段整个挂在:

    FileNotFoundError: '/Users/fjh/obs/new-start/wiki/topics/主题-ci/cd流程.md'

提取阶段的主题词本该出自固定候选表,但模型给了表外的 `CI/CD流程`,slug 里的
斜杠原样进了 `topic/` 标签;wiki 层拿它拼 `wiki/topics/主题-{t}.md`,斜杠成了
目录分隔符,写页面时目录不存在 → 一篇脏文章带崩整个阶段(前面 674 篇的
linkify 都已经写完了,白跑)。

钉住两条,两条都得在,少一条都会复发:
1. **入库侧**:`_topic_slug` 吃掉斜杠(实体侧不能吃 —— `shadcn/ui` 这类仓库名
   里的斜杠是正当的,库里有几十个)。
2. **wiki 侧**:存量脏标签、以及 `: * ?` 之类别的路径非法字符照样可能出现,
   页面名统一过 `sanitize_filename`,并且文件名和双链必须用同一个名字 ——
   只清理其中一边的话页面建出来了、链接却指向不存在的页。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))

import wikilib as W                                    # noqa: E402
from kb.extract import _tag_slug, _topic_slug          # noqa: E402


def _universe(tag_rows: list[tuple[str, str]]) -> W.Universe:
    """用内存库拼一个最小 Universe:每条 (path, tag) 配一篇 dr-gzh 文档。"""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE documents (path TEXT PRIMARY KEY, source TEXT, title TEXT,
            account TEXT, published TEXT, url TEXT, summary TEXT, extracted_at TEXT);
        CREATE TABLE entities (path TEXT, entity TEXT, kind TEXT);
        CREATE TABLE doc_tags (path TEXT, tag TEXT);
    """)
    for path, tag in tag_rows:
        con.execute("INSERT OR IGNORE INTO documents VALUES (?,?,?,?,?,?,?,?)",
                    (path, W.SOURCE, "标题", "某号", "2026-07-16 12:44", "", "", "2026-07-16"))
        con.execute("INSERT INTO doc_tags VALUES (?,?)", (path, tag))
    con.commit()
    return W.Universe(con)


class TopicSlug(unittest.TestCase):
    def test_斜杠不进主题标签(self):
        self.assertEqual(_topic_slug("CI/CD流程"), "ci-cd流程")
        self.assertEqual(_topic_slug("a/b\\c"), "a-b-c")

    def test_实体侧的斜杠保持原样(self):
        # 库里 shadcn/ui、mattpocock/skills 这类仓库名有几十个,收紧这里会把
        # 同一个实体拆成两个标签
        self.assertEqual(_tag_slug("shadcn/ui"), "shadcn/ui")

    def test_常规主题词不受影响(self):
        for t in ("AI编程", "开源项目", "skill工具", "提示词工程"):
            self.assertEqual(_topic_slug(t), _tag_slug(t))


class TopicPageName(unittest.TestCase):
    def test_脏标签不产生子目录(self):
        u = _universe([("source/dr-gzh/a.md", "topic/ci/cd流程")])
        name = u.topic_file["ci/cd流程"]
        self.assertNotIn("/", name)
        self.assertEqual(name, "主题-ci cd流程")

    def test_其它路径非法字符一并清掉(self):
        u = _universe([("source/dr-gzh/a.md", "topic/a:b*c?d")])
        self.assertNotIn(":", u.topic_file["a:b*c?d"])
        self.assertNotIn("*", u.topic_file["a:b*c?d"])

    def test_双链与文件名同一个名字(self):
        u = _universe([("source/dr-gzh/a.md", "topic/ci/cd流程")])
        self.assertEqual(u.topic_link("ci/cd流程"), "[[主题-ci cd流程|ci/cd流程]]")
        # index 里带篇数的那种 label 也指向同一个页面
        self.assertTrue(u.topic_link("ci/cd流程", "x(1)").startswith("[[主题-ci cd流程|"))

    def test_清理后重名不互相覆盖(self):
        u = _universe([("source/dr-gzh/a.md", "topic/a/b"),
                       ("source/dr-gzh/b.md", "topic/a:b")])
        self.assertNotEqual(u.topic_file["a/b"], u.topic_file["a:b"])

    def test_清理到空的主题不建页(self):
        u = _universe([("source/dr-gzh/a.md", "topic//")])
        self.assertNotIn("/", u.topic_file)
        self.assertIsNone(u.topic_link("/"))

    def test_页面名不随篇数漂移(self):
        """重名决胜只按主题名排序 —— 按篇数排的话,某个主题多采几篇就会跟
        另一个换文件名,vault 里凭空多出一份旧名字的孤儿页。"""
        rows_a = [("source/dr-gzh/a.md", "topic/a/b"), ("source/dr-gzh/b.md", "topic/a:b")]
        rows_b = rows_a + [("source/dr-gzh/c.md", "topic/a:b"),
                           ("source/dr-gzh/d.md", "topic/a:b")]
        self.assertEqual(_universe(rows_a).topic_file, _universe(rows_b).topic_file)


if __name__ == "__main__":
    unittest.main()
