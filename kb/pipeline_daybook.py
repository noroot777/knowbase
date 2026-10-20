# -*- coding: utf-8 -*-
"""当日账本:**今天**各来源各目标各步骤还有哪些没完成。「重试失败项」的计划和范围从它派生。

**硬约束在 `docs/decisions/retry-scope.md` 第一节 —— 改这块之前先读那一份。**

**为什么不能只读「最近一次运行」。** 那一格只有一个位子,而一天里会跑好几轮:
2026-10-19 那天 09:12 抓 RSS 失败、10:20 一轮公众号成功收尾,后者当场把前者顶掉
—— 于是点「重试失败项」要么收到一句「上次运行没有失败项」,要么按最近那一轮的
范围从头再跑一遍(10:34 那一次就是这么来的,而 09:12 该补的那条一直没补上)。
用户要的是「同一天点重试能继承之前的状态」。

**账本按 (步骤, 来源, 目标) 记一格,而且只认今天。** 跨天由日键自动清零 ——
「继承只在当日内」是用户定的边界,昨天的账本一格都不参与合成。一格的内容就是那一轮
那个步骤的结论(状态、人话、失败明细),外加派生要用的三样:这一格的范围
(`accounts`)、那个来源留下的失败名单(`note`)、以及是谁哪一刻记的。

**结清的判据是「这一次真跑过的范围覆盖了这一格」,不是「同一条通道刚有一轮结束」。**
记到通道那一层就够的话,同一天「只同步订阅 A 失败」之后「只同步订阅 B 成功」会把
RSS 那一格整个换成 B 的成功 —— A 的失败从此不在计划里,而它的 `last_error` 还在
(第 2 轮审查实测)。所以:

- 这一轮点名了几个目标(`accounts`)⇒ 只写那几格,**不碰整通道那一格、也不碰别的
  目标**;
- 这一轮跑的是整通道(`accounts` 空)⇒ 它确实把这个通道跑了一遍,所以**先清掉这个
  通道这一步的全部旧账**,再写结论:成功写整通道那一格,失败而**采集器留下了失败
  名单**时只写名单里那几个目标(B/C 这一轮明明成功了),没留下名单才写整通道那一格。

**抓取和导出各读自己的有效范围。** 导出范围是单独一份(`export_filter`:抓取收窄
了、导出照旧走全部来源,见 `pipeline_retry.start_retry_failed`),拿抓取范围给导出
记账的后果是「只补导出」那一轮的成功记到了空通道上,原来那个来源的导出 error 留在
原处 —— 下一次重试照旧派生出 export/index/extract,补好的导出被反复重跑(第 2 轮
审查实测)。`run_scopes` 答的是另一个问题(「这一轮整体作用在哪几个来源上」,只读
`crawl_filter`),所以这里不用它。

**一轮收尾时折进来一次,不事后聚合**(同 `pipeline_merge._merge_runs` 那条):折的
是这一轮**自己**的终态,所以每个步骤的结论都挂在它自己那几个来源上。拿「整批合并
出来那一份」事后折的话,一批里 RSS 失败 + 公众号成功会合成一个 error,再按合并后
的来源清单摊开 —— 公众号那一格也成了 error,继承当场反向。

**后记的那一格盖掉先记的那一格,不取最重的那一档。** 这正是「继承」:09:12 的
RSS 抓取失败,10:20 单独同步它成功了,那一格就该变绿。取最重的话今天之内再也变
不回绿色,这颗按钮每次都去重抓一遍已经好了的来源。反过来(先记的说了算)则是
新失败记不进来。

**宁可多记一格失败。** 阶段结论在段合并之后不带来源归属(见
`pipeline_segments.note_retry_targets`),所以一轮同时抓几个来源、只有一个失败时,
这一轮**范围里的每一格**都记成失败 —— 另一个来源会被多抓一次。反过来按「谁留下了
失败名单」去挂才是漏记:整批失败的那个来源一份名单都没留下,而它恰恰是最该重抓的
那个。**漏记是静默丢数据,多记顶多重抓一次。**

**写不上的那一轮留在内存里,下一次折的时候补上。** 折账本是旁路(写不上只记一笔
`persist_error`,不许挡住收尾),可「写不上」的那一轮照样欠着东西 —— 退回去只看
账本就是漏报。所以失败的那一笔留在 `_pending` 里:下一次 `fold` 会连它一起折,
合成快照时也会先把它们盖上去。进程在这中间退出的话那几轮就真丢了,`unfinished`
会把丢了几轮说出来(经接口进面板,口径在 `pipeline_retry.start_retry_failed`)。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from kb import pipeline_state as ps
from kb.pipeline_persist import _dump_bytes, _load_json_strict, _write_atomic

TZ = ZoneInfo('Asia/Shanghai')
# 一格里属于账本自己的记账字段,合成快照时要摘掉 —— 留着会跟着 `_merge_stage`
# 混进阶段结论里,面板上那张卡片就多出几个它不认识的键。
_BOOK_KEYS = ('run', 'at', 'accounts', 'note')
# 「不限定」那一格用空串当键,两层各有含义:**通道那一层** = 不挂在任何具名来源上
# (全库那几步 index/extract/projects/wiki 永远是它);**目标那一层** = 整条通道
# (没点名具体订阅的那一轮)。两层都用空串是因为它们是同一件事的两级:合成范围时
# 空通道整格跳过(没有来源范围可言),空目标写成 `accounts: []`(整条通道都要)。
_WHOLE = ''
# 折进账本失败、只留在内存里的那几轮:{(账本文件, 日键): [run, ...]}。
# **键带上账本文件和日键**:测试里每个用例各有自己的临时状态文件,而生产里跨零点
# 之后昨天那一笔不许再折进今天的账本(「继承只在当日内」)。少了这个键,一次写
# 失败就会把 A 用例的那一轮折进 B 用例的账本里 —— 同跑时红、单跑永远绿。
_pending: dict[tuple[str, str], list[dict]] = {}
# 连内存都没留住的轮数(超过上限挤掉的)。**要说出来**:这一档是真的丢了。
_lost: dict[tuple[str, str], int] = {}
# 内存里最多替多少轮欠着。满了挤掉最早的那一笔并记一笔 `_lost` —— 无上限的话
# 账本一直写不上的那种坏法会把整天的运行留档全攒在进程里。
# **这是每一桶的上限,不是全局的**:桶按 (账本文件, 日键) 分,跨零点就换一桶。
# 所以另有 `_sweep()` 管「旧日那些桶」(见那边)。
_PENDING_MAX = 20
# 一共最多留多少桶。只有「今天」这一桶会被折进账本、也只有它会被 `unfinished`
# 读到,所以旧日那些桶**留着没有任何用处**:磁盘一直写不进去的那种坏法下,它们
# 会一天一桶地攒在进程里(各最多 20 轮完整留档)。扫掉它们**不是把旧账搬进今天**
# ——「继承只在当日内」是用户定的边界,搬过来等于拿昨天的失败去派生今天的重试。
# 留两桶是给跨零点那一刻的**冗余带**:那一刻正在收尾的那一轮,日键可能还是昨天。
_PENDING_DAYS = 2


def _book_file():
    """账本就在 `_STATE_FILE` 旁边。

    **从它派生,不另立一个模块变量** —— 理由同 `pipeline_state._history_file`:
    测试隔离落盘时 patch 的只有 `_STATE_FILE`,另立一个的话每个走到 `_end_run()`
    的用例都得记着再 patch 一次,而漏掉的那个会去写**生产**那份账本,漏没漏在测试
    输出上一个字都看不见。
    """
    return ps._STATE_FILE.with_name(
        f'{ps._STATE_FILE.stem}_today{ps._STATE_FILE.suffix}')


def _day(now: datetime | None = None) -> str:
    """日键。**按项目时区 Asia/Shanghai,不按宿主机**(元规则那条)。"""
    at = now or datetime.now(TZ)
    at = at.replace(tzinfo=TZ) if at.tzinfo is None else at.astimezone(TZ)
    return at.strftime('%Y-%m-%d')


def read(now: datetime | None = None) -> dict:
    """今天那份账本。`{}` = 今天还没有任何一轮折进来(含跨天清零那一档)。

    **读不出来照抛**,降级由调用方决定(见 `unfinished`)—— 在这里吞成 `{}` 的话
    「今天还没跑过」和「账本坏了」长得一模一样,而这两件事导出的结论相反:前者该
    退回最近一次运行,后者得说出来。

    **「可解析但形状不对」和「读不出来」是同一档**(口径同
    `pipeline_history._history_rounds_strict`):三层里任何一层不是字典时,照旧
    往下走只会在 `.values()` 那一步抛 —— 而那一抛的位置在 `today_failed` 里被兜住、
    在 `start_retry_failed` 里没有,于是同一份文件让状态接口照常、重试却 500
    (第 2 轮审查按代码推断)。形状在这一处判一次,降级就只有一条路。
    """
    raw = _load_json_strict(_book_file()) or {}
    if not isinstance(raw, dict) or raw.get('day') != _day(now):
        return {}
    stages = raw.get('stages')
    if not stages:
        return {'day': raw['day'], 'stages': {}}
    if not _shape_ok(stages):
        raise ValueError('当日账本的形状不对')
    return {'day': raw['day'], 'stages': stages}


def _shape_ok(stages) -> bool:
    """账本那三层是不是 {步骤: {来源: {目标: 一格}}}。"""
    if not isinstance(stages, dict):
        return False
    for owned in stages.values():
        if not isinstance(owned, dict):
            return False
        for targets in owned.values():
            if not isinstance(targets, dict) or not all(
                    isinstance(cell, dict) for cell in targets.values()):
                return False
    return True


def _owners(run: dict, stage: str) -> list[dict]:
    """这一轮这个步骤挂在哪几个来源上:`[{"channel", "accounts"}]`。**至少一格。**

    只有 crawl / export 按来源算(`run_scopes` 那一份口径:`_run_index` 扫整个
    vault、`_run_extract` 扫全库待提取队列,这几步连通道参数都不收)。

    **两步各读自己那一份范围。** 导出读 `export_filter`:重试轮里它和抓取范围本来
    就不一样(抓取收窄到失败的那几条、导出照旧走全部来源),而「只补导出」那一轮
    压根没有抓取范围 —— 拿抓取范围给它记账,成功就记到了空通道上,原来那个来源的
    导出 error 留在原处,下一次重试又派生出一遍 export/index/extract(第 2 轮审查
    实测)。回落到 `crawl_filter` 是给升级前那些没有这个字段的旧记录用的,口径同
    `pipeline_retry.start_retry_failed` 那一处。

    **这里不用 `run_scopes`**:它答的是「这一轮整体作用在哪几个来源上」(只读
    `crawl_filter`,还要自己判一遍这一轮有没有计划抓取/导出),而我们问的是「**这
    一步**作用在哪几个来源上」,计划那一道由调用方(`_fold_into` 只折计划过的那几步)
    管住了。

    **没指定来源的那一轮照旧要有一格,键就是空串**(`_WHOLE`)—— 整库同步的
    `crawl_filter` 是空的,`crawl_channels` 给回来的是一个 `channel` 为空的范围。把它
    当成「没有来源可挂」丢掉的话,那一轮的抓取**一格都不记**,而账本里 `stages`
    这一层照旧多出两个空字典 —— 于是账本看着「今天跑过」、每一步却都合成成「未
    选择」,这颗按钮对**最常见的那一轮**(整库同步失败)回一句「上次运行没有失败
    项」。空串这一格的含义是「不挂在任何具名来源上」,合成快照时它不进来源范围
    (`{}` = 没有来源范围可言 = 照默认采集器全抓),和那一轮自己的 `crawl_filter`
    一模一样。
    """
    from kb.pipeline_stages import crawl_channels   # 延迟:避开模块级环
    if stage not in ('crawl', 'export'):
        return [{'channel': _WHOLE, 'accounts': []}]
    key = 'export_filter' if stage == 'export' else 'crawl_filter'
    scopes = crawl_channels(run.get(key) or run.get('crawl_filter'))
    return scopes or [{'channel': _WHOLE, 'accounts': []}]


def _bucket(now: datetime | None = None) -> tuple[str, str]:
    """内存欠账那两个键:哪一份账本、哪一天(见 `_pending`)。**顺手扫一次旧桶。**

    扫在这里是因为它是**唯一的访问入口**:`fold` / `unfinished` / `_overlay` 要用
    哪一桶都从它拿。扫的时候**连 `_lost` 一起**:那个计数只喂「今天有几轮补不到」
    这一句话,旧日的数字念出来就是一句错话。
    """
    key = str(_book_file()), _day(now)
    _sweep(key)
    return key


def _sweep(key: tuple[str, str]) -> None:
    """把**旧日**那些桶丢掉:只留今天往前 `_PENDING_DAYS` 天(含今天)。

    判据是**和今天差几天**,不是「现存的日键里最近那几个」:后者在中间隔了几天不开
    机的情况下留下的恰恰是最老那一桶(5 天前那一笔和今天那一笔正好凑成「最近两
    个」,于是永远扫不掉)。`YYYY-MM-DD` 的字典序就是时间序,所以比字符串就够。

    **同一份账本只扫它自己的**:键里带着账本文件,测试里每个用例各有一份临时状态
    文件,按日键一刀切会把别人的桶也扫掉(同跑时红、单跑永远绿那一类)。
    """
    from datetime import date, timedelta
    try:
        floor = (date.fromisoformat(key[1])
                 - timedelta(days=_PENDING_DAYS - 1)).isoformat()
    except ValueError:               # 日键读不出来:**宁可不扫**,别误删还欠着的
        return
    for book in (_pending, _lost):
        for k in [k for k in book if k[0] == key[0] and k[1] < floor]:
            del book[k]


def fold(run: dict, now: datetime | None = None) -> None:
    """把**刚收尾的这一轮**折进今天的账本。`_end_run` 是唯一的调用方(已持 `_lock`)。

    只折这一轮**计划过**的那几步:没计划过的那一档是 `skipped/未选择`,折进去就是
    拿「这一轮没跑它」盖掉「今天早些时候它失败了」—— 那正是这份账本要挡住的事。

    **之前写不上的那几轮一起折**(`_pending`),折成了才清。写盘/读盘失败照旧抛给
    `_end_run` 去诊断(它三份分别尝试、分别记一笔),而那一轮不会因此从账本里消失:
    它留在内存里等下一次,合成快照时也先盖上去(见 `_overlay`)。
    """
    key = _bucket(now)
    held = _pending.setdefault(key, [])
    held.append(deepcopy(run))
    while len(held) > _PENDING_MAX:
        del held[0]
        _lost[key] = (_lost.get(key) or 0) + 1
    book = read(now) or {'day': _day(now), 'stages': {}}
    for payload in held:
        _fold_into(book, payload, now)
    _write_atomic(_book_file(), _dump_bytes(book))
    _pending.pop(key, None)


def _fold_into(book: dict, run: dict, now: datetime | None = None) -> None:
    """把一轮折进 `book`(就地改)。**纯内存,不读盘不写盘。**

    拆出来是因为有**两个**调用方:收尾那一次(`fold`,折完写盘)、以及合成快照前
    把内存里那几轮盖上去那一次(`_overlay`)。两处各折一遍的话,「一格长什么样」
    就有两份口径了。
    """
    from kb.pipeline_results import cell_for
    from kb.pipeline_state import retry_notes
    from kb.pipeline_retry import stage_failed
    planned = run.get('planned_stages')
    if not isinstance(planned, list):
        # 存量快照没有这一格:按「状态不是跳过的就算计划过」摊平(同
        # `run_scopes` 对老记录那条,宁可多记一格)。
        planned = [s for s, st in (run.get('stages') or {}).items()
                   if (st or {}).get('status') not in ('skipped', '', None)]
    notes = retry_notes(run)
    at = run.get('finished_at') or ps._now()
    for name in planned:
        st = (run.get('stages') or {}).get(name)
        if name not in ps.STAGES or not st:
            continue
        cells = book['stages'].setdefault(name, {})
        for scope in _owners(run, name):
            channel = scope['channel']
            # **这个来源自己那一条结论**(结果账)。拿不到就退回整步那张合并卡片
            # —— 那是保守档:合并卡片不带来源归属,一轮里只要有一个来源失败,整张
            # 卡片就是 error,于是这一轮范围里每一格都记成欠着(多抓一次)。
            # 有自己那一条时**必须用它**:不然一轮同时抓公众号和 RSS、只有 RSS 的
            # 一条订阅失败时,公众号那一格也成了 error,而它明明抓完了 ——「重试
            # 失败项」于是把它又抓一遍(第 3 轮审查 R2 实测)。
            mine = cell_for(run, name, channel)
            here = mine or st
            failed = stage_failed(here)
            # 失败名单只有抓取那一步留得下来(它是「这几个目标要重抓」),而且
            # **按来源各一份** —— 没留下名单的来源照旧按原范围重跑。
            note = deepcopy(notes[channel]) if name == 'crawl' and channel in notes else None
            # **读进来那几格的键也要归一到稳定 id**,不只是这一轮新写的那几个:
            # 旧账里那条订阅记的是显示名,这一轮按 id 写,两格并存时成功清不掉失败
            # (`_restable` 的 docstring 里是实测形态)。
            owned = _restable(channel, cells.setdefault(channel, {}))
            # 记账的键是**实际覆盖的那几个目标、归一到稳定 id**(结果账里那份
            # `covered`),不是请求里写的那几个字:同一条订阅在「请求里叫 A」和
            # 「失败名单里叫 63」两种写法下会记成两格,清掉一格、另一格永远红着
            # (第 3 轮审查 R3)。拿不到结果账才退回请求范围(存量记录那一档)。
            wanted = ([str(t) for t in (mine.get('covered') or [])] if mine
                      else [str(a) for a in (scope.get('accounts') or [])])
            if not wanted:
                # **这一轮真把整条通道跑了一遍** —— 它覆盖这个通道这一步的每一个
                # 目标,所以先把旧账清空,再按结论写:
                owned.clear()
                # 整通道失败、而采集器留下了失败名单 ⇒ 欠的只是名单里那几个
                # (这一轮里 B/C 明明成功了)。名单跟着那几格走,下一次重试照旧
                # 由它收窄(`retry_crawl_scopes`)。
                wanted = [str(t) for t in (note or {}).get('targets') or []] \
                    if failed and note else []
            elif failed and note and name == 'crawl':
                # 点名跑了几个目标、而且留下了失败名单 ⇒ **名单之外那几个这一轮是
                # 成功的**,不许一起记成失败。记成失败的后果是下一次重试又去抓它们
                # (名单恰恰是采集器说「只有这几个还要重抓」)。
                still = [t for t in wanted if t in {str(x) for x in (note.get('targets') or [])}]
                for ok_target in [t for t in wanted if t not in still]:
                    # **这一格的说明要自己写,不许抄整步那一条。** 整步那条带着
                    # 「失败 N 个」,而 `stage_failed` 认的正是这几个字 —— 抄过来
                    # 的「成功」格会被当成失败,这一整段等于没做(实测:账本里那
                    # 条好着的订阅照旧出现在「今天还欠什么」的范围里)。
                    owned[ok_target] = _cell({'status': 'ok', 'detail': '这一轮已抓成'},
                                             run, at, ok_target, None)
                wanted = still or wanted
            for target in (wanted or [_WHOLE]):
                owned[target] = _cell(here, run, at, target, note)


def _restable(channel: str, owned: dict) -> dict:
    """把这一格里**读进来的旧键**归一到稳定 id;**就地改**,返回同一个字典。

    身份口径只有一份(`pipeline_results.stable_keys`)—— 这一轮新写的那几个键过的
    是同一道(`covered_targets`)。只归一新写入那一侧的后果是同一条订阅同时留下
    `A:error` 和 `63:ok`:今天还欠抓取的结论来自旧的 A,而真去重试又被「这条订阅
    后来成功过」挡住,页面于是卡在「说没补完 + 又说没有可补」的死局(第 4 轮审查
    R3,证据 `legacy-name.json`)。

    撞到同一个 id 时**晚的那一笔留下**:账本的规则一直是后记的盖掉先记的,那正是
    「继承」。比不出先后(有一边没有时刻)就留着 id 那一格上原有的 —— 它多半是
    这一轮刚写的,而且这一轮接着还要往里写、自然会盖掉。
    """
    from kb.pipeline_results import stable_keys       # 延迟,同 `_fold_into`
    for raw, stable in stable_keys(channel, owned).items():
        if raw == stable or raw not in owned:
            continue
        cell = owned.pop(raw)
        cur = owned.get(stable)
        if cur is None or _later(cell, cur):
            owned[stable] = cell
    return owned


def _later(a: dict, b: dict) -> bool:
    """a 这一格比 b 那一格晚。**比不出来就不算晚**(保守:留着 b)。"""
    from kb.pipeline_retention import parse_stamp
    ta, tb = parse_stamp(a.get('at')), parse_stamp(b.get('at'))
    return bool(ta and tb and ta > tb)


def _cell(st: dict, run: dict, at: str, target: str, note: dict | None) -> dict:
    """账本里一格长什么样。**只有这一处**(折账要在两个地方造格子)。"""
    cell = {k: deepcopy(v) for k, v in st.items() if k not in _BOOK_KEYS}
    cell.update(run=run.get('run_id') or '', at=at,
                accounts=[target] if target else [])
    if note is not None:
        cell['note'] = deepcopy(note)
    return cell


def unfinished(latest: dict, now: datetime | None = None) -> tuple[dict, str]:
    """今天还没完成的那些事,摆成**一轮运行的样子**;外加一句降级说明(没有就空串)。

    摆成运行的样子是为了「口径只有一份」:计划、抓取范围、导出范围照旧分别由
    `retry_failed_plan` / `retry_crawl_scopes` / `start_retry_failed` 从一份快照
    派生,它们一个字都不用改,只是读到的那一份从「最近一次运行」换成了「今天还
    欠的那些」。

    **今天的账本是空的就退回 `latest`**:跨零点之后那颗按钮照旧补得上昨晚那一轮
    (存量行为),而「继承只在当日内」说的是不拿昨天的成功去抵今天的失败。

    **三种降级都退回 `latest`,而且都要把原因说出来**:账本读不出来(含形状不对)、
    合成不出来、以及今天有几轮连内存都没留住。那一份正是这颗按钮一直以来读的东西,
    不是新的坏法;比「拦住不许重试」合用,代价是今天早些时候那几笔可能补不到,所以
    这句话要跟着起跑消息一起给出去(**一路给到面板**,见
    `pipeline_retry.start_retry_failed` 和 `kb/static/js/pipe.js`)。

    账本非空那一档也**不是只读账本**:合成之前先把内存里欠着的那几轮和 `latest`
    各盖一次(`_overlay`),不然折账本失败的那一轮带着的红就被账本里之前那几格
    盖住了。
    """
    key = _bucket(now)

    def alone(why: str) -> tuple[dict, str]:
        """退回「最近一次运行」,**但照旧按结果账折它一遍**。

        原来是把 `latest` 原样交出去,而它的阶段卡片是各来源合并之后那一张:一轮里
        只有 RSS 失败就把整步记成 error,公众号那一格跟着红 ——「重试失败项」把已经
        抓完的公众号又抓一遍(第 4 轮审查 R2 实测 `retry_wechat=['wechat_local']`)。
        折一遍走的是和账本那条**同一份口径**(`_fold_into` → `cell_for`),所以这份
        账在降级这一档照旧说得上话:账本没了,「谁已经成了」这件事它还记得。

        **折不出来就真的原样退回** —— 这一档是兜底的兜底,不许再抛;`latest` 本身
        是空的(从没跑过)也原样退回,没有「最近一次」可折。
        """
        plain = deepcopy(latest or {})
        if not latest:
            return plain, _and_lost(why, key)
        try:
            day = _day(now)
            return _snapshot(_overlay({}, latest, day, now), day), _and_lost(why, key)
        except Exception:                        # noqa: BLE001 - 见 docstring
            return plain, _and_lost(why, key)

    try:
        book = read(now)
    except Exception as e:                       # noqa: BLE001 - 见 docstring
        return alone(f'今天的账本读不出来({e})，这一次只按最近一次运行补')
    cells = book.get('stages') or {}
    if not cells and not _pending.get(key):
        return alone('')
    day = book.get('day') or _day(now)
    try:
        return _snapshot(_overlay(cells, latest or {}, day, now), day), _and_lost('', key)
    except Exception as e:                       # noqa: BLE001 - 见 docstring
        # 合成那一步自己炸了(账本形状过了那一道、内容仍对不上某个消费方)。
        # **和读不出来分开说**:两句话指向的下一步不同,而两处各有自己的回归。
        # **这一档不许再走 `alone` 里那条合成**(它刚炸的就是它):原样退回。
        return deepcopy(latest or {}), _and_lost(
            f'今天的账本合不成这一次的范围({e})，这一次只按最近一次运行补', key)


def _and_lost(why: str, key: tuple[str, str]) -> str:
    """降级说明外加一句「今天有几轮压根没记上」。两句都可能是空的。

    **丢了几轮要单独说**:它和「这一次读不出来」不是一回事 —— 后者下一次可能就好
    了,前者是**再也补不回来**(进程已经退出,那几轮的终态哪儿都没有)。
    """
    lost = _lost.get(key) or 0
    parts = [t for t in (why, f'今天有 {lost} 轮同步的结果没能记进账本，'
                              '这一次补不到它们' if lost else '') if t]
    return '；'.join(parts)


def _overlay(cells: dict, latest: dict, day: str, now: datetime | None = None) -> dict:
    """账本那几格之上,再盖一次**内存里欠着的那几轮**和「最近一次运行」。

    返回要用的那一份(不改入参)。

    **账本不许盖得住还没写进去的那几轮自己报出来的失败。** 折账本是旁路,写不上只
    记一笔 `persist_error` 就过去了(它不许挡住收尾)—— 可它写不上的那一轮要是正好
    带着红,账本里就只剩之前那几轮的样子,而它们多半是绿的:这颗按钮于是回一句
    「上次运行没有失败项」,那一轮的红**一个入口都没有了**。退回去只读账本是**漏报**,
    而漏报是静默丢数据(元规则那条)。
    **内存里那几轮无条件先盖**(`_pending` 就是「账本里没有它们」的证据),
    `latest` 那一轮再按下面两道判一次 —— 它多半已经在账本里了。

    **只在账本确实没有它那一轮时才盖**(按 run id 认)。这不只是省一次重复折 ——
    批次那一档它是**不许**盖的:「最近一次运行」在批次里是**整批合并出来那一份**
    (`pipeline_batch._batch_aggregate`),它的来源清单是全批的、阶段是合起来的,
    盖上去就是一批里 RSS 失败 + 公众号成功合成一个 error 再摊到两个来源上,公众号
    那一格当场变红(模块头那条「不许拿整批那份折」)。汇总继承最后那个子轮的身份,
    而账本里那几格就是那一轮自己写的,所以按身份认得出来。

    **账本比它新就不盖** —— 比的是同一个时钟(`at` 和 `finished_at` 都出自
    `pipeline_state._now()`),所以跨天那一档自然落到这里:昨晚那一轮比今天任何一
    格都旧,它的红不会被搬进今天(「继承只在当日内」是用户定的边界)。
    **两边的时刻都读不出来时照旧盖** —— 宁可多抓一次,不许漏。
    """
    from kb.pipeline_retention import parse_stamp, run_stamp
    book = {'day': day, 'stages': deepcopy(cells)}
    # **键照旧自己拼**(用的是传进来那个 `day`,不是「现在是哪天」):合成的是
    # 账本自己那一天。扫旧桶那件事在 `_bucket` 里,调用方进来之前已经扫过了。
    for payload in list(_pending.get((str(_book_file()), day)) or []):
        _fold_into(book, payload, now)
    cells = book['stages']
    if not latest:
        return cells
    rid = latest.get('run_id') or ''
    folded = [cell for owned in cells.values()
              for targets in owned.values() for cell in targets.values()]
    if rid and any(cell.get('run') == rid for cell in folded):
        return cells
    stamps = [at for at in (parse_stamp(cell.get('at')) for cell in folded) if at]
    at = run_stamp(latest)
    if stamps and at and at < max(stamps):
        return cells
    _fold_into(book, latest, now)
    return book['stages']


def _owed(owned: dict) -> tuple[list[str], dict | None] | None:
    """这个来源这一步今天还欠什么:`(要重跑的目标, 失败名单)`;不欠就 `None`。

    **名单只在它收窄得住全部欠账时才用。** `retry_crawl_scopes` 拿名单**替换**整个
    范围,所以:整通道那一格还欠着时只认它自己那一份名单(别的目标各欠各的,拿它
    去收窄会把它们摘掉);逐目标那几格要**每一格都有名单**才合起来用,有一格没有
    就整个不用 —— 那一格的欠账会被收窄掉,而漏记是静默丢数据(元规则那条)。
    不用名单不等于乱抓:范围本身已经是那几个失败的目标了。
    """
    from kb.pipeline_retry import stage_failed
    failing = [(target, cell) for target, cell in owned.items() if stage_failed(cell)]
    if not failing:
        return None
    whole = [cell for target, cell in failing if target == _WHOLE]
    if whole:
        return [], whole[0].get('note') if len(failing) == 1 else None
    merged = None
    for _, cell in failing:
        if not cell.get('note'):
            return [target for target, _ in failing], None
        merged = _merge_note(merged, cell['note'])
    return [target for target, _ in failing], merged


def _merge_note(a: dict | None, b: dict) -> dict:
    """两格各自那份失败名单并起来。

    `at` 取**早的那个**,有一份没有时间就整个不设:它只用来问「这个目标后来成功过
    没有」(`sync.still_failing`),取晚的会把该抓的摘掉(漏报),取早的顶多多抓
    一次(口径同 `pipeline_segments.note_retry_targets` 那条「空串等于不设时间判定」)。
    """
    if not a:
        return deepcopy(b)
    targets = list(dict.fromkeys([*(a.get('targets') or []), *(b.get('targets') or [])]))
    ats = [t for t in (a.get('at'), b.get('at')) if t]
    return {'targets': targets, 'at': min(ats) if len(ats) == 2 else ''}


def _snapshot(cells: dict, day: str) -> dict:
    """把账本那几格合成一份运行快照。纯合成:不读盘、不写盘。"""
    from kb.collectors import channel_labels      # 延迟:它反过来 import 管道层
    from kb.pipeline_merge import _merge_stage     # 阶段合并的口径只有那一份
    labels = channel_labels()
    run_id = f'daybook-{day}'
    stages, planned = {}, []
    crawl_scopes, export_scopes, notes = [], [], {}
    for name in ps.STAGES:
        owned = cells.get(name) or {}
        if not any(targets for targets in owned.values()):
            # 今天压根没跑过这一步。**这一档必须是「未选择」而不是「失败」**:
            # `retry_failed_plan` 的链式规则按它判,写成失败就等于每次重试都把
            # 整条链重跑一遍(那正是 10:34 那一次的形状)。
            stages[name] = {'status': 'skipped', 'detail': '未选择'}
            continue
        planned.append(name)
        # 挂名挂到**目标**那一层:同一条通道里哪一条订阅坏了,面板上那张卡片得写
        # 得出来(`_merge_stage` 把这个名字写进 detail 前缀)。
        stages[name] = _merge_stage([
            (_label(labels, channel, target),
             {k: v for k, v in cell.items() if k not in _BOOK_KEYS})
            for channel, targets in owned.items()
            for target, cell in targets.items()])
        for channel, targets in owned.items():
            # **空串那个通道不进来源范围**:它的含义是「不挂在任何具名来源上」,而
            # 范围里写不进这个 —— 留着空 `channel` 会被 `crawl_channels` 当成
            # 「没指定来源」,那恰好就是我们要的结果(`{}` → 照默认采集器全抓),
            # 所以这里跳过它、让范围保持空,而不是往里塞一个空名字。
            if channel == _WHOLE:
                continue
            owed = _owed(targets)
            if owed is None:
                continue
            accounts, note = owed
            scope = {'channel': channel, 'accounts': list(accounts)}
            if name == 'crawl':
                crawl_scopes.append(scope)
                if note:
                    notes[channel] = deepcopy(note)
            else:
                export_scopes.append(scope)
    # **导出范围要盖住抓取那几个来源。** 重抓一个来源之后它必须跟着导出一遍
    # (`retry_failed_plan` 的「crawl 进计划就带上 export」那条),而它的导出这一格
    # 今天可能是绿的 —— 不并进来的话那几篇新抓的文章一篇都不落盘。
    # **并进来的那一格要放宽到整条通道**:抓取收窄到几条订阅时,导出按同样的窄范围
    # 跑会把这个来源别的订阅那几篇漏掉(导出是按来源扫的,不按订阅)。
    for scope in crawl_scopes:
        if not any(s['channel'] == scope['channel'] for s in export_scopes):
            export_scopes.append({'channel': scope['channel'], 'accounts': []})
    return {
        'run_id': run_id, 'run_kind': 'daybook', 'planned_stages': planned,
        'stages': stages,
        'crawl_filter': _scope_filter(crawl_scopes),
        'export_filter': _scope_filter(export_scopes),
        # 身份要和 `run_id` 对得上:`retry_notes` 拿这两样核对,对不上一律当成
        # 「这一轮没有名单」(宁可按原范围重跑,也不拿别人的失败去收窄)。
        'crawl_retry': {'run': run_id, 'channels': notes},
    }


def _label(labels: dict, channel: str, target: str) -> str:
    """这一格在阶段结论里挂谁的名。空通道那一格不挂名(它就是「全库」)。"""
    name = labels.get(channel) or channel
    return f'{name}·{target}' if name and target else name


def _scope_filter(scopes: list[dict]) -> dict:
    """几个来源范围写成一份 `crawl_filter`。口径同 `pipeline_merge.scope_payload`。"""
    from kb.pipeline_merge import scope_payload
    return scope_payload(scopes)
