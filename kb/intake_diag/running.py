# -*- coding: utf-8 -*-
"""「执行中」那一组:**正在跑的那一轮是什么**,以及它影响不影响眼前这一类来源。

**不伪造条目级的「正在处理这一条」。** 管道压根不发布「此刻手上是哪一条」——
`export` 一段里 `materialize()` 一口气取 `MAX_ITEMS` 条,进度只以阶段为粒度落在
`pipeline` 状态上。按「队列里排最前的那条就是正在处理的」去猜,屏幕上就会出现一条
停在「执行中」几十分钟不动的内容,而它其实这一轮压根没被碰到(那一批已经取够了)。
所以这一组给的是**这一轮的事实**:跑到哪一段、这一段自己报的那句进度、起于何时、
作用在哪几类来源上;条目仍然老老实实留在它自己那一档里。

**「待执行」和「执行中」的证据必须不一样**(状态契约):前者是条目在库里的状态
(`lanes` 那几档判据),后者是 `pipeline.get_state()` 这一份运行时快照。两者各有
各的来源,不许互相推断。
"""
from __future__ import annotations

from kb import pipeline
from kb.feeds import registry as reg

#: 阶段的中文名,**逐字照 `kb/static/js/pipe_stages.js` 的 `PIPE_STAGE_DEFS`** ——
#: 同一段活在两个面板上叫两个名字,人会以为是两件事。
STAGE_NAMES = {
    'crawl': '抓取内容', 'export': '导出文章', 'index': '建立索引',
    'extract': '提取要点', 'projects': '整理工具', 'wiki': '更新 Wiki',
}


def snapshot(kind: str = '') -> dict:
    """这一轮的运行时事实。`kind` 非空时同时回答「它影响不影响这一类来源」。

    `affects` 的判据:这一轮**按来源跑的那几步**(`pipeline_stages.run_scopes`,
    口径只有那一份)里有没有这一类;一个来源都没有时说明这一轮只跑全库那几步
    (index / extract),那它影响**所有**类别 —— 不是「跟谁都无关」。
    """
    state = pipeline.get_state()
    scopes = pipeline.run_scopes(state)
    channels = sorted({scope['channel'] for scope in scopes if scope.get('channel')})
    stages = []
    for key, info in (state.get('stages') or {}).items():
        if info.get('status') or key == state.get('current_stage'):
            stages.append({'key': key, 'name': STAGE_NAMES.get(key, key),
                           'status': info.get('status', ''),
                           'detail': info.get('detail', '')})
    return {
        'running': bool(state.get('running')),
        'run_id': state.get('run_id', ''),
        'run_kind': state.get('run_kind', ''),
        'batch': state.get('batch', ''),
        'stage': state.get('current_stage', ''),
        'stage_name': STAGE_NAMES.get(state.get('current_stage', ''),
                                      state.get('current_stage', '')),
        'started_at': state.get('started_at', ''),
        'stopping': bool(state.get('stop_requested')),
        'channels': channels,
        'channel_names': [reg.KINDS.get(name, name) for name in channels],
        'whole_library': not channels,
        'affects': bool(state.get('running')) and (not kind or not channels
                                                   or kind in channels),
        'stages': stages,
    }
