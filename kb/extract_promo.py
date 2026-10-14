# -*- coding: utf-8 -*-
"""软广判定的落库与交付。**硬约束在 `docs/decisions/promo.md` 第一节,改这块之前先读。**

从 `kb/extract.py` 拆出来的一块(700 行上限逼出来的第二次拆分,手法同
`kb.extract_optin`):**判定怎么下**仍然只有 `kb.extract_llm` 那一份口径,这里
只管判完之后的三件事 —— 落到 kb.db、对齐 `doc_tags`、对齐 md frontmatter。
这三件必须一起成立,所以 `commit_promo` 把它们圈进同一个协调区间。

**但「圈进一个区间」只挡得住竞争,挡不住第二步自己失败。** kb.db 的事务管不着
vault 文件:权限、磁盘满、进程被杀都能让判定落了库而 md 那行还在。所以这里的
三件事各有一道自己的闸:落库那道是条件写,`doc_tags` 那道按**库里的判定**重建
(`kb.store._align_promo_tag`),md 那道做完要回读确认,没对齐就留一条持久化
待办(`promo_tag_pending`)并如实记成一次没完成的交付。
"""
from datetime import datetime

# hard 软广在 obs 里也标出来。doc_tags 在重索引时会按 frontmatter 重建,所以
# 这个 tag 跟 topic/entity 一样要靠 writeback 回写 md 才持久。**它留在这里**:
# 回答的是"回写哪个标签",不是跟模型打交道的事。
PROMO_TAG = "flag/软广"

# 「有明确结论」的那几档。'' = 还没判过;见 normalize_promo 的 fallback 那段。
PROMO_DECIDED = ("none", "soft", "hard")


class PromoTagNotAligned(Exception):
    """md frontmatter 里那行 `flag/软广` 没能对齐到库里的判定。

    **只在回读确认之后抛**:不是「写文件报错了」(那是 OSError),而是「写完了、
    再读一遍发现它还在/还不在」。正则摘不掉的合法 YAML 写法就落在这一档 ——
    我们自己写进去的是 `  - flag/软广`,但同一份 frontmatter 被别的工具(或人)
    重排成 `- "flag/软广"`、`tags: [flag/软广]` 之后,按行删的那条正则匹配不到,
    而 `remove_writeback_tag` 返回的 False 跟「本来就没有」长得一模一样。
    不抛的话这就是一次静默失败:交付方报成功、待办不留,而索引下一轮照旧按
    frontmatter 把它读回 `doc_tags`。
    """


def save_promo(conn, path: str, promo: dict, now: str | None = None,
               version_seen: str | None = None) -> bool:
    """写软广判定,并同步 doc_tags 里的 flag。返回「写进去了没有」。

    False 有两种,都不是故障、都该被调用方如实记成跳过:
      - **这一版正文已经不是我们判的那一份**(`version_seen` 对不上);
      - **这次没拿到结论,而这一篇已经有结论了**(`promo["fallback"]`)。

    `version_seen` 给了就把它钉进 UPDATE 自己的 WHERE —— **判据待在写语句里,
    不许在 Python 那边先读一次 `content_hash` 再比**,理由和写法同
    `mark_no_body`:判定在工作线程里做,落库在主线程,这中间另一条连接的补抓 →
    导出 → 索引完全可能把这一篇整篇换掉(重判不持维护锁,是合法交错)。那一版
    新正文往往已经由正常提取判过一次,旧请求回来无条件覆盖就把**新**结论擦了,
    而 `extracted_at` 还在、`pending_docs` 是空的 —— 再同步多少轮也不会自己纠正。
    正文和它那一版的号从 `store.doc_input_snapshot` 一条语句里取(那个函数存在
    的理由就是这个),0 行 = 这期间换版了,**这一轮什么都不写、也不动 tag**,
    由调用方如实记成跳过;下一轮按新版重判一次就对了。

    **`fallback` 那一档挡的是另一件事:没有结论的「兜底 none」不许覆盖已有结论。**
    `normalize_promo` 认不出 level 时填一个 none 并标注 `fallback`(理由见那边)。
    这一篇还没判过时那是对的保守值;可是**提取这条路同样会覆盖已有判定** ——
    正文补抓回来后 `fulltext.retire_stale` 只清 `extracted_at`、明确保留原来的
    promo,于是一篇已经判 hard 的广告带着结论重新排进提取,一个缺 promo 字段的
    回包就把它擦成 none:广告重新进检索、两处 flag 一起摘掉,而界面显示提取成功、
    `extracted_at` 非空,这一篇从此掉出所有待办。`judge_promo(require_verdict)`
    那道 strict 只管得到重判那条路,管不到全量提取(那条路不能开 strict:绝大
    多数文章是第一次判,一个坏回包不该把 entity/topic/summary 整份丢掉)。所以
    闸设在这里 —— **所有覆盖已有判定的路都从这一句过**。明确的 none 照旧放行。
    """
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    old = conn.execute("SELECT promo_level FROM documents WHERE path=?",
                       (path,)).fetchone()
    old_level = (old["promo_level"] if old else "") or ""
    if promo.get("fallback") and old_level in PROMO_DECIDED:
        return False               # 见 docstring:兜底的 none 不许盖掉有结论的
    sql = ("UPDATE documents SET promo_level=?, promo_reason=?, promo_at=?"
           " WHERE path=?")
    args = [promo["level"], promo.get("reason") or "", now, path]
    if version_seen is not None:
        sql += " AND content_hash=?"
        args.append(version_seen)
    if conn.execute(sql, args).rowcount == 0:
        return False               # 见 docstring:换版了,tag 也一个都不许动
    if promo["level"] == "hard":
        conn.execute("INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?, ?)",
                     (path, PROMO_TAG))
    else:
        # 重跑改判时把旧标记摘掉,不然面板上永远挂着
        conn.execute("DELETE FROM doc_tags WHERE path=? AND tag=?",
                     (path, PROMO_TAG))
    return True


def promo_tag_in_md(doc_path: str) -> bool:
    """这篇 md 的 frontmatter 里现在有没有 `flag/软广` —— **按索引的读法**。

    用 `scanner.parse_frontmatter` + `scanner.frontmatter_tags`,和索引重建
    `doc_tags` 用的是同一份口径。这道确认的后果就是「索引下一轮会不会把它读回
    库里」,所以不能另写一套读法:按行删的那条正则覆盖不到的写法(引号项、
    flow-style 列表),只有用索引的眼睛看才发现摘没摘掉。

    读不出来(文件没了、编码坏了)**不吞**:交给调用方当一次没完成的交付。
    """
    from kb.extract import VAULT_ROOT
    from kb.scanner import frontmatter_tags, parse_frontmatter

    fm, _ = parse_frontmatter(
        (VAULT_ROOT / doc_path).read_text(encoding="utf-8"))
    return PROMO_TAG in frontmatter_tags(fm)


def disk_body_indexed(conn, path: str) -> bool:
    """磁盘上这一篇的**正文**已经进索引了吗 —— 只改过 frontmatter 不算没进。

    `version_seen`(= `documents.content_hash`)只证明**索引记录**没换版,证明
    不了磁盘上那份 md 还是模型读过的内容:导出和索引各自取锁、按两个阶段依次
    调用(`feeds.sync` → `pipeline_stages`),一次补判完全可以插在两者之间 ——
    那时磁盘上已经是新正文、chunks 和 `content_hash` 还是旧的,而落库那道条件
    写照旧放行。

    **判据只能是正文,不能是整个文件的 hash。** 后者会把「只改了 frontmatter」
    (翻译回写、tag 回写、wiki 追加图谱尾区,都是常态)也算成"没进索引",于是
    一次正文压根没动的合法改判被拦掉。所以这里走索引自己的那条路:把磁盘正文
    按 `normalize_body` → `split_chunks` 过一遍(**和 `cli.cmd_index` 同一对
    函数**),再和库里的 chunks 拼回来比。相等 = 现在重新索引一次也不会改动
    chunks = 判定针对的那份正文就是磁盘上那份。

    读不出来**不吞**(让调用方记成一次没完成的交付):这一篇的 md 还在不在,
    由 `_still_indexed` 在同一把锁里先问过了,再读不出来就是真的出了事。
    """
    from kb import store
    from kb.chunker import split_chunks
    from kb.extract import VAULT_ROOT, body_fingerprint
    from kb.scanner import file_hash, normalize_body, parse_frontmatter

    text = (VAULT_ROOT / path).read_text(encoding="utf-8")
    if not store.doc_needs_reindex(conn, path, file_hash(text)):
        return True                 # 整个文件都对得上,正文自然对得上
    _, md_body = parse_frontmatter(text)
    fresh = "\n".join(c.text for c in split_chunks(normalize_body(md_body)))
    return body_fingerprint(fresh) == body_fingerprint(store.doc_body(conn, path))


def mark_promo_tag_pending(conn, path: str, pending: bool) -> None:
    """落/清「md 那行 flag 还没对齐」这条待办,并立即提交。

    **这条待办是交付跨两个存储的唯一兜底。** 判定进的是 kb.db 的事务,md 那行
    进的是 vault 文件,没有一个事务能同时管住两者:提交完 DB、写文件之前进程
    被杀(或者没权限、磁盘满),末态就是库里 none、md 里还挂着 flag。而下一轮
    默认候选按 `promo_level <> ''` 把这一篇滤掉,再同步多少次都不会回来看它。
    留下这一行,`promo_backfill sync-tags` 才有东西可修 —— 而且那条修复路**不
    花钱**:目标状态就是库里那份判定,不必再问一次模型。

    立即提交是故意的:它要在「随后那次失败」之后仍然在库里。调用方此刻可能
    持着维护锁(写它的地方都在锁内),所以这里只发一条 UPDATE、不开新事务。
    """
    conn.execute("UPDATE documents SET promo_tag_pending=? WHERE path=?",
                 (1 if pending else 0, path))
    conn.commit()


def promo_tag_pending_docs(conn, accounts: list[str] | None = None) -> list[str]:
    """还留着「md 那行 flag 没对齐」待办的文档路径(`promo_backfill sync-tags`)。

    `accounts` 给了就只看这几个号(空列表当没给,理由同 `pending_promo_docs`)。
    **这条查询不限 source**:待办是交付失败留下的,哪条来源的文章都能留下它。
    """
    where = ["promo_tag_pending <> 0"]
    args: list = []
    if accounts:
        where.append(f"account IN ({','.join('?' * len(accounts))})")
        args += list(accounts)
    rows = conn.execute(
        f"SELECT path FROM documents WHERE {' AND '.join(where)}"
        f" ORDER BY promo_at DESC", args).fetchall()
    return [r["path"] for r in rows]


def align_promo_tag(conn, doc_path: str) -> bool:
    """把 md frontmatter 里那行 flag 对齐到**库里的判定**,回读确认后才算完。
    返回「改了文件没有」;没对齐就抛(`OSError` / `PromoTagNotAligned`)。

    **目标状态取库里的 `promo_level`,不取这一次判定的结果,也不取「上一次是
    不是 hard」。** 原来那个信号(`save_promo` 返回的 stale)只在「这一次恰好
    改判了」时为真,于是一次失败之后就再也没有第二次机会:第二次重判库里已经
    是 none,信号为假,压根不会去碰文件,而 md 那行还在。报告里那条路实测过
    (注入一次 `PermissionError`、撤掉、再重判一次,末态仍是 DB none + md flag,
    两次都报成功)。**按目标状态幂等对齐**则是:谁来调、调几次,末态都一样。

    `promo_level = ''`(还没判过)两样都不做 —— 那时 md 那行是提取阶段写进去的
    唯一记录,凭「库里没有」把它摘掉等于丢数据。口径同 `store._align_promo_tag`。
    """
    from kb.extract import remove_writeback_tag, writeback_tags

    row = conn.execute("SELECT promo_level FROM documents WHERE path=?",
                       (doc_path,)).fetchone()
    level = (row["promo_level"] if row else "") or ""
    if level not in PROMO_DECIDED:
        return False
    want = level == "hard"
    changed = (writeback_tags(doc_path, [PROMO_TAG]) if want
               else remove_writeback_tag(doc_path, PROMO_TAG))
    if promo_tag_in_md(doc_path) != want:
        raise PromoTagNotAligned(
            f"{PROMO_TAG} 在 frontmatter 里仍然{'缺着' if want else '挂着'}:"
            f" {doc_path}")
    # 对齐到了 —— 顺手把待办清掉(条件写,0 行就是「本来没有待办」)。清在这里
    # 而不在某一条调用路上:四条路都从这个函数过,修复那条路(sync-tags)也一样。
    conn.execute("UPDATE documents SET promo_tag_pending=0"
                 " WHERE path=? AND promo_tag_pending<>0", (doc_path,))
    conn.commit()
    return changed


def sync_promo_tag(conn, doc_path: str, level: str) -> None:
    """把 md frontmatter 里的 flag/软广 对齐到当前判定(存量补判走这条)。

    存量补判不重做 entity/topic,所以喂给 writeback_extraction 的是一份只有
    promo 的结果——口径仍然共用那一份,不另写一遍。`level` 只用来告诉那边
    「这条路带着一个 promo 结论」;**要摘还是要补,由它按库里那份判定定**
    (见 `align_promo_tag`)。

    **这条路不往回写传正文指纹。** 不是忘了:判据提到了整个交付区间的入口 ——
    `commit_promo` 在落库**之前**就把「磁盘上这一篇还没进索引」和「索引里的
    正文已经换了」两样一起判掉了(`_body_moved` 那两道)。拦下来的末态是**一个
    字都没写**,库里和 md 里仍然是原来那份判定、彼此一致。在回写这一侧再判一次
    反而会造出不一致:那时判定已经落库了,摘不掉 md 那行就成了「库里 none、
    frontmatter 挂着 flag」,而只改 frontmatter 的流程(翻译回写、tag 回写、
    wiki 追加图谱尾区)是常态,第一道比的又是整个**文件**的 hash。
    """
    from kb.extract import writeback_extraction

    writeback_extraction(conn, doc_path, {"promo": {"level": level}})


def commit_promo(conn, path: str, promo: dict, version_seen: str,
                 body_seen: str, *, on_error=None) -> tuple[str, bool]:
    """交付一次软广重判:核对正文版本 → 条件写 → 对齐 `doc_tags` → 提交 →
    对齐 md 里那行,整段在**同一把**维护锁里。

    返回 `(为什么什么都没写, md 那行对齐了没有)`:
      - `("", True)`  三处一致,这一篇交付完了;
      - `("…", False)` 一个字都没写,原因在第一位,下一轮重判一次就对了;
      - `("", False)` 判定落库了、md 那行没对齐,**已经留了持久化待办**
        (`promo_tag_pending`),由 `promo_backfill sync-tags` 不花钱地修。
        调用方要把这一档和成功分开记 —— 报成功会让人以为末态是一致的。

    **锁序(维护锁 → SQLite 写锁)、进锁之前先 flush、模型请求留在锁外、锁内
    先问一次这一篇还在不在 —— 理由全同 `kb.extract.commit_extraction`**,
    不在这儿复述。这里只说补判多出来的两条:

    **一、`doc_tags` 那行和 md 那行必须在同一个协调区间里对齐。** 原来这两段是
    分开的:`save_promo` 删 `doc_tags` 在锁外,摘 md 在锁内。中间那个窗口里一次
    **完全合法**的索引就能把改判毁掉 —— `upsert_document` 不碰已经改成 none 的
    `promo_level`,却按**还没摘**的 frontmatter 把 `flag/软广` 重新插回
    `doc_tags`;随后的回写只摘 md、不会再回头清库里那行,还顺手把 `content_hash`
    刷成当前文件的 hash,于是下一轮索引认为文件没变、直接跳过
    `upsert_document` —— 错的那行标签**永久**留在库里。

    **二、「我判的那份正文」必须同时是「索引里那份」和「磁盘上那份」,三者同一
    才允许固化。** `version_seen` 只证明索引记录没换版,证明不了磁盘上那份 md
    还是模型读过的内容:导出和索引各自取锁、按两个阶段依次调用
    (`feeds.sync` → `pipeline_stages`),补判完全可以插在两者之间 —— 那时磁盘
    上已经是新正文、chunks 和 `content_hash` 还是旧的,条件写照旧放行。实测
    末态:一篇代充值广告的新正文落在磁盘上,库里存着对**旧**资讯正文判出的
    none,回写摘标签时 `refresh_content_hash` 又把新文件的 hash 写进库,于是
    索引认为「未变」—— 新正文一个字都搜不到,点开文章却是新的,再同步也不恢复
    (2026-10-14 审查三次稳定复现)。所以锁内、**落库之前**两道一起问:磁盘正文
    进索引了吗(`disk_body_indexed`)、索引里那份正文换版了吗(`_body_moved`
    的 ②)。拦下来就是一个字都没写,库里和 md 里仍是原来那份判定、彼此一致,
    下一轮等索引跟上再判。那一侧的 `refresh_content_hash` 现在也只认「库里记
    的还是我动手之前那一份」,不会再替别人的改动签一张已索引的收据。

    `on_error` 只用来**通知**调用方回写出了什么事(打日志),收不收这个回调都
    不改变行为:认得出的交付失败(写文件报错、回读发现没对齐)一律记待办 +
    返回 `("", False)`,别的异常一律原样抛 —— 判什么算「交付失败」是这一层的
    口径,不交给调用方放宽。
    """
    from kb.extract import _body_moved, _still_indexed
    from kb.maintenance import maintenance_lock

    if conn.in_transaction:
        conn.commit()       # 取维护锁之前先落盘,理由同 commit_extraction
    with maintenance_lock():
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not _still_indexed(conn, path):
                # 等锁这段时间里清理把这一篇删掉了。`doc_tags` 对
                # `documents(path)` 是外键,照写会当场报 FOREIGN KEY —— 被
                # 调用方记成一次失败,而文章是用户主动清理掉的。
                return "等锁期间这一篇被清理掉了", False
            if not disk_body_indexed(conn, path):
                # 磁盘上是新正文、chunks 还是旧的:我们判的是旧那一份。
                return "磁盘上这一篇的正文还没进索引", False
            # ② 索引里那份正文自己换过版了吗。**只要 ②,不要 ①** —— ① 比的是
            # 整个文件的 hash,会把只改 frontmatter 的合法改动也算成"版本不对"
            # (理由见 `_body_moved` 的 file_must_match 那段)。
            moved = _body_moved(conn, path, body_seen, file_must_match=False)
            if moved:
                return moved, False      # 见 docstring:落库之前就拦住
            written = save_promo(conn, path, promo, version_seen=version_seen)
            conn.commit()
        finally:
            if conn.in_transaction:
                conn.rollback()
        if not written:
            # 判据在那条 UPDATE 自己的 WHERE 里(换版),或者这次没拿到结论、
            # 而这一篇已经有结论(兜底的 none 不许覆盖)。
            return "正文已换版或没拿到可覆盖的结论", False
        try:
            sync_promo_tag(conn, path, promo["level"])
        except (OSError, PromoTagNotAligned) as e:
            # 兜底再标一次(幂等):共用入口 `writeback_extraction` 已经为对齐
            # 那一步标过了,而这里收得更宽 —— 异常也可能来自它之前的某一步。
            mark_promo_tag_pending(conn, path, True)
            if on_error is not None:
                on_error(e)
            return "", False
        return "", True
