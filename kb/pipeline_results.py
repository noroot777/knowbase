# -*- coding: utf-8 -*-
"""结果账:**这一步在这个来源上实际覆盖了哪些目标、结论是什么。**

**硬约束在 `docs/decisions/retry-scope.md` 第一节 —— 改这块之前先读那一份。**

为什么要有它。当日账本原来按「这一轮请求的范围」把**合并之后那张阶段卡片**摊到
每个来源上,而合并卡片只适合展示、不带来源归属(`pipeline_segments._merge_segments`
把各段扁平并起来)。于是一轮同时抓公众号和 RSS、只有 RSS 的一条订阅失败时,公众号
那一格也成了 error ——「重试失败项」把已经抓完的公众号又抓一遍,而用户要的正是
「9:12 抓过的不要再抓」(第 3 轮审查 R2 实测:`first_wechat=["wechat_local"]`、
`retry_wechat=["wechat_local"]`)。

这份账记三件事,**每件都是执行器交回来的事实,不是从请求反推的**:

- **逐来源的结论**。谁成谁败各记自己那一条,不读合并卡片。
- **稳定身份**。目标键一律归一到采集器认的稳定 id(订阅是它的 id),不是页面上那个
  显示名。同一条订阅在「请求里叫 A」和「失败名单里叫 63」两种写法下会记成两格,
  成功清掉其中一格、另一格的红永远留着:页面说 4/4 完成,又说今天还有 1 步没补上,
  点下去后端回一句「没有要补的来源」—— 一个点不动也消不掉的死局(第 3 轮审查 R3
  实测,证据 `name-id-success.json`)。
- **实际覆盖范围**。一格里**计划范围和执行事实各记一份**:
  - `planned` = 这一轮点名了谁(空 = 不限定)。**红记在它头上** —— 阶段结论不带来源
    归属,所以宁可多记一格失败(漏记是静默丢数据,多记顶多重抓一次)。
  - `covered` / `whole` = 采集器交回来的执行事实(`kb.collectors.Coverage`):**只有
    真跑成了的那几个才敢去结清旧失败**,`whole` 才敢清空这个来源这一步的旧账。
  - `reported` = 上面那两样**是采集器交回来的事实,还是我们按计划范围推断的**。
    推断那一档只有在这一步**真执行过**时才算成功证据(`pipeline_daybook._coverage`
    过一道 `pipeline_retry.stage_ran`):导出跳过、整理被锁、某一步压根没轮到的那
    几轮都带着「整条覆盖」的推断值收尾,拿它去清旧账就是审查 R1 那条静默丢数据。
  两份合不成一份:请求的范围不是执行的范围。退避期内再跑一次同样范围的普通同步,
  那一轮一次取文都没发,却以「检查 0/1 个来源」绿色收尾 —— 拿请求范围当覆盖范围的
  话,这份绿色会把今天早些时候那笔真失败结清,面板上「重试失败项」当场消失,而订阅
  的 `last_error` 还在原处(审查 R1 实测)。导出那一步不用采集器报:它的契约就是
  扫整条来源(`materialize()` 压根不收目标),所以建账时就算整条覆盖。

**说不出来就记窄的那一档。** 覆盖范围报宽了会把没真跑过的旧失败一起清掉(静默丢
数据),报窄了顶多多重跑一次(元规则那条)。所以:只有执行契约明说「这一步扫整条
来源」、或者采集器自己报了整条跑完,才算整条通道;翻不出稳定 id 的目标**原样留着**,
不许从账上摘掉。

**这份账只补充、不替代计划范围。** 折账照旧按这一轮**计划过**的来源摊开
(`pipeline_daybook._owners`),账上没有自己结论的来源退回「这一步整体那个结论」——
不然一轮跑到一半被停、后面那个来源压根没起跑,它那一格就既没有结论也没人记,
今天欠的东西从此查不出来(漏报是静默丢数据)。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

# {步骤: {来源: 一格}}。一格 = {'planned', 'covered', 'whole', 'status', 'detail', ...}。
# `planned` = 这一轮点名了哪几个稳定 id(空 = 不限定);`covered` = **真跑成了**那几个;
# `whole` = 整条来源这一步都跑完了(只有这一档敢清空旧账);`reported` = 后两样是
# 采集器报的事实还是按计划推断的。几份的分工见模块头。
_cells: dict[str, dict[str, dict]] = {}
# {步骤: 这一刻正在跑的来源}。`_stage` 据它知道该把结论记到哪一格
# (**唯一的归属判据**:「`_stage` 写的那一条就是当前这个来源这一步的结论」)。
_current: dict[str, str] = {}
#: 这几步按来源跑(口径同 `pipeline_stages.run_scopes`、前端 `PIPE_SOURCE_STAGES`)。
SOURCE_STAGES = ('crawl', 'export')
#: 这一步扫的是**整条来源**、不按目标收窄。判据来自执行契约本身:
#: `materialize()` 不收目标参数(`kb.collectors.SourceCollector`),而
#: `fetch(FetchScope)` 收。所以只有导出在这张表里 —— 往里加之前先确认那一步
#: 真的扫整条来源,**宽了就是把没跑过的旧失败一起清掉**。
WHOLE_SOURCE_STAGES = ('export',)


def planned_targets(collector, stage: str, requested) -> list[str]:
    """这一步在这个来源上**计划**作用于哪几个目标:`[]` = 不限定(整条来源)。

    **这是「红记在谁头上」那一份,不是「谁真跑成了」那一份**(后者由采集器交回来,
    见 `cover`)。两份分开记的理由和事故形态在模块头。

    三档,顺序就是优先级:

    1. 这一步扫整条来源(`WHOLE_SOURCE_STAGES`)⇒ `[]`,不管请求点了谁;
    2. 请求没点名 ⇒ `[]`(整条来源);
    3. 点名了 ⇒ 归一到采集器认的稳定 id(`TargetIdentity`)。**采集器报不出来就
       原样留着**(公众号那条路的目标就是账号名,它本身已经是稳定身份)。
    """
    if stage in WHOLE_SOURCE_STAGES:
        return []
    return _stable(collector, requested)


def _stable(collector, targets) -> list[str]:
    """这几个目标归一到采集器认的稳定 id。**身份口径只有这一份。**

    计划范围和执行事实都过它:两边用不同口径的后果是同一条订阅记成两格,一格的 ok
    清不掉另一格的 error(`stable_keys` 的 docstring 里是实测形态)。
    """
    wanted = [str(t) for t in (targets or [])]
    if not wanted:
        return []
    from kb.collectors import target_identity
    naming = target_identity(collector)
    if naming is None:
        return wanted
    hit = naming.stable_targets(wanted)
    # **答不出一份名单就用原样那几个。** 这一道不是为了容错好看:`stable_targets`
    # 是可选能力,而 `runtime_checkable` 的协议只看方法名在不在 —— 认错了的话这一格
    # 的键会变成一个没法比较的东西,而账上那几格从此谁也清不掉谁。**退回去只会更宽**
    # (原样那几个就是这一轮点名的),不会把谁从账上摘掉。
    if not isinstance(hit, (list, tuple, set, frozenset)):
        return wanted
    return [str(t) for t in hit]


def stable_keys(channel: str, keys) -> dict[str, str]:
    """这几个**从账本读进来的键**各自的稳定 id:`{原键: 稳定键}`。

    账本里那几格是之前某一轮写下的,键可能还是显示名(升级前那一版就是按显示名
    记的);而这一轮写进去的是稳定 id。不归一的话同一条订阅留下**两格**:旧的
    `A:error` 和新的 `63:ok` 并存,成功清不掉失败 —— 页面说「4/4 完成」,又说
    「今天还有 1 个步骤没补上」,点下去后端回一句「没有要补的来源」,一个点不动
    也消不掉的死局(第 4 轮审查 R3 实测
    `after.stages.crawl.rss.keys()=['A','63']`、`next_started=False`)。

    **只有唯一匹配才归一。** 翻不出来、或者一个名字匹配上好几条订阅(歧义)时
    **原样留着**:并错一格或者从账上摘掉都是静默丢数据,留着顶多多抓一次(元规则
    那条)。采集器报不出稳定身份时(公众号的目标就是账号名,它本身已经是稳定身份)
    一次库都不读。

    **这里一个异常都不许漏出去**:归一是折账路上的辅助,它炸了不该让这一轮的账
    整份写不上 —— 那一轮的红就只剩降级那条路看得见了。
    """
    out = {str(k): str(k) for k in keys}
    if not out:
        return out
    from kb.collectors import active_collector, target_identity
    try:
        naming = target_identity(active_collector(channel))
    except Exception:                              # noqa: BLE001 - 见 docstring
        return out
    if naming is None:
        return out
    for raw in list(out):
        if not raw:
            continue      # 空键 = 整条通道那一格,它不是目标,没有身份可归
        try:
            hit = [str(t) for t in naming.stable_targets([raw])]
        except Exception:                          # noqa: BLE001 - 见 docstring
            continue
        if len(hit) == 1:
            out[raw] = hit[0]
    return out


@contextmanager
def channel_run(stage: str, channel: str, collector, requested):
    """这一段时间里对 `stage` 的汇报,算「`channel` 这个来源这一步的结论」。

    **进来就先建一格**(status=running):建在跑之前,这一格才一定带着它的覆盖范围
    —— 中途抛出去的那一次照样记得下来。

    收尾时还是 running ⇒ 采集器连一次最终状态都没报(异常穿出去了),**不替它编一个
    成功**(口径同 `pipeline_segments.stage_segment` 那条)。

    **出去时把「当前来源」摘掉**:`_end_run` 收尾时还会给没跑完的阶段补一次
    `_stage(...)`,留着的话那一笔会记到最后那个来源头上 —— 而它多半不是没跑完的
    那一个。摘掉之后那一笔谁也不挂,折账自然退回「这一步整体那个结论」(保守档)。
    """
    planned = planned_targets(collector, stage, requested)
    # **执行事实从「按计划算」起步**:采集器报不出覆盖范围的那一档照旧按这一轮点名
    # 的范围记账(存量行为,见 `kb.collectors.Coverage`)。报得出来的在 `cover` 里
    # 原地换成真事实 —— 起步就写零覆盖的话,不实现这个可选能力的来源连失败都记不下。
    cell = {'planned': planned, 'covered': list(planned), 'whole': not planned,
            # **推断出来的就要说是推断的**:`cover` 收到真 `Coverage` 时才翻成
            # True。少了这一格,一步压根没跑的 skipped 也带着「整条覆盖」去清旧账。
            'reported': False, 'status': 'running', 'detail': ''}
    _cells.setdefault(stage, {})[channel] = cell
    _current[stage] = channel
    try:
        yield
    except Exception as e:                         # noqa: BLE001 - 见下,照抛
        # **抛出去就是失败,不管它之前报过什么。** 少了这一道会漏报:导出那一步
        # 进门先落一张空回执、顺手把卡片写成 `ok/已保存 0 条`(`fetch_rounds`),
        # 随后 `materialize` 炸了 —— 这一格停在那个 ok,而编排层会把**整步**记成
        # error。于是账本说这个来源好着、面板说这一步红了,这个来源一笔欠账都没有
        # (它很可能已经往 vault 落了一半)。单来源那条路尤其如此:`_per_channel`
        # 的 `except` 只接多来源那一档,异常直接穿过这里。
        cell.update(status='error', detail=f'{type(e).__name__}: {e}')
        raise
    finally:
        if cell.get('status') == 'running':
            cell.update(status='error', detail=cell.get('detail') or '没有返回结果')
        _current.pop(stage, None)


def cover(stage: str, report) -> None:
    """采集器交回来的**执行事实**:这一轮在当前这个来源上真跑成了哪几个目标。

    `report` 不是 `Coverage` 就一个字都不改(「报不出来」那一档退回按计划范围记账,
    理由在 `kb.collectors.Coverage`)。导出压根不用报 —— 它的契约就是扫整条来源,
    建账那一下就写成整条覆盖了。

    **身份不在这儿再归一一次。** 采集器交回来的就是**稳定身份**(`Coverage` 的契约:
    「真跑过而且成了的那几个目标身份」)—— 订阅源报的是订阅 id,公众号报的是账号名,
    那本身就是它的稳定身份。原来这里又把它丢回范围匹配(`_stable` → `in_scope`)走了
    一遍,而那份匹配是**选源方向**的口径:「id 相等**或** name 相等」。
    **两个方向的安全侧是反的**,这是这一处的全部要害:
    - 选源方向宽一点 = 多抓一条,代价是重抓一次;
    - **记账方向宽一点 = 拿没发生的成功清掉另一条订阅的真实失败**,而那条订阅从此
      不再出现在重试计划里 —— 静默丢数据,重试救不回来。
    实测形态:A 编号 63 名字「A」、B 编号 64 名字「63」;先只同步 B 并让取文失败
    (账上 `{"64":"error"}`),再只同步 A 且真的只抓成了 63 —— 回执裸着的 `63` 经那份
    宽匹配扩成了 `["63","64"]`,账本变成 `{"64":"ok","63":"ok"}`,B 的 `last_error`
    还在原处而重试计划已经是 `[]`。
    **反过来不归一的代价只有安全那一侧**:键对不上就是「这一笔谁也清不掉」= 多抓
    一次。所以这一侧**宁可不认**。

    所以这个函数**不收采集器**:归一要先有采集器(`_stable` 第一件事就是去取它的
    `TargetIdentity`),拿不到它就无从再做一次名字匹配 —— 这条路上不许有模糊匹配是
    靠签名钉住的,不是靠这段话。
    """
    from kb.collectors import Coverage
    cell = (_cells.get(stage) or {}).get(_current.get(stage) or '\x00')
    if cell is None or not isinstance(report, Coverage):
        return
    cell['covered'] = [str(t) for t in report.targets]
    cell['whole'] = bool(report.whole)
    # **这两样现在是执行事实了。** 翻了这一格之后它不再过「这一步执行过没有」那一道
    # (`pipeline_daybook._coverage`)—— 中途被停掉的那一轮里真抓成了的那几条照旧
    # 结清,不能为了挡住跳过那一档把它们一起重新标红。
    cell['reported'] = True


def note(stage: str, entry: dict) -> None:
    """`_stage` 写了一条结论:记进**当前这个来源**那一格。没有当前来源就不记。

    不记的那两档都正当:全库那几步(`index`/`extract`/`projects`/`wiki`)压根没有
    来源可挂;收尾补那一笔时当前来源已经摘掉了(见 `channel_run`)。
    """
    cell = (_cells.get(stage) or {}).get(_current.get(stage) or '\x00')
    if cell is None:
        return
    # **记账那三样要留住**:`_stage` 交来的是阶段结论,它不知道这一格的范围 ——
    # 冲掉的话这一格就既不知道红该记给谁、也不知道谁真跑成了。
    keep = {k: cell[k] for k in ('planned', 'covered', 'whole', 'reported')
            if k in cell}
    cell.clear()
    cell.update(deepcopy(entry), **keep)


def snapshot() -> dict:
    """这一轮到此为止的结果账。**拷一份**(它要跟着落盘走)。"""
    return deepcopy(_cells)


def clear() -> None:
    """收尾之后清掉。下一轮要是接着用上一轮的账,就等于拿昨天的结论清今天的欠账。"""
    _cells.clear()
    _current.clear()


def cell_for(run: dict, stage: str, channel: str) -> dict | None:
    """这一轮里「这个来源这一步」自己那条结论;**没有就 `None`**(退回保守档)。

    `None` 有三种来历,折账时一视同仁:升级前的存量记录压根没有这份账;全库那几步
    不按来源记;以及这一轮没轮到它就被停了。
    """
    if stage not in SOURCE_STAGES:
        return None
    cell = ((run.get('results') or {}).get(stage) or {}).get(channel)
    return deepcopy(cell) if isinstance(cell, dict) else None


def merge(records: list[dict]) -> dict:
    """几轮的结果账合成一份:同一个 `(步骤, 来源)` **后面那一轮说了算**。

    给批次汇总用(`pipeline_merge._merge_runs`)。按轮次顺序盖,和当日账本那条
    「后记的盖掉先记的」是同一个道理 —— 那正是「继承」:前一轮失败、后一轮单独
    跑成功了,就该是绿的。
    """
    out: dict[str, dict[str, dict]] = {}
    for run in records:
        for stage, owned in ((run.get('results') or {})).items():
            if not isinstance(owned, dict):
                continue
            for channel, cell in owned.items():
                if isinstance(cell, dict):
                    out.setdefault(stage, {})[channel] = deepcopy(cell)
    return out
