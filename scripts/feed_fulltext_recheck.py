# -*- coding: utf-8 -*-
"""存量复查:把「补抓报了成功、而抓回来的其实不是文章正文」的条目收拾干净。

`kb.feeds.readable.boilerplate_reason` 这道判据是 2026-10-09 审查之后才加的,
而它要挡的东西**已经在库里了** —— 补抓一旦报成功,`fulltext_at` 落下、条目
离开重试队列,后面任何一轮同步都不会再碰它(见 `fulltext.queued_sql`)。所以
光修判据只管未来,存量得单独走一趟。

**判据直接取 `readable.boilerplate_reason`,不在这里另写一遍。** 两处各判一遍
的话,收拾掉的和以后会被挡掉的不是同一批。它是个纯文本判据,所以这里拿库里
那份 `body` 就能问 —— 不必把 441 个原文链接重新请求一遍。

**「放回队列」不够,必须连那份 body 一起换掉。** 第 1 轮只清了三个标记
(`fulltext_at` / `fulltext_error` / `fulltext_tries`)和译文,body 原样留着 ——
于是重抓三轮都失败之后它**离开补抓队列**,而 `translate.queued_sql` 这时不再
排除它:那段版权声明被**重新翻译一遍、重新导出**,阶段还报「已保存 1 条到
知识库」,`_suspect` 也再也选不中它(它只认 `fulltext_at<>''`)。换句话说清
标记只是让已知的错误内容又走了一遍正文消费链,还多花一次翻译钱
(2026-10-09 第 2 轮审查三轮实测)。

**换完之后它就是一条「从没补抓过」的普通条目**:body 是壳、
`content_kind='summary'`、`content_hash` 跟着重算。于是三轮重抓失败后按壳
入库 —— 那和一条压根没开过补抓的条目待遇完全一样,**是正确的降级**,不需要
任何新列、新标记或新的排除条件。判据自己也不会再选中它(壳过得了
`boilerplate_reason`)。

**壳从哪儿来,两条路,各有判据**:
①**备份库**(`--backup`,或自动扫 `<data>/backups/*/feeds.db` 按新旧倒序)。
  第 1 轮我断言"原始那份壳救不回来",**那是错的** —— 回填前自己做的那份备份
  里就有(审查实测:Stripe 68 字、Fortune 118 字)。恢复回来的壳要**再过一遍
  判据**:备份可能是在某一轮错误补抓之后做的,里面那份同样是样板文字,
  那就不算有效壳。
②**按标题和链接重建**。备份里没有(或者里面那份也过不了判据)时的兜底。
  形态和 feed 原本那份壳一致(标题 + 链接),是从库里现成的字段拼的,不编造
  任何内容。比留着 455 字版权声明严格地好:检索、提取打标、日报素材都不再
  被它污染。

**译文一并作废**:那份译文译的是这段样板文字,body 一换它就无效了(口径同
`fulltext._save`)。`extract_stale=1` 留一个待办,新 body 落盘之后由
`fulltext.retire_stale` 让按旧内容算出来的摘要、主题和实体作废。
`exported_hash` 不动 —— body 一换 `content_hash` 就不一样了,导出闸门
(`exported_hash<>content_hash`)自然成立,下一轮同步会把 vault 里那篇
覆盖掉。

**不取维护锁,也不问管道忙不忙 —— 换来的那道保护在 `_downgrade` 的
WHERE 里。** 那两道闸是给不可逆删除用的(见 `scripts/feed_trim_history.py`),
而且对这件事压根不适用:补抓那一侧的 `fulltext._save` 自己就不持维护锁,
这边取了也挡不住它。
**原来这儿写着「最坏的交错后果是某一条被多抓一次」,那句话是错的。**
第 1 轮版本只改几个标记(那些确实幂等、谁盖谁都一样),第 2 轮加上 `body=?`
之后这条写回就变成了「拿一份旧快照覆盖正文」—— 而 `_suspect` 读完到真写回
之间还隔着一段备份库 I/O:同步在这期间把这一条补抓成功、译文和 vault 都写
好了,随后这份旧计划照样按 `key` 盖回去,914 字完整正文连同中文译文和成功
标记一起变回 29 字的标题加链接,后面三轮再抓不到就永久停在壳上
(2026-10-09 第 3 轮审查双线程实测,独立复现三次)。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import fulltext, readable, registry as reg       # noqa: E402


def _suspect(kind: str | None = None, source: int | None = None) -> list[dict]:
    """开了补抓的订阅里,`body` 过不了 `boilerplate_reason` 的那些。

    **射程是"开了补抓的订阅",不是"报过补抓成功"**(`fulltext_at<>''`)。后者
    漏掉的恰恰是这个脚本自己制造出来的中间态:第 1 轮版本只清标记不换 body,
    于是那两条生产条目的 `fulltext_at` 和 `fulltext_tries` 都归了零、body 还是
    455 字版权声明 —— 和「从没补抓过」长得一模一样,再跑一次复查**一条都选不中**
    (2026-10-09 第 2 轮审查点名的就是这个:「不再被复查选中」)。

    **在这个射程里"body 是样板文字"就是充分判据**:HN Buzzing 这类聚合源的
    feed 原本给的是跳转壳(标题 + 域名 + HN 得分),过得了判据;真正文同理。
    一条 body 是版权声明或作者简介的,只能是补抓这条路写进去的。**没开补抓的
    订阅一个都不碰** —— 它们的正文是 feed 自己给的,不在这个脚本的射程里。
    """
    where = ["EXISTS(SELECT 1 FROM subscription_items x JOIN subscriptions s"
             ' ON s.id=x.subscription_id WHERE x.item_key=fi.key AND s.fulltext=1)']
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


def _backups(explicit: str | None) -> list[Path]:
    """备份库,新的在前。显式给了就只认那一个 —— 指错了要当场看出来。"""
    if explicit:
        path = Path(explicit)
        if path.is_dir():
            path = path / 'feeds.db'
        return [path] if path.exists() else []
    found = sorted((reg.DB_PATH.parent / 'backups').glob('*/feeds.db'),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def _from_backup(paths: list[Path], key: str) -> tuple[str, str]:
    """备份里那份原始 body。返回 `(壳, 来自哪个备份)`,没有就两个空串。

    **恢复回来的要再过一遍判据**:备份可能是在某一轮错误补抓之后做的,里面
    那份同样是样板文字 —— 那就不算有效壳,继续往更早的备份找。
    """
    for path in paths:
        try:
            conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            with conn:
                row = conn.execute('SELECT body FROM feed_items WHERE key=?', (key,)).fetchone()
        except sqlite3.Error:
            continue                      # 老备份的 schema 不同、或者文件坏了
        finally:
            try:
                conn.close()
            except (sqlite3.Error, NameError, UnboundLocalError):
                pass
        body = (row['body'] if row else '') or ''
        if body.strip() and not readable.boilerplate_reason(body):
            return body, path.parent.name
    return '', ''


def _rebuild(row: dict) -> str:
    """按标题和链接拼一份壳。形态同 feed 原本那份,不编造任何内容。"""
    return '\n'.join(part for part in (row['title'], row['url']) if part)


def _plan(rows: list[dict], backups: list[Path]) -> list[dict]:
    for row in rows:
        shell, origin = _from_backup(backups, row['key'])
        row['shell'] = shell or _rebuild(row)
        row['origin'] = origin or '标题与链接'
    return rows


def cmd_scan(args) -> int:
    rows = _plan(_suspect(args.kind, args.source), _backups(getattr(args, 'backup', None)))
    if not rows:
        print('没有需要复查的条目。')
        return 0
    print(f'{len(rows)} 条补抓成品不是文章正文:\n')
    for row in rows:
        mark = '已入库' if row['shipped'] else '未入库'
        print(f"  [{mark}] {row['title'][:48]}")
        print(f"      {row['why']}（{len(row['body'])} 字）")
        print(f"      正文换成 → {row['origin']}（{len(row['shell'])} 字）")
        print(f"      {row['url']}")
    restored = sum(1 for r in rows if r['origin'] != '标题与链接')
    shipped = sum(1 for r in rows if r['shipped'])
    print('\n要做的事:把这段样板文字换成壳、放回补抓队列、译文一并作废。')
    print(f'  {restored} 条从备份里恢复原始内容，{len(rows) - restored} 条按标题与链接重建。')
    print(f'  下一轮同步重抓一次:拿到真正文就补上;仍然拿不到 → 试满'
          f' {fulltext.MAX_TRIES} 次之后按壳入库（和没开过补抓的条目一样）。')
    if shipped:
        print(f'  其中 {shipped} 条已经写进过 vault，那篇 md 下一轮导出就会被覆盖。')
    print('\n真要改:feed_fulltext_recheck.py run --yes')
    return 1


def _downgrade(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """把计划里的每一条换成壳。返回 `(真改了的, 这期间内容已经换过的)`。

    **判据是「这一条还是我扫到的那份 body 吗」,而且待在 UPDATE 自己的 WHERE
    里(`AND body=?`),不许在 Python 这边先 SELECT 一次比对。** 先查后写在两
    个进程之间压根不成立(kb-web 的补抓 worker 是另一个写入方,`fulltext._save`
    也不持任何跨进程锁),而 SQLite 把写入串行化 —— 判据待在写语句里才和那次
    写入同生共死,放行不了就是 0 行。口径同 collector 那条「判据就在 UPDATE
    自己的 WHERE 里」和 `sync._claim_write`。

    **比的是旧 body 本身,不是 `content_hash`。** 那一列在这张表上有两个写入
    方、两套口径(`registry.put_items` 写的是整个 entry 的 JSON 摘要,
    `fulltext.body_hash` 写的是 `sha256(body)`),而这里要回答的恰恰是"我读到
    的那份正文还是当前那一份吗" —— 直接比正文没有第二份口径可漂。

    **0 行不是失败,是我们想要的末态**:同步在这期间把它补抓成功了,那份正文
    (以及译文、`fulltext_at`、已经导出的 md)一个字都不许动。**也不在这里就地
    重判**:重判会变成一个没有终点的循环,而第 2 轮放宽 `_suspect` 射程之后
    「body 是样板文字」就是充分判据、与 `fulltext_at` 无关 —— 新内容要是又一份
    样板文字,下一次 scan 自己就会再选中它。所以如实报出来、让人再跑一次。

    整批在**一个**写事务里(`reg.db()` 退出时才 commit):第一条 UPDATE 就把写
    锁攥住,后面每一条的判据都在锁内求值。
    """
    cut, moved = [], []
    with reg.db() as c:
        for row in rows:
            cur = c.execute(
                """UPDATE feed_items SET body=?,content_kind='summary',content_hash=?,lang=?,
                   fulltext_at='',fulltext_error='',fulltext_tries=0,extract_stale=1,
                   title_zh='',body_zh='',translated_at='',translate_error='',translate_tries=0
                   WHERE key=? AND body=?""",
                (row['shell'], fulltext.body_hash(row['shell']),
                 reg.text_language(row['title'], row['shell']), row['key'], row['body']))
            (cut if cur.rowcount else moved).append(row)
    return cut, moved


def cmd_run(args) -> int:
    rows = _plan(_suspect(args.kind, args.source), _backups(getattr(args, 'backup', None)))
    if not rows:
        print('没有需要复查的条目。')
        return 0
    if not args.yes:
        print(f'{len(rows)} 条待收拾,加 --yes 才会真改。')
        return 1
    cut, moved = _downgrade(rows)
    restored = sum(1 for r in cut if r['origin'] != '标题与链接')
    print(f'已收拾 {len(cut)} 条:正文换成壳（{restored} 条取自备份，'
          f'{len(cut) - restored} 条按标题与链接重建）、放回补抓队列、译文一并作废。')
    if moved:
        print(f'\n另有 {len(moved)} 条在这一趟里内容已经换过，原样留着没动:')
        for row in moved:
            print(f"  {row['title'][:48]}")
        print('  （同步可能刚把正文补上了。再跑一次 scan 看它现在是什么。）')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('scan', 'run'):
        p = sub.add_parser(name)
        p.add_argument('--kind', help='只看这一类来源（rss / social …）')
        p.add_argument('--source', type=int, help='只看这一条订阅（subscriptions.id）')
        p.add_argument('--backup', help='从这个备份里恢复原始正文（目录或 feeds.db）；'
                                        '不给则自动扫 <data>/backups/*/feeds.db')
        if name == 'run':
            p.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    return cmd_scan(args) if args.cmd == 'scan' else cmd_run(args)


if __name__ == '__main__':
    raise SystemExit(main())
