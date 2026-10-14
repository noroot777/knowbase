# -*- coding: utf-8 -*-
"""软广判定的落库与交付。**硬约束在 `docs/decisions/promo.md` 第一节,改这块之前先读。**

从 `kb/extract.py` 拆出来的一块(700 行上限逼出来的第二次拆分,手法同
`kb.extract_optin`):**判定怎么下**仍然只有 `kb.extract_llm` 那一份口径,这里
只管判完之后的三件事 —— 落到 kb.db、对齐 `doc_tags`、对齐 md frontmatter。
这三件必须一起成立,所以 `commit_promo` 把它们圈进同一个协调区间。
"""
from datetime import datetime

# hard 软广在 obs 里也标出来。doc_tags 在重索引时会按 frontmatter 重建,所以
# 这个 tag 跟 topic/entity 一样要靠 writeback 回写 md 才持久。**它留在这里**:
# 回答的是"回写哪个标签",不是跟模型打交道的事。
PROMO_TAG = "flag/软广"


def save_promo(conn, path: str, promo: dict, now: str | None = None,
               version_seen: str | None = None) -> tuple[bool, bool]:
    """写软广判定,并同步 doc_tags 里的 flag。返回 `(写进去了没有, 要不要摘 flag)`。

    第二位是 True = **md 里那行 flag 需要调用方去摘掉**(原来判的是 hard,这次
    不是)。DB 侧这里就清干净了,但 frontmatter 只有 writeback 那条路能碰,而且
    writeback_tags 只会追加;不摘的话,这篇文件之后任何一次改动触发
    upsert_document,doc_tags 都会按 frontmatter 重建,误判的标记又回来。
    改判是小概率事件,所以做成「要不要碰文件」的信号返回给调用方,而不是每篇都
    去读一次 md。

    `version_seen` 给了就把它钉进 UPDATE 自己的 WHERE —— **判据待在写语句里,
    不许在 Python 那边先读一次 `content_hash` 再比**,理由和写法同
    `mark_no_body`:判定在工作线程里做,落库在主线程,这中间另一条连接的补抓 →
    导出 → 索引完全可能把这一篇整篇换掉(重判不持维护锁,是合法交错)。那一版
    新正文往往已经由正常提取判过一次,旧请求回来无条件覆盖就把**新**结论擦了,
    而 `extracted_at` 还在、`pending_docs` 是空的 —— 再同步多少轮也不会自己纠正。
    正文和它那一版的号从 `store.doc_body_version` 一条语句里取(那个函数存在的
    理由就是这个),0 行 = 这期间换版了,**这一轮什么都不写、也不动 tag**,
    由调用方如实记成跳过;下一轮按新版重判一次就对了。
    """
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    old = conn.execute("SELECT promo_level FROM documents WHERE path=?",
                       (path,)).fetchone()
    was_hard = bool(old and old["promo_level"] == "hard")
    sql = ("UPDATE documents SET promo_level=?, promo_reason=?, promo_at=?"
           " WHERE path=?")
    args = [promo["level"], promo.get("reason") or "", now, path]
    if version_seen is not None:
        sql += " AND content_hash=?"
        args.append(version_seen)
    if conn.execute(sql, args).rowcount == 0:
        return False, False        # 见 docstring:换版了,tag 也一个都不许动
    if promo["level"] == "hard":
        conn.execute("INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?, ?)",
                     (path, PROMO_TAG))
        return True, False
    # 重跑改判时把旧标记摘掉,不然面板上永远挂着
    conn.execute("DELETE FROM doc_tags WHERE path=? AND tag=?", (path, PROMO_TAG))
    return True, was_hard


def sync_promo_tag(conn, doc_path: str, level: str, stale: bool) -> None:
    """把 md frontmatter 里的 flag/软广 对齐到当前判定(存量补判走这条)。

    存量补判不重做 entity/topic,所以喂给 writeback_extraction 的是一份只有
    promo 的结果——口径仍然共用那一份,不另写一遍。stale 取自 save_promo:
    只有「上一次判 hard、这次不是」才需要去摘那行,正常的 none 文章连文件
    都不用读。

    **这条路不往回写传正文指纹。** 不是忘了:`_body_moved` 的第一道比的是整个
    **文件**的 hash(`documents.content_hash` 对不上磁盘就算"版本不对"),而只
    改 frontmatter 的流程是常态(翻译回写、tag 回写、wiki 追加图谱尾区)——
    传下去的话,一次正文压根没动的合法改判会被判成作废,md 里那行 flag 摘不掉,
    于是库里是 none、frontmatter 还挂着 flag,下一次索引按 frontmatter 重建
    `doc_tags`,错的那行又回来了。补判真正要防的「结论针对的正文已经不是当前
    那份」由两处管:落库那道条件写(`version_seen` 进 UPDATE 的 WHERE),以及
    `commit_promo` 的协调区间 —— 换正文那条路(补抓 → 导出 → 索引)全程持
    **同一把**维护锁,所以落库到回写之间压根插不进一次换版。
    """
    from kb.extract import writeback_extraction

    writeback_extraction(conn, doc_path, {"promo": {"level": level}}, stale)


def commit_promo(conn, path: str, promo: dict, version_seen: str, *,
                 on_error=None) -> str:
    """交付一次软广重判:条件写 → 对齐 `doc_tags` → 提交 → 摘 md 里那行,整段
    在**同一把**维护锁里。返回空串 = 写进去了,否则是一句「为什么什么都没写」。

    **锁序(维护锁 → SQLite 写锁)、进锁之前先 flush、模型请求留在锁外、锁内
    先问一次这一篇还在不在 —— 理由全同 `kb.extract.commit_extraction`**,
    不在这儿复述。这里只说补判多出来的那一条:**`doc_tags` 那行和 md 那行必须
    在同一个协调区间里对齐,中间不许插进一次索引。**

    原来这两段是分开的:`save_promo` 删 `doc_tags` 在锁外,`sync_promo_tag` 摘
    md 在锁内。中间那个窗口里一次**完全合法**的索引就能把改判毁掉 ——
    `upsert_document` 不碰已经改成 none 的 `promo_level`,却按**还没摘**的
    frontmatter 把 `flag/软广` 重新插回 `doc_tags`;随后的回写只摘 md、不会再
    回头清库里那行,还顺手把 `content_hash` 刷成当前文件的 hash,于是下一轮
    索引认为文件没变、直接跳过 `upsert_document` —— 错的那行标签**永久**留在
    库里。末态:`promo_level` 是 none、普通检索正常,但按 `flag/软广` 过滤仍然
    捞得到这篇已经纠正为正常内容的文章,`pending_docs` 是空的,再同步、再索引
    多少轮都不会自己纠正(2026-10-14 审查确定性复现,事后另跑一轮普通索引仍
    报「未变 1」)。

    **这个区间同时关掉了「落库之后、摘 md 之前正文被换掉」那个窗口**,所以
    回写不必再比一次正文指纹(为什么不比,见 `sync_promo_tag`):换正文那条路
    走的是补抓 → 导出 → 索引,全程持同一把维护锁。模型在途那几十秒是另一回事,
    那段在锁外,由 `version_seen` 钉进 `save_promo` 那条 UPDATE 自己的 WHERE。

    `on_error` 收**回写阶段**的异常:判定已经落库了,摘 md 那行只是展示层,
    单篇失败不该让整组判定作废 —— 不给就原样抛出去。
    """
    from kb.extract import _still_indexed
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
                return "等锁期间这一篇被清理掉了"
            written, stale = save_promo(conn, path, promo,
                                       version_seen=version_seen)
            conn.commit()
        finally:
            if conn.in_transaction:
                conn.rollback()
        if not written:
            return "正文已换版"          # 判据在那条 UPDATE 的 WHERE 里
        try:
            sync_promo_tag(conn, path, promo["level"], stale)
        except Exception as e:
            if on_error is None:
                raise
            on_error(e)
        return ""
