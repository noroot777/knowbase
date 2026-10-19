# -*- coding: utf-8 -*-
"""当日账本:**今天**各来源各步骤还有哪些没完成。「重试失败项」的计划和范围从它派生。

**硬约束在 `docs/decisions/retry-scope.md` 第一节 —— 改这块之前先读那一份。**

**为什么不能只读「最近一次运行」。** 那一格只有一个位子,而一天里会跑好几轮:
2026-10-19 那天 09:12 抓 RSS 失败、10:20 一轮公众号成功收尾,后者当场把前者顶掉
—— 于是点「重试失败项」要么收到一句「上次运行没有失败项」,要么按最近那一轮的
范围从头再跑一遍(10:34 那一次就是这么来的,而 09:12 该补的那条一直没补上)。
用户要的是「同一天点重试能继承之前的状态」。

**账本按 (来源, 步骤) 记一格,而且只认今天。** 跨天由日键自动清零 —— 「继承只在
当日内」是用户定的边界,昨天的账本一格都不参与合成。一格的内容就是那一轮那个
步骤的结论(状态、人话、失败明细),外加派生要用的三样:这一轮给它的范围
(`accounts`)、那个来源留下的失败名单(`note`)、以及是谁哪一刻记的。

**一轮收尾时折进来一次,不事后聚合**(同 `pipeline_merge._merge_runs` 那条):折的
是这一轮**自己**的终态,所以每个步骤的结论都挂在它自己那几个来源上。拿「整批合并
出来那一份」事后折的话,一批里 RSS 失败 + 公众号成功会合成一个 error,再按合并后
的来源清单摊开 —— 公众号那一格也成了 error,继承当场反向。

**后记的那一格盖掉先记的那一格,不取最重的那一档。** 这正是「继承」:09:12 的
RSS 抓取失败,10:20 单独同步它成功了,那一格就该变绿。取最重的话今天之内再也变
不回绿色,这颗按钮每次都去重抓一遍已经好了的来源。反过来(先记的说了算)则是
新失败记不进来。

**宁可多记一格失败。** 一轮同时抓几个来源时阶段结论只有一份(段合并之后不带来源
归属,见 `pipeline_segments.note_retry_targets`),所以这一轮计划里的每个步骤都挂在
**这一轮的全部来源**上 —— 两个来源一起抓、只有一个失败时,另一个会被多抓一次。
反过来按「谁留下了失败名单」去挂才是漏记:整批失败的那个来源一份名单都没留下,
而它恰恰是最该重抓的那个。**漏记是静默丢数据,多记顶多重抓一次。**
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
# 「不挂在任何具名来源上」的那一格用空串当键:全库那几步(index/extract/projects/
# wiki)永远是它,整库同步的抓取/导出也是它(那一轮的 `crawl_filter` 本来就是空的)。
_WHOLE = ''


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
    """
    raw = _load_json_strict(_book_file()) or {}
    if not isinstance(raw, dict) or raw.get('day') != _day(now):
        return {}
    stages = raw.get('stages')
    return {'day': raw['day'], 'stages': stages if isinstance(stages, dict) else {}}


def _owners(run: dict, stage: str) -> list[dict]:
    """这一轮这个步骤挂在哪几个来源上:`[{"channel", "accounts"}]`。**至少一格。**

    只有 crawl / export 按来源算(`run_scopes` 那一份口径:`_run_index` 扫整个
    vault、`_run_extract` 扫全库待提取队列,这几步连通道参数都不收)。

    **没指定来源的那一轮照旧要有一格,键就是空串**(`_WHOLE`)—— 整库同步的
    `crawl_filter` 是空的,`run_scopes` 给回来的是一个 `channel` 为空的范围。把它
    当成「没有来源可挂」丢掉的话,那一轮的抓取**一格都不记**,而账本里 `stages`
    这一层照旧多出两个空字典 —— 于是账本看着「今天跑过」、每一步却都合成成「未
    选择」,这颗按钮对**最常见的那一轮**(整库同步失败)回一句「上次运行没有失败
    项」。空串这一格的含义是「不挂在任何具名来源上」,合成快照时它不进来源范围
    (`{}` = 没有来源范围可言 = 照默认采集器全抓),和那一轮自己的 `crawl_filter`
    一模一样。
    """
    from kb.pipeline_stages import run_scopes      # 延迟:避开模块级环
    if stage not in ('crawl', 'export'):
        return [{'channel': _WHOLE, 'accounts': []}]
    return run_scopes(run) or [{'channel': _WHOLE, 'accounts': []}]


def fold(run: dict, now: datetime | None = None) -> None:
    """把**刚收尾的这一轮**折进今天的账本。`_end_run` 是唯一的调用方(已持 `_lock`)。

    只折这一轮**计划过**的那几步:没计划过的那一档是 `skipped/未选择`,折进去就是
    拿「这一轮没跑它」盖掉「今天早些时候它失败了」—— 那正是这份账本要挡住的事。
    """
    book = read(now) or {'day': _day(now), 'stages': {}}
    _fold_into(book, run, now)
    _write_atomic(_book_file(), _dump_bytes(book))


def _fold_into(book: dict, run: dict, now: datetime | None = None) -> None:
    """把一轮折进 `book`(就地改)。**纯内存,不读盘不写盘。**

    拆出来是因为有**两个**调用方:收尾那一次(`fold`,折完写盘)、以及合成快照前
    把「最近一次运行」盖上去那一次(`_overlay` —— 它那一轮可能压根没折进来过)。
    两处各折一遍的话,「一格长什么样」就有两份口径了。
    """
    from kb.pipeline_state import retry_notes
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
            cell = {k: deepcopy(v) for k, v in st.items() if k not in _BOOK_KEYS}
            cell.update(run=run.get('run_id') or '', at=at,
                        accounts=list(scope.get('accounts') or []))
            # 失败名单只有抓取那一步留得下来(它是「这几个目标要重抓」),而且
            # **按来源各一份** —— 没留下名单的来源照旧按原范围重跑。
            if name == 'crawl' and channel in notes:
                cell['note'] = deepcopy(notes[channel])
            cells[channel] = cell


def unfinished(latest: dict, now: datetime | None = None) -> tuple[dict, str]:
    """今天还没完成的那些事,摆成**一轮运行的样子**;外加一句降级说明(没有就空串)。

    摆成运行的样子是为了「口径只有一份」:计划、抓取范围、导出范围照旧分别由
    `retry_failed_plan` / `retry_crawl_scopes` / `start_retry_failed` 从一份快照
    派生,它们一个字都不用改,只是读到的那一份从「最近一次运行」换成了「今天还
    欠的那些」。

    **今天的账本是空的就退回 `latest`**:跨零点之后那颗按钮照旧补得上昨晚那一轮
    (存量行为),而「继承只在当日内」说的是不拿昨天的成功去抵今天的失败。
    **读不出来也退回 `latest`,但要把原因说出来** —— 那一份正是这颗按钮一直以来
    读的东西,不是新的坏法;比「拦住不许重试」合用,代价是今天早些时候那几笔可能
    补不到,所以这句话要跟着起跑消息一起给出去。

    账本非空那一档也**不是只读账本**:合成之前先把 `latest` 盖一次(`_overlay`),
    不然折账本失败的那一轮带着的红就被账本里之前那几格盖住了。
    """
    try:
        book = read(now)
    except Exception as e:                       # noqa: BLE001 - 见 docstring
        return deepcopy(latest or {}), f'今天的账本读不出来({e})，这一次只按最近一次运行补'
    cells = book.get('stages') or {}
    if not cells:
        return deepcopy(latest or {}), ''
    return _snapshot(_overlay(cells, latest or {}, book['day'], now), book['day']), ''


def _overlay(cells: dict, latest: dict, day: str, now: datetime | None = None) -> dict:
    """账本那几格之上,再盖一次「最近一次运行」。返回要用的那一份(不改入参)。

    **账本不许盖得住最近那一轮自己报出来的失败。** 折账本是旁路,写不上只记一笔
    `persist_error` 就过去了(它不许挡住收尾)—— 可它写不上的那一轮要是正好带着
    红,账本里就只剩之前那几轮的样子,而它们多半是绿的:这颗按钮于是回一句「上次
    运行没有失败项」,那一轮的红**一个入口都没有了**。退回去只读账本是**漏报**,
    而漏报是静默丢数据(元规则那条)。

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
    if not latest:
        return cells
    rid = latest.get('run_id') or ''
    folded = [cell for owned in cells.values() for cell in owned.values()]
    if rid and any(cell.get('run') == rid for cell in folded):
        return cells
    stamps = [at for at in (parse_stamp(cell.get('at')) for cell in folded) if at]
    at = run_stamp(latest)
    if stamps and at and at < max(stamps):
        return cells
    book = {'day': day, 'stages': deepcopy(cells)}
    _fold_into(book, latest, now)
    return book['stages']


def _snapshot(cells: dict, day: str) -> dict:
    """把账本那几格合成一份运行快照。纯合成:不读盘、不写盘。"""
    from kb.collectors import channel_labels      # 延迟:它反过来 import 管道层
    from kb.pipeline_merge import _merge_stage     # 阶段合并的口径只有那一份
    from kb.pipeline_retry import stage_failed
    labels = channel_labels()
    run_id = f'daybook-{day}'
    stages, planned = {}, []
    crawl_scopes, export_scopes, notes = [], [], {}
    for name in ps.STAGES:
        owned = cells.get(name) or {}
        if not owned:
            # 今天压根没跑过这一步。**这一档必须是「未选择」而不是「失败」**:
            # `retry_failed_plan` 的链式规则按它判,写成失败就等于每次重试都把
            # 整条链重跑一遍(那正是 10:34 那一次的形状)。
            stages[name] = {'status': 'skipped', 'detail': '未选择'}
            continue
        planned.append(name)
        stages[name] = _merge_stage([
            (labels.get(ch) or ch, {k: v for k, v in cell.items() if k not in _BOOK_KEYS})
            for ch, cell in owned.items()])
        for channel, cell in owned.items():
            # **空串那一格不进来源范围**:它的含义是「不挂在任何具名来源上」,而
            # 范围里写不进这个 —— 留着空 `channel` 会被 `crawl_channels` 当成
            # 「没指定来源」,那恰好就是我们要的结果(`{}` → 照默认采集器全抓),
            # 所以这里跳过它、让范围保持空,而不是往里塞一个空名字。
            if channel == _WHOLE or not stage_failed(cell):
                continue
            scope = {'channel': channel, 'accounts': list(cell.get('accounts') or [])}
            if name == 'crawl':
                crawl_scopes.append(scope)
                if cell.get('note'):
                    notes[channel] = deepcopy(cell['note'])
            else:
                export_scopes.append(scope)
    # **导出范围要盖住抓取那几个来源。** 重抓一个来源之后它必须跟着导出一遍
    # (`retry_failed_plan` 的「crawl 进计划就带上 export」那条),而它的导出这一格
    # 今天可能是绿的 —— 不并进来的话那几篇新抓的文章一篇都不落盘。
    for scope in crawl_scopes:
        if not any(s['channel'] == scope['channel'] for s in export_scopes):
            export_scopes.append(scope)
    return {
        'run_id': run_id, 'run_kind': 'daybook', 'planned_stages': planned,
        'stages': stages,
        'crawl_filter': _scope_filter(crawl_scopes),
        'export_filter': _scope_filter(export_scopes),
        # 身份要和 `run_id` 对得上:`retry_notes` 拿这两样核对,对不上一律当成
        # 「这一轮没有名单」(宁可按原范围重跑,也不拿别人的失败去收窄)。
        'crawl_retry': {'run': run_id, 'channels': notes},
    }


def _scope_filter(scopes: list[dict]) -> dict:
    """几个来源范围写成一份 `crawl_filter`。口径同 `pipeline_merge.scope_payload`。"""
    from kb.pipeline_merge import scope_payload
    return scope_payload(scopes)
