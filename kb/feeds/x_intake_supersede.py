"""清 `repair`:「只比库里多了链接 / 只多了完整证明」的新版覆盖库里那一条;库里那条已是成品的
(`x_intake_store.kept`)直接认它、放行候选。后者是 `stage` / `finish` 现在就会走的那条路,这里
兜的是升级前已经停进 `repair` 的那几条 —— 没有这一步,它们要等下一次被看到才自愈,滑出窗口前
没被看到就以「已超自动窗口」报红,而那条推文其实早在库里。

**硬约束在 `docs/decisions/x-channels.md` 第一节。** 两条通道抄同一条推文的链接不一样
(判据在 `x_post_meta.supersedes`),于是库里那一份和这次看到的那一份正文对不上,候选停在
`repair` —— 没有哪一轮自动同步会再碰它。这一步就是它的出路。正文真不同的(少了链接、改了
字)原样留在 `repair`,不覆盖。

**改写和放行是同一笔事务,判据全在这一笔里核**,先写再拒等于没拒:
- 发起这份工作的订阅此刻还是它(`current`),候选也还挂在**这一版**订阅身份底下
  (`l.identity`)—— 停用、改名、换地址都会让旧那一轮的结果失去交付权,跟 `stage` /
  `confirm` / `finish` 同一个口径;
- 候选还是取样时那一份(`entry` 的 CAS);
- 库里那一条按 **social 的条目键**定位,读、写、放行是同一行。`grab_store.known` 是跨来源
  查重(只按 url),同一条推文被订阅源收过时它会交出那一篇 —— 改的就成了别人;
- 库里那一条还是读到的那一版(`content_hash`/`content_rev` 的 CAS)。

**写哪几列分两种**:导出负载(标题、正文、作者、日期)变了就走 `grab_store.write_version`
(译文作废重排、提取标脏、导出欠账跟着内容走);只差完整证明(`x_post_meta` /
`body_provenance`)时只补那两列 —— 正文没变,译文和提取结果都还是对的,不该作废。
"""
import json

from kb.feeds import grab_store, registry as reg, x_post_meta
from kb.feeds import x_intake_store as store


class _Lost(Exception):
    """库里那一条在这一笔里没写成:连同候选放行一起回滚。"""


def run(sources) -> int:
    """覆盖或认库里那条、放行了几条。读写失败照抛,由调用方按本机故障处理(那几条留在 `repair`)。"""
    released = 0
    for source in sources:
        with store.read_db() as conn:
            rows = conn.execute('''SELECT DISTINCT p.post_id,p.entry FROM x_intake_candidates p
                JOIN x_intake_links l USING(post_id)
                WHERE p.state='repair' AND l.source_id=? AND l.identity=?''',
                (source['id'], store.identity(source))).fetchall()
        for row in rows:
            try:
                released += _supersede(source, row['post_id'], row['entry'])
            except _Lost:
                pass
    return released


def _supersede(source, post_id, entry_json) -> int:
    entry = json.loads(entry_json)
    if not store.complete(entry):
        return 0
    with store._lock(), store.db() as conn:
        if not store.current(conn, source):
            return 0
        if not conn.execute('''UPDATE x_intake_candidates SET state='ready',reason=''
                WHERE post_id=? AND state='repair' AND entry=? AND EXISTS (SELECT 1 FROM x_intake_links
                    WHERE post_id=? AND source_id=? AND identity=?)''',
                (post_id, entry_json, post_id, source['id'], store.identity(source))).rowcount:
            return 0
        seen = conn.execute("SELECT * FROM feed_items WHERE key=? AND kind='social'",
                            (reg.item_key('social', entry['url']),)).fetchone()
        if seen and store.kept(dict(seen), entry):
            # 交付的是库里那一份(`DO NOTHING` 写不动它),口径同 `stage` 里 `existing` 那条路
            conn.execute('UPDATE x_intake_candidates SET entry=? WHERE post_id=?',
                         (json.dumps(dict(seen), ensure_ascii=False), post_id))
            return 1
        if not seen or not x_post_meta.supersedes(dict(seen), entry) or not _write(conn, dict(seen), entry):
            raise _Lost
        return 1


def _write(conn, seen, entry) -> bool:
    if not grab_store.unchanged(seen, entry):
        return grab_store.write_version(conn, seen, entry)
    proof = (entry.get(x_post_meta.FIELD, ''), entry.get('body_provenance', ''))
    if proof == (seen['x_post_meta'], seen['body_provenance']):
        return True                    # 库里已经是这一版(这一笔持着写锁,读到的就是此刻)
    return conn.execute('''UPDATE feed_items SET x_post_meta=?,body_provenance=?,content_rev=content_rev+1
        WHERE key=? AND content_hash=? AND content_rev=? AND x_post_meta=? AND body_provenance=?''',
        (*proof, seen['key'],
         *grab_store.work_version(seen), seen['x_post_meta'], seen['body_provenance'])).rowcount > 0
