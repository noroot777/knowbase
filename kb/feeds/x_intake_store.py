"""Transactional candidate/progress writes; only proven complete bodies reach feed_items."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kb.feeds import registry as reg, x_article, x_body, x_post, x_post_meta
from kb.feeds import x_intake_batches as batches
from kb.feeds import x_intake_blocks as blocks
from kb.feeds import x_intake_capacity as capacity
from kb.feeds import x_intake_retention as retention
from kb.maintenance import LockBusy, maintenance_lock

# 存条目 / 水位那几笔**单次**等维护锁的上限(秒)。原来是 0(拿不到当场报「本机待续」),那时
# 来源串着跑,大V观点抓取期间没有别人持锁;各来源同时跑之后,订阅源导出、YouTube 入库都会
# 短暂攥着它,零等待就是让这一路平白交一轮「待续」。**观测那一笔(`set_block`)照旧零等待**:
# 它的契约是「拿不到只警告、原话先随结果交上去」,见 docs/x-incremental-intake.md。
LOCK_WAIT = float(os.environ.get('KB_X_LOCK_WAIT') or 300)

# **单次上限不是边界,整轮才是**(`bounded`)。这几笔是逐账号调的,每一笔各拿一份完整的
# 300 秒,锁被长期占着时 51 个账号光准备就能等四个多钟头,按了停止也照等 —— 取消只在等完
# 之后才被看见。所以一轮 X 采集里所有等锁的地方共用同一个「该停了吗」和同一个截止时刻,
# 每一笔只等到那一刻为止,按了停止就当场放弃(抛 `LockBusy`,按本机故障记「待续」,
# 跟原来零等待拿不到锁同一条路)。截止分两档,调用方各自给:
# - **还没联网的准备工作**(`prepare`)只等到联网截止 —— 过了那一刻本来就不会再联网,等来
#   的锁没有用处;
# - **已经做完的成品**(暂存、确认、收尾、最后那次存盘)可以晚交:截止是整轮预算之后再宽限
#   一个 `LOCK_WAIT`,整轮最多多等这么久,不是每个账号各多等这么久。
# 等锁的时间**算在整轮预算里**,不另外补:等得久,这一轮能联网的时间就少。
_bound = threading.local()


@contextmanager
def bounded(should_stop=None, deadline=None):
    """这一段里(同一条线程)每一笔等锁都受这一轮的停止和截止(monotonic)约束。"""
    before = getattr(_bound, 'at', None)
    _bound.at = (should_stop, deadline)
    try:
        yield
    finally:
        _bound.at = before


def _limits(deadline=None, should_stop=None):
    """这一笔该听谁的停止、等到哪一刻:调用方给了就用它的,没给用这一轮的(`bounded`)。"""
    stop, end = getattr(_bound, 'at', None) or (None, None)
    return should_stop or stop, deadline if deadline is not None else end


def _lock(deadline=None, should_stop=None):
    from kb.pipeline_state import _log
    stop, end = _limits(deadline, should_stop)
    wait = LOCK_WAIT if end is None else min(LOCK_WAIT, max(0.0, end - time.monotonic()))

    def poll(waited):
        if stop and stop():
            raise LockBusy(f'已按停止请求中断：等了 {int(waited)} 秒维护锁，没再等下去')
    return maintenance_lock(wait=wait, on_poll=poll, on_wait=lambda waited: _log(
        f'大V观点:等维护锁(另一路正在写库),已等 {int(waited)} 秒'))


def save_lock(validators, should_stop):
    """最后那次存盘(`feeds.sync._save_result`)的锁:成品那一档(整轮预算之后再宽限一个 `LOCK_WAIT`)。"""
    end = validators.get('x_deadline')
    return _lock(None if end is None else end + LOCK_WAIT, should_stop)


_PHASE_FIELDS = {'discovery': ('scan_at',), 'history': ('history_at',),
                 'recovery': ('history_at',), 'combined': ('scan_at', 'history_at')}


def _phase_fields(phase):
    if phase not in _PHASE_FIELDS:
        raise ValueError(f'未知 X 扫描阶段：{phase}')
    return _PHASE_FIELDS[phase]


def _coverage_gap(covered, coverage_end, floor, end):
    if covered and coverage_end and utc(covered) <= utc(floor) and utc(coverage_end) >= utc(end):
        return ''
    return '最近五天仍有历史缺口'


def _confirmed_progress(progress, phase, incomplete, observed, boundary, end, proven):
    fields = _phase_fields(phase)
    covered, coverage_end = progress['covered'], progress['coverage_end']
    if proven:
        if utc(boundary) is None or utc(end) is None or utc(boundary) > utc(end):
            raise ValueError('X 覆盖证明必须提供有效的连续区间')
        connected = (covered and coverage_end and utc(boundary) <= utc(coverage_end)
                     and utc(end) >= utc(covered))
        if connected:
            covered = min(covered, boundary, key=utc)
            coverage_end = max(coverage_end, end, key=utc)
        elif not coverage_end or utc(end) >= utc(coverage_end):
            covered, coverage_end = boundary, end
    gap = _coverage_gap(covered, coverage_end, progress['floor'], progress['window_end'] or end)
    if gap and 'history_at' in fields and incomplete:
        gap = incomplete
    discovery = ('complete' if proven else 'partial') if 'scan_at' in fields else progress['discovery']
    latest = max(filter(None, [progress['latest'], observed]), key=utc, default='')
    return covered, coverage_end, gap, discovery, latest


# **SQLite 写锁也等,跟别的来源一个口径**(`reg.db()` 默认等 30 秒):页面上任何一个请求开库都会在写锁
# 里核一遍表结构,一秒都不等的话这么短的一下也能让一个号白跑一轮(实测:切完通道立刻点同步,排在最前面
# 的 7 个号全报 `database is locked`,第 8 个起全部正常)。**但跟维护锁一样受这一轮的停止和截止约束**
# (`bounded`),逐账号各等满 30 秒的话,锁被长期占着时一样会累加成几十分钟。
#
# **边界是整笔的,不是每次争锁的**:SQLite 的 `timeout` 是**每一次**争锁各自的上限,而开一笔写要争两次
# (`reg.db()` 初始化里那个 `BEGIN IMMEDIATE`,和交出连接之后这一笔自己的),中间别人插得进来 —— 给连接
# 一个固定的 `timeout`,整笔就能等到它的两倍;SQLite 等着的时候也叫不停。所以这里按 `SLICE` 一小段一小段
# 地争,每段之前重算还剩多少、问一次停没停,两次争锁共用一个截止。**只在没写任何东西的地方重试**:开库
# (失败时 `reg.db()` 自己回滚、关连接)和这一笔开头的 `BEGIN IMMEDIATE` —— 所以 `db()` 交出来的连接
# **已经在写事务里**,调用方不要再 BEGIN。拿到之后不再有要等的:WAL 下持着写锁提交不必再争。
# 等不到照旧抛 `database is locked`,调用方照旧记成本机失败、待办保留。
SQLITE_WAIT = 30.0
SLICE = 0.25


def busy(error):
    """这个 SQLite 错误是不是「锁被别人占着」(等一会儿再来就可能成)。"""
    text = str(error).lower()
    return 'locked' in text or 'busy' in text


@contextmanager
def _write_db(deadline=None, should_stop=None):
    stop, end = _limits(deadline, should_stop)
    cap = time.monotonic() + SQLITE_WAIT
    end = cap if end is None else min(end, cap)

    def contend(take):
        while True:
            halted = bool(stop and stop())
            try:
                return take(0.0 if halted else max(0.0, min(SLICE, end - time.monotonic())))
            except sqlite3.OperationalError as error:
                if not busy(error) or halted or (stop and stop()) or time.monotonic() >= end:
                    raise

    def begin(timeout):
        conn.execute(f'PRAGMA busy_timeout={int(timeout * 1000)}')
        conn.execute('BEGIN IMMEDIATE')

    with ExitStack() as stack:
        conn = contend(lambda timeout: stack.enter_context(reg.db(timeout=timeout)))
        contend(begin)
        yield conn


def db(deadline=None):
    """这一笔的写连接,交出来时已经 `BEGIN IMMEDIATE` 过了(见上)。"""
    return _write_db(deadline)


def read_db(timeout=None):
    return reg.read_db(timeout=_sqlite_wait() if timeout is None else timeout)


def _sqlite_wait():
    stop, end = _limits()
    if stop and stop():
        return 0.0
    return SQLITE_WAIT if end is None else min(SQLITE_WAIT, max(0.0, end - time.monotonic()))


def save_db(validators, should_stop):
    """最后那次存盘(`feeds.sync._save_result`)的写连接,跟 `save_lock` 同一档截止,交出来时同样已在写事务里。"""
    end = validators.get('x_deadline')
    return _write_db(None if end is None else end + LOCK_WAIT, should_stop)


def utc(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (stamp.replace(tzinfo=ZoneInfo('Asia/Shanghai')) if stamp.tzinfo is None else stamp).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def identity(source):
    return json.dumps([source['url'], source.get('feed_url', ''), source.get('rev', 0)])


def post_id(entry):
    match = re.search(r'/status/(\d+)(?:[/?#]|$)', entry.get('url', ''))
    return match.group(1) if match else ''


def complete(entry):
    if x_post.HINT in entry or x_article.HINT in entry:
        return False
    reason, _ = x_post_meta.eligibility(entry)
    return (reason == 'article_merged' or reason == '' and not x_article.needs_article(entry)) and x_body.parts(entry).source_known


def current(conn, source):
    row = conn.execute('SELECT * FROM subscriptions WHERE id=?', (source['id'],)).fetchone()
    return row is not None and identity(dict(row)) == identity(source)


def floor_for(source, now):
    floor = now - capacity.WINDOW
    if boundary := utc(source.get('history_floor')):
        floor = max(floor, boundary)
    return floor


def observed(entries):
    stamps = [utc(entry.get('x_observed_at') or entry.get('published')) for entry in entries]
    stamps.append(getattr(entries, 'skipped_newest', None))
    latest = max((stamp for stamp in stamps if stamp), default=None)
    return latest.isoformat() if latest else ''


def prepare(sources, channel, now, deadline=None):
    with _lock(deadline), db(deadline) as conn:
        retention.prune(conn, now)
        for source in sources:
            if not current(conn, source):
                continue
            floor = floor_for(source, now).isoformat()
            previous = conn.execute('SELECT * FROM x_intake_sources WHERE source_id=?',
                                    (source['id'],)).fetchone()
            if previous and (previous['identity'], previous['channel']) != (identity(source), channel):
                conn.execute('''DELETE FROM x_intake_batch_posts WHERE batch_id IN
                    (SELECT id FROM x_intake_batches WHERE source_id=?)''', (source['id'],))
                conn.execute('DELETE FROM x_intake_batches WHERE source_id=?', (source['id'],))
                conn.execute('DELETE FROM x_intake_expirations WHERE source_id=?', (source['id'],))
                conn.execute('DELETE FROM x_intake_links WHERE source_id=?', (source['id'],))
                conn.execute('DELETE FROM x_intake_sources WHERE source_id=?', (source['id'],))
                previous = None
            conn.execute('''INSERT INTO x_intake_sources(source_id,identity,channel,floor,window_end,gap,gap_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                floor=excluded.floor,window_end=excluded.window_end,gap=excluded.gap,
                gap_at=CASE WHEN x_intake_sources.gap='' AND excluded.gap<>'' THEN excluded.gap_at
                    ELSE x_intake_sources.gap_at END,discovery='unattempted',error='' ''',
                (source['id'], identity(source), channel, floor, now.isoformat(),
                 _coverage_gap(previous['covered'], previous['coverage_end'], floor, now.isoformat())
                 if previous else '最近五天尚未确认完整覆盖', now.timestamp()))
            settle(conn, source, now)
            retention.expire(conn, source['id'], identity(source), floor, now)
            batches.expire(conn, source['id'], identity(source), floor)
            note_expiry(conn, source['id'], identity(source), now)


def excluded(conn, entry):
    identifier = post_id(entry)
    return conn.execute('''SELECT 1 FROM feed_item_exclusions WHERE kind='social' AND
        (key=? OR url LIKE ?)''', (reg.item_key('social', entry['url']), f'%/status/{identifier}')).fetchone() is not None


def existing(conn, entry):
    row = conn.execute('''SELECT * FROM feed_items WHERE kind='social' AND (key=? OR url LIKE ?)''',
                       (reg.item_key('social', entry['url']), f'%/status/{post_id(entry)}')).fetchone()
    cached = conn.execute("SELECT entry FROM x_intake_candidates WHERE post_id=? AND state='ready'",
                          (post_id(entry),)).fetchone()
    proof = json.loads(cached['entry']) if cached else entry
    if row and kept(dict(row), proof):
        return dict(row)
    return None


def kept(row, entry):
    """库里那一条就是这条推文的成品:按链接认出来的这一行能证明自己完整,这次看到的那份
    也完整、同属帖子或同属长文,而且不比库里那份全(`x_post_meta.supersedes`)。

    **「收没收」看链接,「换不换」才看内容**:两份都完整、只是抄法不同(标题派生、空白、
    某个字),库里那份就是成品,这条算已收,不进 `repair` 挂成待办、滑出窗口报超窗。
    只多了链接的新版不认库里那份,留给 `x_intake_supersede` 覆盖;库里那份证不了自己
    (升级前入库、没有完整证明)或帖子/长文对不上的,照旧走 `repair` 等版本化回填。"""
    if not (complete(row) and complete(entry)) or x_post_meta.eligibility(row)[0] != x_post_meta.eligibility(entry)[0]:
        return False
    return same_version(row, entry) or not x_post_meta.supersedes(row, entry)


def collected(row, entry, outside):
    """库里那一行算不算这条候选的成品。这次那份完整时问的就是 `kept`(比库里全的那份留给
    `x_intake_supersede` 覆盖 —— 新版就在手里,跨没跨窗口都写得进去)。这次那份不完整、又已经
    **滑出窗口**的,再没有哪一轮会来补它,库里那行能自证完整就是成品 —— 除非这条候选等的是一篇
    长文、库里那行却不是长文。窗口内不完整的归正文阶段。"""
    if complete(entry) or not outside:
        return kept(row, entry)
    if not complete(row):
        return False
    if x_article.HINT in entry or x_article.needs_article(entry):
        return x_post_meta.eligibility(row)[0] == 'article_merged'
    return True


def adopt(conn, source_id, ident, post_id, entry_json, state, row, outside) -> bool:
    """认库里那一行为这条候选的成品,写在调用方的写事务里;判据待在 UPDATE 自己的 WHERE 里
    (候选还是读到的那一份)。窗口外的再没有哪一轮会来确认、交付它,所以这个账号这一条直接记成
    已确认、已交付(交付本来也只是 `DO NOTHING`),它名下那条超窗记录一并删掉。"""
    if not conn.execute('''UPDATE x_intake_candidates SET state='ready',reason='',entry=?
            WHERE post_id=? AND state=? AND entry=?''',
            (json.dumps(row, ensure_ascii=False), post_id, state, entry_json)).rowcount:
        return False
    if outside:
        conn.execute('''UPDATE x_intake_links SET confirmed=1,delivered=1
            WHERE source_id=? AND identity=? AND post_id=?''', (source_id, ident, post_id))
        conn.execute('DELETE FROM x_intake_expirations WHERE source_id=? AND identity=? AND post_id=?',
                     (source_id, ident, post_id))
    return True


def note_expiry(conn, source_id, ident, now):
    """按超窗记录现算这个账号的超窗计数和那句提示,写回进度行(收尾时那句提示会进订阅的
    `last_notice`)。`prepare` 记完账、清扫删掉报警之后各调一次,两处一个口径。"""
    row = conn.execute('SELECT floor FROM x_intake_sources WHERE source_id=? AND identity=?',
                       (source_id, ident)).fetchone()
    if row:
        expired = retention.recent(conn, source_id, ident, now)
        conn.execute('UPDATE x_intake_sources SET expired=?,outside=? WHERE source_id=? AND identity=?',
                     (expired, retention.notice(utc(row['floor']), expired), source_id, ident))


def settle(conn, source, now) -> int:
    """**超窗记账之前**,把这个账号名下库里其实已有成品的候选收敛成已收(`collected`)。

    超窗报警从候选状态派生(`retention.expire` 连 `expired` 也计),所以光删报警下一轮就会重建;
    只在收尾时清 `repair` 又晚了一步 —— `prepare` 先把跨过窗口的那几条转成 `expired`,从此再没人
    核对它。窗口内只收 `repair`(正文待补那几条归正文阶段);窗口外 `pending`/`repair`/`expired`
    都核对;永久排除的不碰。返回收敛了几条。"""
    ident, outside_before = identity(source), (now - capacity.WINDOW).isoformat()
    rows = conn.execute('''SELECT p.post_id,p.entry,p.state,p.published FROM x_intake_candidates p
        JOIN x_intake_links l USING(post_id) WHERE l.source_id=? AND l.identity=? AND l.delivered=0
        AND p.state IN ('pending','repair','expired')''', (source['id'], ident)).fetchall()
    settled = 0
    for candidate in rows:
        entry, outside = json.loads(candidate['entry']), candidate['published'] < outside_before
        if (not outside and candidate['state'] != 'repair') or excluded(conn, entry):
            continue
        row = conn.execute("SELECT * FROM feed_items WHERE kind='social' AND key=?",
                           (reg.item_key('social', entry['url']),)).fetchone()
        if row and collected(dict(row), entry, outside):
            settled += adopt(conn, source['id'], ident, candidate['post_id'], candidate['entry'],
                             candidate['state'], dict(row), outside)
    return settled


def same_version(row, entry):
    """库里那一条和这次观察到的是不是同一版。**`needs_repair` 只认这一份**;`existing` 认的是
    更宽的 `kept`(同一版,或两份都完整、这次那份不比库里全),它也从这一份起判。

    推文标题是从正文派生的、各条路派生法不同(见 `x_post_meta.same_post`),逐字比会把
    一条正文一字未改的推文判成新版、停在 `repair` —— 没有哪一轮自动同步会再碰它,它就
    以「正文待补」挂到滑出五天窗口为止。"""
    return (all(row[field] == entry.get(field) for field in ('title', 'body', 'x_post_meta', 'body_provenance'))
            or x_post_meta.same_post(row, entry))


def needs_repair(conn, entry):
    row = conn.execute('SELECT * FROM feed_items WHERE key=?', (reg.item_key('social', entry['url']),)).fetchone()
    return bool(row and not same_version(dict(row), entry))


def stage(source, entries, *, phase, incomplete, observed='', now=None, boundary='', end='', batch_id='', block_tokens=None):
    fields = _phase_fields(phase)
    reused = 0
    now = time.time() if now is None else now
    with _lock(), db() as conn:
        if not current(conn, source):
            return None
        progress = conn.execute('SELECT * FROM x_intake_sources WHERE source_id=?', (source['id'],)).fetchone()
        events = blocks.replay(conn, source['id'], identity(source), progress['channel'], progress['block'])
        conn.execute('UPDATE x_intake_sources SET block=? WHERE source_id=?',
                     (json.dumps(events, ensure_ascii=False), source['id']))
        batch_id = batches.begin(conn, source['id'], identity(source), boundary or progress['floor'],
                                 end or datetime.now(timezone.utc).isoformat(), batch_id,
                                 block_token=json.dumps(blocks.snapshot(events, phase) if block_tokens is None else block_tokens))
        for entry in entries:
            identifier, published = post_id(entry), utc(entry.get('x_observed_at') or entry.get('published'))
            if not identifier or not published or published < utc(progress['floor']):
                continue
            cached = conn.execute('SELECT * FROM x_intake_candidates WHERE post_id=?', (identifier,)).fetchone()
            saved = existing(conn, entry)
            blocked = excluded(conn, entry)
            state = 'excluded' if blocked else 'ready' if saved or complete(entry) else 'pending'
            if state == 'ready' and needs_repair(conn, saved or entry):
                state = 'repair'
            if saved:
                entry = saved
                reused += 1
            if cached and cached['state'] == 'unavailable' and not blocked:
                reused += 1
            elif cached and cached['state'] == 'ready' and complete(json.loads(cached['entry'])) and not blocked:
                reused += not bool(saved)
            else:
                conn.execute('''INSERT INTO x_intake_candidates(post_id,entry,published,state,staged_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET
                    entry=excluded.entry,published=excluded.published,state=excluded.state,
                    staged_at=CASE WHEN x_intake_candidates.state NOT IN ('pending','repair')
                        AND excluded.state IN ('pending','repair') THEN excluded.staged_at
                        ELSE x_intake_candidates.staged_at END''',
                    (identifier, json.dumps(entry, ensure_ascii=False), published.isoformat(), state, now))
            conn.execute('INSERT OR IGNORE INTO x_intake_links(source_id,identity,post_id,linked_at) VALUES(?,?,?,?)',
                         (source['id'], identity(source), identifier, now))
            batches.add(conn, batch_id, identifier)
        columns = ','.join(f'{field}=?' for field in fields)
        discovery = 'partial' if 'scan_at' in fields else progress['discovery']
        conn.execute(f'UPDATE x_intake_sources SET {columns},discovery=? WHERE source_id=?',
                     (*([now] * len(fields)), discovery, source['id']))
    return reused


def confirm(source, entries, *, phase, incomplete=None, observed, boundary, end='', coverage_complete=False,
            batch_id=''):
    fields = _phase_fields(phase)
    with _lock(), db() as conn:
        if not current(conn, source):
            return False
        if batch_id:
            if not batches.confirm(conn, source['id'], identity(source), batch_id):
                return False
        else:
            for entry in entries:
                conn.execute('UPDATE x_intake_links SET confirmed=1 WHERE source_id=? AND identity=? AND post_id=?',
                             (source['id'], identity(source), post_id(entry)))
        progress = conn.execute('SELECT * FROM x_intake_sources WHERE source_id=?', (source['id'],)).fetchone()
        proven = coverage_complete is True and incomplete == ''
        end = end or observed or boundary
        state = _confirmed_progress(progress, phase, incomplete, observed, boundary, end, proven)
        if proven and 'history_at' in fields:
            batches.recover(conn, source['id'], identity(source), boundary, end)
        conn.execute('''UPDATE x_intake_sources SET covered=?,coverage_end=?,gap=?,discovery=?,latest=?,error='',
            gap_at=CASE WHEN gap='' AND ?<>'' THEN ? ELSE gap_at END WHERE source_id=?''',
                     (*state, state[2], time.time(), source['id']))
        blocks.resolve(conn, progress, batch_id, proven or bool(observed), phase)
        waits = json.loads(progress['screen_waits'])
        for name in blocks.phases(phase):
            waits.pop(name, None)
        conn.execute('UPDATE x_intake_sources SET screen_waits=? WHERE source_id=?', (json.dumps(waits), source['id']))
    return True


def progress(source):
    with read_db() as conn:
        row = conn.execute('SELECT * FROM x_intake_sources WHERE source_id=? AND identity=?',
                           (source['id'], identity(source))).fetchone()
        result = dict(row) if row else {}
        if row:
            blocks.project(result, blocks.replay(conn, source['id'], identity(source), row['channel'], row['block']))
            result['unconfirmed'] = batches.count(conn, source['id'], identity(source), row['floor'])
    return result


def set_block(source, block):
    with maintenance_lock(wait=0), db() as conn:
        if current(conn, source):
            row = conn.execute('SELECT block FROM x_intake_sources WHERE source_id=? AND identity=?',
                               (source['id'], identity(source))).fetchone()
            if not row:
                return
            events = blocks.merge(row['block'], {block.get('phase', 'discovery'): block})
            conn.execute('UPDATE x_intake_sources SET block=? WHERE source_id=? AND identity=?',
                         (json.dumps(events, ensure_ascii=False), source['id'], identity(source)))


def screen_wait(source, phase, run_started):
    with _lock(), db() as conn:
        if not current(conn, source):
            return 0
        row = conn.execute('SELECT screen_waits FROM x_intake_sources WHERE source_id=? AND identity=?',
                           (source['id'], identity(source))).fetchone()
        waits = json.loads(row['screen_waits'])
        prior = waits.get(phase, {})
        count = prior.get('count', 0) + (prior.get('run') != run_started)
        waits[phase] = dict(count=count, run=run_started)
        conn.execute('UPDATE x_intake_sources SET screen_waits=? WHERE source_id=? AND identity=?',
                     (json.dumps(waits), source['id'], identity(source)))
        return count


def attempt(source, phase, reason=''):
    fields = _phase_fields(phase)
    with db() as conn:
        if current(conn, source):
            columns = ','.join(f'{field}=?' for field in fields)
            conn.execute(f'UPDATE x_intake_sources SET {columns},error=? WHERE source_id=? AND identity=?',
                         (*([time.time()] * len(fields)), reason, source['id'], identity(source)))
            if 'scan_at' in fields:
                conn.execute("UPDATE x_intake_sources SET discovery='partial' WHERE source_id=?", (source['id'],))


def pending(sources, now=None):
    allowed = {(source['id'], identity(source)): source for source in sources}
    now = time.time() if now is None else now
    with read_db() as conn:
        rows = conn.execute('''SELECT p.*,l.source_id,l.identity FROM x_intake_candidates p
            JOIN x_intake_links l USING(post_id) JOIN x_intake_sources s ON s.source_id=l.source_id
            AND s.identity=l.identity WHERE p.state='pending' AND p.retry_at<=?
            AND p.published>=s.floor ORDER BY p.attempted,p.post_id''', (now,)).fetchall()
    groups, seen = {}, set()
    for row in rows:
        source = allowed.get((row['source_id'], row['identity']))
        if source and row['post_id'] not in seen:
            seen.add(row['post_id'])
            groups.setdefault(source['id'], []).append((source, dict(row)))
    result = []
    while groups:
        for source_id in list(groups):
            result.append(groups[source_id].pop(0))
            if not groups[source_id]:
                del groups[source_id]
    return result


def finish(source, task, entries, unavailable, reason, *, block_tokens=None, lock_wait=None):
    """`lock_wait`:这一笔连等维护锁带等 SQLite 写锁一共最多等几秒,缺省按这一轮的边界(`bounded`)。
    人点了按钮在等的那一下传短的(`x_article_capture.save`)—— 一轮大编码能攥锁几分钟,那是整条
    HTTP 连接挂死。"""
    end = None if lock_wait is None else time.monotonic() + lock_wait
    with _lock(end), db(end) as conn:
        if not current(conn, source):
            return
        progress = conn.execute('SELECT * FROM x_intake_sources WHERE source_id=? AND identity=?',
                                (source['id'], identity(source))).fetchone()
        if not progress or utc(task['published']) < utc(progress['floor']):
            return
        entry = next((entry for entry in entries if complete(entry)), None)
        audit = next((item for item in unavailable if item.get('url') == json.loads(task['entry'])['url']), None)
        state = 'excluded' if excluded(conn, json.loads(task['entry'])) else (
            'ready' if entry else 'unavailable' if audit else 'pending')
        if state == 'unavailable':
            reason = audit.get('reason', '')
        if state == 'ready' and (saved := existing(conn, entry)):
            entry = saved
        elif state == 'ready' and needs_repair(conn, entry):
            state = 'repair'
            reason = '存量正文缺少同版完整证明，需走版本化回填；未覆盖旧正文'
        failures = task['failures'] + (state == 'pending')
        changed = conn.execute('''UPDATE x_intake_candidates SET entry=?,state=?,attempted=?,failures=?,
            retry_at=?,reason=?,unavailable=? WHERE post_id=? AND entry=? AND state='pending' AND failures=?''',
            (json.dumps(entry, ensure_ascii=False) if entry else task['entry'], state, time.time(), failures,
             time.time() + min(3600, 60 * 2 ** min(failures, 6)) if state == 'pending' else 0,
             reason[:500], json.dumps(audit, ensure_ascii=False) if state == 'unavailable' else '',
             task['post_id'], task['entry'], task['failures'])).rowcount
        if changed and state in ('ready', 'unavailable') and block_tokens:
            conn.execute('UPDATE x_intake_sources SET block=? WHERE source_id=? AND identity=?',
                         (json.dumps(blocks.clear(progress['block'], block_tokens), ensure_ascii=False),
                          source['id'], identity(source)))
        return bool(changed and state in ('ready', 'unavailable'))


def delivery(source):
    with read_db() as conn:
        rows = conn.execute('''SELECT p.*,l.delivered,l.confirmed FROM x_intake_candidates p
            JOIN x_intake_links l USING(post_id) WHERE l.source_id=? AND l.identity=?''',
            (source['id'], identity(source))).fetchall()
        progress = conn.execute('SELECT floor,expired FROM x_intake_sources WHERE source_id=? AND identity=?',
                                (source['id'], identity(source))).fetchone()
    entries, pending_count, unavailable, expired = [], 0, [], progress['expired'] if progress else 0
    for row in rows:
        entry = json.loads(row['entry'])
        if row['state'] == 'ready' and row['confirmed'] and not row['delivered'] and complete(entry):
            entries.append(entry)
        pending_count += row['state'] in ('pending', 'repair')
        if row['state'] == 'unavailable':
            unavailable.append(json.loads(row['unavailable']) if row['unavailable'] else
                               dict(url=entry['url'], reason=row['reason']))
    return entries, pending_count, unavailable, expired


def acknowledge(conn, source, entries):
    if not current(conn, source):
        return False
    for entry in entries:
        conn.execute('UPDATE x_intake_links SET delivered=1 WHERE source_id=? AND identity=? AND post_id=?',
                     (source['id'], identity(source), post_id(entry)))
    return True


def statuses(sources, channel='browser'):
    from kb.feeds import x_channels
    from kb.feeds.prefetch import X_LOCAL_SECONDS
    eligible = {source['id'] for source in sources if channel in x_channels.CHANNELS
                and source.get('enabled') and reg.uses_channel(source) and x_channels._handle(source)}
    schedule = capacity.schedule_notice() if eligible else ''
    now = datetime.now(timezone.utc)
    with read_db(timeout=30) as conn:
        rows = {row['source_id']: dict(row) for row in conn.execute('SELECT * FROM x_intake_sources')}
        pending_rows = conn.execute('''
            SELECT l.source_id,l.identity,count(*) AS pending,
                max(max(p.attempted),max(p.staged_at),max(l.linked_at)) AS attempted FROM x_intake_links l
            JOIN x_intake_candidates p USING(post_id) WHERE p.state IN ('pending','repair')
            GROUP BY l.source_id,l.identity''').fetchall()
        counts = {(row['source_id'], row['identity']): row['pending'] for row in pending_rows}
        # 正文待补那一条自己的时刻:那几条最后一次补正文、最后一条进入待补、或最后一条
        # **进入这个账号**的时刻。候选按 `post_id` 全局只有一行,别的账号先收过的转推在
        # 这个账号这里是新事,`staged_at` 却还是那边的旧时间 —— 所以账号这一维认
        # `linked_at`;反过来不能为它去刷新共享的 `staged_at`,那会把先收的账号也变新。
        pending_at = {(row['source_id'], row['identity']): row['attempted'] or 0 for row in pending_rows}
        unconfirmed = {source_id: batches.count(conn, source_id, row['identity'], row['floor'])
                       for source_id, row in rows.items()}
        unconfirmed_at = {source_id: batches.latest(conn, source_id, row['identity'], row['floor'])
                          for source_id, row in rows.items()}
        for source_id, row in rows.items():
            row['expired'] = retention.recent(conn, source_id, row['identity'], datetime.now(timezone.utc))
            row['expired_at'] = retention.recent_at(conn, source_id, row['identity'], datetime.now(timezone.utc))
            row['outside'] = retention.notice(utc(row['floor']), row['expired'])
    for source in sources:
        row = rows.get(source['id'])
        if row and row['identity'] == identity(source) and row['channel'] == ('browser' if channel == 'attach' else channel):
            source['x_intake'] = {key: row[key] for key in
                                  ('discovery', 'gap', 'gap_at', 'expired', 'expired_at', 'error', 'floor',
                                   'scan_at', 'history_at', 'outside')}
            source['x_intake']['pending'] = counts.get((source['id'], identity(source)), 0)
            source['x_intake']['pending_at'] = pending_at.get((source['id'], identity(source)), 0)
            source['x_intake']['unconfirmed'] = unconfirmed[source['id']]
            source['x_intake']['unconfirmed_at'] = unconfirmed_at[source['id']]
            with read_db(timeout=30) as conn:
                events = blocks.replay(conn, source['id'], identity(source), row['channel'], row['block'])
            blocks.project(source['x_intake'], events)
        if source['id'] in eligible:
            state = source.setdefault('x_intake', dict(discovery='unattempted'))
            if state.get('gap') or state['discovery'] != 'complete':
                state['window_capacity'] = capacity.observe(capacity.plan(len(eligible), channel, budget=X_LOCAL_SECONDS,
                                                         window=max(timedelta(0), now - floor_for(source, now))), state, now)
                state['capacity_limited'] = capacity.limited(state['window_capacity'])
                state['coverage_warning'] = capacity.warning(state['window_capacity'], schedule)
        if 'x_intake' in source:
            from kb.feeds.x_intake_diagnostics import issues
            source['x_intake']['issues'] = issues(source['x_intake'])


def save_metrics(started, metrics):
    with db() as conn:
        conn.execute('INSERT INTO x_intake_runs(started,metrics) VALUES(?,?)',
                     (started, json.dumps(metrics, ensure_ascii=False)))
        conn.execute('DELETE FROM x_intake_runs WHERE id NOT IN (SELECT id FROM x_intake_runs ORDER BY id DESC LIMIT 30)')


def latest_metrics(channel=None):
    with read_db(timeout=30) as conn:
        row = conn.execute('SELECT * FROM x_intake_runs ORDER BY id DESC LIMIT 1').fetchone()
    metrics = dict(started=row['started'], **json.loads(row['metrics'])) if row else None
    return metrics if metrics and (channel is None or metrics.get('channel') == channel) else None
