# -*- coding: utf-8 -*-
"""本日的抓取账本:今天抓了第几次、每一次往 vault 里**新落了**多少篇。

日报面板那颗药丸报的就是这个事实。它和日报的取材结果是两件事,**不许互相
顶替**:一次抓取抓了多少,是抓取自己的事,生成日报、重新生成、删掉日报都改变
不了它;而「这一次生成能选进去几条」受名额(`daily.MAX_ITEMS`/`LATE_ITEMS`)和
发布时间排序左右,算出来的数和「抓了多少」可以差两个数量级。拿后者冒充前者的
后果是:第二次同步真抓回来 9 条、按钮上写 3 条;抓回来的那条发布得早、抢不到
席位时按钮写「没有新内容」—— 而用户正是靠这个数决定要不要再花一次模型钱。

**成果的归属由抓取自己定,不由任何下游阶段反推。** 数的是**物化**那一步往 vault
新落下的 md(`vault_snapshot` 前后差集,记在 `_run_export` 里)。一度改成"库里
头一回出现这个路径"的文档数,那仍然是拿消费者的结果猜抓取:空抓取碰上 3 篇
历史未索引的旧文会报成「+3 条」,反过来真抓到 2 篇而这一轮不索引则报 0、之后
单独索引也补不回来(两种都是实测)。索引、重建索引、CLI 索引、手工导入都不经过
物化,一个数都不会变。

**「一次业务抓取」的身份是业务批次,不是管道运行。** 一轮日报 = N+1 次管道运行
(每个来源各一次),它们共用一个 `pipeline_batch.open_batch()` 发的身份;勾了三个
来源手动同步也是一次抓取、三次运行。按运行记的话面板上会写成「今天第 7 次抓取」
而用户只点过两次。没有批次身份的独立运行(单来源同步、重试失败项)各算一次,
那时它自己的 `run_id` 就是身份。

**先有 `open_round` 才认 `note_new`。** 轮次在**抓取阶段**开 —— 只勾了导出/索引的
那种运行压根没抓过,它落下来的东西(比如上一轮抓回来还没导的)不该凭空变出
一个「第 N 次抓取」。`note_new` 找不到那一轮就什么都不做。

**账本分得清「确定没有」和「不可确认」。** 读不出来、或者有成果没落下去时
`today()` 返回 `None`,一路交给前端显示「读不出来」;`n=0` 是另一句话:确定今天
还没有抓取过。两件事混成一句的后果是,一次写失败之后面板会一直笃定地说「今天
还没有抓取」。为此落盘是**追加**而不是整份重写(整份重写一次写坏就把当天已经
确认过的轮次全带走,追加最坏只留一条半截行,`_fold` 认得出来),写失败的那几行
攥在手里下一次操作再试,读不全的文件**一个字都不动**。

**只有一种情形救不回来**:写失败之后进程就没了 —— 那几行既没落盘也不在内存里,
重启后账本里确实没有它。没有哪个单独的存储能补上这一刀(要么和内容本身同一次
事务,那就又回到"拿索引结果猜抓取"),所以这里不假装能恢复。

面板那一侧怎么显示(几种说法、自定义区间、tooltip 里那句队列积压)写在
`docs/decisions/daily.md` 的「这个计数长在生成按钮里」那一节。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kb.config import DATA_DIR, VAULT_ROOT
from kb.pipeline_persist import _write_atomic

TZ = ZoneInfo('Asia/Shanghai')
FILE_NAME = 'fetch_rounds.jsonl'
# 账本里留几天。只回答「今天」,但不能只留今天:23:59 开的那一轮 00:01 才物化完,
# 记它的那一下得找得到它(轮次按**开工那天**归属,不在追加时重新猜今天)。
_KEEP_DAYS = 2
# 攥在手里等下次重试的行数上限。磁盘一直写不进去的话,这里挡住它无界增长。
_KEEP_UNSAVED = 64

# 落盘失败、还没写下去的那几行。**不是缓存**:它存在期间 `today()` 一律答
# 「不可确认」,因为此刻磁盘上那份账本比事实少。
_unsaved: list[dict] = []


def _file() -> Path:
    return DATA_DIR / FILE_NAME


def _now(now: datetime | None = None) -> datetime:
    """项目时区的此刻(CLAUDE.md:一律 Asia/Shanghai,不按宿主机)。

    UTC 机器上按宿主机算的话,北京时间 00:00~07:59 这八小时会被记进前一天,
    面板上「今天第几次抓取」要到早上八点才归零。不带时区的按项目时区补齐。
    """
    at = now or datetime.now(TZ)
    return at.replace(tzinfo=TZ) if at.tzinfo is None else at.astimezone(TZ)


def _day(at: datetime) -> str:
    return at.date().isoformat()


def vault_snapshot() -> frozenset[str]:
    """此刻 vault 里**算「抓到的新内容」**的那些 md(相对 `VAULT_ROOT` 的路径)。

    物化前后各取一次,差集就是这一轮抓取真往 vault 落下的文章。只看路径、不读
    正文,所以一次全量 rglob 的代价远小于它后面那次网络抓取。

    **范围口径只有一份**:走 `scanner.iter_md`,和索引扫的是同一批树,再去掉衍生
    树。条目化衍生树(`dr-gzh-projects` 等)和原文是同一批内容,日报取材本来就
    排除它们(`daily._scope`),算进来的话一轮条目化就能让药丸写出三位数,而日报
    那边一条新的都没有;停用的树(`Source.enabled=False`)不再收新内容,`iter_md`
    已经整棵跳过。

    这个窗口里别的进程往非衍生树里落的文件(实时 saver 的 `gzh` 树)也会算进来 ——
    那确实是会进索引、会进日报的新内容,只是归属的轮次可能偏一次。
    """
    from kb.config import SOURCES
    from kb.scanner import iter_md

    return frozenset(
        str(md.relative_to(VAULT_ROOT))
        for _src, md in iter_md([s for s in SOURCES if not s.derived]))


def _fold(lines) -> tuple[list[dict], bool]:
    """把账本每一行折成「开过哪几轮、各自新落了多少」,外加一句「读全了没有」。

    `ok=False` 表示有行读不出来(半截行、被改坏、成果对不上轮次)。那时这份账本
    只能说「不可确认」—— 拿认得出来的那部分冒充完整事实,就又回到了把写失败说成
    「今天还没有抓取」。
    """
    rounds: list[dict] = []
    index: dict[str, dict] = {}
    ok = True
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            ok = False                  # 半截行(写一半断电)或被人改坏
            continue
        if not isinstance(obj, dict) or not obj.get('key'):
            ok = False
            continue
        key = str(obj['key'])
        if 'added' in obj:
            got = index.get(key)
            try:
                n = int(obj['added'])
            except (TypeError, ValueError):
                ok = False
                continue
            if got is None:
                ok = False              # 有成果、却找不到它那一轮:记不全
                continue
            got['added'] += n
            continue
        if key in index:
            continue                    # 同一轮重复开:幂等,不是第二次抓取
        got = {'key': key, 'day': str(obj.get('day') or ''),
               'at': str(obj.get('at') or ''), 'added': 0}
        rounds.append(got)
        index[key] = got
    return rounds, ok


def _read() -> tuple[list[dict], bool]:
    """读账本。**文件不存在是「确定还没有记录」,读不出来是「不可确认」** ——
    两种都压成空的话,一次权限故障就会让面板笃定地说「今天还没有抓取」。
    """
    try:
        text = _file().read_text(encoding='utf-8')
    except FileNotFoundError:
        return [], True
    except OSError:
        return [], False
    return _fold(text.splitlines())


def _append(objs: list[dict]) -> bool:
    """追加这几行。失败返回 False,**不抛** —— 展示账本写不上不该把这一轮抓取
    带红,但也不许假装成功(那是上一版吞掉 `OSError` 的那条:25 篇文章已经入库、
    药丸永久写 0 条,补齐文章也不会自己补回来)。
    """
    body = ''.join(json.dumps(o, ensure_ascii=False) + '\n' for o in objs)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_file(), 'a', encoding='utf-8') as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        return False
    return True


def _flush(objs: list[dict]) -> bool:
    """把这几行连同之前没落下去的一起追加。

    磁盘满、权限被改这类故障多半是一阵子的,而它们之后的那次账本操作照样该把
    漏掉的那几行补上 —— 所以攥着重试,而不是就地丢掉。
    """
    global _unsaved
    batch = _unsaved + objs
    if not batch:
        return True
    if _append(batch):
        _unsaved = []
        return True
    _unsaved = batch[-_KEEP_UNSAVED:]
    return False


def _rotate(rounds: list[dict], ok: bool, at: datetime) -> None:
    """把早于保留窗口的轮次清出账本(整份重写)。

    **读不全的时候一个字都不动**:那时我们并不知道文件里还有什么,拿认得出来的
    那部分重写回去,等于把读不出来的那几行悄悄删掉。轮换失败也不出声 ——
    最坏只是文件长一点,结论不受影响。
    """
    if not ok:
        return
    floor = _day(at - timedelta(days=_KEEP_DAYS - 1))
    live = [r for r in rounds if r['day'] >= floor]
    if len(live) == len(rounds):
        return
    lines: list[dict] = []
    for r in live:
        lines.append({'day': r['day'], 'key': r['key'], 'at': r['at']})
        if r['added']:
            lines.append({'key': r['key'], 'added': r['added']})
    try:
        _write_atomic(_file(), ''.join(
            json.dumps(o, ensure_ascii=False) + '\n' for o in lines).encode('utf-8'))
    except OSError:
        pass


def open_round(key: str, now: datetime | None = None) -> None:
    """抓取阶段开始时报到:今天又开了一次抓取(同一个 `key` 重复调是空操作)。

    多来源同步时每个来源各调一次,但它们带着同一个批次身份,所以只开一轮 ——
    这正是「不许把多来源算成多次抓取」那条落地的地方。
    """
    if not key:
        return
    at = _now(now)
    rounds, ok = _read()
    if ok and any(r['key'] == str(key) for r in rounds):
        _flush([])                      # 顺手把攥着的那几行再试一次
        return
    _rotate(rounds, ok, at)
    _flush([{'day': _day(at), 'key': str(key),
             'at': at.strftime('%Y-%m-%d %H:%M')}])


def note_new(key: str, paths) -> None:
    """这一轮抓取往 vault 新落了哪几篇。

    **参数是身份(路径集合)而不是一个数** —— 这样调用方就没法把别的阶段算出来的
    产出当成抓取成果递进来:前两版正是这么错的,先拿日报取材结果、再拿全库首次
    索引数冒充它。

    按 `key` 找轮次、**不按「今天」过滤**:23:59 开的轮次 00:01 才物化完,按今天找
    会找不到它,那一轮永远停在 0 条。读不全时什么都不记(状态本来就是「不可确认」,
    往一份读不懂的账本里追加成果只会多一条对不上轮次的孤行)。
    """
    if not key or not paths:
        return
    rounds, ok = _read()
    if not ok:
        return
    if not any(r['key'] == str(key) for r in rounds):
        _flush([])                      # 没开过这一轮:它不是一次抓取
        return
    _flush([{'key': str(key), 'added': len(paths)}])


def today(now: datetime | None = None) -> dict | None:
    """今天抓了第几次、最后那一次新落了多少 —— 面板那颗药丸要的就是这个。

    `{'n': 今天第几次, 'added': 那一次新落了多少篇, 'at': 'HH:MM'}`;
    `n=0` 是「确定今天还没有抓取过」。**读不出来、或者有成果没落下去时返回
    `None`(不可确认)**,由 API 原样交给前端显示「读不出来」。
    """
    rounds, ok = _read()
    if not ok or _unsaved:
        return None
    day = _day(_now(now))
    mine = [r for r in rounds if r['day'] == day]
    if not mine:
        return {'n': 0, 'added': 0, 'at': ''}
    last = mine[-1]
    return {'n': len(mine), 'added': last['added'], 'at': last['at'][-5:]}
