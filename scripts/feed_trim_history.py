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

**清理同时会给这个订阅钉上 `history_floor`**,否则下一次同步按老样子(空下界)
把整个存档原样再拉一遍,这一趟白做。没有旧文可清时同样要钉 —— 见 `cmd_run`。

动手整段持 `reg.trim_lock()`(同步那一侧也认这把锁),删除进度落在备份目录的
`progress.json` 里,中途失败重跑会先把没做完的那几步续完。
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
    别猜。真正的互斥靠 `reg.trim_lock()`,这里只是开工前的早一道。

    **已知缺口**:`reg.trim_lock()` 只有订阅存盘(`sync._save_result`)和导出
    (`sync.export`)认,index 阶段(`pipeline_stages._run_index` / `cli.cmd_index`)
    不认。清理开跑之后才起的「只跑 index」那种同步,可以在我们删文件之前把这篇
    读进内存、在删完之后 upsert 回 kb.db,留下一行指向不存在文件的 `documents`
    (搜得到、点开没有原文)。它会被**下一次** index 自己收掉
    (`remove_missing_documents` 按磁盘上还在的那些算),所以不是永久损坏;
    真要堵死得让所有 kb.db 写入方共用这把锁,那是另一件事。
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
    """删 `documents` 行(级联 chunk/tag/entity/收藏与向量)。

    连接由 `_apply` 给 —— 收藏复查和这一步必须是**同一条**连接上的同一个写
    事务。另开一条连接去删就是跟自己抢写锁,当场自锁。
    """
    from kb import store
    alive = {r['path'] for r in conn.execute('SELECT path FROM documents')} - set(paths)
    return store.remove_missing_documents(conn, alive)


def _apply(out: Path, plan: dict, allow_favorites: bool = False) -> None:
    """按 feeds.db → vault → kb.db 往下做;每一步幂等,做完就记一笔。

    顺序要紧:先钉下界(不钉的话下一次同步把存档原样再拉一遍),再删 feeds.db
    的行 —— 它是「要不要重新导出」的事实来源,先删它才不会留下「文件没了、
    exported_hash 还在」那种既不重建也不存在的半状态。

    **删文件和删索引整段跑在 kb.db 的一个 `BEGIN IMMEDIATE` 里,收藏的最终
    复查也在同一条连接的同一个事务里做。** 收藏护栏原来是「查完就关连接」,
    从那一刻到真正删除之间有备份打包、进度落盘、逐个 unlink 这么长一段 ——
    用户在这段时间里收藏一篇还读得到的文章,收藏 API 返回 200、库里真有那
    一行,随后 `remove_missing_documents` 删掉 `documents` 行、`favorites`
    的 `ON DELETE CASCADE` 把它连同用户刚写的备注一起带走,命令还以 0 退出
    (2026-10-01 第 3 轮审查实测,整篇和带备注的片段各复现三次)。索引能从
    vault 重建,**收藏和备注不能** —— 备份里压根没有它们。
    `BEGIN IMMEDIATE` 当场拿到 kb.db 的写锁,于是这期间的收藏写入只有两种
    结局:赶在我们取锁之前提交成功(那它就会被这次复查看见、把这一轮拦下),
    或者等/报错(`store.connect` 的 `timeout=30`)—— 不会再有「成功了又被
    静默删掉」这一档。清理锁 `reg.trim_lock()` 管不了这件事:收藏 API 不认
    它,只有 kb.db 自己的写锁挡得住。
    """
    if not plan.get('feeds'):
        _cut_feeds(plan)
        _mark(out, plan, 'feeds')
        print(f"  feeds.db 已清,history_floor={plan['floor']}")
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
            # 显式放行时也把要毁掉的那几行(备注、片段锚、状态、行动卡)存进
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
        print(f"!! 有一份没做完的清理:{out}(订阅 {plan['subscription_id']},已完成 {done})"
              f" —— 跑 run --source {plan['subscription_id']} --yes 续完它")
    return 0


def cmd_run(args) -> int:
    if blocked := _pipeline_busy():
        print(blocked, file=sys.stderr)
        return 2
    try:
        override = norm_floor(args.floor) if args.floor else ''
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    # 动手整段在清理锁里:同步那一侧的存盘和导出也认这把锁,不然一轮在备份
    # 期间起跑的同步会把刚删掉的存档按旧快照原样写回来(见 `reg.trim_lock`)。
    with reg.trim_lock():
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
                        '确认要删加 --allow-favorites。计划留着,改完参数能接着续。',
                      file=sys.stderr)
                return 2
        if left:
            with reg.db() as c:
                source = dict(c.execute('SELECT * FROM subscriptions WHERE id=?',
                                        (source['id'],)).fetchone())
        try:
            floor = override or planned_floor(source)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        with reg.db() as c:
            cut = below_floor(_items(c, source['id']), floor)
            keys = [r['key'] for r in cut]
            shared = _shared_keys(c, keys, source['id'])
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
                  + '\n收藏是用户显式留下的,确认要删加 --allow-favorites。', file=sys.stderr)
            return 2
        print(f"[{source['id']}] {source['name']}:下界 {floor},清掉 {len(cut)} 条"
              f"({cut[0]['published'][:10]} ~ {cut[-1]['published'][:10]});"
              f"其中 {len(shared)} 条被共享、只摘归属,{len(paths)} 篇连文件和索引一起删")
        if not args.yes:
            print('这一步不可逆(有备份)。确认请加 --yes。')
            return 1
        # index 阶段不认这把锁(见 `_pipeline_busy` 那条已知缺口)。
        print('  清理期间别起同步 —— 尤其是「只跑 index」那种,它不认清理锁。')

        out = _backup(source, cut, paths, floor)
        plan = {'subscription_id': source['id'], 'name': source['name'], 'floor': floor,
                'keys': keys, 'paths': paths}
        _mark(out, plan, 'planned')
        print(f'  备份 → {out}')
        try:
            _apply(out, plan, args.allow_favorites)
        except FavoritesAppeared as e:
            print(f'这一趟开跑之后有 {len(e.paths)} 篇被收藏了:\n  '
                  + '\n  '.join(e.paths[:10])
                  + '\n收藏连备注一起删掉就找不回来了,所以这一轮不删。'
                    f'计划留在 {out},确认要删加 --allow-favorites 续跑。',
                  file=sys.stderr)
            return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('scan', help='只读:每个订阅在下界之前压着多少条')
    s.add_argument('-v', '--verbose', action='store_true')
    s.set_defaults(func=cmd_scan)
    r = sub.add_parser('run', help='清理一个订阅(先备份);有没做完的先续完')
    r.add_argument('--source', required=True, help='订阅 id 或名字')
    r.add_argument('--floor', help='覆盖下界(YYYY-MM-DD),缺省按「创建日 - HISTORY_DAYS」')
    r.add_argument('--yes', action='store_true', help='确认执行')
    r.add_argument('--allow-shared', action='store_true', help='条目被别的订阅共享时照样摘归属')
    r.add_argument('--allow-favorites', action='store_true', help='命中收藏时照删')
    r.set_defaults(func=cmd_run)
    args = ap.parse_args()
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
