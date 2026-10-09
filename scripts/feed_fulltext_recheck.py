# -*- coding: utf-8 -*-
"""存量复查:把「补抓报了成功、而抓回来的其实不是文章正文」的条目放回队列。

`kb.feeds.readable.boilerplate_reason` 这道判据是 2026-10-09 审查之后才加的,
而它要挡的东西**已经在库里了** —— 补抓一旦报成功,`fulltext_at` 落下、条目
离开重试队列,后面任何一轮同步都不会再碰它(见 `fulltext.queued_sql`)。所以
光修判据只管未来,存量得单独走一趟。

**判据直接取 `readable.boilerplate_reason`,不在这里另写一遍。** 两处各判一遍
的话,放回队列的和以后会被挡掉的不是同一批。它是个纯文本判据,所以这里拿库里
那份 `body` 就能问 —— 不必把 441 个原文链接重新请求一遍。

**放回队列 = 清掉这三样**:`fulltext_at`(「抓到过」的事实来源)、`fulltext_error`、
`fulltext_tries`(额度重新给满)。顺带清掉译文:那份译文译的是这段版样板文字,
正文一换它就作废了(口径同 `fulltext._save`);`extract_stale=1` 留一个待办,
新正文真的落盘之后由 `fulltext.retire_stale` 让旧摘要、主题和实体作废。

**原始那份壳救不回来,这件事要说清楚。** `_save` 是就地覆盖 `body` 的,feed 里
那一条也早就滚出去了 —— 重抓拿得到真正文最好,**拿不到的话试满
`fulltext.MAX_TRIES` 之后库里仍然是这段样板文字**(Fortune 那页的正文只存在于
`<script>` 里,重抓多少次都一样)。`scan` 把这一档如实列出来。

**不取维护锁,也不问管道忙不忙。** 那两道是给不可逆删除用的(见
`scripts/feed_trim_history.py`);这里改的是「抓到过没有」这个标记,幂等、可重跑,
最坏的交错后果是某一条被多抓一次 —— 而 `_save` 本来就是覆盖写。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import fulltext, readable, registry as reg       # noqa: E402


def _suspect(kind: str | None = None, source: int | None = None) -> list[dict]:
    """已经报过补抓成功、而正文过不了 `boilerplate_reason` 的那些。"""
    where = ["fi.fulltext_at<>''"]
    args: list = []
    if kind:
        where.append('fi.kind=?')
        args.append(kind)
    join = ''
    if source is not None:
        join = ' JOIN subscription_items si ON si.item_key=fi.key'
        where.append('si.subscription_id=?')
        args.append(source)
    sql = (f'SELECT DISTINCT fi.key,fi.kind,fi.title,fi.url,fi.body,fi.path,'
           f"fi.exported_hash<>'' AS shipped FROM feed_items fi{join}"
           f' WHERE {" AND ".join(where)}')
    with reg.db() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    out = []
    for row in rows:
        if why := readable.boilerplate_reason(row['body']):
            out.append({**row, 'why': why})
    return out


def cmd_scan(args) -> int:
    rows = _suspect(args.kind, args.source)
    if not rows:
        print('没有需要复查的条目。')
        return 0
    print(f'{len(rows)} 条补抓成品不是文章正文:\n')
    for row in rows:
        mark = '已入库' if row['shipped'] else '未入库'
        print(f"  [{mark}] {row['title'][:48]}")
        print(f"      {row['why']}（{len(row['body'])} 字）")
        print(f"      {row['url']}")
    shipped = sum(1 for r in rows if r['shipped'])
    print('\n放回队列之后下一轮同步会重抓一次。原始那份壳已经被覆盖、救不回来:')
    print('  重抓拿到真正文 → 译文和旧摘要跟着更新;')
    print(f'  重抓仍然拿不到 → 试满 {fulltext.MAX_TRIES} 次之后库里仍是这段样板文字。')
    if shipped:
        print(f'其中 {shipped} 条已经写进过 vault,那篇 md 要等重抓成功才会被覆盖。')
    print('\n真要改:feed_fulltext_recheck.py run --yes')
    return 1


def cmd_run(args) -> int:
    rows = _suspect(args.kind, args.source)
    if not rows:
        print('没有需要复查的条目。')
        return 0
    if not args.yes:
        print(f'{len(rows)} 条待放回队列,加 --yes 才会真改。')
        return 1
    with reg.db() as c:
        c.executemany(
            "UPDATE feed_items SET fulltext_at='',fulltext_error='',fulltext_tries=0,"
            "extract_stale=1,title_zh='',body_zh='',translated_at='',"
            "translate_error='',translate_tries=0 WHERE key=?",
            [(r['key'],) for r in rows])
    print(f'已把 {len(rows)} 条放回正文补抓队列（译文一并作废，下一轮同步重抓）。')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('scan', 'run'):
        p = sub.add_parser(name)
        p.add_argument('--kind', help='只看这一类来源（rss / social …）')
        p.add_argument('--source', type=int, help='只看这一条订阅（subscriptions.id）')
        if name == 'run':
            p.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    return cmd_scan(args) if args.cmd == 'scan' else cmd_run(args)


if __name__ == '__main__':
    raise SystemExit(main())
