# -*- coding: utf-8 -*-
"""手动收录的端点:一条链接 → 抓取 → 入库 → 写进知识库。

抓取和入库的全部口径在 `kb.feeds.grab`(**硬约束在
`docs/decisions/link-grab.md` 第一节**),这一层只做三件事:把 `GrabError`
翻成 400、把「已经收过了」和「刚收下」分开说、以及**尽力**把后半条链
(导出 → 索引 → 提取)跑起来。

**占不到管道不是失败。** 内容已经在 feeds.db 里了,写进 vault 这一步由任何
一轮 `inbox` 的 export 兜住(面板上那颗按钮、下一次收录、定时同步都算),所以
那一档照旧回 200,只是如实说清「还没写进知识库」。反过来谎报成功的代价是用户
以为检索里搜得到,而 vault 里还没有这一篇。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from kb import pipeline
from kb.feeds import grab
from kb.feeds.registry import KINDS

router = APIRouter(prefix='/api/grab')


class GrabRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


@router.post('')
def grab_link(body: GrabRequest):
    try:
        url = grab.normalize(body.url)
        # **查重排在抓取前面**:已经收过的那一条连 HTTP 都不必发。它只是一句
        # 提示 —— 真正防重复的是 `put_items` 的 `DO NOTHING`(见 `grab.known`)。
        if seen := grab.known(url):
            return {'ok': True, 'added': 0, 'duplicate': True, 'url': url,
                    'title': seen['title'], 'kind': seen['kind'],
                    'detail': f"这条已经在知识库里了（{KINDS.get(seen['kind'], seen['kind'])}）"}
        entry = grab.grab(url)
        saved = grab.save(entry)
    except grab.GrabError as e:
        raise HTTPException(400, str(e)) from None
    # 已经入库的事实先定下来,再谈写不写得进知识库这一轮。
    out = {'ok': True, 'added': saved['added'], 'duplicate': not saved['added'],
           'url': url, 'title': entry['title'], 'kind': grab.KIND,
           'source_id': saved['source']['id'], 'source_name': saved['source']['name'],
           'published': entry['published'], 'chars': len(entry['body'])}
    if not saved['added']:
        # 同一条链接在**这一类**里再投一次:`DO NOTHING` 挡住了,库里那一份一个
        # 字节都没动。这正是想要的末态,不是故障(元规则「0 行不是失败」)。
        out['detail'] = '这条之前就收过了，库里那一份没有改动'
        return out
    why: list[str] = []
    # **这一轮没有 crawl**:手动收录没有可轮询的源,内容刚才已经抓好入库了
    # (`inbox` 的 `pollable=False`,见 `sync.collect` 开头那一道)。要跑的是
    # 后半条链:export 把它写成 vault 里那一篇、index 让检索找得到、extract
    # 买一次打标(收件桶默认开着提取,见 `grab.bucket`)。
    started = pipeline.start_pipeline(['export', 'index', 'extract'], refused=why,
                                      crawl_filter={'channel': grab.KIND})
    out['pipeline'] = 'started' if started else 'busy'
    out['detail'] = ('已收下，正在写入知识库' if started else
                     (why[0] if why else '其他同步正在运行') +
                     '；内容已经存下来了，稍后在「手动收录」里点一次「写入知识库」')
    return out
