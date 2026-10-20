#!/usr/bin/env python
"""把订阅在历史下界之前的存量条目清出去(feeds.db + vault + kb.db)。

`sync._within_history` 只管**往后**:它拦的是以后抓回来的东西。可 RSS 这几个
订阅是 2026-09-29 同一天建的,那之前已经有几个源把自己的整个存档一次交出来了
——实测 Hugging Face Blog 503 条、最早到 2024-04-10,OpenAI News 506 条、
HN Top 100 Blogs 499 条。这些旧文逐批翻译、逐批重新导出,每重新导出一次
`indexed_at` 就刷新一次,于是连着十几天以「补录」的形态占满当天日报
(2026-10-01 那份 46 条素材里补录占了 30 条)。这个脚本是那一次性的清理。

    scripts/feed_trim_history.py scan                # 只读:每个订阅会清掉多少
    scripts/feed_trim_history.py run --source 60     # 真清(先备份)

另一条筛选口径 `--pending-translation`:清掉某个源**还排在翻译队列里**的条目,
给服务已经停掉的源用 —— 内容不会再更新,译了也没人看,可它们每一轮都占着
`translate.MAX_ITEMS` 的额度,把还活着的源挤到后面去。这一路**不钉
`history_floor`**(筛的维度不是日期)。

    scripts/feed_trim_history.py scan --pending-translation
    scripts/feed_trim_history.py run --source 52 --pending-translation --yes

**清理同时会给这个订阅钉上 `history_floor`**,否则下一次同步按老样子(空下界)
把整个存档原样再拉一遍,这一趟白做。没有旧文可清时同样要钉 —— 见 `cmd_run`。

动手整段持 `kb.maintenance.maintenance_lock()`(同步的存盘/导出和索引的整趟扫描
写库也认这把锁),删除进度落在备份目录的 `progress.json` 里,中途失败重跑会把没做
完的那几步续完 —— **那一趟只续,不会再开新的一轮**(`--yes` 批准的是那份计划,
不是另一种清理;要再清一轮就重新跑一次)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.config import DATA_DIR, VAULT_ROOT          # noqa: E402
from kb.feeds import registry as reg                # noqa: E402
from kb.maintenance import LockBusy, maintenance_lock  # noqa: E402

STATUS_URL = 'http://127.0.0.1:8787/api/pipeline/status'
PHASES = ('feeds', 'vault', 'index')
_FLOOR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def norm_floor(value: str, where: str = '--floor') -> str:
    """校验并规范成 `YYYY-MM-DD`;不合格的直接抛,一个字节都别动。

    下界是拿来和 `published[:10]` 做**字符串**比较的(`below_floor` 与
    `sync._within_history` 都是),而少写一个零之后字典序就不是日期序:
    `2026-10-1` 既大于当天那篇 `2026-10-01 08:00`(本该留着),也大于
    `2026-10-02`(第二天发的,本该收)——实测当天那篇在 feeds/vault/kb 三处
    全被删掉,第二天的新文还被钉死的下界挡在门外(2026-10-01 审查发现)。
    所以非法或非规范的写法在备份和任何删除**之前**就拒掉,不拿它去比较。
    """
    text = (value or '').strip()
    if not _FLOOR_RE.match(text):
        raise ValueError(f'{where} 要写成 YYYY-MM-DD(四位年-两位月-两位日),收到 {value!r}')
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ValueError(f'{where} 不是一个真实的日期:{value!r}') from None
    return text


def planned_floor(source: dict) -> str:
    """这个订阅「本该」有的历史下界 = 创建日 - HISTORY_DAYS。

    已经钉过下界的就用它自己那个:两处给出不同答案的话,清掉的和以后拦掉的
    不是同一批,库里会留下一段谁也说不清的灰色地带。
    """
    if source['history_floor']:
        return norm_floor(source['history_floor'][:10],
                          where=f"订阅 {source['id']} 存着的 history_floor")
    created = datetime.fromisoformat(source['created_at'])
    return reg.history_floor(created)


def below_floor(rows: list[sqlite3.Row], floor: str) -> list[dict]:
    """低于下界的那些。没有发布时间的照留 —— 和 `sync._within_history` 同一个口径。"""
    return [dict(r) for r in rows if r['published'] and r['published'][:10] < floor]


def pending_translation(conn, rows: list[sqlite3.Row]) -> list[dict]:
    """还排在翻译队列里的那些 —— `--pending-translation` 用这个口径。

    判据直接取 `translate.queued_sql()`,不在这里另写一遍:两处各判各的话,
    清掉的和翻译那头还在排队的不是同一批,库里会留下一段谁也说不清的灰色地带
    (`sync._export` 的四档统计、来源面板那三档读的都是那一份)。

    用在服务已经停掉的源上:内容不会再更新,排着的那些译了也没人看,可它们
    每一轮都占着 `MAX_ITEMS` 的额度,把还活着的源挤到后面去。

    **把那段 SQL 真的拿去查,不在 Python 里照着抄一遍。** 原来是只取
    `extra[0]`(重试上限)、再用三个 Row 字段把条件复写一遍 —— 注释里那句
    "直接取"当时就只兑现了一半,而判据一加条件就当场漂:正文补抓那一档
    (`fulltext.queued_sql`,它问的是"这个条目属不属于某条开了补抓的订阅")
    在 Row 上压根没有对应字段,照抄的那一版会把**还在等正文**的条目当成
    "排在翻译队列里"清掉,而它们一次模型调用都还没花过。查一次库就不会有
    下一个这样的缺口。
    """
    from kb.feeds import translate
    cond, extra = translate.queued_sql()
    picked: set[str] = set()
    keys = [r['key'] for r in rows]
    # SQLite 的变量上限(默认 32766)够用,但一个订阅的条目数没有上界,分批稳妥。
    for i in range(0, len(keys), 400):
        batch = keys[i:i + 400]
        ph = ','.join('?' * len(batch))
        picked |= {r[0] for r in conn.execute(
            f'SELECT key FROM feed_items WHERE key IN ({ph}) AND ({cond})', (*batch, *extra))}
    # 按 rows 的顺序返回:调用方拿它去建计划、打备份,顺序稳定才可复现。
    return [dict(r) for r in rows if r['key'] in picked]


def find_source(conn, token: str) -> tuple[dict | None, str]:
    """按 id 或名字定位订阅,返回 `(订阅, 拦下的原因)`。

    **纯数字一律按 id 查,不许退回名字。** 原来是 `id=? OR name=?` 再
    `fetchone()`:一个**名字恰好叫「62」**的订阅会抢在 id=62 前面被取走 ——
    实测 `run --source 62` 清掉的是 61 号那个名叫「62」的订阅,真正点名的 62 号
    一个字节没动,而且是确定性的(2026-10-01 第 2 轮审查实测)。

    **名字命中多条时拒绝执行、要求改用 id。** 注册表允许重名(同名不同链接是
    正当形态),挑哪一条都可能是错的那一条,而这是不可逆删除。
    """
    if token.isdigit():
        row = conn.execute('SELECT * FROM subscriptions WHERE id=?', (int(token),)).fetchone()
        if not row:
            return None, f'找不到 id 为 {token} 的订阅(先跑 scan 看 id)'
        return dict(row), ''
    rows = conn.execute('SELECT * FROM subscriptions WHERE name=? ORDER BY id',
                        (token,)).fetchall()
    if not rows:
        return None, f'找不到叫「{token}」的订阅(先跑 scan 看 id)'
    if len(rows) > 1:
        ids = '、'.join(str(r['id']) for r in rows)
        return None, (f'有 {len(rows)} 个订阅都叫「{token}」(id {ids});'
                      f'这是不可逆删除,请改用 id 点名。')
    return dict(rows[0]), ''


def _items(conn, source_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        'SELECT fi.* FROM feed_items fi JOIN subscription_items si ON si.item_key=fi.key '
        'WHERE si.subscription_id=? ORDER BY fi.published', (source_id,)))


def _shared_keys(conn, keys: list[str], source_id: int) -> set[str]:
    """这些条目里还被别的订阅收录着的那些(聚合源和单号源常常收同一篇)。

    它们只摘掉本订阅那条归属:`feed_items` 行、vault 文件、kb.db 索引全留着,
    否则另一边仍然列着这篇文章,点进去却读不到、搜不到,而 `exported_hash`
    还等于 `content_hash` —— 下一次导出直接跳过,再同步也不恢复
    (2026-10-01 审查实测)。
    """
    if not keys:
        return set()
    ph = ','.join('?' * len(keys))
    return {r['item_key'] for r in conn.execute(
        f'SELECT DISTINCT item_key FROM subscription_items '
        f'WHERE item_key IN ({ph}) AND subscription_id<>?', [*keys, source_id])}


def _live_paths(paths: list[str]) -> set[str]:
    """这些路径里还有 `feed_items` 行指着的那些 —— 文件和索引不能动。"""
    if not paths:
        return set()
    with reg.db() as c:
        ph = ','.join('?' * len(paths))
        return {r['path'] for r in c.execute(
            f'SELECT DISTINCT path FROM feed_items WHERE path IN ({ph})', paths)}


class FavoritesAppeared(Exception):
    """动手时发现收藏(多半是这一趟开跑之后才加的),这一轮不删。"""

    def __init__(self, paths: list[str]):
        super().__init__(f'{len(paths)} 篇在收藏里')
        self.paths = paths


def _favorites(conn, paths: list[str]) -> list[str]:
    """这些路径里被收藏的那些。**用调用方那条连接查**,不自己开一条。

    真正的闸门是 `_apply` 里那次 —— 它跑在同一条连接的 `BEGIN IMMEDIATE`
    里,所以「查完」到「删完」之间没有缝。自己开一条连接就做不到这件事:
    查完即关,收藏 API 照样能在备份/删除期间提交成功(见 `_apply`)。
    """
    if not paths:
        return []
    ph = ','.join('?' * len(paths))
    # DISTINCT:一篇可以有整篇 + 若干片段好几行,消息和计数说的都是「几篇」。
    return [r['path'] for r in conn.execute(
        f'SELECT DISTINCT path FROM favorites WHERE path IN ({ph}) ORDER BY path', paths)]


def _favorites_rows(conn, paths: list[str]) -> list[dict]:
    ph = ','.join('?' * len(paths))
    return [dict(r) for r in conn.execute(
        f'SELECT * FROM favorites WHERE path IN ({ph}) ORDER BY id', paths)]


def _pipeline_busy() -> str:
    """拦下这一趟的原因;`''` = 可以动手。

    管道跑在 kb-web 进程内,它的运行状态只有那个进程知道,所以问 HTTP。
    **连接被拒 = 服务没起 = 没有管道在跑**,放行;别的毛病(超时、HTTP 错、
    回包不是 JSON)证明不了"没人在跑",一律拦下——这是不可逆删除,问不到就
    别猜。真正的互斥靠 `kb.maintenance.maintenance_lock()`(index 阶段也持它),
    这里只是开工前的早一道:管道正忙时当场说清楚,而不是让人对着一个一声不响
    等满 `KB_MAINT_LOCK_WAIT` 的终端等下去。
    """
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=5) as r:
            if json.load(r).get('running'):
                return '管道正在跑,先等它跑完 —— index 阶段也在写 kb.db。'
            return ''
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            return ''
        return f'问不到管道状态({e.reason});确认 kb-web 空闲后再跑。'
    except (OSError, ValueError, TimeoutError) as e:
        return f'问不到管道状态({type(e).__name__}: {e});确认 kb-web 空闲后再跑。'


# ---- 续跑日记 ----------------------------------------------------------------
# feeds.db 那一步是先提交的,后面 unlink 或者 kb.db 清理抛异常时,重跑按 feeds
# 查出来的待清集合已经空了 —— 命令打印「无事可做」并以 0 退出,而 md 文件、
# documents 行和 FTS 命中都还在,没有任何一条路把这趟清理做完(2026-10-01 审查
# 实测两种中断各一次)。所以把待删路径和已完成的阶段写在备份目录里。

def _plan_path(out: Path) -> Path:
    return out / 'progress.json'


def _mark(out: Path, plan: dict, phase: str) -> None:
    plan[phase] = True
    p = _plan_path(out)
    fd, tmp = tempfile.mkstemp(dir=out, suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _plans(source_id: int | None = None) -> list[tuple[Path, dict]]:
    """没做完的清理(新到旧)。"""
    out = []
    for p in sorted((DATA_DIR / 'backups').glob('feedtrim-*/progress.json')):
        try:
            plan = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if plan.get('done') or not isinstance(plan.get('paths'), list):
            continue
        if source_id is None or plan.get('subscription_id') == source_id:
            out.append((p.parent, plan))
    return out


def _cut_feeds(plan: dict) -> None:
    keys, source_id = plan['keys'], plan['subscription_id']
    with reg.db() as c:
        c.execute('BEGIN IMMEDIATE')
        # 下界只属于"清历史存量"那条路。`--pending-translation` 筛的维度压根
        # 不是日期,顺手钉一个下界就是做了没被要求的事 —— 这个源将来真要重新
        # 启用时,那条下界会把它自己的存档永久挡在门外。老计划没有 mode 字段,
        # 照旧走钉下界那一路。
        if plan.get('mode') != 'pending-translation':
            c.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                      (plan['floor'], source_id))
        if keys:
            ph = ','.join('?' * len(keys))
            c.execute(f'DELETE FROM subscription_items WHERE item_key IN ({ph}) '
                      f'AND subscription_id=?', [*keys, source_id])
            # 别的订阅还收着的那些留在 feed_items 里,只摘掉本订阅那条归属。
            c.execute(f'DELETE FROM feed_items WHERE key IN ({ph}) AND key NOT IN '
                      f'(SELECT item_key FROM subscription_items)', keys)


def _drop_index(conn, paths: list[str]) -> int:
    """删 `documents` 行及 chunk/tag/entity/向量,收藏只解除文档关联。

    连接由 `_apply` 给 —— 收藏复查和这一步必须是**同一条**连接上的同一个写
    事务。另开一条连接去删就是跟自己抢写锁,当场自锁。
    """
    from kb import store
    return store.remove_documents(conn, paths)


def _apply(out: Path, plan: dict, allow_favorites: bool = False) -> None:
    """按 feeds.db → vault → kb.db 往下做;每一步幂等,做完就记一笔。

    顺序要紧:先钉下界(不钉的话下一次同步把存档原样再拉一遍),再删 feeds.db
    的行 —— 它是「要不要重新导出」的事实来源,先删它才不会留下「文件没了、
    exported_hash 还在」那种既不重建也不存在的半状态。

    **删文件和删索引整段跑在 kb.db 的一个 `BEGIN IMMEDIATE` 里,收藏的最终
    复查也在同一条连接的同一个事务里做。** `--allow-favorites` 只授权清理
    被收藏的原文,不授权删除收藏:外键将 path 置空,引文、备注、行动卡继续保留。
    默认仍拦截被收藏的原文,避免用户失去本地阅读入口;显式放行时另外备份收藏。
    """
    if not plan.get('feeds'):
        _cut_feeds(plan)
        _mark(out, plan, 'feeds')
        # 新模式不钉下界,就别在这里报一个没发生的改动。
        print('  feeds.db 已清' + ('' if plan.get('mode') == 'pending-translation'
                                  else f",history_floor={plan['floor']}"))
    # 还有 feed_items 行指着的路径一律跳过:计划是动手之前算的,这一道是兜底。
    dead = [p for p in plan['paths'] if p not in _live_paths(plan['paths'])]
    from kb import store
    conn = store.connect()
    try:
        conn.execute('BEGIN IMMEDIATE')          # 这一刻起收藏写不进来
        if (fav := _favorites(conn, dead)) and not allow_favorites:
            conn.rollback()
            raise FavoritesAppeared(fav)
        if fav:
            # 显式放行时也把会解除关联的那几行(备注、片段锚、状态、行动卡)存进
            # 备份 —— 它们不在 vault 里,md 压缩包重建不出来。
            (out / 'favorites.json').write_text(
                json.dumps(_favorites_rows(conn, dead), ensure_ascii=False, indent=1),
                encoding='utf-8')
        if not plan.get('vault'):
            gone = 0
            for p in dead:
                fp = VAULT_ROOT / p
                if fp.exists():
                    fp.unlink()
                    gone += 1
            _mark(out, plan, 'vault')
            print(f'  vault 删除 {gone} 个 md')
        if not plan.get('index'):
            dropped = _drop_index(conn, dead)
            conn.commit()
            _mark(out, plan, 'index')
            print(f'  kb.db 删除 {dropped} 篇文档(级联 chunk/tag/entity 与向量)')
        else:
            conn.commit()
    finally:
        conn.close()
    _mark(out, plan, 'done')


def _backup(source: dict, cut: list[dict], paths: list[str], floor: str) -> Path:
    """备份 + 这一趟的续跑记录,目录名带 id 且**原子地占一个唯一名字**。

    原来是「slug + 秒」加 `exist_ok=True`:两个同名订阅在同一秒里清理会复用
    同一个目录,manifest / feed_items.json / md 压缩包 / `progress.json` 全被
    后一个覆盖 —— 前一个清掉的那些内容从此既不在库里也不在备份里,它那份没做
    完的计划也一起没了,重跑只会说「无事可做」(2026-10-01 第 2 轮审查实测)。
    slug 截断和非字母数字归一化还会让**不同**名字撞到一起,所以唯一性不能靠
    名字:`mkdtemp` 由内核保证这个目录是我们新建的。
    """
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    slug = ''.join(ch if ch.isalnum() else '-' for ch in source['name'])[:30].strip('-')
    root = DATA_DIR / 'backups'
    root.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=f'feedtrim-{source["id"]}-{slug}-{stamp}-', dir=root))
    (out / 'feed_items.json').write_text(json.dumps(cut, ensure_ascii=False, indent=1))
    (out / 'manifest.json').write_text(json.dumps(
        {'subscription_id': source['id'], 'name': source['name'], 'floor': floor,
         'keys': [r['key'] for r in cut], 'paths': paths}, ensure_ascii=False, indent=1))
    with tarfile.open(out / 'vault-md.tar.gz', 'w:gz') as t:
        for p in paths:
            if (VAULT_ROOT / p).exists():
                t.add(VAULT_ROOT / p, arcname=p)
    return out


def cmd_scan(args) -> int:
    with reg.db() as c:
        subs = [dict(r) for r in c.execute('SELECT * FROM subscriptions ORDER BY kind, id')]
        for s in subs:
            rows = _items(c, s['id'])
            if not rows:
                continue
            if args.pending_translation:
                cut = pending_translation(c, rows)
                print(f"[{s['id']:>3}] {s['name'][:28]:30} {len(rows):>4} 条 "
                      f"最早 {rows[0]['published'][:10] or '—'}  "
                      f"还排在翻译队列里 {len(cut)}")
            else:
                floor = planned_floor(s)
                cut = below_floor(rows, floor)
                mark = '' if s['history_floor'] else '  (还没钉下界)'
                print(f"[{s['id']:>3}] {s['name'][:28]:30} {len(rows):>4} 条 "
                      f"最早 {rows[0]['published'][:10] or '—'}  下界 {floor}  "
                      f"可清 {len(cut)}{mark}")
            if cut and args.verbose:
                shared = len(_shared_keys(c, [r['key'] for r in cut], s['id']))
                print(f"       待清区间 {cut[0]['published'][:16]} ~ {cut[-1]['published'][:16]}"
                      f"；与别的订阅共享 {shared} 条(只摘归属,文件和索引留着)")
    for out, plan in _plans():
        done = '/'.join(p for p in PHASES if plan.get(p)) or '无'
        # 续跑按计划里记着的 keys/paths 走,**不看**命令行上的筛选开关,所以这条
        # 提示不带 `--pending-translation`/`--floor` 是对的。它原来也不带,可那时
        # 候续完之后还会接着按这次的口径再开一轮 —— 照提示恢复就等于换个口径再
        # 删一遍(见 `_run_locked`)。口径这一栏是给人看的:这份没做完的计划当初
        # 是按什么筛出来的。
        scope = ('翻译队列' if plan.get('mode') == 'pending-translation'
                 else f"下界 {plan.get('floor') or '—'}")
        print(f"!! 有一份没做完的清理:{out}(订阅 {plan['subscription_id']},"
              f"口径 {scope},已完成 {done}) —— 跑 "
              f"run --source {plan['subscription_id']} --yes 续完它"
              f"(只续这一份,不会再开新的一轮)")
    return 0


def cmd_run(args) -> int:
    # **这道检查必须排在所有副作用之前,包括续跑那一步。** 它原来在
    # `_run_locked` 里、排在「续上没做完的那一份」后面:同一个订阅存在一份
    # 已经备份、还没删完的计划时(备份落盘之后进程被杀或 I/O 异常,是脚本
    # 明确支持的恢复场景),带两个开关重跑会先把那份计划**续完** —— feeds
    # 行、vault md、`documents`、全文索引和 `history_floor` 全动了 —— 然后
    # 才打印「不能一起用」并退 2。于是「两个开关同时给时一个字节都不动」在
    # 这条路上压根不成立,用户看到的是一句拒绝加一篇读不到了的文章
    # (2026-10-08 审查实测:退出码 2,而 feed 行/文件/documents/检索四处
    # 同时归零)。原来那条回归只摆了「没有未完成计划」的局,所以全绿骗了
    # 过去 —— 判据是**有旧计划时也不许动**,不是「提示对不对」。
    # 取锁也算副作用:非法参数不该先去等满 `KB_MAINT_LOCK_WAIT`。
    #
    # **「给了 --floor 没有」要问 `is not None`,不许问真值。** argparse 本来
    # 就把两件事分得清:没写这个开关是 `None`,写了 `--floor ""` 是空字符串。
    # 拿真值判的话空字符串和「压根没给」长得一模一样 —— 而这两条判断都在这
    # 上面:①互斥检查放行,于是 `--floor "" --yes --pending-translation` 这条
    # 本该一个字节都不动的命令,把那份没做完的**日期**计划续完了(文章四处
    # 归零、下界被钉上、退 0);②`norm_floor` 压根没被调用,于是**没有**未完成
    # 计划时单给 `--floor "" --yes` 也不报错,而是悄悄按默认下界新建一轮清理
    # 把旧文删掉(2026-10-08 第 3 轮审查用真 argparse + 真子进程复现,前者独立
    # 三次一致)。空字符串不是一种顺手的写法,它是 `--floor "$CUTOFF"` 而
    # `CUTOFF` 还没赋值这种最常见的 shell 事故 —— 而这是不可逆删除。
    # 显式给了就必须过 `norm_floor`(`''` 不是 `YYYY-MM-DD`,它自己会拒),
    # 显式给了就算「给了日期」(于是和 `--pending-translation` 互斥)。
    if args.pending_translation and args.floor is not None:
        print('--pending-translation 筛的是翻译队列,不是日期;这两个开关不能一起用。',
              file=sys.stderr)
        return 2
    if blocked := _pipeline_busy():
        print(blocked, file=sys.stderr)
        return 2
    try:
        override = norm_floor(args.floor) if args.floor is not None else ''
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    # 动手整段在维护锁里:同步那一侧的存盘和导出、索引那一侧的整趟扫描写库都
    # 认这把锁。不然一轮在备份期间起跑的同步会把刚删掉的存档按旧快照原样写回来,
    # 一轮并发的索引会把刚删掉的 documents 行按删除之前读到的内容写回来
    # (见 `kb.maintenance`)。
    try:
        with maintenance_lock(on_wait=_lock_wait):
            return _run_locked(args, override)
    except LockBusy as e:
        print(e, file=sys.stderr)
        return 2


def _lock_wait(waited: float) -> None:
    print(f'等维护锁:另一个 kb.db 维护任务(索引或另一趟清理)正在跑'
          f'(已等 {int(waited)} 秒)…', file=sys.stderr, flush=True)


def _run_locked(args, override: str) -> int:
    with reg.db() as c:
        source, why = find_source(c, args.source)
    if not source:
        print(why, file=sys.stderr)
        return 2
    left = _plans(source['id'])
    if left and not args.yes:
        for out, plan in left:
            done = '/'.join(p for p in PHASES if plan.get(p)) or '无'
            print(f'有一份没做完的清理:{out}(已完成 {done});加 --yes 续完它。',
                  file=sys.stderr)
        return 1
    if left:
        # **有没做完的计划时,这一趟只续它,续完就结束。** 原来续完之后还会接着
        # 按**这次命令行**上的筛选开关新建第二份计划再删一轮,而 `cmd_scan` 给出
        # 的恢复提示是 `run --source N --yes`(不带任何筛选开关)—— 于是「照工具
        # 自己的提示恢复一份 `--pending-translation` 的计划」= 续完该续的那一份,
        # 紧接着按**默认的日期口径**又清一轮:已经译好、还读得到搜得到的旧文章
        # 连 feeds 行、md 文件、`documents` 和全文索引一起没了,`history_floor`
        # 也被钉上,而命令以 0 退出(2026-10-08 第 2 轮审查实测,独立复现三次;
        # 恢复时手动补上 `--pending-translation` 则只删该删的那一篇 —— 可那个
        # 选项压根不在提示里)。日期口径自己撞不上这件事(续跑已经把 floor 钉成
        # 同一个值,第二份计划筛出来是空的),它是新口径**不钉下界**带出来的。
        # 判据是**授权的范围**:`--yes` 批准的是「把那份已经备份、删了一半的计划
        # 做完」,不是「再启动另一种清理」——一次调用只做一件事,要再清一轮就重新
        # 跑一次(那时没有未完成计划,走的是正常的确认流程)。这样续跑压根不需要
        # 「继承口径」或者「比对口径」:计划自带它要删的 keys 和 paths,`_apply`
        # 认计划里的 `mode`,筛选开关只用来**建**新计划。
        for out, plan in left:
            # 续跑**不是**「接着执行上次批准的那件事」:收藏那道护栏排在新计划
            # 的后面,续跑从旁边绕过去了 —— 失败之后用户把那篇还读得到的文章
            # 收藏起来,重跑(没加 --allow-favorites)照样把文件、documents 和
            # 收藏一起删掉,退出码还是 0(2026-10-01 第 2 轮审查实测)。所以按
            # **此刻**真要删的那几篇再查一遍。
            print(f'续上没做完的那一份:{out}')
            try:
                _apply(out, plan, args.allow_favorites)
            except FavoritesAppeared as e:
                print(f'这份没做完的清理({out})里有 {len(e.paths)} 篇在收藏里:\n  '
                      + '\n  '.join(e.paths[:10])
                      + '\n收藏是用户显式留下的(可能是这次失败之后才加的),'
                        '清理原文需加 --allow-favorites，收藏和备注仍保留。计划留着,改完参数能接着续。',
                      file=sys.stderr)
                return 2
        print('续完了。这一趟只做这一件事 —— 要再清一轮(不管按哪个口径)请重新跑一次。')
        return 0
    # 互斥检查在 `cmd_run` 里、续跑之前就做完了(理由见那里)。
    mode = 'pending-translation' if args.pending_translation else ''
    try:
        # 新模式下这一趟跟下界无关,压根不算它:算出来只会出现在打印和计划里,
        # 让人以为钉了一个。真正"不动它"那一下在 `_cut_feeds` 里。
        floor = '' if mode else (override or planned_floor(source))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    with reg.db() as c:
        rows = _items(c, source['id'])
        cut = pending_translation(c, rows) if mode else below_floor(rows, floor)
        keys = [r['key'] for r in cut]
        shared = _shared_keys(c, keys, source['id'])
    if not cut and mode:
        print(f"[{source['id']}] {source['name']}:翻译队列里没有这个源的条目,无事可做。")
        return 0
    if not cut:
        if source['history_floor'][:10] == floor:
            print(f"[{source['id']}] {source['name']}:下界 {floor} 之前没有条目,无事可做。")
            return 0
        # 空集合不等于"不用钉下界":老订阅(空下界)眼下恰好没有旧文,源站
        # 下次补发 7 月的东西照样整批收 —— 提前返回的话这一趟什么都没改变,
        # 而命令以 0 退出,看着像做完了(2026-10-01 审查实测)。
        print(f"[{source['id']}] {source['name']}:下界 {floor} 之前没有条目;"
              f"把 history_floor 从「{source['history_floor'] or '空'}」钉成 {floor}。")
        if not args.yes:
            print('确认请加 --yes。')
            return 1
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                      (floor, source['id']))
        print(f'  history_floor={floor}')
        return 0
    # 共享条目只摘归属,所以它们的文件和索引不进待删清单。
    paths = [r['path'] for r in cut if r['path'] and r['key'] not in shared]
    if shared and not args.allow_shared:
        print(f'{len(shared)} 条还被别的订阅收录着:这边只摘掉归属,文件和索引'
              f'留给那边。确认请加 --allow-shared。', file=sys.stderr)
        return 2
    # 早一道:动手之前就把话说清楚(省掉建备份目录)。真闸门在 `_apply` 的
    # 写事务里 —— 这里查完到那里删完之间还有确认、备份、unlink 一整段。
    from kb import store as _store
    _c = _store.connect()
    try:
        fav = _favorites(_c, paths)
    finally:
        _c.close()
    if fav and not args.allow_favorites:
        print(f'这批里有 {len(fav)} 篇在收藏里:\n  ' + '\n  '.join(fav[:10])
              + '\n清理这些原文需加 --allow-favorites，收藏和备注仍保留。', file=sys.stderr)
        return 2
    scope = ('还排在翻译队列里的 ' + str(len(cut)) + ' 条') if mode \
        else f'下界 {floor},清掉 {len(cut)} 条'
    print(f"[{source['id']}] {source['name']}:{scope}"
          f"({cut[0]['published'][:10]} ~ {cut[-1]['published'][:10]});"
          f"其中 {len(shared)} 条被共享、只摘归属,{len(paths)} 篇连文件和索引一起删")
    if not args.yes:
        print('这一步不可逆(有备份)。确认请加 --yes。')
        return 1

    out = _backup(source, cut, paths, floor)
    plan = {'subscription_id': source['id'], 'name': source['name'], 'floor': floor,
            'keys': keys, 'paths': paths, 'mode': mode}
    _mark(out, plan, 'planned')
    print(f'  备份 → {out}')
    try:
        _apply(out, plan, args.allow_favorites)
    except FavoritesAppeared as e:
        print(f'这一趟开跑之后有 {len(e.paths)} 篇被收藏了:\n  '
              + '\n  '.join(e.paths[:10])
              + '\n这一轮保留被收藏的原文，收藏和备注不会删除。'
                f'计划留在 {out},清理原文需加 --allow-favorites 续跑。',
              file=sys.stderr)
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('scan', help='只读:每个订阅在下界之前压着多少条')
    s.add_argument('-v', '--verbose', action='store_true')
    s.add_argument('--pending-translation', action='store_true',
                   help='改为按「还排在翻译队列里」统计')
    s.set_defaults(func=cmd_scan)
    r = sub.add_parser('run', help='清理一个订阅(先备份);有没做完的那一趟只续完它')
    r.add_argument('--source', required=True, help='订阅 id 或名字')
    r.add_argument('--floor', help='覆盖下界(YYYY-MM-DD),缺省按「创建日 - HISTORY_DAYS」')
    r.add_argument('--pending-translation', action='store_true',
                   help='改为清掉「还排在翻译队列里」的条目(源已停服时用),不动 history_floor')
    r.add_argument('--yes', action='store_true', help='确认执行')
    r.add_argument('--allow-shared', action='store_true', help='条目被别的订阅共享时照样摘归属')
    r.add_argument('--allow-favorites', action='store_true', help='允许清理被收藏的原文，收藏和备注仍保留')
    r.set_defaults(func=cmd_run)
    args = ap.parse_args()
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
