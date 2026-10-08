"""Incremental fetch and atomic Markdown export for all feed-backed source types."""
from __future__ import annotations

import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from kb.config import VAULT_ROOT
from kb.feeds import registry as reg
from kb.maintenance import maintenance_lock
from kb.feeds import translate, x_channels
from kb.feeds.fetching import CollectionBudgetExceeded, SOURCE_SECONDS, fetch_source
from kb.pipeline_state import _state, _stage
from kb.scanner import parse_frontmatter

FETCH_WORKERS = 4
COLLECT_SECONDS = 90
# X 的登录态通道(feedgrab / 浏览器)另算预算:它们是串行的 —— 同一个浏览器
# profile 只能被一个实例打开 —— 而单个账号要滚时间线、必要时还要等懒加载,
# 90 秒连五个号都跑不完。这条通道按天同步(见 `source_types` 里 social 的
# interval),所以给得宽;用完的账号记成"留待下次",靠 last_attempt 轮转补齐。
X_LOCAL_SECONDS = float(os.environ.get('KB_X_LOCAL_SECONDS', '1200'))
BRIDGE_FAILURE_LIMIT = 3
BUDGET_MESSAGE = '本轮采集时间预算不足，剩余订阅留待下次同步'


def _fetch_with_budget(source, settings, scope, collect_deadline):
    # Check again in the worker: submission/scheduling can use up the margin.
    # Each admitted source gets its own full allowance, never a clipped tail.
    deadline = time.monotonic() + SOURCE_SECONDS
    if deadline > collect_deadline:
        raise CollectionBudgetExceeded
    return fetch_source(source, settings, unconditional=scope.ranged,
                        deadline=deadline, collect_deadline=collect_deadline)


def _within_history(source, entries):
    """新订阅只收最近几天的内容,不把源站的整个存档一起拖进来。

    下界是订阅创建(或改链接重置)那一刻钉死的一个日期,**不是"距今 N 天"的
    滑动窗口**。两点理由,缺一条这道闸就从防线变成漏斗:

    ①滑动窗口会在源站停更几天后恢复时,把那几天的内容静默丢掉——而钉死的下界
      对一个已经建立起来的订阅永远是过去式,只在它自己最前面那几次抓取起作用。
    ②只拦"第一次抓取"同样不行:第一次之后 etag 命中 304、什么都不返回,等源站
      下次更新时整份 feed(连同那 500 条存档)又回来了,那时闸门已经撤了。
      按固定日期判则与抓第几次无关。

    没有发布时间的条目照收:它身上没有任何"很旧"的证据,丢掉是不可恢复的静默
    损失,而日报那边本来就按"补录"接住这一档(只会出现一次)。区间补抓
    (`scope.ranged`)不受这道闸限制——那是用户点名要的那段日期,拿它当逃生口。

    **下界现读,不信手里那份订阅快照**:快照是发请求**之前**读的,而这中间
    `feed_trim_history` 可能刚给这个订阅钉上下界、把存档清了出去——按旧快照
    (空下界)存结果就是把刚删掉的旧文原样再入库一遍,文件和归属一起回来
    (2026-10-01 审查实测 1 条)。订阅行已经没了的时候才退回快照那一份。
    """
    floor = _stored_floor(source)[:10]
    if not floor:
        return entries
    return [e for e in entries if not e['published'] or e['published'][:10] >= floor]


def _stored_floor(source) -> str:
    with reg.db() as c:
        row = c.execute('SELECT history_floor FROM subscriptions WHERE id=?',
                        (source['id'],)).fetchone()
    return (row['history_floor'] if row else source.get('history_floor')) or ''


def _save_result(source, result, scope):
    entries, validators = result
    if scope.ranged:
        entries = [e for e in entries if e['published'] and scope.since <= e['published'][:10] <= scope.until]
        # A ranged import must not cache away entries outside the selected dates.
        validators = {k: source[k] for k in ('etag', 'modified', 'resolved_url')}
    # 存盘整段在维护锁里:存量清理和索引持同一把锁,两边交错的话它删掉的旧文
    # 会被这一轮按旧快照原样写回去(见 `kb.maintenance`)。锁内再现读一次下界,
    # 所以对方不持锁的旁路同样挡得住 —— 两道各管一段,缺一不可。
    with maintenance_lock():
        if not scope.ranged:
            entries = _within_history(source, entries)
        added = reg.put_items(source, entries)
        with reg.db() as c:
            c.execute('''UPDATE subscriptions SET last_attempt=?,last_success=?,last_error='',
                failures=0,retry_at=0,etag=?,modified=?,resolved_url=? WHERE id=?''',
                (reg.now(),reg.now(),validators['etag'],validators['modified'],validators['resolved_url'],source['id']))
    return added


def _save_failure(source, error, summary):
    reason = str(error)[:1200] or type(error).__name__
    summary['errors'].append({'title': source['name'], 'reason': reason})
    with reg.db() as c:
        c.execute('''UPDATE subscriptions SET last_attempt=?,last_error=?,failures=failures+1,
            retry_at=? WHERE id=?''', (reg.now(),reason,
            time.time() + min(86400, 1800 * 2 ** min(source['failures'], 6)),source['id']))


def _prefetch_x(kind, sources, settings):
    """X 登录态通道的整批预取。跑在起线程池之前,所以读侧不必加锁。

    一个账号都抓不到也不抛:每个账号的失败已经落在它自己那一项上,随后
    `fetch_source` 会把它翻成这条订阅的失败 —— 一个改了名的号不该把整轮带掉。
    通道是 bridge(或这一类来源没有通道概念)时 `targets()` 返回空,直接空转。
    """
    x_channels.reset()
    if not x_channels.supports_channels(kind):
        return
    picked = x_channels.targets(sources, settings)
    if not picked:
        return
    name = x_channels.label(x_channels.channel_of(settings))
    _stage('crawl', 'running', f'{reg.KINDS[kind]} · {name}:准备读取 {len(picked)} 个账号')

    def progress(done, count, handle):
        _stage('crawl', 'running', f'{reg.KINDS[kind]} · {name} {done}/{count} · @{handle}')

    x_channels.prefetch(sources, settings, budget=X_LOCAL_SECONDS,
                        on_progress=progress,
                        should_stop=lambda: bool(_state.get('stop_requested')))


def collect(kind, scope):
    started = reg.now()
    sources = [s for s in reg.subscriptions(kind) if s['enabled'] and
               (not scope.targets or str(s['id']) in scope.targets or s['name'] in scope.targets)]
    summary = {'sources': len(sources), 'checked': 0, 'added': 0, 'deferred': 0,
               'unconfigured': 0, 'skipped': 0, 'message': '', 'errors': [], 'stopped': False}
    settings = reg.get_settings(kind)
    pending, unconfigured_why = [], ''
    for source in sources:
        if problem := reg.configuration_error(source, settings):
            summary['unconfigured'] += 1
            # 理由按当前通道走(桥接要模板、feedgrab 要登录态、浏览器要登录过),
            # 所以收尾那行提示也从这里取,不写死"请填写 X 桥接"。
            unconfigured_why = unconfigured_why or problem.removeprefix('待配置：')
        elif source['retry_at'] > time.time() and not scope.force:
            summary['deferred'] += 1
        else:
            pending.append(source)
    # Budget-limited runs continue with never/least recently attempted sources.
    pending.sort(key=lambda s: (s['last_attempt'], s['id']))
    _prefetch_x(kind, pending, settings)
    # 预取可能跑十几分钟(见 X_LOCAL_SECONDS),所以 HTTP 那一侧的预算要在它
    # **之后**才开始算。算在前面的话,预取一跑完每条订阅都已经超了预算,整轮
    # 一条也取不走、全部记成"留待下次"—— 而东西明明已经抓在手里了。
    deadline = time.monotonic() + COLLECT_SECONDS
    # 桥接熔断只对桥接通道成立:它防的是"公共实例挂了,别再拿 50 个账号去撞"。
    # 登录态通道的内容已经在预取里抓完,这里只是逐条取走,断开只会白扔掉已经
    # 付过代价的结果;而那两条通道真出问题(登录失效)时每个账号拿到的是同一
    # 句话,熔断省不下任何东西。
    shared_upstream = (x_channels.supports_channels(kind)
                       and x_channels.channel_of(settings) == x_channels.BRIDGE)
    bridge_failures = 0
    # Fetches alone run concurrently; persistence and pipeline state have one writer.
    # Submit only one bounded batch so failures/stop never leave a 51-job backlog.
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix='feed') as pool:
        while pending:
            if _state.get('stop_requested'):
                summary['stopped'] = True
                break
            if deadline - time.monotonic() < SOURCE_SECONDS:
                summary['skipped'] += len(pending)
                summary['message'] = BUDGET_MESSAGE
                break
            if bridge_failures >= BRIDGE_FAILURE_LIMIT:
                independent = [s for s in pending if not reg.uses_bridge(s)]
                summary['skipped'] += len(pending) - len(independent)
                summary['message'] = 'X 桥接连续失败，已暂停本轮其余桥接请求；请检查桥接设置后重试'
                pending = independent
            batch, pending = pending[:FETCH_WORKERS], pending[FETCH_WORKERS:]
            if not batch:
                break
            _stage('crawl', 'running', f"{reg.KINDS[kind]} · 正在读取 {len(batch)} 个来源（已检查 {summary['checked']}/{len(sources)}）")
            futures = {pool.submit(_fetch_with_budget, s, settings, scope, deadline): s for s in batch}
            shared_ok, shared_errors = False, 0
            for future in as_completed(futures):
                source = futures[future]
                try:
                    result = future.result()
                    shared_ok |= shared_upstream and reg.uses_bridge(source)
                    summary['added'] += _save_result(source, result, scope)
                except CollectionBudgetExceeded:
                    summary['skipped'] += 1
                    summary['message'] = BUDGET_MESSAGE
                    continue
                except Exception as e:
                    shared_errors += int(shared_upstream and reg.uses_bridge(source))
                    _save_failure(source, e, summary)
                summary['checked'] += 1
                _stage('crawl', 'running', f"{reg.KINDS[kind]} {summary['checked']}/{len(sources)} · {source['name']}")
            bridge_failures = 0 if shared_ok else bridge_failures + shared_errors
    summary['stopped'] |= bool(_state.get('stop_requested'))
    with reg.db() as c:
        c.execute('INSERT INTO feed_runs(kind,started_at,finished_at,summary) VALUES(?,?,?,?)',
                  (kind,started,reg.now(),json.dumps(summary,ensure_ascii=False)))
        c.execute('DELETE FROM feed_runs WHERE kind=? AND id NOT IN '
                  '(SELECT id FROM feed_runs WHERE kind=? ORDER BY id DESC LIMIT 30)', (kind,kind))
    label = f"检查 {summary['checked']}/{len(sources)} 个来源，新增 {summary['added']} 条"
    if summary['deferred']:
        label += f"，{summary['deferred']} 个等待重试"
    if summary['errors']:
        label += f"，失败 {len(summary['errors'])} 个"
    if summary['unconfigured']:
        label += f"，待配置 {summary['unconfigured']} 个（{unconfigured_why}）"
    if summary['message']:
        label += f"，跳过 {summary['skipped']} 个；{summary['message']}"
    status = 'error' if summary['errors'] else 'skipped' if summary['unconfigured'] else 'ok'
    _stage('crawl', 'stopped' if summary['stopped'] else status,
           label, failures=summary['errors'])
    return summary


# export 自己产出的 frontmatter 字段:每一轮都按 registry 里的当前事实重写。
# 这张表之外的键都是**别人**写进这篇 md 的(提取回写的 tags 是唯一的现役例子),
# 重导出时必须原样留下。
_OWNED_META = ('source', 'title', 'account', 'published', 'url', 'fetched_at',
               'content_kind', 'lang', 'translated_from', 'original_title')


def _kept_meta(path: Path) -> dict:
    """旧文件里那些不属于 export 的 frontmatter 字段。

    重导出是整份重写这篇 md。原来只写 export 自己那几个键,于是提取阶段回写的
    `tags`(topic/entity/flag)被一起抹掉 —— 而索引按 frontmatter 重建 doc_tags、
    `documents.extracted_at` 又还在,提取不会再做一遍:这篇文章从此按话题再也
    搜不到,以后每次同步都是同一个结果(2026-09-30 审查实测:一篇已提取的英文
    旧文补译后,按原话题检索当场从 1 条归零)。

    重导出原本少见(只有源内容变了才发生),翻译把它变成了常规路径:没配 key
    那阵子按英文入库的条目补上 key 之后要重写一遍,老解析器留下的半成品补译
    标题也要重写一遍。保留而不是重做提取,是因为标签口径没变 —— 内容还是同一
    篇,只是换了语言,再买一次提取只是重复付钱。
    """
    try:
        meta, _ = parse_frontmatter(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError):
        return {}                     # 还没导出过(或者读不了),没什么可留的
    return {k: v for k, v in meta.items() if k not in _OWNED_META}


def export(kind: str, hold_untranslated: bool = False,
           translated: dict | None = None):
    # 整段在维护锁里:导出是「选出待导出的行 → 逐篇写 vault」,存量清理是
    # 「删 feeds.db 行 → 删 vault 文件」。不互斥的话清理删掉行、导出随后按
    # 自己那份快照把文件写回去,而 feed_items 里已经没有这一条——vault 里从此
    # 躺着一篇谁也管不着的旧文,索引照样收它,下一次清理也找不到它
    # (见 `kb.maintenance`)。
    with maintenance_lock():
        return _export(kind, hold_untranslated, translated)


def _export(kind: str, hold_untranslated: bool = False,
            translated: dict | None = None):
    # hold_untranslated:还排在翻译队列里的条目这一轮不写 vault。一轮只译
    # MAX_ITEMS 条,不拦的话第 41 条会先以英文落盘,索引/提取/检索/日报都按
    # 英文处理过一遍。翻译整个跳过时(没配 key)不能拦,否则这些条目永远进
    # 不了知识库 —— 那时英文总比没有强。
    #
    # translated:这一轮翻译的成品摘要(`translate.translate_pending` 的返回值)。
    # 导出是这条链上**最后**一个报阶段状态的人,翻译失败必须在这里汇总:只写
    # 进日志的话,这一行 _stage 随后把阶段刷成绿色,而实测那一轮确实有新文章
    # 因为翻译 503 一篇都没进知识库,日报那头照样记「同步完成」
    # (2026-09-30 审查实测)。
    queue = 'FROM feed_items WHERE kind=?'
    base = queue + ' AND exported_hash<>content_hash'
    args: list = [kind]
    held = stuck = shipped = shipped_stuck = 0
    if hold_untranslated:
        cond, extra = translate.queued_sql()
        with reg.db() as c:
            # 数的是**整个翻译队列**,不是下面那个"待导出"集合。按英文导出过、
            # 内容又没变的条目 exported_hash==content_hash,压根不在待导出集合
            # 里,可它们照样排在队列里等补译 —— 拿待导出集合去数,这一档的补译
            # 失败一条也数不出来,导出阶段绿、日报记「同步完成」,而素材清单里
            # 还躺着英文标题(2026-09-30 第 2 轮审查实测)。
            #
            # 两个维度各回答一件事,四档都得分开说:
            #   fresh(exported_hash='') 还没入库,导出闸门确实扣着它
            #   bad(translate_error<>'') 译过、报了错、还可以再试
            # 合成一个数就等于把故障说成积压,或者把"库里还是外文"说成"读不到"。
            # 判据和来源面板那三档是同一个(见 `registry.items`)。
            tally = c.execute(
                f"SELECT exported_hash='' AS fresh, translate_error<>'' AS bad,"
                f' count(*) AS n {queue} AND ({cond}) GROUP BY fresh, bad',
                (*args, *extra)).fetchall()
        for t in tally:
            if t['fresh']:
                held += t['n']
                stuck += t['n'] if t['bad'] else 0
            else:
                shipped += t['n']
                shipped_stuck += t['n'] if t['bad'] else 0
        base += f' AND NOT ({cond})'
        args += extra
    with reg.db() as c:
        rows = c.execute(f'SELECT * {base}', args).fetchall()
    errors, done = [], 0
    root = VAULT_ROOT / 'source' / kind
    root.mkdir(parents=True, exist_ok=True)
    for item in rows:
        if _state.get('stop_requested'):
            break
        path = root / (item['key'] + '.md')
        rel = str(path.relative_to(VAULT_ROOT))
        title, body = translate.display(item)
        # 标题被译了也算"这一篇是译文":只有一行标题的短动态(正文为空)整条
        # 可读内容就是标题,不标的话 vault 里这一篇看不出是译文、也丢掉了
        # 回查原标题的入口。
        # 名字不能叫 translated:那是本函数的形参(这一轮的翻译摘要)。同名的话
        # 循环跑完之后摘要已经变成一个布尔值,收尾处 `.get('errors')` 当场
        # AttributeError —— 而那时成功的文章已经落盘、exported_hash 也更新过,
        # 异常却把 materialize 连同后面的 index/extract 一起带崩:同一轮里译成
        # 的新文章一篇都进不了检索和当天日报(2026-09-30 第 2 轮审查实测)。
        is_zh = body != item['body'] or title != item['title']
        meta = dict(source=kind, title=title, account=item['author'], published=item['published'],
                    url=item['url'], fetched_at=item['fetched_at'], content_kind=item['content_kind'])
        if is_zh:
            meta.update(lang='zh', translated_from='en')
            # 标题没跟着译时不写这一行:原标题和 title 一模一样,写上去只是噪音。
            # 真译了才留 —— 正文已经整篇换成中文,想按英文名找回这条只剩这个入口。
            if title != item['title']:
                meta['original_title'] = item['title']
        # 留在最后:writeback_tags 是往 frontmatter 末尾追加 tag 的,tags 不在
        # 末尾的话新 tag 会落进别的键下面。
        meta.update(_kept_meta(path))
        notice = '\n> 以下为订阅源提供的摘要；完整内容请打开原文。\n' if item['content_kind'] == 'summary' else ''
        if is_zh:
            notice += '\n' + translate.NOTE + '\n'
        text = '---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---\n\n'
        # Keep the parsed text intact for the vault, indexing and extraction.
        # HTML safety belongs to the reader (textContent / DOMPurify).
        text += '# ' + title + '\n' + notice + '\n' + body + '\n'
        temp = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=root, suffix='.tmp', encoding='utf-8', delete=False) as f:
                temp = Path(f.name)
                f.write(text)
            os.replace(temp, path)
            with reg.db() as c:
                c.execute('UPDATE feed_items SET exported_hash=content_hash,path=? WHERE key=?', (rel,item['key']))
            done += 1
        except Exception as e:
            errors.append({'title': item['title'], 'reason': str(e)[:300]})
        finally:
            if temp:
                temp.unlink(missing_ok=True)
    note = f'已保存 {done} 条到知识库'
    if stuck:
        note += f'，{stuck} 条翻译失败（暂未入库）'
    if held - stuck > 0:
        note += f'，{held - stuck} 条待翻译（译完再入库）'
    # 已经按原文入库的那一档:补译失败不影响"读得到",可库里躺的还是外文,
    # 说成「同步完成」照样是谎报 —— 实测那一轮日报的素材清单里就是英文标题。
    # 所以它和 stuck 一样判红,只是文案说清原文还在。
    if shipped_stuck:
        note += f'，{shipped_stuck} 条补译失败（库里仍是原文）'
    if shipped - shipped_stuck > 0:
        note += f'，{shipped - shipped_stuck} 条等待中文译文覆盖'
    # 翻译失败让导出阶段判红:这一轮确实有内容没能按中文进知识库,那不是
    # "稍后再说"。已经试满 MAX_TRIES、按原文入库的那些不算——它们不在扣留
    # 集合里(见 `translate.queued_sql` 的出口),留着原文是设计好的退让。
    bad = stuck + shipped_stuck
    fails = errors + ([] if not bad else (translated or {}).get('errors') or [])
    _stage('export', 'error' if errors or bad else 'ok', note, failures=fails)
