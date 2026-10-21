# -*- coding: utf-8 -*-
"""列表页上的发布时间:一段文字怎么读,以及**整页一次**把它们索引起来。

**硬约束在 `docs/decisions/page-list.md` 第一节 —— 改这块之前先读那一份。**

从 `page_list` 拆出来的,因为那一份已经顶到 700 行上限,而"日期怎么读"是
一条独立职责:它不认识链接、分组、记录容器,只认文字和节点区间。

**为什么要索引,而不是每问一次就扫一遍子树。** `item_published(node)` 的
语义是"把 node 整片子树的文字拼起来、找第一个日期",所以**逐个问**的写法
一律是二次的 —— 而上移这件事天然要逐层问祖先(日期跟链接常常是兄弟而不是
祖孙)。实测过三条都会退化的路径:非法日期跨两个文本节点时逐层回退(16000
层 20 秒)、每层都有空 `<time>` 时候选和祖先一样多(1600 层 0.84 秒)、日期
在卡片外层时向外逐层查(3200 层 1.6 秒)。**按节点记备忘挡不住这些** ——
每个被问的节点只算一次,并不保证它的**后代**只被遍历固定次数,不同祖先的
查询会一次又一次走到同一片后代。

**所以这里换成"整页一次、之后按区间查"**:
- 把整页所有文本节点按文档顺序归一化空白、用单空格连成一串 `text`,每个
  节点记下自己那段的 `[lo, hi)`。这一串和 `' '.join(node.get_text(' ')
  .split())` **逐字相同**(两边都是"所有非空白 token 按序、单空格分隔"),
  在两份真实样本的 1418 个标签上逐字对过。
- 四个日期正则各在 `text` 上 `finditer` 一次,命中位置排好序。
- 查一个节点 = 在它区间里二分找第一个命中。

于是 `O(页面)` 建一次、`O(log n)` 查一次,**同一片子树只被遍历固定次数**,
不依赖"日期形状一般很稀疏"这类输入假设。

**这同时把一个语义裂缝补上了。** 上一版的剪枝按**单个文本节点**看有没有
日期形状,而提取按**拼接后的子树文字** —— `<span>Oct 20,</span>`
`<span>2026</span>` 拼起来才是日期,两个文本节点各自都不是,于是那一层
压根不进候选、里面读得出日期的容器一个都没被问到,整页报"找不到带发布时间
的文章列表"(实测两篇真文章一条都收不到)。现在候选和提取是**同一串文字、
同一套正则、同一个优先级**,裂缝不存在了。
"""

from __future__ import annotations

import re
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bs4 import NavigableString

TZ = ZoneInfo('Asia/Shanghai')

_MONTH = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
     'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
_ISO = re.compile(r'\b(\d{4})-(\d{1,2})-(\d{1,2})'
                  r'(?:[T ](\d{1,2}):(\d{2})(?::\d{2})?(?:\.\d+)?'
                  r'(Z|z|[+-]\d{2}:?\d{2})?)?')
_CN = re.compile(r'\b(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_MDY = re.compile(r'\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b')
_DMY = re.compile(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b')
# 优先级:ISO 最明确,中文次之,英文月名两种写法最后。顺序不许改 —— 一段
# 文字里同时出现两种写法时,换顺序就换了结论。
_PATTERNS = (_ISO, _CN, _MDY, _DMY)


def _day(y: int, m: int, d: int) -> str:
    """`YYYY-MM-DD`,算不成一个真实日期就返回空串(挡住 2 月 30 日那类)。"""
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return ''


def _stamp(pattern, m) -> tuple[bool, str]:
    """一处命中 → `(这一处算不算定局, 时间)`。

    **"定局"和"读出来了"是两件事,别合并。** `_ISO` / `_CN` 命中就定局 ——
    哪怕日子非法(`2026-02-30`)也返回空、不再去试后面的写法,这是日期提取
    器的**既有边界**(原生容器里一个非法编号会遮住后面的文字日期),动它是
    另一件事。而 `_MDY` / `_DMY` 命中、月份词却不认识(`Hello 20, 2026`)时
    **不定局**,要接着试下一种写法 —— 这两条分支原先就写在 `read_published`
    的 `if month := ...` 里面。

    索引查询和 `read_published` 都过这个函数:同一段文字两边必须给同一个
    答案,所以口径只有一份。
    """
    if pattern is _ISO:
        day = _day(int(m[1]), int(m[2]), int(m[3]))
        if not day or not m[4]:
            return True, day
        hour, minute = int(m[4]), int(m[5])
        if hour > 23 or minute > 59:
            return True, day
        stamp = datetime.fromisoformat(f'{day}T{hour:02d}:{minute:02d}')
        if zone := m[6]:
            if zone in ('Z', 'z'):
                offset = timezone.utc
            else:
                sign = -1 if zone[0] == '-' else 1
                raw = zone[1:].replace(':', '')
                offset = timezone(sign * timedelta(hours=int(raw[:2]), minutes=int(raw[2:])))
            stamp = stamp.replace(tzinfo=offset).astimezone(TZ)
        return True, stamp.strftime('%Y-%m-%d %H:%M')
    if pattern is _CN:
        return True, _day(int(m[1]), int(m[2]), int(m[3]))
    if pattern is _MDY:
        if month := _MONTH.get(m[1][:3].lower()):
            return True, _day(int(m[3]), month, int(m[2]))
        return False, ''
    if month := _MONTH.get(m[2][:3].lower()):
        return True, _day(int(m[3]), month, int(m[1]))
    return False, ''


def read_published(text: str) -> str:
    """一段文字里的发布时间 → `YYYY-MM-DD HH:MM` / `YYYY-MM-DD` / 空串。

    精度**如实保留**:列表页给到日就只给到日,不补一个 00:00 假装知道几点
    (`sync._within_history` 和 `daily.parse_ts` 对只有日期的写法都成立,
    库里本来就有 92 篇是这种)。

    不带时区的写法一律按**项目时区**补齐,不按部署机器的 —— 口径同
    `x_browser._utc`:被比较的那一头(`parse_feed` 的 `published`)固定按
    Asia/Shanghai 格式化,两头不同时区的话每天有 8 小时窗口会差一整天。
    """
    t = ' '.join((text or '').split())
    if not t:
        return ''
    for pattern in _PATTERNS:
        if m := pattern.search(t):
            decided, stamp = _stamp(pattern, m)
            if decided:
                return stamp
    return ''


class DateIndex:
    """整页建一次,之后按节点区间查发布时间。

    建一次是 `O(页面)`,查一次是 `O(log n)`。**这就是"同一片子树只被遍历
    固定次数"那条不变量的落点** —— 它不依赖"日期形状很稀疏"、也不留"实在
    不行就逐层重扫"的后路,两者都被实测推翻过(见模块开头)。

    **做法**:整页节点按文档顺序(前序)编号一次,于是任何节点的子树都是
    一段**连续**的编号区间 `[p, p + size)`;文本按同一顺序归一化拼成一串,
    每个节点的文字就是这串上的一段 `[lo, hi)`;四个日期正则各在这串上
    `finditer` 一次。查一个节点 = 在它那一段里二分找第一处命中。

    **子树大小按逆前序累加算,不沿 parent 链往上走。** 沿 parent 链走是
    `O(文本节点 × 深度)` —— 每层一个字的深嵌套页面上又是二次,而那正是这
    一轮要消掉的东西。

    `root` 可以是整棵 soup,也可以是任意一个标签(`item_published` 不带索引
    调用时就这么用),语义一样。
    """

    def __init__(self, root):
        order = [root] + list(root.descendants)
        self._pre = {id(node): i for i, node in enumerate(order)}
        size = [1] * len(order)
        for i in range(len(order) - 1, 0, -1):
            size[self._pre[id(order[i].parent)]] += size[i]
        self._size = size
        self.text, spans = _flatten(order)
        self._spans = spans
        self._first_text = _scan_forward(spans)
        self._last_text = _scan_backward(spans)
        self._times, self._time_at = _time_stamps(order)
        self._first_time = _scan_forward(self._time_at)
        # 从第 j 个 `<time>` 起,第一个**读得出**日期的是第几个。`<time>` 优先
        # 这条语义是"子树里第一个读得出日期的 time",不是"第一个 time"。
        self._next_read = [len(self._times)] * (len(self._times) + 1)
        for j in range(len(self._times) - 1, -1, -1):
            self._next_read[j] = j if self._times[j][1] else self._next_read[j + 1]
        self._hits, self._starts = {}, {}
        for pattern in _PATTERNS:
            self._hits[pattern] = list(pattern.finditer(self.text))
            self._starts[pattern] = [m.start() for m in self._hits[pattern]]

    def subtree(self, node) -> tuple[int, int]:
        """这个节点的子树在前序编号上的那一段 `[start, end)`。

        给调用方问"某片子树里有几个某种东西"用的:前序编号下子树是**连续
        一段**,于是那种计数可以靠二分做到 `O(log n)`,不必沿 parent 链逐个
        往上比 —— 后者是 `O(条数 × 深度)`,深嵌套页面上又是二次。
        """
        start = self._pre[id(node)]
        return start, start + self._size[start]

    def published(self, node) -> str:
        """这个节点的发布时间 —— 语义同原先的"拼起子树文字再读"。"""
        start = self._pre[id(node)]
        end = start + self._size[start]
        if stamp := self._from_time(start, end):
            return stamp
        return self._from_text(start, end)

    def _from_time(self, start: int, end: int) -> str:
        # **从 `start + 1` 起,节点自己那个 `<time>` 不算。** 原先问的是
        # `node.find_all('time')`,bs4 的 `find_all` 只找**后代** —— 把自己
        # 算进去的话,一个 `<time datetime=...></time>` 节点本身的答案就从
        # 空变成了那个日期。差分测试在 400 份随机输入上逮到这一类 81 处。
        at = self._first_time[start + 1]
        if at >= end:
            return ''
        j = self._next_read[self._time_at[at][0]]
        if j < len(self._times) and self._times[j][0] < end:
            return self._times[j][1]
        return ''

    def _from_text(self, start: int, end: int) -> str:
        first, last = self._first_text[start], self._last_text[end - 1]
        if first >= end or last < start:
            return ''
        lo, hi = self._spans[first][0], self._spans[last][1]
        # 和 `read_published` 同一个循环:按写法的优先级依次问"这一段上这种
        # 写法的第一处命中",**不定局就接着试下一种** —— 那正是它
        # `if month := _MONTH.get(...)` 不成立时落到下一个分支的那条路。
        for pattern in _PATTERNS:
            if m := self._first_hit(pattern, lo, hi):
                decided, stamp = _stamp(pattern, m)
                if decided:
                    return stamp
        return ''

    def _first_hit(self, pattern, lo: int, hi: int):
        """`[lo, hi)` 这一段上这种写法的第一处命中 —— 同 `pattern.search(那段)`。"""
        hits, starts = self._hits[pattern], self._starts[pattern]
        i = bisect_left(starts, lo)
        while i < len(hits) and hits[i].start() < hi:
            if hits[i].end() <= hi:
                return hits[i]
            # 这一处伸出了节点的右边界。整页那一串上它是一处命中,在这个节点
            # 自己的文字上却可能是**另一处更短的**命中(`<time>` 里写
            # `2026-10-20`、右边兄弟写 `12:30` 时,整页串上两段连起来才被
            # `_ISO` 一口吃掉)。所以按截到 `hi` 的那一段重试一次,重试不成
            # 才算这一处在本节点上不存在、接着往后找。
            if m := pattern.match(self.text, hits[i].start(), hi):
                return m
            i += 1
        return None


def _flatten(order: list) -> tuple[str, list]:
    """整页文字归一化拼成一串;顺带记下每个文本节点占这串的哪一段。

    和 `' '.join(node.get_text(' ').split())` **逐字相同**:两边都是"所有
    非空白 token 按文档顺序、单空格分隔"。`type(...) is NavigableString`
    而不是 `isinstance`,是为了把 `Comment` / `CData` 排除掉 —— `get_text`
    也不要它们,口径得一致。
    """
    parts, spans, off = [], [None] * len(order), 0
    for i, node in enumerate(order):
        if type(node) is not NavigableString:
            continue
        if norm := ' '.join(node.split()):
            spans[i] = (off, off + len(norm))
            parts.append(norm)
            off += len(norm) + 1
    return ' '.join(parts), spans


def _time_stamps(order: list) -> tuple[list, list]:
    """整页每个 `<time>` 读出来的时间(可能是空),以及它排第几。"""
    times, at = [], [None] * len(order)
    for i, node in enumerate(order):
        if getattr(node, 'name', None) != 'time':
            continue
        stamp = (read_published(node.get('datetime') or '')
                 or read_published(node.get_text(' ')))
        at[i] = (len(times), stamp)
        times.append((i, stamp))
    return times, at


def _scan_forward(marks: list) -> list:
    """`out[i]` = 从 i 起(含)第一个有标记的下标;没有就给越界值。"""
    out = [len(marks)] * (len(marks) + 1)
    for i in range(len(marks) - 1, -1, -1):
        out[i] = i if marks[i] is not None else out[i + 1]
    return out


def _scan_backward(marks: list) -> list:
    """`out[i]` = 从 i 往前(含)第一个有标记的下标;没有就给 -1。"""
    out = [-1] * len(marks)
    last = -1
    for i, mark in enumerate(marks):
        last = i if mark is not None else last
        out[i] = last
    return out


def item_published(node, index: DateIndex | None = None) -> str:
    """这条记录的发布时间。`<time>` 优先,它是"这是个时间"的标准语义。

    **不带索引时就地建一个**,而不是走另一条"直接扫子树"的实现 —— 同一个
    问题两处各判一遍必然会漂(上一版就是这么漂出一道语义裂缝的,见模块开头)。
    就地建的那个只覆盖 `node` 自己的子树,一次调用的代价和原先扫一遍同阶。
    """
    return (index if index is not None else DateIndex(node)).published(node)
