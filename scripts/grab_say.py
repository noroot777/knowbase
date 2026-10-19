# -*- coding: utf-8 -*-
"""手动收录那条命令行入口的**输出层**:把收敛完的那几条说成终端上的话,或者
一行 JSON。

从 `grab_cli.py` 拆出来的(单文件要过 700 行的上限)。**这一层不做任何判断** ——
`status` 是上游 `verdict` 收敛好的终态,这边只负责「每一档各说自己那句话」和
「把后端那几个事实原样透出去」。
"""
from __future__ import annotations

import json

#: 终态词 → 终端上那个前缀。**每一档都有自己的话**,不许几档共用一句
#: (「已存下」和「已写入知识库」混成一句就是谎报,用户搜不到)。
_LABELS = {
    'saved': '已写入知识库',
    'duplicate': '早就在知识库里了',
    'pending': '已存下，还没写进知识库',
    'bad_content': '收下的那一份看不出是正文',
    # **「没换成」和「内容不合格」是两个事实。** 重抓到的内容和库里那一份一样
    # (或者这期间已经有人换过)时,库里那一份**本来是正文**、一个字节也没坏 ——
    # 说成「看不出是正文」是把一次无害的空转报成质量问题。
    'unchanged': '库里那一份没动',
    # **「收下了然后没了」和「压根没收下」是两件事。** 前者再投一次是重新抓一篇,
    # 后者是接着上次没做完的 —— 混成 `failed` 那一句会让人以为一个字节都没写进去。
    'gone': '收下了，现在不在库里',
    'failed': '没收下',
    'unknown': '结果不确定',
    'absent': '还没收进来',
    'not_tried': '还没投',
}


# ---------------------------------------------------------------------- 说出来

def line_of(it: dict) -> str:
    """一条的人读形态:状态词 + 标题,下一行地址,再一行那句话。

    **状态词按 `_LABELS` 逐档取**,不许几档共用一句:「已存下」和「已写入知识
    库」混成一句话,用户就会去检索里找一篇还没写进去的文章。
    """
    label = _LABELS.get(it['status'], it['status'])
    head = f"{label}  {it.get('title') or ''}".rstrip()
    rows = [head, f"  {it['url']}"]
    if it.get('detail'):
        rows.append(f"  {it['detail']}")
    return '\n'.join(rows)


def record_of(it: dict) -> dict:
    """一条的机读形态(JSONL 一行)。skill `kb-grab` 读的就是这个。

    **`status` 是收敛之后的终态**,另把后端那几个事实原样透出 —— 调用方要能
    自己复核,而不是只拿到我们归纳的一个词。
    """
    res, state = it.get('res') or {}, it.get('state') or {}
    return {'url': it['url'], 'status': it['status'], 'title': it.get('title') or '',
            'detail': it.get('detail') or '', 'label': _LABELS.get(it['status'], it['status']),
            # **缺证据不是 0。** 回包丢了的那一次可能真的新建了一条,把它编码成
            # `added: 0` 是在断言一件我们不知道的事(N2):那时给 `null`。
            'added': res.get('added', 0) if res else None,
            'duplicate': bool(res.get('duplicate')),
            'replaced': res.get('replaced'), 'kind': state.get('kind') or res.get('kind') or '',
            # **没查过不许编码成「确认未完成」。** 幂等那一档不查 state(它早就
            # 走完了),而后端回包里就有 `done=true` —— 只看 state 会输出
            # `status=duplicate, done=false` 这种自相矛盾的一行。
            'done': bool(state.get('done', res.get('done'))),
            'content_ok': state.get('content_ok', res.get('content_ok')),
            'owing': state.get('owing', ''), 'exported': state.get('exported'),
            'stored': state.get('stored'), 'indexed': state.get('indexed'),
            'refetch': bool(it.get('refetch')), 'renews': it.get('renews', 0)}


def _emit(items: list[dict], as_json: bool) -> None:
    for it in items:
        print(json.dumps(record_of(it), ensure_ascii=False) if as_json else line_of(it))
    if not as_json and len(items) > 1:
        tally: dict[str, int] = {}
        for it in items:
            tally[it['status']] = tally.get(it['status'], 0) + 1
        parts = [f'{n} 条{_LABELS.get(s, s)}' for s, n in tally.items()]
        print(f"共 {len(items)} 条:" + '、'.join(parts))
