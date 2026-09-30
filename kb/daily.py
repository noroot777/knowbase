# -*- coding: utf-8 -*-
"""日报:把一段时间里**生产出来**的内容(公众号、大V观点、RSS、视频…)写成一份综述。

素材取自 kb.db 的 documents —— 也就是说日报只认"已经落进 vault 并索引过"的
内容,不认识任何具体来源。这是故意的:新增一种来源,日报自动就覆盖了。

**窗口按内容的生产时间(`published`)算,不是抓取/入库时间(`indexed_at`)。**
两者差几天是常态:实测一批 RSS 文章发布于 09-26,09-29 才被抓进来。按入库
时间出日报,读到的是"今天抓到了什么",而人想知道的是"这段时间世界上发生了
什么"。抓取时间只用来回答另一个问题——哪些东西是这一轮才拿到的(见"补录")。

窗口也不是自然日,而是**上一份日报的截止时刻到这一次的生成时刻**:日报在
早上七点出,那它该覆盖的就是昨天七点到今天七点,而不是"昨天 00:00-24:00"
(后者会漏掉昨晚七点之后的一切,再也不会被任何一份日报提到)。上一份的截止
时刻记在它自己的 frontmatter 里(`window_until`),所以链条自描述:删掉状态
文件也能接上,补生成一份旧的也不会让后面的窗口错位。

产物是 vault 里的一篇 markdown(`<vault>/daily/YYYY-MM-DD.md`),不是数据库里的
一行:kb.db 是可以随时删掉重建的索引,而日报是花了模型钱写出来的东西,重建索引
不该把它弄丢;放进 vault 也顺带让 Obsidian 那头能直接读。这个目录不在
`kb.config.SOURCES` 的任何一棵树下,所以日报自己不会被索引进库、更不会成为
下一份日报的素材。
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from kb.config import PRIMARY_SOURCE_NAMES, VAULT_ROOT
from kb.scanner import parse_frontmatter
from kb.source_types import SOURCE_TYPES
from kb.store import promo_exclude_sql

# 一份日报最多带多少条素材进 prompt。一天正常十几到几十条;真有几百条时
# 取最新的那批,并在日报里如实写明"另有 N 条未纳入"。
MAX_ITEMS = 120
LATE_ITEMS = 30         # 补录那一段最多带多少条
EXCERPT = 400           # 每条素材的摘要上限(字符)
DIR_NAME = 'daily'
# **只有**在一份日报都没有、接不上任何截止时刻时回溯多久(天)。
FIRST_DAYS = 1
STAMP = '%Y-%m-%d %H:%M:%S'

# 来源名 → 人话。feed 类来源的标签跟着 SOURCE_TYPES 走,其余几棵树是固定的。
_SOURCE_LABEL = {
    'dr-gzh': '公众号', 'gzh': '公众号', 'dr': 'AI 日报',
    'dr-youtube-dig': 'YouTube 视频',
    **{k: v.label for k, v in SOURCE_TYPES.items()},
}

PROMPT = """你是这个知识库主人的情报助理。下面是 {range_label} 这段时间里**发布**的 {n} 条内容\
(来自公众号、社交观点、RSS 订阅和视频摘要)。请写一份日报。

输出要求:
- 全中文,直接输出 markdown 正文,不要代码围栏,不要重复标题行。
- 每条结论后面用编号标注出处,如 [3][7];编号就是下面列表的序号。
- 只依据下面给出的材料写,不要编造事实、数字、评价;材料没说的不要推断。
- 不要逐条复述所有内容,该合并的合并,该舍弃的舍弃。

结构严格如下:

## 今日要点

3~6 条真正值得知道的事。每条一个列表项:**一句话点明事件**,再用一两句说清\
"是什么、为什么值得注意"。教程、旧闻盘点、招聘、纯营销不算要点。

## 工具与项目

这段时间出现的、可以拿来用的开源项目或产品。每条一行:名字 — 一句话说明它解决什么问题。\
没有就写"这段时间没有值得单独拿出来的新工具"。

## 观点与讨论

社交观点和评论里值得留意的看法,1~4 条。没有就写一行"没有明显的讨论热点"。

## 一句话速览

其余内容各一行,一行不超过 30 字。最多 12 行,超出的不写。
{late_section}
材料:
{items}
"""

_LATE_SECTION = """
## 补录

材料里标了「补录」的,是发布时间更早、这一轮才抓进来的内容。每条一行,\
写清它是什么、发布于什么时候。上面四节不要提它们。
"""


def report_dir() -> Path:
    return VAULT_ROOT / DIR_NAME


def _label(source: str) -> str:
    return _SOURCE_LABEL.get(source, source)


def parse_ts(value) -> datetime | None:
    """把库里的时间字符串解析成本地朴素时间;认不出来给 None。

    `published` 在库里实测有四种写法:`2026-09-28 17:08`(2.1 万篇)、
    `2026-06-14T11:29:37+08:00`(1178 篇)、`2026-09-29`(92 篇)、空(1 篇)。
    **所以不能拿字符串直接比大小**:同一天里 `T`(0x54)排在空格(0x20)后面,
    `2026-09-29T07:00:00+08:00` 字典序上大于 `2026-09-29 08:00`,按字符串筛
    窗口会把早上七点发的内容算进八点才开始的窗口。带时区的先换算到本地,
    只有日期的按当天 00:00 —— 那是这种写法唯一能确定的下界。
    """
    s = str(value or '').strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


@dataclass
class Material:
    """一次取材的结果。`late` 是"发布得更早、这一轮才入库"的那批。"""
    items: list[dict] = field(default_factory=list)
    total: int = 0
    late: list[dict] = field(default_factory=list)
    late_total: int = 0

    def all(self) -> list[dict]:
        return [*self.items, *self.late]

    def __bool__(self) -> bool:
        return bool(self.items or self.late)


def _label_window(since: datetime, until: datetime) -> str:
    same = since.date() == until.date()
    return (f"{since:%Y-%m-%d %H:%M} 至 {until:%H:%M}" if same
            else f"{since:%Y-%m-%d %H:%M} 至 {until:%Y-%m-%d %H:%M}")


_FIELDS = ('SELECT d.path,d.source,d.title,d.account,d.published,d.url,d.summary,'
           'd.indexed_at FROM documents d WHERE ')


def _scope() -> tuple[str, list]:
    """所有取材共用的口径:只看非衍生来源 + 挡掉 hard 软广。

    条目化产物(dr-gzh-projects 等)与原文是同一批内容,两份都进 prompt 的话
    同一个工具会在日报里出现两次;软广口径跟检索共用 `promo_exclude_sql`。
    """
    names = ','.join('?' * len(PRIMARY_SOURCE_NAMES))
    return f'd.source IN ({names}) AND {promo_exclude_sql("d")}', list(PRIMARY_SOURCE_NAMES)


def _enrich(conn, row, when: datetime | None, late: bool) -> dict:
    doc = dict(row)
    doc['when'] = when
    doc['late'] = late
    doc['entities'] = [e[0] for e in conn.execute(
        'SELECT entity FROM entities WHERE path=? LIMIT 8', (doc['path'],))]
    doc['topics'] = [t[0].split('/', 1)[-1] for t in conn.execute(
        "SELECT tag FROM doc_tags WHERE path=? AND tag LIKE 'topic/%'", (doc['path'],))]
    doc['excerpt'] = (doc['summary'] or '').strip()
    if not doc['excerpt']:
        # 没提取过的文档没有 summary(提取失败、或者刚索引完还没轮到它)。
        # 拿第一个 chunk 顶上,总比把这条从日报里整个漏掉强。
        first = conn.execute(
            'SELECT text FROM chunks WHERE path=? ORDER BY seq LIMIT 1',
            (doc['path'],)).fetchone()
        doc['excerpt'] = (first[0] if first else '').strip()
    doc['excerpt'] = doc['excerpt'][:EXCERPT]
    return doc


def collect(conn, since: datetime, until: datetime, limit: int = MAX_ITEMS) -> Material:
    """取窗口 (since, until] 内**发布**的素材,外加这一轮才入库的早前内容。

    SQL 那一层只按日期前缀粗筛(两头各放一天,给时区换算留余量),精确到分钟的
    判断在 Python 里按解析出来的时间做 —— 库里四种时间写法互相之间没法用字符串
    比大小,见 `parse_ts`。粗筛之后只剩两三天的量,排序开销可以忽略。
    """
    where, args = _scope()
    lo = (since.date() - timedelta(days=1)).isoformat()
    hi = (until.date() + timedelta(days=1)).isoformat()
    picked = []
    for row in conn.execute(_FIELDS + where + ' AND substr(d.published,1,10) BETWEEN ? AND ?',
                            [*args, lo, hi]):
        when = parse_ts(row['published'])
        if when and since < when <= until:
            picked.append((when, row))
    picked.sort(key=lambda p: p[0], reverse=True)
    items = [_enrich(conn, row, when, False) for when, row in picked[:limit]]

    # 补录:发布时间落在窗口之前(或者压根没有发布时间),但这一轮才入库。
    # 不捡回来的话它永远进不了任何一份日报 —— 实测 RSS 差三天是常事。
    # 判据用 indexed_at,它在库里只有 `YYYY-MM-DD HH:MM:SS` 一种写法,可以直接比。
    late = []
    for row in conn.execute(_FIELDS + where + ' AND d.indexed_at > ? AND d.indexed_at <= ?'
                            ' ORDER BY d.indexed_at DESC',
                            [*args, since.strftime(STAMP), until.strftime(STAMP)]):
        when = parse_ts(row['published'])
        if when is None or when <= since:
            late.append((when, row))
    return Material(items=items, total=len(picked),
                    late=[_enrich(conn, row, when, True) for when, row in late[:LATE_ITEMS]],
                    late_total=len(late))


def _material(items: list[dict]) -> str:
    lines = []
    for i, d in enumerate(items, 1):
        mark = f"补录·发布于 {d['when']:%m-%d %H:%M}" if d['late'] and d['when'] else \
               '补录·无发布时间' if d['late'] else f"{d['when']:%m-%d %H:%M}"
        head = f"{i}. [{mark}|{_label(d['source'])}·{d['account'] or '未署名'}] {d['title']}"
        tail = []
        if d['entities']:
            tail.append('涉及: ' + '、'.join(d['entities'][:6]))
        if d['topics']:
            tail.append('主题: ' + '、'.join(d['topics'][:4]))
        lines.append(head + '\n   ' + (d['excerpt'] or '(无摘要)')
                     + ('\n   ' + ';'.join(tail) if tail else ''))
    return '\n'.join(lines)


# 一行正文里**不许被改写**的片段,跟出处编号一起匹配:谁命中决定动不动它。
# 光靠「后面别跟 `(`」挡不住两种真实形态(2026-09-30 审查实测,都是先写坏文件、
# 页面/复制/Obsidian 一起受影响,不是渲染层的事):
#   ① 行内代码 `items[1]` —— 落盘后成了 `items[\[1\]](<url> "…")`,页面上那个
#      <code> 里就明晃晃显示这一串,照着复制的命令当场是错的;
#   ② 已有链接的文字 `[版本 [1]](https://…)` —— 里面那个 `[1]` 后面跟的是 `]`
#      不是 `(`,照样被改写,一条链接当场裂成两个不同目的地。
# 所以按 markdown 的构造跳:行内代码(开闭反引号数量相同)、链接/图片整体
# (文字里允许一层方括号,目标允许 `<>` 包裹或平衡圆括号)。
_GUARD_RE = re.compile(
    r'(?P<tick>`+).*?(?P=tick)'
    r'|!?\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\]'
    r'\((?:<[^>]*>|[^()\s]*(?:\([^()]*\)[^()\s]*)*)(?:\s+"[^"]*")?\)'
    r'|\[(?P<nums>\d{1,3}(?:\s*[,，、]\s*\d{1,3})*)\]')
_FENCE_RE = re.compile(r'^\s*(?:```|~~~)')


def _md_dest(url: str) -> str:
    """markdown 链接的目标一律用尖括号包起来。

    库里的 url 实测有带圆括号的(`…-(clean).pdf`、`S2589-0042(26)00811-4`),
    裸写进 `](…)` 里会在第一个 `)` 处提前收尾,链接当场断掉、后半截漏成正文。
    尖括号里唯一不能裸出现的是 `<` `>` 本身,转义掉。
    """
    return '<' + ' '.join(str(url or '').split()).replace('<', '%3C').replace('>', '%3E') + '>'


def _md_text(text: str) -> str:
    """链接文字里的方括号一律转义。

    库里十来篇标题带着 `[视频]`、`[四川话、粤语等]` 这种写法,平衡的还好,
    落单一个就能把素材清单那条链接从中间截断 —— 后半截标题漏成正文、url
    露在外面。
    """
    return (str(text or '').replace('\\', '\\\\')
            .replace('[', '\\[').replace(']', '\\]'))


def _md_title(text: str) -> str:
    return ' '.join(str(text or '').split()).replace('\\', '\\\\').replace('"', '\\"')


def _link_refs(body: str, shown: list[dict]) -> str:
    """把模型写的出处编号变成指向原文的链接。

    **写进文件、不留到阅读时**:日报是拿走就读的产物(复制出去、在 Obsidian 里
    翻回来),编号只在 kb-web 上可点的话,换一个读法就退回一串对不上号的数字。
    素材清单本来就带着每条的链接,正文这几个编号指的是同一批东西。

    链接文字保留方括号(`[\[18\]](url)`),读起来还是原来的角标;转义是因为
    Obsidian 会把裸 `[[18]]` 当成 wikilink。编号对不上素材(模型偶尔会编一个
    超出范围的)或那条素材没有 url 时原样留着,不编一个链接出来。

    围栏代码、行内代码、已有的链接与图片整段跳过(见 `_GUARD_RE`)——那里面的
    数字方括号不是出处,改了就是把别人的正文改坏。
    """
    links = {}
    for i, d in enumerate(shown, 1):
        if d.get('url'):
            links[str(i)] = (d['url'],
                             f"{d['title']} — {_label(d['source'])}·{d['account'] or '未署名'}")
    if not links:
        return body

    def _one(num: str) -> str:
        hit = links.get(num)
        return f'[\\[{num}\\]]({_md_dest(hit[0])} "{_md_title(hit[1])}")' if hit else f'[{num}]'

    def _sub(m) -> str:
        if m.group('nums') is None:
            return m.group(0)       # 行内代码 / 已有链接 / 图片:一个字都不动
        nums = [n.strip() for n in re.split(r'[,，、]', m.group('nums'))]
        # 一个都对不上就整段别动:那多半不是出处编号(年份区间、条目序号之类)。
        return ''.join(_one(n) for n in nums) if any(n in links for n in nums) else m.group(0)

    out, fenced = [], False
    for line in body.split('\n'):
        if _FENCE_RE.match(line):
            fenced = not fenced
        out.append(line if fenced else _GUARD_RE.sub(_sub, line))
    return '\n'.join(out)


def _page(since: datetime, until: datetime, mat: Material, body: str,
          model: str, sync_issues: list[dict] | None = None) -> str:
    shown = mat.all()
    counts: dict[str, int] = {}
    for d in shown:
        counts[_label(d['source'])] = counts.get(_label(d['source']), 0) + 1
    # 哪些来源这一轮没同步成功。**必须写进文件本身**:日报是拿走就读的产物
    # (复制、Obsidian、明天再翻),面板上的提示留不住。一份"只用旧素材"的日报
    # 和一份"全都同步到了"的日报长得一模一样,那是在骗人(2026-09-30 审查实测:
    # 三个来源全部连接失败,页面照样只有一句绿色的「日报已生成」)。
    issues = [f"{i.get('label') or i.get('channel')}：{i.get('detail') or '未同步成功'}"
              for i in (sync_issues or [])]
    meta = {
        'type': 'daily-report', 'date': until.date().isoformat(),
        'window_since': since.strftime(STAMP), 'window_until': until.strftime(STAMP),
        'docs': len(mat.items), 'total': mat.total,
        'late': len(mat.late), 'late_total': mat.late_total,
        'sources': counts, 'model': model,
        'generated': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    if issues:
        meta['sync_failed'] = issues
    refs = '\n'.join(
        f"{i}. [{_md_text(d['title'])}]({_md_dest(d['url'])})"
        f" — {_label(d['source'])}·{d['account'] or '未署名'}"
        f"{' · 补录' if d['late'] else ''}"
        if d['url'] else
        f"{i}. {d['title']} — {_label(d['source'])}·{d['account'] or '未署名'}"
        f"{' · 补录' if d['late'] else ''}"
        for i, d in enumerate(shown, 1))
    more = []
    if mat.total > len(mat.items):
        more.append(f'窗口内共 {mat.total} 条,以上列出最新的 {len(mat.items)} 条。')
    if mat.late_total > len(mat.late):
        more.append(f'补录共 {mat.late_total} 条,以上列出最近入库的 {len(mat.late)} 条。')
    tail = ('\n\n> ' + ' '.join(more)) if more else ''
    head = (f'# 日报 · {until:%Y-%m-%d}\n\n'
            f'> 覆盖 {_label_window(since, until)} 发布的内容(按发布时间,不是抓取时间)。\n\n')
    if issues:
        head += (f'> **本轮有 {len(issues)} 个来源没有同步成功**,'
                 '以下内容只来自已经入库的素材:\n'
                 + ''.join(f'> - {t}\n' for t in issues) + '\n')
    return ('---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---\n\n'
            + head + f'{_link_refs(body.strip(), shown)}\n\n'
            f'## 素材清单({len(shown)} 条)\n\n{refs}{tail}\n')


def _write(path: Path, text: str) -> None:
    """同目录唯一临时文件 + rename:写到一半失败时,昨天那份日报逐字节不变。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def next_window(first_days: int = FIRST_DAYS,
                now: datetime | None = None) -> tuple[datetime, datetime]:
    """(since, until)。起点是**上一份日报的截止时刻**,隔多久都照接。

    算起点时跳过"今天这一份":重新生成今天的日报时,要的是从昨天那一刻起的
    完整一天,而不是从今早那份的截止时刻起的几个小时 —— 后者会把今天上午
    已经写进旧版本的内容整段丢掉(那一份马上要被同名文件覆盖掉)。

    **这里没有"最长回溯几天"的上限。** 上限会把起点往后挪,被挪过去的那段
    内容就再也进不了任何一份日报:服务停四天、上限三天的话,第一天整段永久
    消失,而且一行提示都没有。素材太多是"限量"问题——`collect` 按条数截到
    MAX_ITEMS,日报里如实写明"窗口内共 N 条,以上列出最新的 M 条",能补;
    窗口断链不能补。只有一份日报都没有、接不上任何截止时刻时才回溯
    `first_days` 天,那时没有"上一份"可言,回溯多久是个纯粹的偏好。
    """
    until = (now or datetime.now()).replace(microsecond=0)
    today = until.date().isoformat()
    marks = [parse_ts(r.get('window_until')) for r in reports() if r['date'] != today]
    marks = [m for m in marks if m and m < until]
    return (max(marks) if marks else
            until - timedelta(days=max(1, int(first_days or FIRST_DAYS)))), until


def generate(since: datetime | None = None, until: datetime | None = None,
             first_days: int = FIRST_DAYS, model=None, progress=None,
             sync_issues: list[dict] | None = None) -> dict:
    """生成一份日报并落盘,返回 {date, since, until, docs, total, late, path, model}。

    素材为空时抛 LookupError(那不是失败,是"这段时间没有新内容"),调用方据此
    提示,而不是拿一份空日报盖掉可能已经存在的那份。

    `sync_issues` 是这一轮没同步成功的通道(`daily_run` 给的通道结果),原样写进
    日报:允许降级用已有内容出报,但不能让它看起来像是全都同步到了。
    """
    from kb import llm, llm_stages, store

    if since is None or until is None:
        auto_since, auto_until = next_window(first_days)
        since, until = since or auto_since, until or auto_until
    if since >= until:
        raise ValueError('日报的起始时刻必须早于截止时刻')
    conn = store.connect()
    try:
        mat = collect(conn, since, until)
    finally:
        conn.close()
    if not mat:
        raise LookupError(f'{_label_window(since, until)} 没有已入库的内容,先同步再生成日报')
    picked = model or llm_stages.stage_model('daily')
    shown = mat.all()
    if progress:
        progress(f'正在让模型综合 {len(shown)} 条内容')
    body = llm.chat(PROMPT.format(range_label=_label_window(since, until),
                                  n=len(shown), items=_material(shown),
                                  late_section=_LATE_SECTION if mat.late else ''),
                    picked, max_tokens=8192, timeout=600)
    if not body.strip():
        raise RuntimeError('模型返回了空日报,请稍后重试或换一个模型')
    path = report_dir() / f'{until.date().isoformat()}.md'
    _write(path, _page(since, until, mat, body, str(picked), sync_issues))
    return {'date': until.date().isoformat(), 'since': since.strftime(STAMP),
            'until': until.strftime(STAMP), 'path': str(path), 'docs': len(mat.items),
            'total': mat.total, 'late': len(mat.late), 'model': str(picked)}


# ── 已生成的日报 ────────────────────────────────────────────────────────────────

def _entry(path: Path, with_body: bool = False) -> dict:
    meta, body = parse_frontmatter(path.read_text(encoding='utf-8'))
    issues = meta.get('sync_failed')
    out = {'date': str(meta.get('date') or path.stem), 'docs': int(meta.get('docs') or 0),
           'total': int(meta.get('total') or 0), 'late': int(meta.get('late') or 0),
           'model': str(meta.get('model') or ''),
           'generated': str(meta.get('generated') or ''),
           'sources': meta.get('sources') if isinstance(meta.get('sources'), dict) else {},
           'window_since': str(meta.get('window_since') or ''),
           'window_until': str(meta.get('window_until') or ''),
           'sync_failed': [str(t) for t in issues] if isinstance(issues, list) else []}
    if with_body:
        out['body'] = body
    return out


def _path_for(day: str) -> Path:
    """按日期取文件路径。日期先过 fromisoformat —— 它同时是校验:
    这个值来自 HTTP 参数,不校验就等于把整个 vault 变成可读的。"""
    return report_dir() / f'{Date.fromisoformat(day).isoformat()}.md'


def reports(limit: int = 60) -> list[dict]:
    root = report_dir()
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob('*.md'), reverse=True)[:limit]:
        try:
            out.append(_entry(path))
        except (OSError, ValueError):
            continue            # 手工放进来的文件不该让整个列表打不开
    return out


def report(day: str) -> dict | None:
    path = _path_for(day)
    return _entry(path, with_body=True) if path.is_file() else None


def delete(day: str) -> bool:
    path = _path_for(day)
    if not path.is_file():
        return False
    path.unlink()
    return True
