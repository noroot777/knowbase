# -*- coding: utf-8 -*-
"""处理记录:这块面板上点过的每一下都落一行,人回头能看见「我对这一条做过什么」。

为什么要有它:这几个出口里有两个**改完之后从列表上就看不出来了** ——
「仅重新排队」把重试次数清零,那一条当场从「重试耗尽」跳回「排队等补齐」,
跟从来没失败过长得一样;「换链接」只换补抓地址,列表上那一行的标题和原
地址一个字都不变。没有这份记录,下一轮再看见它失败时无从判断「这是我上次
放回去的那一条吗」。

**不做快照回滚。** 演示页上每个出口都挂着「撤销」,那是内存里一份种子数据
才能给的东西。落到库上,把 `fulltext_tries` 按快照写回 `MAX_TRIES` 是在**伪造
一次没发生过的失败**,而下一轮补抓会照着这个假数字拒绝重试它。所以这里只有
一个出口真的可撤销:暂缓/不再收录(`shelf` 那张表删一行就回到原状,正文、
行、文件一个字都没动过)。其余几个出口记录在案、不给撤销按钮,`undo` 列留空。

**那颗「恢复」绑的是它自己那一次决定**(`decisions` 列,见 `shelf.release`):
同一条内容先暂缓、恢复、再「不再收录」,第一条记录上那颗按钮只按 key 删的话
会把后来那次决定一起取消。后来改过决定的那几笔,`recent` 会把 `undo_ready`
报成 0 —— 按钮要**明确失效**,不是点下去才拿到一句拒绝(原型的操作契约)。

**记录也绑「哪一世」**(`item_lives` 列):`item_key` 按网址算,删掉重收之后 key
一个字不变,只按 key 查会把上一世的处理记录列在一条全新内容的详情里。

保留 30 天(`KEEP_DAYS`):**写的时候清,读的时候也按同一个下界过滤**。只清不
滤的后果是一段时间不操作之后,界面上还列着两个月前的记录,而旁边那句话写着
「只留最近 30 天」—— 两者只能有一个是真的(审查第 1 轮 B10)。

记录只是给人看的旁证,不参与任何判据 —— 面板上的档位和计数一律现查
`feed_items`,不读这张表。

**硬约束在 `docs/decisions/intake-diagnostics.md` 第一节。**
"""
import json
from datetime import datetime, timedelta, timezone

from kb.intake_diag import shelf

SCHEMA = '''
CREATE TABLE IF NOT EXISTS intake_care_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 at TEXT NOT NULL,
 action TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT '',
 summary TEXT NOT NULL DEFAULT '',
 detail TEXT NOT NULL DEFAULT '',
 affected INTEGER NOT NULL DEFAULT 0,
 item_keys TEXT NOT NULL DEFAULT '',
 undo TEXT NOT NULL DEFAULT '',
 -- 排在最后,和老库 ALTER 补出来的列序一致(见 `kb/feeds/schema.py`)。
 item_lives TEXT NOT NULL DEFAULT '[]',
 decisions TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_care_log_at ON intake_care_log(at DESC);
'''

KEEP_DAYS = 30
MAX_KEYS = 40
LIMIT = 60

# 可撤销的那一个出口:把 shelf 那一行删掉就回到原状。
UNDO_RELEASE = 'shelf_release'


def _reg():
    """延迟 import:`kb.feeds.schema` 把本模块的 `SCHEMA` 拼进建表脚本,而
    `registry` 又从那里取 —— 模块层 import 它会成环(`shelf.py` 同理)。"""
    from kb.feeds import registry as reg
    return reg


def _cutoff() -> str:
    """保留窗口的下界,写成和 `reg.now()` **同一种形状**的字符串。

    `at` 列存的是 `reg.now()` 的 UTC ISO 带偏移,所以这里也得折回 UTC 再
    `isoformat` —— 拿 `datetime('now','-30 day')` 那种没有 `T`、没有偏移的
    形状去比会在日界上判反(`'T' > ' '`),和 `lanes.resolved_cutoff` 同一个坑。
    """
    at = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    return at.isoformat(timespec='seconds')


def record(action: str, kind: str, summary: str, *, detail: str = '',
           affected: int = 0, keys=(), undo: str = '', decisions=()) -> int:
    """落一行。`affected` 必须是**实际写入行数**,不是计划条数。

    `keys` 同理只收**真改成了的那几条**:整份请求里被跳过的那些后来可能有了别的
    决定,而这一行上那颗「恢复」会按 `decisions` 去取消它自己那一次
    (审查第 1 轮 F08)。`decisions` 和 `keys` 一一对应,没有可撤销动作时留空。
    """
    reg = _reg()
    sample = list(dict.fromkeys(keys))[:MAX_KEYS]
    with reg.db() as c:
        lives = []
        if sample:
            slots = ','.join('?' for _ in sample)
            rows = c.execute(f'SELECT key,life FROM feed_items WHERE key IN ({slots})',
                             sample).fetchall()
            found = {row['key']: row['life'] for row in rows}
            lives = [found[key] for key in sample if found.get(key)]
        cur = c.execute(
            'INSERT INTO intake_care_log'
            ' (at,action,kind,summary,detail,affected,item_keys,undo,item_lives,decisions)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?)',
            (reg.now(), action, kind, summary, detail, int(affected),
             json.dumps(sample, ensure_ascii=False), undo,
             json.dumps(lives), json.dumps(list(decisions)[:MAX_KEYS])))
        row_id = int(cur.lastrowid or 0)
        c.execute('DELETE FROM intake_care_log WHERE at<?', (_cutoff(),))
    return row_id


def _loads(value) -> list:
    try:
        found = json.loads(value or '[]')
    except ValueError:
        return []
    return found if isinstance(found, list) else []


def recent(kind: str = '', limit: int = LIMIT) -> list:
    """最近的记录。**窗口下界和写入时那一刀同一份**(`_cutoff`,理由见模块说明)。

    每条可撤销的记录带一个 `undo_ready`:它那几个决定编号里还有几个没被后来的
    操作换掉。0 = 这颗按钮已经失效,前端据此禁掉并说明原因。
    """
    sql = ('SELECT id,at,action,kind,summary,detail,affected,item_keys,undo,decisions'
           ' FROM intake_care_log WHERE at>=?')
    args: list = [_cutoff()]
    if kind:
        sql += ' AND kind=?'
        args.append(kind)
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(max(1, min(limit, 200)))
    out = []
    with _reg().db() as c:
        for r in c.execute(sql, args).fetchall():
            row = dict(r)
            row['item_keys'] = _loads(row['item_keys'])
            row['decisions'] = _loads(row['decisions'])
            out.append(row)
    # **一次问清所有记录的决定编号**,不逐行各发一条:处理记录一屏 60 行。
    alive = set(shelf.live_decisions(
        [one for row in out for one in row['decisions']]))
    for row in out:
        row['undo_ready'] = sum(1 for one in row['decisions'] if one in alive)
    return out


def for_item(key: str, life: str, limit: int = 12) -> list:
    """**这一世**这一条自己的记录。

    `item_keys` / `item_lives` 都是 JSON 数组,用 LIKE 配带引号的整串避免前缀误配
    (key 和 life 都是 32 位定长 hex,带引号之后不会互为前缀)。两道都要:只按 key
    查会把上一世的记录列在一条删掉重收的新内容上(审查第 1 轮 F07)。`life` 取不到
    时一行都不列 —— 认不出来就当「这一世还没有记录」,而不是把别人的记录算上。
    """
    if not life:
        return []
    with _reg().db() as c:
        rows = c.execute(
            'SELECT id,at,action,summary,detail,affected,undo FROM intake_care_log'
            ' WHERE at>=? AND item_keys LIKE ? AND item_lives LIKE ?'
            ' ORDER BY id DESC LIMIT ?',
            (_cutoff(), '%' + json.dumps(key) + '%', '%' + json.dumps(life) + '%',
             max(1, min(limit, 50)))).fetchall()
    return [dict(r) for r in rows]
