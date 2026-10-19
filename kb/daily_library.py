# -*- coding: utf-8 -*-
"""日报出完报之后那一段:条目化(图鉴条目)和 wiki 层(双链、汇总页、三张图谱)。

从 `kb/daily_run.py` 里分出来的(单文件 700 行上限)。**分的是新增的这一块职责,
不是把旧代码搬走** —— 旧代码那边有一大批回归在 `patch.object(daily_run, ...)`
打桩模块属性(`SETTINGS_FILE`、`_channel_labels`、`datetime`…),而 `from ... import`
带过去的是**名字不是绑定**:搬走定义之后那些打桩会**静默穿不过去**,看起来像代码
坏了(实测 7 条红,其中 4 条是 `_book` 这个模块级名字漏带出来的 NameError)。
往外搬的成本不在行数上,在这儿。

当初把它们排除在日报之外的理由成立(日报不读它们的产物,跑一遍要几十分钟),
但从"日报不跑"推到了"没有任何定时跑" —— 这一份改的就是后半句。

**这一段必须跑在日报那一轮的业务批次里。** 批次外的一轮收尾会把自己写成
「最近一次运行」,于是这一批的抓取失败和失败名单一起从主视图消失(抓取写
「未选择」、页头写「同步已完成」),「重试失败项」跟着去抓一个空计划 —— 正是
`docs/decisions/pipeline-history.md` 里记在案的那个事故形态。所以 `daily_run`
把 `close_batch()` 推迟到了调用本模块之后。

**位置的两条理由,缺一条都不该放在那儿:**
· **要在出报之后** —— 它一轮几十分钟,排在前面就是把日报推迟一个钟头,而日报
  压根不读它的产物;
· **要在 `finally` 里** —— 出报失败(模型 503 那一族)照样要跑。绑在"出报成功"
  上的话,2026-10-19 那种连失败三次的日子里图鉴和 wiki 也跟着三次不跑,而它们
  和日报的成败毫无关系:它们消费的是**已经入库**的内容,那一步早就成了。

依赖方向:本模块 → `daily_state`;`daily_run` 反过来 import 本模块。跑一轮要用的
`_sync_round` 由调用方传进来(它属于编排,住在 `kb.daily_sync`)。
"""
from __future__ import annotations

import os
import time

from kb.config import ScheduleUnreadable
from kb.daily_state import _lock, _log, _state, library_progress

# 出完报之后那一段:条目化(图鉴条目)和 wiki 层(双链、汇总页、三张图谱)。
#
# **它们原来只有手动一条路,结果就是一次都没跑。** 日报那一轮不跑它们(理由成立:
# 全库的活、几十分钟,日报不读它们的产物),而「重试失败项」只在它们**上一轮真跑过
# 且失败了**时才重排 —— 上一轮根本没跑过,于是永远轮不到。合起来:只要用户不手动点
# 一次「新建同步」,这两段**永远不执行**。2026-10-19 实测:三张图谱页停在 10-09
# 17:23、wiki 层 10-10 之后一个文件没动、图鉴最后一条条目 10-09 09:50,而那 10 天里
# 新入库 4000+ 篇。
#
# 更糟的是**两处滑动窗口会造成永久空白**:`_run_projects` 的公众号条目化带
# `--since 今天-3天`、`wiki_daily --recent-days 2`。窗口滑过去之后,那几天的内容
# 再点「新建同步」也补不回来,只能手动回溯。天天跑就没有这件事。
#
# **挂在日报尾巴上,不新开一条定时**:管道是单例,两份定时到点撞在一起,先到的会把
# 后到的挤成「跳过」,而面板上两边都显示正常(CLAUDE.md 那条警告)。跑在**出报之后**
# 所以不拖慢出报。
LIBRARY_STAGES = ['projects', 'wiki']
LIBRARY_LABEL = '整理工具与 Wiki'
# 出报之后那一段的预算。给得宽(默认 90 分钟):wiki 子进程自己的超时就是 45 分钟,
# 条目化那四步各 20~90 分钟。**超了也不影响任何已经交付的东西** —— 报已经出了,
# 这一段只是在它后面补图鉴和 wiki,下一轮接着补。
LIBRARY_BUDGET = int(os.environ.get('KB_DAILY_LIBRARY_BUDGET', str(90 * 60)))
def amend_done(extra: str) -> None:
    """往已经写好的收尾文案后面补一句。只补、不改 phase。

    批次要罩住出报之后那一段(见 `_library_round`),所以「发布成没成」比收尾文案
    晚一步才知道。**不补的话那句话就是一句干净的「日报已生成」**,而同步中心那边
    看不到这一轮的失败、也点不了重试 —— 两边都显示正常,正是不许出现的那一档。

    phase 不是 `done` 时什么都不做:失败/停止那两档自己那句话已经说明问题了,
    往后面接一句批次发布的事只会把真正的原因挤到后面去。
    """
    with _lock:
        if _state['phase'] != 'done':
            return
        _state['detail'] += extra
    _log(_state['detail'])
def library_round(batch: str, sync_round) -> None:
    """出报之后那一段:条目化(图鉴条目)和 wiki 层(双链、汇总页、三张图谱)。

    **这一段原来只有手动一条路,结果就是一次都没跑**(事故形态和实测数字写在
    `LIBRARY_STAGES` 上面)。挂在日报尾巴上而不是新开一条定时:管道是单例,两份
    定时到点撞在一起,先到的把后到的挤成「跳过」,而面板上两边都显示正常。

    **跳过的三档各有各的理由,都不是「出报失败了就别跑」**:
    · 这一轮压根没同步(`sync=False`,手点「直接生成」)—— 没有批次可进,而且
      用户要的就是"只出报";
    · 用户按了停止 —— 停止是对整轮说的(口径同入库那一段);
    · 设置里关掉了 —— 它烧额度,得让人关得掉。
    **超预算不算**:超预算的本意是"别再抓了",而这一段压根不抓,它消费的是已经
    入库的内容;跟着跳过等于让"今天抓得慢"决定"图鉴更不更新"。

    红了就红在它自己这一项上(`_sync_issues` 会把它列进「N 项未完成」),不牵连
    任何一个来源 —— 它和入库那一段一样是全库的活,`channel` 留空只有 label。
    """
    if not batch:
        return
    if _state['stop_requested']:
        _state['channels'].append(
            {'channel': '', 'label': LIBRARY_LABEL, 'status': 'skipped',
             'detail': '已停止,未整理'})
        library_progress('skipped', f'{LIBRARY_LABEL}:已停止,未整理')
        return
    try:
        from kb.daily_run import load_settings
        if not load_settings()['library']:
            return
    except ScheduleUnreadable as e:
        # 读不出设置**就不跑**:这一段烧额度,而"读不出来"证明不了用户开着它。
        # 反过来(读不出就跑)是拿一份读不懂的配置替用户花钱。
        _log(f'{LIBRARY_LABEL}:读不出定时设置({e}),这一轮跳过')
        return
    # **进度写自己那一格(`progress=library_progress`),不写日报的 phase/detail。**
    # 写同一格就是把已经确定的 done/failed 改写成「同步中」,而前端只在
    # `phase === 'failed'` 时留失败横幅 —— 一次根本没出来的日报于是被报成「日报
    # 已生成」(审查 R1,实测终态 `phase='sync', error='Claude 503', result=None`)。
    entry = sync_round(
        LIBRARY_STAGES, None, LIBRARY_LABEL,
        time.monotonic() + LIBRARY_BUDGET,
        halted=lambda: bool(_state['stop_requested']), batch=batch,
        progress=library_progress)
    _state['channels'].append(entry)
    # 终态落回自己那一格(`quiet`:`_sync_round` 收尾那句已经进过日志了)。不落的话
    # 这一格永远停在「同步中」,页面上那段整理跑完了还在转。
    library_progress('failed' if entry['status'] == 'error' else 'done',
                     f'{LIBRARY_LABEL}:{entry["detail"]}', quiet=True)


def library_tail(batch: str, sync_round, verdict: str) -> None:
    """出报之后那一段的完整收尾:结论记进同步中心 → 跑整理 → 关批次。

    **这里面每一步炸了都不许挡住后面的步骤**(审查 R3):`library_round` 要去解析
    采集器配置、起管道、开线程,一步都不是"语言层面不会抛"的操作,而它后面压着
    `close_batch`(不关的话下一轮管道运行被算进一个早就结束的批次)和调用方的
    `_release_round`(唯一一处把日报的 `running` 放回 False 的地方)。整理红了就红在
    它自己那一项上,不牵连任何一个来源 —— 它和入库那一段一样是全库的活。

    `verdict` 是日报这一轮的结论(那句收尾文案)。**它要在同步中心也看得见**
    (事故形态在 `docs/decisions/sync-schedule.md`):进的是**批次账本**,而且按
    **我们自己这一批**的身份记、记完立刻发布 —— 理由都在 `pipeline_batch.batch_note`
    上。记不上就在日报自己的日志里说一句,不许静默丢掉(审查 R6)。
    """
    from kb import pipeline as pl
    try:
        if not pl.batch_note(verdict, batch) and batch:
            _log('这一轮的结论没能记进同步中心:这一批已经不在了')
        library_round(batch, sync_round)
    except Exception as e:                       # noqa: BLE001 - 理由见 docstring
        _state['channels'].append(
            {'channel': '', 'label': LIBRARY_LABEL, 'status': 'error',
             'detail': f'整理中断:{e}'[:400]})
        library_progress('failed', f'{LIBRARY_LABEL}:整理中断({e})')
    finally:
        # 发布不成不是日报的失败,但**不许咽下去**(见 `amend_done`)。
        if issue := pl.close_batch():
            _log(f'同步结果未发布:{issue}')
            amend_done(f'；{issue}')
