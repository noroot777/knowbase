#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给**已经入库**的空正文长文补上正文(X Article 存量回填)。

**为什么需要一个脚本**:`registry.put_items` 是 `ON CONFLICT(key) DO NOTHING` ——
一条推文入库之后,再抓到同一条也不会更新它。于是 `kb/feeds/x_article.py` 那一步
只对**新**长文生效,而库里已经躺着的那些(标题回落成「某某 的动态」、正文一个字
没有、提取阶段每轮报一次「正文为空,需回填」)永远不会自己好。这是它们唯一的出路。

**改的是 feeds.db 的 `title`/`body`,外加清掉 `exported_hash`**,剩下的交给正常
管道:下一轮 `sync.export` 按 `exported_hash<>content_hash` 重写那篇 vault md,
index 重建它的 chunk,而这些文档的 `extracted_at` 本来就是 NULL(正因如此才会被
报成「需回填」),提取自然会把它们选上。**不写 kb.db、不写 vault 文件** —— 所以
不在维护锁的射程里(口径见 `docs/decisions/maintenance-lock.md`「凡是写 kb.db 或 vault 文件的入口」);
和采集并发也不冲突:采集那一侧的 `put_items` 是 DO NOTHING,覆盖不了这里改的行。

**不需要备份,而这句话的依据是那道条件更新**:被改写的原值是一个空正文和一个
占位标题,确实没有东西可丢 —— 可它只在「写回那一刻正文还是空的」时才成立。扫描
(逐条问一次接口,几十秒到几分钟)和写回之间,另一个写入方完全可能已经把正文补好
(一次成功的补抓、一趟存量复查、另一个并发跑的这个脚本),那时按 `key` 盖回去丢掉
的就是一整篇正文连同它的译文 —— 而两次命令都报成功(2026-10-09 第 1 轮审查双线程
实测)。所以判据待在 UPDATE 自己的 WHERE 里(见 `apply_one`),"没有东西可丢"由它
保证,不靠一份备份去兜。

`lang` 一并清成空串:空正文那会儿它是按占位标题判的(「GREG ISENBERG 的动态」里
有「的动态」三个汉字 → 判成中文),正文补上之后那个结论对一篇英文长文就是错的,
而 `lang='zh'` 的条目永远进不了翻译队列。清掉之后由 `translate._label_unknown`
按真正文重判。**代价要知道**:英文长文会因此进翻译队列(一轮 40 条、每条正文
截到 12000 字符),实测两篇 Greg Isenberg 的文章各 3 万字。

用法:
    python scripts/x_article_backfill.py scan              # 只读,逐条问一次,报三档
    python scripts/x_article_backfill.py run --yes         # 真回填
    python scripts/x_article_backfill.py run --yes --limit 3
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import registry as reg, x_article, x_post_meta

# 三档结论:有长文 / 确定没有长文(纯图、纯视频推文)/ 这一次问不到(推文已删、
# 接口故障)。只有第一档会写库,后两档如实报出来 —— 第三档下次再跑就是。
GOT, NONE, FAIL = 'got', 'none', 'fail'


def empty_items(conn, limit=0):
    """空正文、而且看得出是一条推文的 social 条目。"""
    sql = ("SELECT key, url, title, published, content_kind, content_hash FROM feed_items"
           " WHERE kind='social' AND length(trim(body))=0 AND url LIKE '%/status/%'"
           " ORDER BY published DESC")
    rows = conn.execute(sql).fetchall()
    return rows[:limit] if limit else rows


def probe(row):
    """问一次。返回 (档位, 结论或原因)。"""
    try:
        got = x_article.fetch(row['url'])
    except x_article.ArticleUnavailable as e:
        return FAIL, str(e)
    return (GOT, got) if got else (NONE, '')


def _hash(row, title, body):
    """新的内容指纹。

    **不保证和 `put_items` 那次算出来的逐字同形**(它对整个 entry dict 取摘要,
    而那个 dict 的键集合由采集器决定、这里重建不出来)。它只被 `sync.export` 的
    `exported_hash<>content_hash` 用来回答「要不要重写这篇 md」,所以"和原值不同、
    而且稳定"就够了;采集那一侧永远不会更新它(DO NOTHING)。
    """
    payload = {'url': row['url'], 'title': title, 'body': body,
               'published': row['published'], 'content_kind': row['content_kind']}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()


def apply_one(conn, row, title, body):
    """把正文写回那一行。**写进去了返回 True,期间已经被人改过返回 False。**

    导出那道闸门是 `exported_hash<>content_hash`,而这里**两道各自都够**:新的
    `content_hash` 和清空的 `exported_hash` 任意一道成立,那篇 md 下一轮就会被
    重写。定点注入实测:只去掉其中任一道,一条用例都不红;**两道一起失效**才让
    「md 真的被重写」那条当场红。都写上的理由是「内容变了」这件事在这两列上各有
    一个表达 —— 哪一列的口径以后变了,另一道还兜着。

    **判据待在 UPDATE 自己的 WHERE 里,不许在 Python 那边先 SELECT 一次比对。**
    扫描是「逐条问一次长文接口」,几十秒到几分钟;这期间另一个写入方
    (`fulltext._save`、`scripts/feed_fulltext_recheck`、另一个并发跑的这个脚本)
    完全可能已经把正文补好、翻译好。按 `key` 无条件盖回去的后果实测过:正文从
    `CURRENT_BODY from later successful run` 变回 `OUTDATED_BODY from earlier
    request`、`body_zh` 和 `translated_at` 一并清空,而**两次命令都报成功**
    (2026-10-09 第 1 轮审查双线程复现)。`BEGIN IMMEDIATE` 挡不住它 —— 它只
    串行化最后那个写事务,管不着「读到的那一版还是不是当前那一版」。先查后写同理
    不成立:两个执行体之间那条缝关不掉,而 SQLite 把写入串行化,判据待在写语句的
    WHERE 里才和这次写入同生共死(口径同 `feed_fulltext_recheck._downgrade`、
    `sync._claim_write`、`store.mark_no_body` 和 collector 那条 UPSERT)。

    **两道条件,各回答一件事**:`content_hash` 是"我扫到的还是当前这一版吗",
    `length(trim(body))=0` 是"正文此刻还是空的吗"。**如实记一笔:今天这两道同真
    同假** —— 现有的写入方都必然连 body 和 `content_hash` 一起换,所以只留任一道
    在真实路径上行为等价。留着两道的理由是**构造上的**:这个脚本的前提是
    `empty_items` 那两个条件(空正文 + 那一版内容),判据就该覆盖两项;哪天出现
    一个只改其中一列的写入方(手工 SQL、一个只刷 hash 的迁移),另一道还兜着。
    各有一条**契约**用例钉着(摆的局只有其中一道挡得住)。

    **0 行不是失败,是我们想要的末态** —— 另一边已经把这件事做了,这一趟什么都
    不该动。也**不就地重判**:`empty_items` 的判据是"正文是空的",现在它非空,
    下一次 scan 自己就不会再选中它。
    """
    final_title = title or row['title']
    annotated = x_post_meta.stamp(dict(title=final_title, body=body), 'article', '')
    cur = conn.execute(
        """UPDATE feed_items
              SET title=?, body=?, content_hash=?, exported_hash='', lang='',
                  title_zh='', body_zh='', translated_at='', translate_error='',
                  translate_tries=0, x_post_meta=?
            WHERE key=? AND content_hash=? AND length(trim(body))=0""",
        (final_title, body, _hash(row, final_title, body), annotated[x_post_meta.FIELD],
         row['key'], row['content_hash']))
    return cur.rowcount > 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('cmd', choices=['scan', 'run'])
    ap.add_argument('--yes', action='store_true', help='run 时真的写库')
    ap.add_argument('--limit', type=int, default=0, help='最多处理几条')
    args = ap.parse_args(argv)

    with reg.db() as conn:
        rows = empty_items(conn, args.limit)
    if not rows:
        print('没有空正文的推文条目,无事可做。')
        return 0

    print(f'{len(rows)} 条空正文推文条目,逐条问一次长文接口……\n')
    plan, counts = [], {GOT: 0, NONE: 0, FAIL: 0}
    for row in rows:
        kind, got = probe(row)
        counts[kind] += 1
        label = (row['title'] or '')[:18]
        if kind == GOT:
            plan.append((row, got['title'], got['body']))
            print(f'  长文    {label:<20} {len(got["body"]):>6} 字  《{got["title"][:34]}》')
        elif kind == NONE:
            print(f'  没长文  {label:<20} 这条推文确实没有长文(纯图/纯视频)  {row["url"]}')
        else:
            print(f'  问不到  {label:<20} {got}')

    print(f'\n合计:长文 {counts[GOT]} 条、确实没有 {counts[NONE]} 条、'
          f'这一次问不到 {counts[FAIL]} 条。')
    if args.cmd == 'scan':
        print('这是 scan,一个字节都没改。要回填跑:run --yes')
        return 0
    if not plan:
        print('没有可回填的,库里一个字节都没改。')
        return 0
    if not args.yes:
        print(f'要回填这 {len(plan)} 条请加 --yes(只改 feeds.db 的标题和正文)。')
        return 1

    # **报的是实际写进去了几条,不是计划里有几条。** 按计划长度报的话,一趟
    # 整批都被跳过的命令会说「已回填 N 条」而库里一个字节都没改 —— 人看完就以为
    # 处理过了(口径同 `scripts/feed_fulltext_recheck`)。
    done, skipped = 0, []
    with reg.db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for row, title, body in plan:
            if apply_one(conn, row, title, body):
                done += 1
            else:
                skipped.append(row)
        conn.commit()
    if done:
        print(f'已回填 {done} 条。下一轮同步(或 `./kb.sh sync`)的导出阶段会重写'
              f'这几篇 vault md,之后索引和提取会自己把它们收掉。')
    else:
        print('一条都没写:计划里那几条在这期间都已经被改过了,库里一个字节没动。')
    if skipped:
        print(f'跳过 {len(skipped)} 条 —— 扫描之后这几条的正文已经被别人改过'
              f'(一次成功的补抓、一趟存量复查,或者另一个并发跑的本脚本),'
              f'这一趟不覆盖它们:')
        for row in skipped:
            print(f'    {(row["title"] or "")[:18]:<20} {row["url"]}')
    if counts[FAIL]:
        print(f'另有 {counts[FAIL]} 条这一次问不到,过后再跑一次就是。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
