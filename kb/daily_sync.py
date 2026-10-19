# -*- coding: utf-8 -*-
"""日报那一轮怎么驱动同步管道:等它空出来、起一轮、盯到收尾、给出成绩单。

从 `kb/daily_run.py` 拆出来(单文件 700 行上限)。**整段逐符号搬过来,一个字没改**
——除了多出来的 `progress` 这一格(见 `_sync_round`)。那边剩下的是"一轮日报的编排"
(设置、定时账本、起跑、收尾),这边只答"跑一轮管道怎么跑"。

**搬动也搬走了打桩的落点**:`_wait_idle` 现在是本模块的全局名,`_sync_round` 解析
的也是本模块那一份 —— 回归要打桩就得写 `patch.object(daily_sync, '_wait_idle', ...)`。
`daily_run` **故意不把 `_wait_idle` 再 import 回去**:re-import 只会让
`patch.object(daily_run, '_wait_idle', ...)` 看着成立、而桩**静默穿不过去**(CLAUDE.md
「搬代码要逐符号对过原文」那条)。`_sync_round` 反过来必须 import 回去:`_run` 按
模块全局名调它,而 `patch('kb.daily_run._sync_round')` 要换的正是那一处。

硬约束见 `docs/decisions/sync-schedule.md`。
"""

from __future__ import annotations

import time

from kb.daily_state import _halting, _lock, _log, _set, _state

# 管道被别人占着时,等它空出来的上限(每个通道各算各的)。
WAIT_BUSY = 900


def _wait_idle(deadline: float, halted=None) -> bool:
    """等管道空下来。True = 空了;False = 等超时或被要求停止。"""
    from kb import pipeline as pl
    halted = halted or _halting
    while pl.get_state()['running']:
        if halted() or time.monotonic() > deadline:
            return False
        time.sleep(2)
    return True


def _sync_round(stages: list[str], crawl_filter: dict | None, label: str,
                deadline: float, channel: str = '', halted=None,
                batch: str = '', progress=None) -> dict:
    """跑一轮管道并等它结束,返回这一轮在成绩单上的那条记录。

    `channel` 只是成绩单上的身份:抓取那一段按通道一轮一个,入库那一段是全库的
    一次、不属于任何通道(空串)。判红只看 `stages` 里点名的那几个阶段。

    `batch` 原样传给 `start_pipeline`:带着它的几轮算同一个业务批次,每一轮收尾
    都由结果存储层把整批合并着落盘。**这里不再自己攒结果** —— 账本由 `_end_run`
    喂,所以"没亲眼看到自己那一轮收尾"的那一档(被下一轮挤出记录)照样进账,
    不像这个函数还得去 `run_result` 找回来。

    `halted` 是"要不要半路收手"的判据,默认 `_halting`(停止 + 超预算都算)。
    入库那一段传的是**只认用户停止**的那一份:超预算的本意是"别再抓了,拿已经
    抓到的内容出报",而不入库的话这一轮抓回来的东西连 `daily.generate` 都查不到
    (它取材自 kb.db 的 documents),那正好把它要保的东西丢了。

    `progress` 是这一轮的进度写到**哪一格**,默认写日报自己那格(`_set`)。出报
    之后那一段传的是 `daily_state.library_progress` —— 它跑在日报的结论已经落下
    之后,写同一格就是把一份**根本没出来**的日报改写成「同步中」,而前端只在
    `phase === 'failed'` 时留失败横幅,于是报成「日报已生成」(审查 R1;那边还有
    一道冻结守卫兜底,见 `daily_state._set`)。
    """
    from kb import pipeline as pl

    halted = halted or _halting
    progress = progress or _set
    entry = {'channel': channel, 'label': label, 'status': 'pending', 'detail': ''}
    progress('sync', f'{label}:等待同步管道空闲')
    if not _wait_idle(min(deadline, time.monotonic() + WAIT_BUSY), halted):
        entry.update(status='skipped', detail='同步管道一直被占用,本轮跳过')
        return entry
    # **收下真实原因再报**(口径同 `pipeline_api` / `intake_api` 那三处):没起跑有
    # 两种,而「被其它任务占用」只覆盖一种 —— 另一种是这一轮的持久化边界落不下来
    # (数据目录写不进去,见 `pipeline.start_pipeline` 的 `refused`)。日报把它也说成
    # 占用的后果是**每一段各劝一次「等一下」**:各来源那几轮加入库那一轮都报等待,
    # 而那一档等不好(得去看磁盘和权限),真实原因只留在管道日志里。
    why: list[str] = []
    run = pl.start_pipeline(stages, crawl_filter=crawl_filter, batch=batch,
                            refused=why)
    if not run:
        entry.update(status='skipped',
                     detail=f'本轮跳过:{why[0]}' if why
                     else '同步管道已被其它任务占用,本轮跳过')
        return entry
    with _lock:
        _state['pipeline_run'] = run
    # 停止请求正好落在"等到空闲"和"起跑"之间时,request_stop 那一刻还不归我们,
    # 它不会去停这一轮;补一次,免得用户按了停止还要眼看着它跑完整个通道。
    if halted():
        pl.request_stop(run)
    progress('sync', f'{label}:同步中')
    # 只认 run id 相同的快照。两秒一跳的轮询里,我们那一轮跑完、另一项同步
    # 马上接手全局管道是完全可能的:只看 running 的话看到的是连绵不断的 True,
    # 既不知道已经换人,还会把别人的阶段结果当成自己这一轮的成绩单。
    #
    # `mine` 只装**终态**(自己那一轮 running 已经放下的那份快照)。中途快照不算:
    # 那时阶段还是 running/pending,拿它数 error 一个都数不出来,于是"被接替"和
    # "跑完了还全是绿的"长得一模一样——报成功却没有任何证据。被接替的那条路统一
    # 去问管道按 id 留的收尾记录。
    mine: dict | None = None
    over = False
    while True:
        snapshot = pl.get_state()
        if snapshot.get('run_id') != run:
            break                    # 我们那一轮已经收尾,现在跑的是别人的
        if not snapshot['running']:
            mine = snapshot          # 正常收尾:这份快照就是最终结果
            break
        stage = snapshot.get('current_stage') or ''
        detail = (snapshot['stages'].get(stage) or {}).get('detail') or ''
        # **两秒一跳的那种进度 `quiet=True`**:进日志的话同一句话每两秒刷一行,
        # 60 行的日志当场只剩这一个阶段。
        progress('sync', f'{label}:{detail}' if detail else f'{label}:同步中',
                 quiet=True)
        # `over` 这个一次性开关**不能省**:原来靠 `not _halting()` 兼任"只说一次",
        # 而入库那一段的 `halted` 不认超预算 —— 没有它的话,超时之后每两秒就会
        # 重新 request_stop 一次、日志里刷同一行。
        if not over and time.monotonic() > deadline and not halted():
            over = True
            _log(f'{label}:超出时间预算,请求停止后直接生成日报')
            _state['over_budget'] = True
            pl.request_stop(run)
        time.sleep(2)
    with _lock:
        if _state['pipeline_run'] == run:
            _state['pipeline_run'] = ''
    # 没能亲眼看到自己那一轮收尾(它跑得比下一跳还快,接替者已经顶上来):
    # 管道按 run id 留了收尾快照,照 id 去取自己那一份。
    if mine is None:
        mine = pl.run_result(run)
    if mine is None:
        # 连收尾记录都没有(被更多轮挤出去了)。**这一档不许算成功**:我们既没
        # 看到 run-1 的任何阶段状态,也不知道它是跑完了、失败了还是被停的,写
        # 一句"同步完成"等于拿一份没人证明过的结果去记账(2026-09-30 第 3 轮
        # 审查实测)。如实记未知,日报照样生成——素材是库里已有的那些,不因为
        # 一个通道的结果查不到就整轮不出。
        entry.update(status='unknown',
                     detail='没拿到这一轮的同步结果(管道已被下一轮运行接替),'
                            '成败未知')
        _log(f'{label}:{entry["detail"]}')
        return entry
    # 只看这一轮请求了的那几个阶段:没选中的阶段一开跑就落 skipped(见
    # `pipeline.start_pipeline`),全收进来的话每一轮都要多报几句"条目化/
    # wiki 未执行"。
    graded = [(name, mine['stages'].get(name) or {}) for name in stages]
    bad = [f'{name} {s.get("detail") or "失败"}'
           for name, s in graded if s.get('status') == 'error']
    # skipped / stopped = 这个阶段该做的事一件没做,**不能跟着算成功**。
    # 2026-09-30 实测:51 个大V账号全都缺 X 通道配置,crawl 因此整个落 skipped
    # (「待配置 51 个」),而这里只数 error —— 那一轮 social 通道报的是绿色的
    # 「同步完成」,日报里那行只剩下 extract 的结果,"一条都没抓到"这件事连同
    # 原因一起看不见。超预算/被停止时管道落 stopped,同理。
    idle = [f'{name} {s.get("detail") or "本轮未执行"}'
            for name, s in graded if s.get('status') in ('skipped', 'stopped')]
    entry.update(status='error' if bad else 'skipped' if idle else 'ok',
                 detail='；'.join(bad + idle)[:400] if bad or idle else '同步完成')
    _log(f'{label}:{entry["detail"]}')
    return entry

