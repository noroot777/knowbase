"""`repair` 里那几条「只比库里多了链接」的新版,用它覆盖库里那一条。

**硬约束在 `docs/decisions/x-channels.md` 第一节。** 两条通道抄同一条推文的链接不一样
(判据在 `x_post_meta.supersedes`),于是库里那一份和这次看到的那一份正文对不上,候选停在
`repair` —— 没有哪一轮自动同步会再碰它。这一步就是它的出路:改写走 `grab_store.replace`
(取样那一刻的版本 CAS、译文作废重排、提取标脏、导出欠账跟着内容走),写成了才放行候选。
正文真不同的(少了链接、改了字)原样留在 `repair`,不覆盖。
"""
import json

from kb.feeds import grab_store, x_post_meta
from kb.feeds import x_intake_store as store


def run(sources) -> int:
    """覆盖并放行了几条。读写失败照抛,由调用方按本机故障处理(那几条留在 `repair`)。"""
    ids = [source['id'] for source in sources]
    if not ids:
        return 0
    with store.read_db() as conn:
        rows = conn.execute(f'''SELECT DISTINCT p.post_id,p.entry FROM x_intake_candidates p
            JOIN x_intake_links l USING(post_id)
            WHERE p.state='repair' AND l.source_id IN ({','.join('?' * len(ids))})''', ids).fetchall()
    released = 0
    for row in rows:
        entry = json.loads(row['entry'])
        seen = grab_store.known(entry['url'])
        if not seen or not store.complete(entry) or not x_post_meta.supersedes(seen, entry):
            continue
        # 换没换成都去问一次:没换(库里已经是这一版)时放行照样成立,换输了(别人
        # 刚改过它)时 `release_repair` 自己那道判据会拒。
        grab_store.replace(seen, entry)
        released += store.release_repair(row['post_id'], row['entry'])
    return released
