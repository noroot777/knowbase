# -*- coding: utf-8 -*-
"""日报:把一段时间里**生产出来**的内容(公众号、大V观点、RSS、视频…)写成一份综述。

**硬约束在 `docs/decisions/daily.md` 第一节 —— 改这块之前先读那一份。**

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

**「这一条上报过没有」按账本判,不靠时间窗推断**(`daily_carried` 表)。原来两个
取材桶都是"时间窗 + 取最新 N 条":要点桶取窗口内发布的最新 `MAX_ITEMS` 条,补录桶
取这一轮入库的最新 `LATE_ITEMS` 条。没挤进名额的条目在下一个窗口里**两个桶都选
不到** —— 它的 `published` 和 `indexed_at` 都已经早于新的起点,于是掉进两个桶中间
的缝里**永久消失,一行提示都没有**。实测一份日报 `late_total=679` 而只带得走 30 条,
96% 静默丢弃;而"重试失败项"和"新加一个订阅"抓回来的内容恰好全是这个形态(发布
时间早于当天那份日报的截止时刻),于是"日报生成之后再抓到的东西再也不会出现"。

换成账本之后:没带走的条目**留在队列里**,下一份接着带;条数上限从"丢弃线"变成
"每份带多少条"的限流。顺带治好了另一件事 —— 老文章重新导出会刷新 `indexed_at`,
旧口径下它每天都重新算一次"这一轮才入库",连着十几天占满补录;账本只认"带走过
没有",带走一次就再也不是候选。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from kb import daily_carry as carry
from kb import daily_refs
from kb.config import PRIMARY_SOURCE_NAMES, VAULT_ROOT
from kb.daily_carry import STAMP, commit_lock, mark_carried, parse_ts, release
from kb.daily_gain import material_sig, pending  # noqa: F401  (搬出去的两个口径,
# `daily.pending` / `daily.material_sig` 这类既有写法照旧可用;见 daily_gain 的说明)
from kb.daily_materials import material_view
from kb.daily_prompt import PROMPT, late_section
from kb.daily_carry import UNCARRIED as _UNCARRIED
# 账本那半拆到 daily_carry 了,这个名字这边自己不用、只为让 `daily.SEED_DAY`
# 这类既有写法照旧可用。noqa 得待在它自己那一行上:多行 import 的 F401 是按
# **各个名字所在的行**报的,挂在括号那一行压不住它。
from kb.daily_carry import SEED_DAY  # noqa: F401
from kb.daily_carry import day_key as _day_key
from kb.daily_page import REF_SEP as _REF_SEP
from kb.daily_page import carried_keys as _carried_keys
from kb.daily_page import material_rows as _material_rows
from kb.daily_page import material_text as _material_text
from kb.daily_page import doc_key as _doc_key
from kb.daily_page import md_dest as _md_dest
from kb.daily_page import md_text as _md_text
from kb.daily_page import md_title as _md_title
from kb.daily_page import queue_tail as _queue_tail
from kb.scanner import parse_frontmatter
from kb.source_types import SOURCE_TYPES
from kb.store import promo_exclude_sql

# 一份日报最多带多少条素材进 prompt。真有几百条时取最新的那批,并在日报里如实
# 写明"另有 N 条未纳入"。
#
# **这个数必须大于「窗口内发布的候选」的实际进水量,否则它自己就是积压的来源。**
# 120 那一版小于进水量:实测近 7 天窗口内发布 **184 条/天**(10-08~10-14 依次
# 188/198/152/128/207/215/203),于是每天约 50~64 条被名额挤出去 —— 而它们在下一个
# 窗口里已经走过自己的发布时间,只能落进补录桶(10-01 以来累计 **656 条**)。补录
# 那边进水 ≈84 条/天(挤出来的 ~50 + 天生错窗的 ~34)而席位只有 60,**净涨 ~24
# 条/天**:队列排干之后会自己重新涨起来,和抓取频率无关。260 覆盖掉 215 那个峰值
# 还有余量,于是"被挤出去"这条进水源整个消失,补录只剩天生错窗的那 ~34 条/天、
# 低于 60 个席位,积压才真正是单调下降的。
#
# 别拿"要点桶没用完的席位借给补录"代替这件事:那治的是另一侧(轻量日的浪费),
# 治不了重量日**要点桶本身装不下**;而且补录一节的出稿长度是有上限的
# (`_late_section`),把总量放成无界反而会撑爆出稿。
#
# 上界由模型吃得下多少定,不是估的:453 条 / 11.5 万字符的请求实测跑通过。
MAX_ITEMS = 260
LATE_ITEMS = 60         # 补录那一段最多带多少条
# 补录席位里保底留给"发布得最早、下一轮就要被 BACKLOG_DAYS 挡掉"的那几条。
# 不留的话,只要持续有新内容进来,积压里最老的那些永远排不上队 —— 账本保证了
# 它们还在队列里,席位分配却让它们等到过期,等于换一种方式再丢一次。
LATE_DRAIN = 10
# 补录最多往回捡多久**发布**的内容。这是编辑口径,不是限流:日报不该回头报一个
# 月前发布的东西。**超出的条目在成为候选之前就被排除,所以也不计进 `late_total`**
# —— 面板上那个"还有多少没进过日报"同理不含它们。跟 MAX_ITEMS / LATE_ITEMS 是
# 两回事:那两个是"这一份带多少条",没带走的留在队列里;这一条是"压根不考虑"。
BACKLOG_DAYS = 30
EXCERPT = 400           # 每条素材的摘要上限(字符)
DIR_NAME = 'daily'
# **只有**在一份日报都没有、接不上任何截止时刻时回溯多久(天)。
FIRST_DAYS = 1

# 来源名 → 人话。feed 类来源的标签跟着 SOURCE_TYPES 走,其余几棵树是固定的。
_SOURCE_LABEL = {
    'dr-gzh': '公众号', 'gzh': '公众号', 'dr': 'AI 日报',
    'dr-youtube-dig': 'YouTube 视频',
    **{k: v.label for k, v in SOURCE_TYPES.items()},
}


def report_dir() -> Path:
    return VAULT_ROOT / DIR_NAME


def _label(source: str) -> str:
    return _SOURCE_LABEL.get(source, source)


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


# `mine_at` / `mine_sig` = 「**正在写的那一份**自己上一版带走它的时刻,和当时送进
# 模型的那一版的指纹」。WHERE 里那道 `UNCARRIED` 已经把别的日报盖过章的排除干净了,
# 所以还查得到章的只可能是今天那一份自己的 —— 于是**同一次读**里就分得开三件事:
# 「它一份日报都没进过」(`mine_at` 空)、「今天带走过、而且带走的就是现在这一版」、
# 「今天带走过,但那是另一个版本」。`pending` 不必再补一次查询去扣减或比对(两个
# 读取时刻拼出来的数会在别人中途提交时自相矛盾,口径见 `pending` 的说明)。
_FIELDS = ('SELECT d.path,d.source,d.title,d.account,d.published,d.url,d.summary,'
           'd.indexed_at,'
           ' (SELECT c.carried_at FROM daily_carried c WHERE c.path=d.path) AS mine_at,'
           ' (SELECT c.material_sig FROM daily_carried c WHERE c.path=d.path) AS mine_sig'
           ' FROM documents d WHERE ')


def _scope() -> tuple[str, list]:
    """所有取材共用的口径:只看非衍生来源 + 挡掉 hard 软广。

    条目化产物(dr-gzh-projects 等)与原文是同一批内容,两份都进 prompt 的话
    同一个工具会在日报里出现两次;软广口径跟检索共用 `promo_exclude_sql`。
    """
    names = ','.join('?' * len(PRIMARY_SOURCE_NAMES))
    return f'd.source IN ({names}) AND {promo_exclude_sql("d")}', list(PRIMARY_SOURCE_NAMES)


def _enrich(conn, row, when: datetime | None, late: bool) -> dict:
    doc = dict(row)
    # `mine_at` / `mine_sig` 只给 `pending` 算「再生成一次会多带进来几条」,素材
    # 字典的形状不跟着变 —— 它还得喂给 `material_sig`,**多一个键就多一份指纹**,
    # 那样取材那一刻算出来的和事后比对时算出来的永远对不上。
    doc.pop('mine_at', None)
    doc.pop('mine_sig', None)
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


def _select(conn, since: datetime, until: datetime,
            day: str) -> tuple[list, list]:
    """两个桶各自的候选(已解析时间、未取摘要)。返回 (要点候选, 补录候选)。

    SQL 那一层只按日期前缀粗筛(两头各放一天,给时区换算留余量),精确到分钟的
    判断在 Python 里按解析出来的时间做 —— 库里四种时间写法互相之间没法用字符串
    比大小,见 `parse_ts`。粗筛之后只剩两三天的量,排序开销可以忽略。

    两个桶都只看**账本里没有**的条目,所以这个函数同时是 `collect`(真取材)和
    `pending`(面板上"再出一份能多带进来多少")的唯一口径 —— 两处各判一遍的话
    必然各漂各的。返回的补录候选已经排好序:`[:LATE_ITEMS]` 就是该带走的那一批。

    行上多带两列 `mine_at` / `mine_sig`(见 `_FIELDS`):**正在写的那一份**自己上一版
    什么时候带走的它、带走的是哪一版。`collect` 不读它们,`pending` 靠它们在同一次
    读里把「一份日报都没进过」「带走过、还是同一版」「带走过、但版本变了」分开。
    """
    where, args = _scope()
    args = [*args, day]
    lo = (since.date() - timedelta(days=1)).isoformat()
    hi = (until.date() + timedelta(days=1)).isoformat()
    picked = []
    for row in conn.execute(_FIELDS + where + _UNCARRIED
                            + ' AND substr(d.published,1,10) BETWEEN ? AND ?',
                            [*args, lo, hi]):
        when = parse_ts(row['published'])
        if when and since < when <= until:
            picked.append((when, row))
    picked.sort(key=lambda p: p[0], reverse=True)

    # 补录:发布时间落在窗口之前(或者压根没有发布时间),一份日报都还没带走过。
    # 不捡回来的话它永远进不了任何一份日报 —— 实测 RSS 差三天是常事,而"重试
    # 失败项"抓回来的整批都是这个形态。判据是账本而**不是** `indexed_at`:后者
    # 每次重新导出都会刷新,既会把老文章反复算成"刚入库",又答不出"这一条到底
    # 报过没有"。
    floor = until - timedelta(days=BACKLOG_DAYS)
    pool = []
    for row in conn.execute(_FIELDS + where + _UNCARRIED
                            + " AND (d.published='' OR substr(d.published,1,10) >= ?)",
                            [*args, floor.date().isoformat()]):
        when = parse_ts(row['published'])
        if when is None or (floor < when <= since):
            pool.append((when, row))
    # 新的在前,读起来才像日报;末尾几个席位留给发布得最早的那些(见 LATE_DRAIN)。
    # 没有发布时间的永远不会被 BACKLOG_DAYS 挡掉,所以不需要救,按最新排。
    pool.sort(key=lambda p: p[0] or until, reverse=True)
    head = pool[:max(0, LATE_ITEMS - LATE_DRAIN)]
    rest = pool[len(head):]
    tail = rest[len(rest) - LATE_DRAIN:] if LATE_DRAIN else []
    return picked, head + tail + rest[:len(rest) - len(tail)]


def collect(conn, since: datetime, until: datetime, limit: int | None = None,
            day: str | None = None) -> Material:
    """取这一份日报该带走的素材。`day` 缺省是 until 那一天(正在写的那一份)。

    `limit` 缺省**在调用时**取 `MAX_ITEMS`,不写成形参默认值 —— 后者在 def 那一刻
    就把当时的值绑死了,回归里 `patch.object(daily, 'MAX_ITEMS', 2)` 改到的是另一
    份副本,于是一条本该验"名额装不下的留在队列里"的用例实际上一条都没落选。
    """
    cap = MAX_ITEMS if limit is None else limit
    picked, late = _select(conn, since, until, _day_key(day, until))
    return Material(
        items=[_enrich(conn, row, when, False) for when, row in picked[:cap]],
        total=len(picked),
        late=[_enrich(conn, row, when, True) for when, row in late[:LATE_ITEMS]],
        late_total=len(late))


# ── 上报账本 ───────────────────────────────────────────────────────────────────


def _report_chain() -> list[carry.PastReport]:
    """已有的日报:每一份的窗口、日期,和它实际带走的素材,按截止时刻升序。

    窗口和清单都写在每一份自己的文件里,链条自描述 —— 启用账本时要靠它回答
    "这一条当初是哪一份带走的"。读不出窗口的(手工放进来的文件)直接跳过。

    **不设条数上限**(`reports()` 那个 limit 管的是面板列表):链起点决定"多早
    算链条之前的存量",砍掉最老的几份会让它们覆盖过的内容在迁移时重新变成候选、
    倒灌进下一份日报。这个函数只在真要盖章那一次跑。
    """
    chain = []
    root = report_dir()
    for path in sorted(root.glob('*.md')) if root.is_dir() else []:
        try:
            meta, body = parse_frontmatter(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        since = parse_ts(meta.get('window_since'))
        until = parse_ts(meta.get('window_until'))
        if since and until and since < until:
            chain.append(carry.PastReport(
                since, until, str(meta.get('date') or path.stem),
                frozenset(_carried_keys(body)), _material_rows(body),
                _material_text(body),
                _plain_paths(meta.get('carried_plain'))))
    chain.sort(key=lambda c: c.until)
    return chain


def _plain_paths(value) -> tuple:
    """frontmatter 里那份「带走的无链接素材」名单(`_page` 写的)。

    手改过的文件什么都可能写,所以只收字符串成员、别的一概丢掉 —— 这份名单是
    拿来**盖章**的,混进一个认不出的东西要么当场抛、要么盖到别处去。读不出就是
    空名单,那一份照旧回落到按清单推断(老日报本来就没有这一行)。
    """
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(v.strip() for v in value if isinstance(v, str) and v.strip())


def _seed_args(first_days: int = FIRST_DAYS, now: datetime | None = None) -> tuple:
    """盖章要的那几样东西。

    已有的日报只有这边读得到(窗口在 frontmatter 里、带走了什么在正文的素材清单
    里),所以在这里读好再传下去 —— 账本那边不认识 vault,两个模块也就不用互相
    import。素材身份那一份口径(`_doc_key`)同理跟着传过去。
    """
    return (_report_chain, _doc_key,
            max(1, int(first_days or FIRST_DAYS)),
            (now or datetime.now()).replace(microsecond=0))


def _ensure_ledger(conn, first_days: int = FIRST_DAYS,
                   now: datetime | None = None) -> str:
    """启用账本那一次的一次性盖章(判据和理由都在 `daily_carry.seed_locked`)。"""
    return carry.ensure_ledger(conn, *_seed_args(first_days, now))


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


def _link_refs(body: str, shown: list[dict]) -> str:
    r"""把模型写的出处编号变成指向原文的链接。

    **写进文件、不留到阅读时**:日报是拿走就读的产物(复制出去、在 Obsidian 里
    翻回来),编号只在 kb-web 上可点的话,换一个读法就退回一串对不上号的数字。
    素材清单本来就带着每条的链接,正文这几个编号指的是同一批东西。

    链接文字保留方括号(`[\[18\]](url)`),读起来还是原来的角标;转义是因为
    Obsidian 会把裸 `[[18]]` 当成 wikilink。编号对不上素材(模型偶尔会编一个
    超出范围的)或那条素材没有 url 时原样留着,不编一个链接出来。

    代码和已有链接里的数字方括号不是出处,改了就是把别人的正文改坏 ——
    换不换由 `kb.daily_refs` 渲染一遍裁决,那边不写「哪些片段不许动」的规则。
    """
    links = {}
    for i, d in enumerate(shown, 1):
        if d.get('url'):
            links[str(i)] = (
                _md_dest(d['url']),
                _md_title(f"{d['title']} — {_label(d['source'])}·{d['account'] or '未署名'}"))
    return daily_refs.link_refs(body, links)


def _page(since: datetime, until: datetime, mat: Material, body: str,
          model: str, sync_issues: list[dict] | None = None,
          taken: datetime | None = None) -> str:
    shown = mat.all()
    counts: dict[str, int] = {}
    for d in shown:
        counts[_label(d['source'])] = counts.get(_label(d['source']), 0) + 1
    # 哪些来源这一轮没同步成功。**必须写进文件本身**:日报是拿走就读的产物
    # (复制、Obsidian、明天再翻),面板上的提示留不住。一份"只用旧素材"的日报
    # 和一份"全都同步到了"的日报长得一模一样,那是在骗人(2026-09-30 审查实测:
    # 三个来源全部连接失败,页面照样只有一句绿色的「日报已生成」)。
    # 措辞一律「项」不「来源」:入库那一段(索引/提取)是全库的一次,不属于任何
    # 来源 —— 日报是拿走就读的产物,把"磁盘满"写成"某个来源没同步成功"是在文件
    # 里留一句假话。横幅、列表行、完成提示共用这个口径。
    issues = [f"{i.get('label') or i.get('channel')}：{i.get('detail') or '未完成'}"
              for i in (sync_issues or [])]
    meta = {
        'type': 'daily-report', 'date': until.date().isoformat(),
        'window_since': since.strftime(STAMP), 'window_until': until.strftime(STAMP),
        'docs': len(mat.items), 'total': mat.total,
        'late': len(mat.late), 'late_total': mat.late_total,
        'sources': counts, 'model': model,
        'generated': datetime.now().strftime('%Y-%m-%d %H:%M'),
        # 取材快照(秒精度)。和 `generated` 是两件事:那一个是**出稿完成**、只到
        # 分钟,这一个才是"这一份的输入定下来"的那一刻 —— 盖章要的是后者。
        'collected': (taken or datetime.now()).strftime(STAMP),
    }
    if issues:
        meta['sync_failed'] = issues
    # 这一份带走的素材里**没有原文链接**那几条,各自的文档路径 —— 盖章时唯一的
    # 硬证据。没有 url 的素材身份退化成标题(`daily_page.doc_key`),同名的两篇
    # 共享一个身份,于是账本升版重算时只能去问"当初带走的是哪一篇" —— 而那件事
    # **推断不出来**:五轮审查逐个加判据,每一道都只是把"排除它不可能是那一篇"做得
    # 更细,证不了"它就是那一篇"(实测窗口内 121 条而上限 120 条时,落选那篇同名
    # 同署名文章照样被盖成已报道,待报道 1 → 0、当天生成答「没有未上报过的新内容」)。
    # 判据因此整个退役,这份名单是无 url 素材唯一的身份来源(见 `daily_carry` 的
    # `seed_locked`)。
    # **有链接的不记**:url 本身就是身份、清单那一行已经存着它,而 path 会随标题
    # 或账号改名而变 —— 记下来反倒是个会过期的身份。
    # 路径裸取,和 `mark_carried` 记账那一侧同一个写法:取不到就当场抛。`.get()`
    # 会把"读不到 path"静默降级成"这一篇不在名单上",而名单少一条正是上面那个
    # 漏洞的入口 —— 它会悄悄退回推断,恰恰是这次要修掉的那条路。
    plain = [d['path'] for d in shown if not d['url']]
    if plain:
        meta['carried_plain'] = plain
    refs = '\n'.join(
        f"{i}. [{_md_text(d['title'])}]({_md_dest(d['url'])})"
        f"{_REF_SEP}{_label(d['source'])}·{d['account'] or '未署名'}"
        f"{' · 补录' if d['late'] else ''}"
        if d['url'] else
        f"{i}. {d['title']}{_REF_SEP}{_label(d['source'])}·{d['account'] or '未署名'}"
        f"{' · 补录' if d['late'] else ''}"
        for i, d in enumerate(shown, 1))
    tail = _queue_tail(docs=len(mat.items), total=mat.total,
                       late=len(mat.late), late_total=mat.late_total)
    head = (f'# 日报 · {until:%Y-%m-%d}\n\n'
            f'> 覆盖 {_label_window(since, until)} 发布的内容(按发布时间,不是抓取时间)。\n\n')
    # **说「项」不说「来源」**:同步分两段,抓取那一段按来源一项,入库那一段
    # (索引与提取)是全库的一次、自己占一项,它不属于任何来源
    # (`daily_run.DIGEST_LABEL`)。日报是拿走就读的产物,把它写成"某个来源没同步
    # 成功"是在文件里留一句假话。
    if issues:
        head += (f'> **本轮有 {len(issues)} 项没有完成**,'
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
    MAX_ITEMS、其余留在队列里等下一份,日报里也如实写明条数),能补;
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
    day = until.date().isoformat()
    conn = store.connect()
    try:
        _ensure_ledger(conn, first_days, until)
        mat = collect(conn, since, until, day=day)
    finally:
        conn.close()
    if not mat:
        raise LookupError(f'{_label_window(since, until)} 没有未上报过的新内容,先同步再生成日报')
    # **取材快照就是这一刻。** 下面那次模型请求要跑几十秒到几分钟,所以 `generated`
    # 记的"出稿完成"证明不了"这篇文章进过这一份的输入" —— 那中间入库的文章一个都
    # 没被这一份看见过。两个时刻都写进 frontmatter,别混:盖章不读它们(归属只认
    # `carried_plain` 那份名单),这一行是给人看的事实。
    taken = datetime.now()
    picked = model or llm_stages.stage_model('daily')
    shown = mat.all()
    if progress:
        progress(f'正在让模型综合 {len(shown)} 条内容')
    body = llm.chat(PROMPT.format(range_label=_label_window(since, until),
                                  n=len(shown), items=_material(shown),
                                  late_section=(late_section(len(mat.late))
                                                if mat.late else '')),
                    picked, max_tokens=8192, timeout=600)
    if not body.strip():
        raise RuntimeError('模型返回了空日报,请稍后重试或换一个模型')
    path = report_dir() / f'{day}.md'
    out = {'date': day, 'since': since.strftime(STAMP),
           'until': until.strftime(STAMP), 'path': str(path), 'docs': len(mat.items),
           'total': mat.total, 'late': len(mat.late), 'model': str(picked)}
    # 放回 + 落盘 + 记账是**一段**,整段和「放回 + 删文件」互斥(见 commit_lock):
    # 两件事交错时会留下"记着已上报、却没有任何日报文件"的条目。段内的顺序让三步
    # 的失败方向**一律倒向「多报一遍」**:
    # ①**先把这一天上一版的章放回。** 覆盖是整份替换文件,上一版带走、这一版没选
    #   上的那些素材(名额挤出去的、补录轮不到的)会从盘上消失;放回排在落盘之后
    #   的话,它们的章还盖着而文件里已经没有它们 —— 从此两头落空,不可恢复。放回
    #   自己失败时文件一个字节都还没动,整轮抛错,旧日报和旧账本都完好。首次生成
    #   时它删 0 行。
    # ②**盖章排在落盘之后**:章盖不上只是这一天的素材下一份再出现一遍(多报一次,
    #   看得见),反过来落盘失败而章已经盖上,这批素材就再也选不到了。
    with commit_lock():
        conn = store.connect()
        try:
            release(conn, day)
            _write(path, _page(since, until, mat, body, str(picked),
                               sync_issues, taken))
            try:
                # 指纹按**真送进模型的那一份**算(`shown` 就是上面拼 prompt 用的
                # 那批),下一次面板问"再生成会不会多出东西"时和现在这一版比。
                out['carried'] = mark_carried(
                    conn, day, [d['path'] for d in shown],
                    {d['path']: material_sig(d) for d in shown})
            except (sqlite3.Error, OSError) as e:  # 日报已经在盘上了,别带崩这一轮
                out['carry_error'] = str(e)
                if progress:
                    progress(f'日报已生成,但上报记录没写进库({e});'
                             '这一天的素材会在下一份日报里重复出现一遍')
        finally:
            conn.close()
    return out


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
        out['late_total'] = meta.get('late_total') if type(meta.get('late_total')) is int else None
        out['material_view'] = material_view(body, meta)
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
    """删一份日报,顺带把它带走的条目放回队列。

    **先放回、再删文件。** 反过来的话,放回那一步失败时文件已经没了而章还盖着,
    那批内容从此两头落空:日报里看不到、也再不会被下一份选中。按这个顺序最坏
    情况是章放回了文件还在,那只是下一份日报多报一遍。

    **整段和生成那一段互斥**(`commit_lock`):这个顺序只管自己这一段失败时往
    哪边倒,管不住"生成写完文件、还没记账"的那一瞬间插进来 —— 那时放回放了个
    空,生成随后把章补上,条目就被记成已上报而文件并不存在。锁要连 `is_file()`
    一起圈进去,不然判断和删除之间又开一道同样的缝。
    """
    from kb import store

    path = _path_for(day)
    with commit_lock():
        if not path.is_file():
            return False
        conn = store.connect()
        try:
            # **先把账本启用了再放回。** 这一份马上就没了,而"它带走过哪些素材"
            # 只写在它自己的文件里 —— 账本还没启用就先删,随后那次首访读不到这
            # 一份,它报道过的内容会按发布时间被判成历史存量、记成 `#seed` 永久
            # 写死(自由工作流审查第 3 轮:删掉唯一一份昨天的日报,里面的文章从此
            # 再也选不回来)。盖完再 release,那批章当场回到队列。
            # 这里已经持着 `commit_lock`,所以走不取锁的那个入口。
            carry.seed_locked(conn, *_seed_args())
            release(conn, Date.fromisoformat(day).isoformat())
        finally:
            conn.close()
        path.unlink()
    return True
