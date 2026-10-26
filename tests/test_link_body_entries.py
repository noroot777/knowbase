# -*- coding: utf-8 -*-
"""**每一条写 `feed_items.body` 的 UPDATE,内容从哪来都得声明。** 仅在验收阶段执行。

这条用例替掉的是一张**人手维护的入口名单**。能力判据(「这一类来源的正文允不允许
被它 `url` 指向的那一页替换」)原先靠「记得在每个入口调一次 `permits_link_body()`」
落实,而名单只能写在注释和一条列举式用例里:前四处都接上了,第五处
(`grab_store.replace`,人点「重新抓一次」那条路)照旧把简报里一条简讯的 28 字摘要换
成了外链那篇仓库 README 的全文 —— 走的是公开接口、不是内部直调,审查实测可达。
漏接的形态是**静默覆盖**:简报不进补抓队列,没有任何一轮同步会把那两句话补回来。

所以判据搬到了**写入层**(`kb.source_types.link_body_sql`),而这条用例钉的是「搬完
了没有」:
- **声明集合必须和扫出来的完全相等** —— 新加一条写 `body` 的 UPDATE 而不声明就红,
  搬走了一条而不改声明也红。这是集合级的红,不是「再补一个实例」。
- **逐函数比「带闸几条 / 不带闸几条」**,不按语句顺序对号 —— 删掉某条的闸会红;
  **反过来,给 `OWN` 那几条加上闸同样红**:备份恢复那一支要把原文原样装回不许可来源
  的条目上,带上闸之后 rowcount 恒 0,存量复查那条出路会整条失效。

**残留盲区写在这里,别靠读者自己发现**:`fulltext._fail` 的 SET 列表是在 Python 里
拼出来的(`DYNAMIC`),扫描器看不见里面有什么。哪天有人把 `body` 拼进那个列表,这条
用例不会红 —— 那一处的判据只能靠它自己那段注释。
"""
from __future__ import annotations

import ast
import re
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: 扫这两棵树。`tests/` 自己不算(夹具里的 UPDATE 是在造局,不是产品代码)。
TREES = ('kb', 'scripts')
_UPDATE = re.compile(r'UPDATE\s+feed_items\s+SET', re.I)
#: `body=` 而不是 `body_zh=` / `body_provenance=`。
_BODY = re.compile(r'(?<![a-z_])body\s*=')
#: 这条语句自己带着能力判据:直接拼片段,或者经 `fulltext._eligible()` 带上。
_GATE = re.compile(r'link_body_sql|_eligible')

LINK = 'link'        # 内容来自这一行 `url` 指向的那一页 —— 必须带闸
OWN = 'own'          # 内容是这一条自己的,不来自外链那一页 —— 不许带闸
DYNAMIC = 'dynamic'  # SET 列表在 Python 里拼,里面没有 body

#: **声明表。** 键是 `(相对路径, 函数名)`,值是该函数里每条语句各自的来路。
DECLARED: dict[tuple[str, str], tuple[str, ...]] = {
    # 正文补抓:内容是 `url` 那一页抓回来的。
    ('kb/feeds/fulltext.py', '_save'): (LINK,),
    # 补抓失败只写标记那几列(重试次数、错误、挂起),SET 列表动态拼。
    ('kb/feeds/fulltext.py', '_fail'): (DYNAMIC,),
    # 浏览器采集:人在自己的浏览器里打开 `url` 那一页,把正文交回来。
    ('kb/browser_capture.py', 'save'): (LINK,),
    # 手动收录的「重新抓一次」:重抓 `url` 那一页替换库里那一份。
    ('kb/feeds/grab_store.py', 'replace'): (LINK,),
    # 存量复查写回,两支:许可来源换壳回队列(内容下一轮从 `url` 那一页来)、
    # 不许可来源把备份里**它自己原本那一份**原样装回去。
    ('scripts/feed_fulltext_recheck.py', '_downgrade'): (LINK, OWN),
    # X 长文回填:取的是 x.com 上这条推文**自己**的长文,不是外链那一页。
    ('scripts/x_article_backfill.py', 'apply_one'): (OWN,),
}


def _statements() -> dict[tuple[str, str], list[dict]]:
    """扫出每一条 `UPDATE feed_items SET …`,按 `(相对路径, 函数名)` 归堆。

    排除 docstring:讲这件事的文档里本来就会出现这串字样(`link_body_sql` 自己那段
    就是),把它算成写入点会让声明表里多出几条假的。
    """
    found: dict[tuple[str, str], list[dict]] = {}
    for tree in TREES:
        for path in sorted((ROOT / tree).rglob('*.py')):
            src = path.read_text()
            if not _UPDATE.search(src):
                continue
            rel = path.relative_to(ROOT).as_posix()
            parsed = ast.parse(src)
            docstrings = {id(ast.get_docstring(node, clean=False) and node.body[0].value)
                          for node in ast.walk(parsed)
                          if isinstance(node, (ast.Module, ast.ClassDef,
                                               ast.FunctionDef, ast.AsyncFunctionDef))}
            owner = {}

            def walk(node, name):
                for child in ast.iter_child_nodes(node):
                    inner = (child.name if isinstance(child, (ast.FunctionDef,
                                                              ast.AsyncFunctionDef))
                             else name)
                    owner[id(child)] = inner
                    walk(child, inner)

            walk(parsed, '<module>')
            seen_lines = set()
            for node in ast.walk(parsed):
                if not isinstance(node, (ast.Constant, ast.JoinedStr)):
                    continue
                if id(node) in docstrings:
                    continue
                text = ast.get_source_segment(src, node) or ''
                if not _UPDATE.search(text) or node.lineno in seen_lines:
                    continue
                seen_lines.add(node.lineno)
                sets = text.split('SET', 1)[1].split('WHERE')[0]
                if not (_BODY.search(sets) or '{' in sets):
                    continue              # 不写 body、也不是动态拼的:不在射程里
                found.setdefault((rel, owner.get(id(node), '<module>')), []).append(
                    {'line': node.lineno, 'gated': bool(_GATE.search(text)),
                     'dynamic': not _BODY.search(sets)})
    return found


class LinkBodyEntryTests(unittest.TestCase):
    def setUp(self):
        self.found = _statements()

    def test_every_body_write_is_declared(self):
        """**声明集合和扫出来的集合必须完全相等。**

        少声明一条 = 一个没人看过的写入口(第五个入口就是这么漏掉的);多声明一条 =
        那段代码搬走了或改名了,声明跟着失真,下一次漏接照样看不出来。
        """
        self.assertEqual(sorted(DECLARED), sorted(self.found),
                         '写 body 的 UPDATE feed_items 变了:对照 DECLARED 逐条声明来路')

    def test_link_sourced_writes_carry_the_capability_gate(self):
        """逐函数比「带闸几条 / 不带闸几条」。

        不按语句顺序对号,因为顺序会随无关编辑漂移;而条数能同时钉住两边 ——
        `LINK` 少一条带闸的就红,`OWN` 多一条带闸的也红。
        """
        for where, kinds in DECLARED.items():
            with self.subTest(where='/'.join(where)):
                statements = self.found.get(where, [])
                self.assertEqual(len(kinds), len(statements), '语句条数和声明不一致')
                want = Counter(kind == LINK for kind in kinds)
                self.assertEqual(want, Counter(s['gated'] for s in statements),
                                 f'带闸的条数和声明不符:{statements}')
                for statement, kind in zip(sorted(statements, key=lambda s: s['line']),
                                           kinds):
                    if kind == DYNAMIC:
                        self.assertTrue(statement['dynamic'], '声明成动态拼的却写死了列')

    def test_the_scan_sees_the_entries_it_is_supposed_to_see(self):
        """**红 0 条先怀疑用例。** 扫描器自己得先被钉住,不然上面两条恒绿。

        三件事:扫到的总数不是 0、`link_body_sql` 真被认成闸、以及**排除 docstring
        这一道真的在干活**(`link_body_sql` 的那段说明里就有这串字样,不排除的话它会
        作为一条假写入点出现)。
        """
        self.assertGreaterEqual(sum(len(v) for v in self.found.values()), 7)
        gated = [where for where, items in self.found.items()
                 if any(item['gated'] for item in items)]
        self.assertIn(('kb/feeds/grab_store.py', 'replace'), gated)
        self.assertNotIn('link_body_sql', [name for _, name in self.found])


if __name__ == '__main__':
    unittest.main()
