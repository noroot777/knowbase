# -*- coding: utf-8 -*-
"""**每一条写 `feed_items.body` 的 UPDATE,内容从哪来、带哪一道闸,都得声明。** 仅在验收阶段执行。

这条用例替掉的是一张**人手维护的入口名单**。判据原先靠「记得在每个入口调一次
`permits_link_body()`」落实,而名单只能写在注释和一条列举式用例里:前四处都接上了,
第五处(`grab_store.replace`,人点「重新抓一次」那条路)照旧把简报里一条简讯的 28 字摘要
换成了外链那篇仓库 README 的全文 —— 走的是公开接口、不是内部直调,审查实测可达。
漏接的形态是**静默覆盖**:简报不进补抓队列,没有任何一轮同步会把那两句话补回来。

所以判据搬到了**写入层**,而这条用例钉的是「搬完了没有、而且搬对了没有」。

**闸有两道,问的是两个不同的问题**(定义见 `kb/source_types.py`):
- `link_body_sql` / `_eligible()` —— 这一类条目的正文该不该由**通用的 HTTP + 提取器**
  去它外链那一页上取(补抓队列、浏览器采集)。
- `origin_body_sql` —— 这一行的 `url` 指向的**是不是它自己的原文**(人点「重新抓一次」、
  拿推文链接补回长文)。

两者在 rss / 网页专栏 / 手动收录 / 简报上答案一致,**在大V观点上分叉**:推文进不了补抓
队列(通用提取器对 x.com 无效),但它的 `url` 就是它本人的永久链接。所以这条用例比的不是
「带没带闸」而是**「带的是哪一道」** —— 只比前者的那一版对「带错了那一道」恒绿,而带错的
后果实测过:给人工重抓那条路挂上通用路那道闸,3356 条推文里那些空正文/短残片唯一的出路
被整条收走,界面上按钮消失、直接打接口得到的还是一句反过来的解释。

三件事:
- **声明集合必须和扫出来的完全相等** —— 新加一条写 `body` 的 UPDATE 而不声明就红,
  搬走了一条而不改声明也红。这是集合级的红,不是「再补一个实例」。
- **逐函数比「哪几道闸各几条」** —— 删掉某条的闸会红,**把 `ORIGIN` 那条换成 `LINK`
  也红**(这一轮的倒退就是这个形态);**反过来,给 `OWN` 那几条加上闸同样红**:备份恢复
  那一支要把原文原样装回不许可来源的条目上,带上闸之后 rowcount 恒 0,存量复查那条出路
  会整条失效。
- **拼出来的 `UPDATE feed_items` 一个都不许有** —— 见下。

**残留盲区写在这里,别靠读者自己发现。**
- `fulltext._fail` 的 SET 列表是在 Python 里拼出来的(`DYNAMIC`),扫描器看不见里面有
  什么。哪天有人把 `body` 拼进那个列表,这条用例不会红 —— 那一处的判据只能靠它自己那段
  注释。(它那条按「不带闸」比:今天确实不带,而它压根不写 body;哪天要带,声明跟着改。)
- 扫的是单个 `ast.Constant` / `JoinedStr` 节点,所以 `'UPDATE feed_items ' + SETS` 这种
  **跨字面量拼出来**的语句整条都看不见。这一条**已经不再是盲区**:
  `test_no_update_statement_is_assembled_from_pieces` 把「出现 `UPDATE feed_items`、却不
  在同一个字面量里带着 `SET`」的字面量全扫出来,名单必须是空的 —— 要拼就先让那条用例红,
  由人决定怎么声明,而不是悄悄绕过上面两条。
- 两道闸都靠**字样**认(`origin_body_sql` / `link_body_sql` / `_eligible` 出现在语句文本
  里),不求值。所以 `import link_body_sql as origin_body_sql` 这种**改名包装**它认不出
  (实测过:这一层照旧全绿)。那个形态由行为那一层逮住 —— 同一次注入让
  `test_grab_store.py::OriginBodyGateTests` 里推文那条当场红(元规则「两份实现各有自己
  的回归」:这张表管「名单齐不齐、闸对不对」,行为用例管「真拦住了没有」)。
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
#: 只要出现这张表的 UPDATE —— 用来抓「拼出来的」那一类(上面盲区第二条)。
_UPDATE_ANY = re.compile(r'UPDATE\s+feed_items(?![_a-z])', re.I)
#: `body=` 而不是 `body_zh=` / `body_provenance=`。
_BODY = re.compile(r'(?<![a-z_])body\s*=')

LINK = 'link'        # 内容来自对这一行 `url` 发的那次通用抓取 —— 必须带 link 那道闸
ORIGIN = 'origin'    # 内容是拿这一行 `url` 重抓它本人取回来的 —— 必须带 origin 那道闸
OWN = 'own'          # 内容是这一条自己的,不从它的 `url` 来 —— 不许带任何一道闸
DYNAMIC = 'dynamic'  # SET 列表在 Python 里拼,里面没有 body

#: 哪一道闸认哪些字样。`_eligible()` 内部就是两次 `link_body_sql`。
_GATES = {LINK: re.compile(r'link_body_sql|_eligible'),
          ORIGIN: re.compile(r'origin_body_sql')}
#: 声明的来路 → 这条语句该带的那道闸(`''` = 不许带)。
_WANT = {LINK: LINK, ORIGIN: ORIGIN, OWN: '', DYNAMIC: ''}

#: **声明表。** 键是 `(相对路径, 函数名)`,值是该函数里每条语句各自的来路。
DECLARED: dict[tuple[str, str], tuple[str, ...]] = {
    # 正文补抓:内容是通用抓取从 `url` 那一页取回来的。
    ('kb/feeds/fulltext.py', '_save'): (LINK,),
    # 补抓失败只写标记那几列(重试次数、错误、挂起),SET 列表动态拼。
    ('kb/feeds/fulltext.py', '_fail'): (DYNAMIC,),
    # 浏览器采集:人在自己的浏览器里打开 `url` 那一页,把正文交回来 —— 同一条通用路
    # 的手动档(准入判据也是 `permits_link_body`)。
    ('kb/browser_capture.py', 'save'): (LINK,),
    # 「手动补正文」:人把那一页的正文直接粘回来。来路和上面那条**逐字同一条** ——
    # 同一套准入判据、同一份复位列,只是没有「浏览器交回的网址等于目标网址」那一道
    # 证据(断言正文属于这篇文章的是人自己)。所以带的也是同一道闸。
    ('kb/browser_capture.py', 'save_pasted'): (LINK,),
    # 人点「重新抓一次」:拿这一行的 `url` 重抓**它本人**替换库里那一份。
    ('kb/feeds/grab_store.py', 'replace'): (ORIGIN,),
    # X 长文回填:拿推文链接问公开接口,取回**这条推文自己**的长文 —— 和上面那条
    # 同一个来路,所以同一道闸。
    ('scripts/x_article_backfill.py', 'apply_one'): (ORIGIN,),
    # 存量复查写回,两支:许可来源换壳回队列(内容下一轮由通用路从 `url` 那一页来)、
    # 不许可来源把备份里**它自己原本那一份**原样装回去。
    ('scripts/feed_fulltext_recheck.py', '_downgrade'): (LINK, OWN),
    # YouTube 成品写回:内容是它自己那条链的产物(字幕或音频转录),不从 `url` 那一页
    # 来 —— 播放页上只有简介和推荐列表。**它比 OWN 还更钉一层**:语句里写死了
    # `kind='youtube'`,而且那一类的正文另有一道完整性证明(`youtube.products`
    # 的隔离会把任何对不上证明的正文换成占位),所以别人写进去也留不住。
    ('kb/youtube/products.py', 'replace'): (OWN,),
    # 老库补列那一步给 `life` 种值的触发器:SET 里只有 `life` 一列(那一列是「这一行
    # 是哪一世」,见 `kb/feeds/schema.py`),正文一个字节都不碰,所以不带任何一道闸。
    # 它落进这张表只因为那个随机值是插值进去的 —— 而「SET 里有插值」正是这张表要
    # 盯的形状(哪天有人把 `body` 拼进去,声明就得跟着改)。
    ('kb/feeds/schema.py', '_migrate'): (DYNAMIC,),
}


def _literals(src: str):
    """这份源码里每一个**非 docstring** 的字符串字面量,带着它所属的函数名。

    排除 docstring:讲这件事的文档里本来就会出现这些字样(两个 `*_body_sql` 自己那段
    就是),把它算成写入点会让声明表里多出几条假的。
    """
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
    for node in ast.walk(parsed):
        if not isinstance(node, (ast.Constant, ast.JoinedStr)):
            continue
        if id(node) in docstrings:
            continue
        yield node, owner.get(id(node), '<module>')


def _sources():
    for tree in TREES:
        for path in sorted((ROOT / tree).rglob('*.py')):
            src = path.read_text()
            if _UPDATE_ANY.search(src):
                yield path.relative_to(ROOT).as_posix(), src


def _statements() -> dict[tuple[str, str], list[dict]]:
    """扫出每一条 `UPDATE feed_items SET …`,按 `(相对路径, 函数名)` 归堆。"""
    found: dict[tuple[str, str], list[dict]] = {}
    for rel, src in _sources():
        seen_lines = set()
        for node, func in _literals(src):
            text = ast.get_source_segment(src, node) or ''
            if not _UPDATE.search(text) or node.lineno in seen_lines:
                continue
            seen_lines.add(node.lineno)
            sets = text.split('SET', 1)[1].split('WHERE')[0]
            if not (_BODY.search(sets) or '{' in sets):
                continue              # 不写 body、也不是动态拼的:不在射程里
            gates = sorted(name for name, rx in _GATES.items() if rx.search(text))
            found.setdefault((rel, func), []).append(
                {'line': node.lineno, 'gate': '+'.join(gates),
                 'dynamic': not _BODY.search(sets)})
    return found


def _assembled() -> list[tuple[str, int, str]]:
    """出现了这张表的 UPDATE、却没在同一个字面量里带着 `SET` 的那些字面量。

    这是「拼出来的写语句」的信号(`'UPDATE feed_items '` + 别处拼好的 SET 列表):
    上面那个扫描器逐个字面量看,拼起来的整条语句它一条都看不见。
    """
    out = []
    for rel, src in _sources():
        for node, func in _literals(src):
            text = ast.get_source_segment(src, node) or ''
            if _UPDATE_ANY.search(text) and not _UPDATE.search(text):
                out.append((rel, node.lineno, func))
    return out


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

    def test_each_write_carries_the_gate_its_provenance_demands(self):
        """逐函数比「哪一道闸各几条」。

        不按语句顺序对号,因为顺序会随无关编辑漂移;而「哪道闸各几条」这个多重集合能
        同时钉住三边:`LINK` / `ORIGIN` 少带或带错都红,`OWN` 多带一道也红。
        """
        for where, kinds in DECLARED.items():
            with self.subTest(where='/'.join(where)):
                statements = self.found.get(where, [])
                self.assertEqual(len(kinds), len(statements), '语句条数和声明不一致')
                want = Counter(_WANT[kind] for kind in kinds)
                self.assertEqual(want, Counter(s['gate'] for s in statements),
                                 f'带的闸和声明的来路不符:{statements}')
                for statement, kind in zip(sorted(statements, key=lambda s: s['line']),
                                           kinds):
                    if kind == DYNAMIC:
                        self.assertTrue(statement['dynamic'], '声明成动态拼的却写死了列')

    def test_the_scan_sees_the_entries_it_is_supposed_to_see(self):
        """**红 0 条先怀疑用例。** 扫描器自己得先被钉住,不然上面两条恒绿。

        四件事:扫到的总数不是 0、**两道闸各自真被认出来**(一道认不出的话,带错闸
        那种红就成了「两边都是空字符串」的假绿)、以及**排除 docstring 这一道真的在
        干活**(两个 `*_body_sql` 的那段说明里都有这些字样,不排除的话它们会作为假
        写入点出现)。
        """
        self.assertGreaterEqual(sum(len(v) for v in self.found.values()), 7)
        gates = {where: {s['gate'] for s in items} for where, items in self.found.items()}
        self.assertEqual({ORIGIN}, gates[('kb/feeds/grab_store.py', 'replace')])
        self.assertEqual({LINK}, gates[('kb/feeds/fulltext.py', '_save')])
        self.assertNotIn('link_body_sql', [name for _, name in self.found])
        self.assertNotIn('origin_body_sql', [name for _, name in self.found])

    def test_no_update_statement_is_assembled_from_pieces(self):
        """**拼出来的写语句一个都不许有**(上面那段「残留盲区」的第二条)。

        扫描器逐个字面量看,所以 `'UPDATE feed_items ' + SETS` 这种跨字面量拼出来的
        语句整条都看不见 —— 声明表里不会多一行、闸也无人问。这条用例把那个形态本身
        拦掉:要拼就先让它红,由人决定怎么声明。
        """
        self.assertEqual([], _assembled(),
                         '有 UPDATE feed_items 是拼出来的,上面那个扫描器看不见它')


if __name__ == '__main__':
    unittest.main()
