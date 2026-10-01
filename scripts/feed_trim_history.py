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
把整个存档原样再拉一遍,这一趟白做。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tarfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.config import DATA_DIR, VAULT_ROOT          # noqa: E402
from kb.feeds import registry as reg                # noqa: E402

STATUS_URL = 'http://127.0.0.1:8787/api/pipeline/status'


def planned_floor(source: dict) -> str:
    """这个订阅「本该」有的历史下界 = 创建日 - HISTORY_DAYS。

    已经钉过下界的就用它自己那个:两处给出不同答案的话,清掉的和以后拦掉的
    不是同一批,库里会留下一段谁也说不清的灰色地带。
    """
    if source['history_floor']:
        return source['history_floor'][:10]
    created = datetime.fromisoformat(source['created_at'])
    return reg.history_floor(created)


def below_floor(rows: list[sqlite3.Row], floor: str) -> list[dict]:
    """低于下界的那些。没有发布时间的照留 —— 和 `sync._within_history` 同一个口径。"""
    return [dict(r) for r in rows if r['published'] and r['published'][:10] < floor]


def _items(conn, source_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        'SELECT fi.* FROM feed_items fi JOIN subscription_items si ON si.item_key=fi.key '
        'WHERE si.subscription_id=? ORDER BY fi.published', (source_id,)))


def _shared(conn, keys: list[str], source_id: int) -> int:
    """这些条目里有几条还被别的订阅收录着(聚合源和单号源常常重叠)。"""
    ph = ','.join('?' * len(keys))
    return conn.execute(f'SELECT count(DISTINCT item_key) FROM subscription_items '
                        f'WHERE item_key IN ({ph}) AND subscription_id<>?',
                        [*keys, source_id]).fetchone()[0]


def _favorites(paths: list[str]) -> list[str]:
    from kb import store
    conn = store.connect()
    try:
        ph = ','.join('?' * len(paths))
        return [r['path'] for r in conn.execute(
            f'SELECT path FROM favorites WHERE path IN ({ph})', paths)]
    finally:
        conn.close()


def _pipeline_busy() -> bool:
    """管道跑在 kb-web 进程内,它的运行状态只有那个进程知道,所以问 HTTP。

    连不上 = 服务没起 = 没有管道在跑,放行。
    """
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=5) as r:
            return bool(json.load(r).get('running'))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


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
                print(f"       待清区间 {cut[0]['published'][:16]} ~ {cut[-1]['published'][:16]}"
                      f"；与别的订阅共享 {_shared(c, [r['key'] for r in cut], s['id'])} 条")
    return 0


def _backup(source: dict, cut: list[dict], paths: list[str], floor: str) -> Path:
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    slug = ''.join(ch if ch.isalnum() else '-' for ch in source['name'])[:30].strip('-')
    out = DATA_DIR / 'backups' / f'feedtrim-{slug}-{stamp}'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'feed_items.json').write_text(json.dumps(cut, ensure_ascii=False, indent=1))
    (out / 'manifest.json').write_text(json.dumps(
        {'subscription_id': source['id'], 'name': source['name'], 'floor': floor,
         'keys': [r['key'] for r in cut], 'paths': paths}, ensure_ascii=False, indent=1))
    with tarfile.open(out / 'vault-md.tar.gz', 'w:gz') as t:
        for p in paths:
            if (VAULT_ROOT / p).exists():
                t.add(VAULT_ROOT / p, arcname=p)
    return out


def cmd_run(args) -> int:
    if _pipeline_busy():
        print('管道正在跑,先等它跑完 —— index 阶段也在写 kb.db。', file=sys.stderr)
        return 2
    with reg.db() as c:
        row = c.execute('SELECT * FROM subscriptions WHERE id=? OR name=?',
                        (args.source if args.source.isdigit() else -1, args.source)).fetchone()
        if not row:
            print(f'找不到订阅 {args.source}(先跑 scan 看 id)', file=sys.stderr)
            return 2
        source = dict(row)
        floor = args.floor or planned_floor(source)
        cut = below_floor(_items(c, source['id']), floor)
        keys = [r['key'] for r in cut]
        shared = _shared(c, keys, source['id']) if keys else 0
    if not cut:
        print(f"[{source['id']}] {source['name']}:下界 {floor} 之前没有条目,无事可做。")
        return 0
    paths = [r['path'] for r in cut if r['path']]
    if shared and not args.allow_shared:
        print(f'{shared} 条还被别的订阅收录着,删掉会把那边一起挖空;'
              f'确认要删加 --allow-shared。', file=sys.stderr)
        return 2
    if (fav := _favorites(paths)) and not args.allow_favorites:
        print(f'这批里有 {len(fav)} 篇在收藏里:\n  ' + '\n  '.join(fav[:10])
              + '\n收藏是用户显式留下的,确认要删加 --allow-favorites。', file=sys.stderr)
        return 2
    print(f"[{source['id']}] {source['name']}:下界 {floor},清掉 {len(cut)} 条"
          f"({cut[0]['published'][:10]} ~ {cut[-1]['published'][:10]})")
    if not args.yes:
        print('这一步不可逆(有备份)。确认请加 --yes。')
        return 1

    out = _backup(source, cut, paths, floor)
    print(f'  备份 → {out}')

    # 顺序要紧:先钉下界(不钉的话下一次同步把存档原样再拉一遍),再删 feeds.db
    # 的行 —— 它是「要不要重新导出」的事实来源,先删它才不会留下「文件没了、
    # exported_hash 还在」那种既不重建也不存在的半状态。
    with reg.db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('UPDATE subscriptions SET history_floor=? WHERE id=?', (floor, source['id']))
        ph = ','.join('?' * len(keys))
        c.execute(f'DELETE FROM subscription_items WHERE item_key IN ({ph}) '
                  f'AND subscription_id=?', [*keys, source['id']])
        # 别的订阅还收着的那些留在 feed_items 里,只摘掉本订阅那条归属。
        c.execute(f'DELETE FROM feed_items WHERE key IN ({ph}) AND key NOT IN '
                  f'(SELECT item_key FROM subscription_items)', keys)
    print(f'  feeds.db 已清,history_floor={floor}')

    gone = 0
    for p in paths:
        fp = VAULT_ROOT / p
        if fp.exists():
            fp.unlink()
            gone += 1
    print(f'  vault 删除 {gone} 个 md')

    from kb import store
    conn = store.connect()
    try:
        alive = {r['path'] for r in conn.execute('SELECT path FROM documents')} - set(paths)
        dead = store.remove_missing_documents(conn, alive)
        conn.commit()
    finally:
        conn.close()
    print(f'  kb.db 删除 {dead} 篇文档(级联 chunk/tag/entity 与向量)')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('scan', help='只读:每个订阅在下界之前压着多少条')
    s.add_argument('-v', '--verbose', action='store_true')
    s.set_defaults(func=cmd_scan)
    r = sub.add_parser('run', help='清理一个订阅(先备份)')
    r.add_argument('--source', required=True, help='订阅 id 或名字')
    r.add_argument('--floor', help='覆盖下界(YYYY-MM-DD),缺省按「创建日 - HISTORY_DAYS」')
    r.add_argument('--yes', action='store_true', help='确认执行')
    r.add_argument('--allow-shared', action='store_true', help='条目被别的订阅共享时照删')
    r.add_argument('--allow-favorites', action='store_true', help='命中收藏时照删')
    r.set_defaults(func=cmd_run)
    args = ap.parse_args()
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
