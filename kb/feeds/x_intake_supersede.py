"""清 `repair`:「只比库里多了链接 / 只多了完整证明」的新版覆盖库里那一条;库里那条已是成品的
(`x_intake_store.kept`)直接认它、放行候选 —— 后者就是 `store.settle`,跟 `prepare` 里超窗记账前
那一遍同一个函数,逐账号判。`stage` / `finish` 当场就按 `kept` 判,这里兜的是一轮之内又停进
`repair` 的那几条。

**硬约束在 `docs/decisions/x-channels.md` 第一节。** 两条通道抄同一条推文的链接不一样
(判据在 `x_post_meta.supersedes`),于是库里那一份和这次看到的那一份正文对不上,候选停在
`repair` —— 没有哪一轮自动同步会再碰它。这一步就是它的出路。少了链接、改了字的不覆盖:
库里那份能自证就认它(上一段),证不了自己才留在 `repair`。

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
from datetime import datetime, timezone

from kb.feeds import grab_store, registry as reg, x_post_meta
from kb.feeds import x_intake_store as store


class _Lost(Exception):
    """库里那一条在这一笔里没写成:连同候选放行一起回滚。"""


def run(sources, now=None) -> int:
    """覆盖或认库里那条、放行了几条推文(`repair`,以及没等到覆盖就跨窗转成 `expired` 的;
    同一条挂在几个账号底下也只算一条)。读写失败照抛,由调用方按本机故障处理(那几条留在原档)。

    **先把所有该覆盖的写完,再逐账号收敛一遍**(`store.settle`)。覆盖改的是全库一行的候选和库里
    那条,确认、交付、超窗却是逐账号的:覆盖完之后库里那条就是这一版的成品,每个账号自己那条关联
    (包括别的账号先放行、自己还没确认的)都由收敛那一遍认下、撤掉自己的超窗 —— 认旧正文和覆盖
    新版两条路收在同一个出口,不各删各的报警。`now` 是判窗口的那一刻,缺省此刻(跟同一轮
    `prepare` 用的是同一个时钟)。"""
    released = set()
    for source in sources:
        with store.read_db() as conn:
            rows = conn.execute('''SELECT DISTINCT p.post_id,p.entry,p.state FROM x_intake_candidates p
                JOIN x_intake_links l USING(post_id)
                WHERE p.state IN ('repair','expired') AND l.source_id=? AND l.identity=?''',
                (source['id'], store.identity(source))).fetchall()
            # 跨窗的那一档里多数是库里压根没有的(没等到正文就过期了):不为它们逐条去等写锁
            rows = [row for row in rows if row['state'] == 'repair' or conn.execute(
                "SELECT 1 FROM feed_items WHERE key=? AND kind='social'",
                (reg.item_key('social', json.loads(row['entry'])['url']),)).fetchone()]
        for row in rows:
            try:
                if _supersede(source, row['post_id'], row['entry'], row['state']):
                    released.add(row['post_id'])
            except _Lost:
                pass
    now = now or datetime.now(timezone.utc)
    for source in sources:
        with store._lock(), store.db() as conn:
            if store.current(conn, source):
                released |= store.settle(conn, source, now)
                store.note_expiry(conn, source['id'], store.identity(source), now)
    return len(released)


def _supersede(source, post_id, entry_json, state='repair') -> int:
    """`state` 是取样时它在哪一档:`repair`,或者在 `repair` 里没等到这一步就跨过窗口、被转成了
    `expired` 的那几条 —— 新版就在手里、不用联网,跨没跨窗口都覆盖得了;只认 `repair` 的话它们从此
    没人核对,超窗报警每轮从 `expired` 重建。超窗记录不在这里删:覆盖完库里那条就是成品,
    由 `run` 收尾那一遍逐账号收敛时撤。"""
    entry = json.loads(entry_json)
    if not store.complete(entry):
        return 0
    with store._lock(), store.db() as conn:
        if not store.current(conn, source):
            return 0
        seen = conn.execute("SELECT * FROM feed_items WHERE key=? AND kind='social'",
                            (reg.item_key('social', entry['url']),)).fetchone()
        if seen and store.kept(dict(seen), entry):
            return 0                   # 库里那条就是成品:归 `store.settle`,这里不许改写它
        if not conn.execute('''UPDATE x_intake_candidates SET state='ready',reason=''
                WHERE post_id=? AND state=? AND entry=? AND EXISTS (SELECT 1 FROM x_intake_links
                    WHERE post_id=? AND source_id=? AND identity=?)''',
                (post_id, state, entry_json, post_id, source['id'], store.identity(source))).rowcount:
            return 0
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
