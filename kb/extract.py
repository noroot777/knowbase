# -*- coding: utf-8 -*-
"""entity/topic/summary/软广 提取。

**软广与「正文空」两道判定的硬约束在 `docs/decisions/promo.md` / `body-gap.md` 第一节;提取结果
与正文版本绑定那条在 `fulltext.md` —— 改这块之前先读。**

两条路:
- pre_tagged 来源(dr-youtube-dig):frontmatter 里已有 entity/xxx、topic/xxx
  标签,直接解析进 entities 表,零成本。
- 其他来源(dr-gzh、gzh):调 LLM API 读正文提取,结果进 DB;用哪个模型看设置
  面板的「管道 · 提取打标」阶段;--writeback 时同步回写 md frontmatter 的
  tags(obs 里可见可点)。

**软广判定搭在这一步,不单开一次调用**:提取是「每篇原文各跑一次、覆盖所有
非 pre_tagged 来源」的唯一一步,正文已经读进 prompt 了,多要一个 promo 字段
只多几十个输出 token。单开一个阶段等于把同一篇正文再买一遍。判定结果落
documents.promo_level,下游(条目化 gate、检索、wiki)一律读这一列。

回写只动 frontmatter 的 tags 列表,正文一个字节不碰 —— 但它**写的是 vault 文件**,
所以整段在 `kb.maintenance` 那把维护锁里,而且锁内先确认这一篇还在(见
`writeback_extraction`)。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from kb.config import SOURCES, VAULT_ROOT
from kb.norm import entity_norm, kind_norm

# 提取走 kb.llm 的 API 通道,用哪家的哪个模型由设置面板的「管道 · 提取打标」
# 阶段决定(kb.llm_stages 的 "extract");没配就用那里登记的内置默认 gpt-5.5。
# 别在这里写死字面量:改模型要能在面板上看见、也能按供应商分开选。

# 提示词、正文截断、调模型与回包归一都在 `kb/extract_llm.py`(700 行上限逼出来
# 的一次拆分,那一块没有模块级可变状态、不碰库也不碰文件)。**在这里原样再导出**:
# `extract.clip_body` / `extract.PROMO_HEAD` / `patch.object(extract,
# "extract_with_llm", …)` 这些写法一个字不用改,调用方函数内 import 的也照旧
# 拿到被打桩的那一份。
from kb.extract_llm import (  # noqa: F401  (原样再导出,见上面那段)
    BODY_HEAD, BODY_TAIL, PROMO_HEAD, PROMO_LEVELS, PROMO_TAIL, clip_body,
    extract_with_llm, judge_promo, normalize_promo)

# feeds 来源那道「要不要买提取」的闸用到的几个零件(700 行上限逼出来的一次拆分,
# 判据本身留在下面的 `pending_docs` —— 它是唯一口径)。**原样再导出**:
# `extract._FEED_DOC_PATH` 这类写法一个字不用改。
from kb.extract_optin import (  # noqa: F401  (原样再导出)
    FEEDS_ALIAS as _FEEDS_ALIAS, FEED_DOC_PATH as _FEED_DOC_PATH,
    attached_feeds as _attached_feeds, feed_kinds as _feed_kinds)

# hard 软广在 obs 里也标出来。doc_tags 在重索引时会按 frontmatter 重建,所以
# 这个 tag 跟 topic/entity 一样要靠 writeback 回写 md 才持久。**它留在这里**:
# 回答的是"回写哪个标签",不是跟模型打交道的事。
PROMO_TAG = "flag/软广"


def _tag_slug(name: str) -> str:
    s = re.sub(r"\s+", "-", name.strip().lower())
    return re.sub(r"[#,\[\]{}|^]", "", s)


def _topic_slug(name: str) -> str:
    """主题词的 slug:比实体多吃掉斜杠。

    `topic/` 之后的那一段会被 wiki 层当成页面文件名(`wiki/topics/主题-X.md`),
    斜杠在那里是目录分隔符。模型偶尔给出候选表外的词(2026-07-16 那篇 InfoQ
    出过 `CI/CD流程`),整个 wiki 阶段就挂在 FileNotFoundError 上。实体名里的
    斜杠是正当的(`shadcn/ui` 这类仓库名),所以只收紧主题这一侧。"""
    return re.sub(r"[/\\]+", "-", _tag_slug(name)).strip("-")


def parse_pretagged(doc) -> dict | None:
    """从已有 tags 解析 entity/topic(pre_tagged 来源的管线产物)。

    条目化来源(dr-video-projects / dr-gzh-projects)的 frontmatter 还带
    project/kind/summary:主实体的 kind、文档 summary 一并取用。"""
    entities, topics = [], []
    meta = getattr(doc, "meta", None) or {}
    proj = str(meta.get("project") or "").strip()
    proj_norm = entity_norm(proj) if proj else None
    proj_kind = kind_norm(str(meta.get("kind") or "")) if meta.get("kind") else None
    for t in doc.tags:
        if t.startswith("entity/"):
            name = t.split("/", 1)[1]
            kind = "other"
            if proj_norm and proj_kind and entity_norm(name) == proj_norm:
                kind = proj_kind
            entities.append({"name": name, "kind": kind})
        elif t.startswith("topic/"):
            topics.append(t.split("/", 1)[1])
    if not entities and not topics:
        return None
    return {"entities": entities, "topics": topics,
            "summary": str(meta.get("summary") or "")}




class StaleBody(Exception):
    """这次提取结果针对的那份正文,已经不是当前那一份了 —— 结果作废。

    不是故障:这一篇留在待提取集合里(`extracted_at` 一个字不写),下一轮按
    新正文重提一次就对了。三条提取路径都把它归到「跳过」那一档,不记失败
    ——记失败会让面板标红,而这里什么都没坏。
    """


def body_fingerprint(text: str) -> str:
    """一份正文的指纹。提取开始时按读到的那份算一次,落库前再比一次。

    口径只有这一份:调用方拿自己手里那份 body 算,`save_extraction` 落库前
    按**索引里此刻**那份算,两个数相等才说明"这个结论还是针对当前正文的"。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def save_promo(conn, path: str, promo: dict, now: str | None = None) -> bool:
    """写软广判定,并同步 doc_tags 里的 flag。

    返回 True = **md 里那行 flag 需要调用方去摘掉**(原来判的是 hard,这次不是)。
    DB 侧这里就清干净了,但 frontmatter 只有 writeback 那条路能碰,而且
    writeback_tags 只会追加;不摘的话,这篇文件之后任何一次改动触发
    upsert_document,doc_tags 都会按 frontmatter 重建,误判的标记又回来
    (第 2 轮审查发现)。改判是小概率事件,所以做成「要不要碰文件」的信号
    返回给调用方,而不是每篇都去读一次 md。
    """
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    old = conn.execute("SELECT promo_level FROM documents WHERE path=?",
                       (path,)).fetchone()
    was_hard = bool(old and old["promo_level"] == "hard")
    conn.execute(
        "UPDATE documents SET promo_level=?, promo_reason=?, promo_at=? WHERE path=?",
        (promo["level"], promo.get("reason") or "", now, path),
    )
    if promo["level"] == "hard":
        conn.execute("INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?, ?)",
                     (path, PROMO_TAG))
        return False
    # 重跑改判时把旧标记摘掉,不然面板上永远挂着
    conn.execute("DELETE FROM doc_tags WHERE path=? AND tag=?", (path, PROMO_TAG))
    return was_hard


def sync_promo_tag(conn, doc_path: str, level: str, stale: bool) -> None:
    """把 md frontmatter 里的 flag/软广 对齐到当前判定(存量补判走这条)。

    存量补判不重做 entity/topic,所以喂给 writeback_extraction 的是一份只有
    promo 的结果——口径仍然共用那一份,不另写一遍。stale 取自 save_promo:
    只有「上一次判 hard、这次不是」才需要去摘那行,正常的 none 文章连文件
    都不用读。
    """
    writeback_extraction(conn, doc_path, {"promo": {"level": level}}, stale)


def _body_moved(conn, path: str, body_seen: str) -> str:
    """这次结果针对的那份正文还是**当前**那一份吗 —— 变了就返回一句原因。

    **口径只有这一份,两个消费点**:落库前 `_require_current_body` 拿它抛
    `StaleBody`,回写前 `writeback_extraction` 在同一把维护锁里拿它判一次布尔。
    两处各写一遍的话,拦下来的和写回去的不是同一批。

    两道各管一种交错时序,少哪一道哪一种就漏过去:

    ①**索引还没扫到当前这份 md**(`documents.content_hash` 对不上文件)。这是
      「导出完成、索引还没跟上」那一档:`fulltext.retire_stale` 在导出之后就把
      这一篇排回了待提取集合,而提取读的是**索引里**那份正文(`store.doc_body`
      拼的是 chunks)—— 旁路的一轮 `kb extract` 正好落在这个缝里,就会拿旧壳
      再提一遍。那一刻 chunks 压根没变,所以 ② 对它是瞎的。
    ②**索引里那份正文已经和我读到的不是同一份了**。这是「旧正文的提取请求
      在途、模型回包晚于全文更新」那一档:中间那次 index 已经把 chunks 换成
      新正文,`content_hash` 跟着对上了文件 —— 所以 ① 对它是瞎的。

    **拦不住的后果是永久的**:落库会把 `extracted_at` 设上,这一篇从此再也不进
    `pending_docs`,摘要、主题、实体永久停在那个跳转壳上,再同步、再索引都不会
    自己纠正(2026-10-09 第 2 轮审查两种时序各三次稳定复现)。

    **md 压根读不出来(清理正把这一篇删掉)不算"版本不对",直接放行。** 这一档
    本来就有人管:`writeback_extraction` 在锁内复查「这一篇还在吗」,落进库的判定
    随后被外键级联一并带走(真半途失败也由下一轮 `remove_missing_documents` 收掉),
    末态干净 —— 那是本来就对的一条路。在这里改判成作废只是把它换了个形状,代价
    却是实打实的:回写压根不会被调到,**于是"回写在锁里、读也不许读"那组回归对它
    不再敏感**(把回写那把锁去掉也照样全绿)。这道判据只回答「我读到的正文还是
    当前那一份吗」,"这一篇还在不在"不是它的问题。
    """
    from kb import store
    from kb.scanner import file_hash
    try:
        text = (VAULT_ROOT / path).read_text(encoding="utf-8")
    except OSError:
        return ""                   # 见 docstring:这一档交给回写那道锁内复查
    if store.doc_needs_reindex(conn, path, file_hash(text)):
        return "索引还没扫到当前这份 md,提取结果作废"
    if body_fingerprint(store.doc_body(conn, path)) != body_seen:
        return "正文已更新,提取结果作废"
    return ""


def _require_current_body(conn, path: str, body_seen: str) -> None:
    """落库之前确认正文版本,对不上抛 `StaleBody`(判据见 `_body_moved`)。

    **这道检查本身给不了保证,得由 `commit_extraction` 把它和写入圈进同一个
    维护锁 + 写事务里。** 单独跑只回答"检查的那一瞬间正文还是不是我读到的
    那份",答不了"我写进去的那一瞬间还是不是" —— 两者之间那个窗口有一份实测
    后果,见 `commit_extraction` 的说明。
    """
    reason = _body_moved(conn, path, body_seen)
    if reason:
        raise StaleBody(f"{reason}: {path}")


def save_extraction(conn, path: str, data: dict, body_seen: str | None = None) -> bool:
    """把一篇的提取结果落库。`body_seen` 给了就先校验正文版本(见 `StaleBody`)。

    **提取是「读正文 → 等几十秒模型 → 落库」,正文在这中间可能整篇换掉。**
    补抓把跳转壳换成全文正是这么一条路:它会把这一篇送回待提取集合,于是
    「模型读走旧壳 → 期间正文换新并重新索引 → 旧结论落库」成了常规时序,
    而落库会把 `extracted_at` 设上 —— 这一篇从此再也不进 `pending_docs`,
    摘要、主题、实体永久停在那个壳上,再同步也不会自己纠正(2026-10-09
    第 2 轮审查两种交错时序各三次稳定复现)。所以落库之前比一次指纹,
    对不上就整份丢掉。

    判据有两道,**各管一种交错时序、缺一不可**(都在 `_body_moved`)。校验和这
    一串写入之间还有个窗口,**那由 `commit_extraction` 把两头圈进同一个维护锁
    + 写事务里关掉** —— 三条读正文的提取路径一律走它,不要在外面自己拼。
    不校验的那几条路(`body_seen=None`)是压根不读索引正文的:`parse_pretagged`
    直接解析 frontmatter,软广补判只改 promo —— 它们没有"基于哪一份正文"可言。
    """
    if body_seen is not None:
        _require_current_body(conn, path, body_seen)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM entities WHERE path=?", (path,))
    for e in data["entities"]:
        name = str(e.get("name") or "").strip()
        if name:
            conn.execute(
                "INSERT OR IGNORE INTO entities (path, entity, kind, norm)"
                " VALUES (?, ?, ?, ?)",
                (path, name, kind_norm(str(e.get("kind") or "")), entity_norm(name)),
            )
    # topic 以 tag 形式统一存(和 pre_tagged 来源对齐)
    for t in data["topics"]:
        t = str(t).strip()
        if t and _topic_slug(t):
            conn.execute(
                "INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?, ?)",
                (path, f"topic/{_topic_slug(t)}"),
            )
    for e in data["entities"]:
        name = str(e.get("name") or "").strip()
        if name:
            conn.execute(
                "INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?, ?)",
                (path, f"entity/{_tag_slug(name)}"),
            )
    conn.execute(
        "UPDATE documents SET extracted_at=?, summary=? WHERE path=?",
        (now, str(data.get("summary") or ""), path),
    )
    # promo 单独一条 UPDATE:pre_tagged 来源走的是 parse_pretagged,产物里没有
    # promo 字段,一并写的话会把源头文章判过的结果覆盖成空。
    # 返回值透传 save_promo 的「md 里的 flag 该摘了」,调用方据此决定碰不碰文件。
    promo = data.get("promo")
    if isinstance(promo, dict) and promo.get("level"):
        return save_promo(conn, path, promo, now)
    return False


def extraction_tags(data: dict) -> list[str]:
    """一篇提取结果该回写进 frontmatter 的 tag(topic/entity,判 hard 再加软广)。

    topic 走 `_topic_slug`(比实体多吃掉斜杠)、entity 走 `_tag_slug`,和
    save_extraction 写 doc_tags 用的是同一对函数——两侧不一致的话,重索引会按
    frontmatter 重建 doc_tags,带斜杠的主题当场回到库里,wiki 阶段照样崩。
    slug 完为空的主题直接丢掉,不写一个 `topic/` 空标签。

    PROMO_TAG 在 slug 之后追加:它本身就是最终形态(`flag/软广`),跟着过一遍
    会被改写成别的东西。
    """
    tags = [f"topic/{_topic_slug(t)}" for t in data.get("topics") or []
            if _topic_slug(str(t))]
    tags += [f"entity/{_tag_slug(e['name'])}" for e in data.get("entities") or []
             if e.get("name")]
    if (data.get("promo") or {}).get("level") == "hard":
        tags.append(PROMO_TAG)
    return tags


def _still_indexed(conn, doc_path: str) -> bool:
    """这一篇此刻还在 vault、也还在库里吗。**只在持着维护锁时问才有意义。**"""
    if not (VAULT_ROOT / doc_path).exists():
        return False
    return conn.execute("SELECT 1 FROM documents WHERE path=? LIMIT 1",
                        (doc_path,)).fetchone() is not None


def writeback_extraction(conn, doc_path: str, data: dict, stale: bool,
                         body_seen: str | None = None) -> bool:
    """把提取结果的 tag 对齐到 md frontmatter,改了文件就刷新 content_hash。

    提取有三条路(管道 `_extract_pass`、`kb extract --writeback`、
    `scripts/extract_parallel.py`),口径必须只有这一份:各写各的话,软广标记
    会只在其中一条路上跟着判定走,另外两条要么写不进 md(重索引即丢)、要么
    改判后摘不掉(重索引又把误判带回库里)——第 1、2 轮审查各栽过一次,
    第 3 轮就栽在这两条旁路上。

    **读文件 → 改 frontmatter → 写回 → 刷 hash 整段在维护锁里**
    (`kb.maintenance`,模型请求留在锁外、不占它)。这里写的是 vault 文件,而
    `writeback_tags` 是「`read_text` 拿一份内存快照 → 过一会儿 `write_text`
    整份写回」——存量清理在这中间把这一篇从 feeds.db / vault / kb.db 三处删干净
    的话,那次 `write_text` 会把**文件重新创建出来**:下一轮索引合法地扫到它、
    upsert 回库,旧文从此搜得到、点得开,而 feeds.db 里已经没有这一条,重跑清理
    只会说「无事可做」——比 index 那条路更糟,那一条下一轮 `remove_missing_documents`
    还收得掉,这一条文件真的回来了,谁也收不掉(2026-10-01 第 2 轮审查两种合法
    时序各实测一次:`kb extract --writeback` 与同步的并行存量提取)。
    **外键级联挡不住这一步**:标签是在删除**之前**写进去并提交的,清理的级联
    删除照样全部成功,它管的是库里那几张表,管不着谁在之后重建文件。

    **取锁之前先把调用方那个写事务提交掉**(见下面 `conn.in_transaction` 那段):
    SQLite 的写锁不认维护锁,攥着写事务等锁会把持锁的清理卡死在它自己的 DELETE 上。

    **锁拿到手之后先确认这一篇还在**(`_still_indexed`)——等锁的这段时间里清理
    很可能正是把它删掉了,那就什么都不做、返回 False(「没改文件」)。不抛异常:
    文章是用户主动清理掉的,把它记成提取失败会让面板标红、谎报一次故障,而判定
    本身早就落库了(随后被外键级联一并删掉,末态干净)。

    **给了 `body_seen` 就在锁内再确认一次「这份 tag 算的还是当前那份正文吗」**
    (`_body_moved`,和落库那道同一份口径)。正常路径上 `commit_extraction` 已经
    把落库和回写圈在同一个锁区间里、中间没有窗口,所以这一道是**这个公开入口
    自己的契约**:谁在锁外隔着一段时间拿一份基于旧正文的结果来回写,都该被挡
    下来 —— 旧 tag 一写进新全文就摘不掉了(`writeback_tags` 只追加),而它还顺手
    刷了 content_hash,下一次索引未必重建(2026-10-09 第 3 轮审查实测:新全文的
    frontmatter 里混着 `topic/link-only` + `topic/ai-coding` 两代主题)。
    同样返回 False、不抛异常 —— 判定要么已经落库、要么由落库那道挡掉了,这里
    没有任何坏事发生,下一轮按新正文重提会写进对的那一批。
    """
    tags = extraction_tags(data)
    need_remove = stale and (data.get("promo") or {}).get("level") != "hard"
    if not tags and not need_remove:
        return False     # 没什么要写的,不必为此读一遍文件
    if conn.in_transaction:
        # **取锁之前把调用方手里那个写事务落盘,这件事由这里负责、不靠调用方自觉。**
        # SQLite 的写锁不认维护锁:攥着它去等锁,持锁的清理就卡在自己的 DELETE 上,
        # 两边互等到 SQLite 的 busy 超时(默认 30 秒)把清理打断 —— 清理报
        # `database is locked`、索引那一步没做完,搜索里还留着刚删掉的旧文
        # (2026-10-01 第 3 轮审查跨进程实测 32.5 秒后报错,末态 documents=1、
        # 全文命中 1,要重试一次才收得干净)。原来这条契约写在**调用方**那一侧,
        # 于是软广补判这个经 `sync_promo_tag` 进来的第四个调用方被漏掉,而回归
        # 只枚举了三条提取路径、全绿照过。改成在共用入口自己 flush:多一个调用
        # 方就少一处可漏的地方。
        # 提前提交不新增副作用 —— 改了文件本来也要在锁内 `conn.commit()`,这里
        # 只是把同一个提交挪早;四个调用方的判定都是逐篇独立、可重跑的,组内
        # 那次提交没有「要么全要么全不」的语义。
        conn.commit()
    from kb.maintenance import maintenance_lock

    with maintenance_lock():
        if not _still_indexed(conn, doc_path):
            return False
        if body_seen is not None and _body_moved(conn, doc_path, body_seen):
            return False            # 见 docstring:旧 tag 贴到新全文上就摘不掉了
        changed = writeback_tags(doc_path, tags) if tags else False
        if need_remove:
            changed = remove_writeback_tag(doc_path, PROMO_TAG) or changed
        if changed:
            refresh_content_hash(conn, doc_path)
            # 在锁里提交:攥着 SQLite 写事务走出锁的话,下一个拿到维护锁的清理
            # 会卡在它的 DELETE 上(SQLite 的写锁不认我们这把锁)。
            conn.commit()
        return changed


def commit_extraction(conn, path: str, data: dict, body_seen: str, *,
                      writeback: bool = True, on_error=None) -> bool:
    """交付一次提取的结果:校验正文版本 → 落库 → 提交 → 回写 md,整段在
    「维护锁 → kb.db 写锁」里。返回回写有没有改文件。

    **这个协调区间就是它存在的理由。** `save_extraction` 里那道版本校验只回答
    "检查的那一瞬间正文还是不是我读到的那份",答不了"我写进去的那一瞬间还是
    不是" —— 校验到第一条 SQL、以及落库到回写之间各有一个窗口,而把正文整篇
    换掉的那条路(补抓 → 翻译 → 导出 → 作废旧提取 → 索引)全程持着**同一把**
    维护锁。两个窗口各有一份实测后果(2026-10-09 第 3 轮审查,各三次稳定复现):

    - **校验到写入**:校验合法通过,随后另一边走完补抓和索引、`extract_stale`
      也清了零,这才放行旧结果落库 —— 末态是新全文配旧结论
      (`summary="只有一条链接和 HN 得分。"`、`entities=["htmx"]`),`extracted_at`
      非空、`pending=[]`,**再同步一轮也不变**。
    - **落库到回写**:旧结果合法落库并提交,期间新全文进了索引、md 里那批旧 tag
      已经被作废摘掉,这才放行回写 —— `topic/link-only` 重新写进新全文的
      frontmatter,而 `writeback_tags` 只追加不摘除、还顺手刷了 content_hash,
      所以下一次索引未必重建它。

    **原来那条兜底论证不成立,记在这儿备查**:`_require_current_body` 曾写着
    「窗口里真被写进去了也不是死局 —— `feed_items.extract_stale` 那个待办只在
    作废**做完**之后才放下,下一轮导出会再作废一次」。那句话的前提是旧保存落在
    作废**之前**;上面第一个时序正好相反 —— 待办已经清零,没有第二次作废。

    **锁序是维护锁 → SQLite 写锁,不许反过来**(见 `kb.maintenance` 和
    `writeback_extraction` 里 `conn.in_transaction` 那两段):攥着一个没提交的写
    事务去等维护锁,持锁的清理会卡在它自己的 DELETE 上,两边互等到 SQLite 的
    busy 超时把清理打断。所以进锁之前先 flush,进锁之后才 `BEGIN IMMEDIATE`。

    **模型请求留在锁外,粒度是每篇各取一次。** 这一趟几千篇要跑几十分钟,把整趟
    圈起来等于攥着锁等几十次 LLM 往返,面板上的同步、定时同步和日报一起冻住。

    **锁拿到手之后先确认这一篇还在**(`_still_indexed`,原来只有回写那一侧问)。
    等锁的这段时间里清理很可能正是把它删掉了,而 `doc_tags` / `entities` 对
    `documents(path)` 都是外键 —— 落库会当场报 `FOREIGN KEY constraint failed`,
    被调用方记成一次提取失败、面板标红,而文章是用户主动清理掉的。安静返回
    False(什么都没写、`extracted_at` 一个字没动)才是这一档的末态。
    把 save 挪进锁里之后这道检查就只能在这儿问:原来它赶在清理之前跑完,所以
    外键那时是合法的。

    `writeback=False` 给 `kb extract` 不带 `--writeback` 的那条路。`on_error`
    收回写阶段的异常:判定已经落库了,回写只是展示层,单篇回写失败不该把这一篇
    记成提取失败 —— 不给就原样抛出去(管道那条路靠外层统一记账)。
    """
    from kb.maintenance import maintenance_lock

    if conn.in_transaction:
        conn.commit()           # 取维护锁之前先落盘,理由同 writeback_extraction
    with maintenance_lock():
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not _still_indexed(conn, path):
                return False    # 见 docstring:等锁期间清理把这一篇删掉了
            stale = save_extraction(conn, path, data, body_seen)
            conn.commit()
        finally:
            if conn.in_transaction:
                conn.rollback()
        if not writeback:
            return False
        try:
            return writeback_extraction(conn, path, data, stale,
                                        body_seen=body_seen)
        except Exception as e:
            if on_error is None:
                raise
            on_error(e)
            return False


def writeback_tags(doc_path: str, new_tags: list[str]) -> bool:
    """把新 tag 合并进 md 的 frontmatter tags 列表。只改 frontmatter,不碰正文。"""
    full = VAULT_ROOT / doc_path
    text = full.read_text(encoding="utf-8")
    m = re.match(r"\A(---\n.*?\n---\n)", text, re.S)
    if not m:
        return False
    fm_block, body = m.group(1), text[m.end():]

    existing = set(re.findall(r"^\s*-\s+(\S+)\s*$", fm_block, re.M))
    to_add = [t for t in new_tags if t not in existing]
    if not to_add:
        return False

    add_lines = "".join(f"  - {t}\n" for t in to_add)
    if re.search(r"^tags:\s*$", fm_block, re.M):
        # 已有 tags: 列表 → 追加到列表末尾(即 frontmatter 结束线前)
        new_fm = re.sub(r"\n---\n\Z", f"\n{add_lines}---\n", fm_block)
    else:
        new_fm = re.sub(r"\n---\n\Z", f"\ntags:\n{add_lines}---\n", fm_block)
    full.write_text(new_fm + body, encoding="utf-8")
    return True


def remove_writeback_tag(doc_path: str, tag: str) -> bool:
    """从 md frontmatter 的 tags 列表里摘掉一个 tag。writeback_tags 的反向操作。

    只删 tags 列表里那一行(格式与 writeback_tags 写进去的一致),正文和其余
    frontmatter 一个字节不碰。文件不在/没有 frontmatter/本来就没这个 tag
    都返回 False,调用方据此决定要不要刷新 hash。
    """
    full = VAULT_ROOT / doc_path
    try:
        text = full.read_text(encoding="utf-8")
    except OSError:
        return False
    m = re.match(r"\A(---\n.*?\n---\n)", text, re.S)
    if not m:
        return False
    fm_block, body = m.group(1), text[m.end():]
    new_fm = re.sub(rf"^[ \t]*-[ \t]+{re.escape(tag)}[ \t]*\n", "",
                    fm_block, flags=re.M)
    if new_fm == fm_block:
        return False
    full.write_text(new_fm + body, encoding="utf-8")
    return True


def refresh_content_hash(conn, doc_path: str) -> None:
    """writeback 改了 frontmatter 后刷新库里的 hash,避免下次 index 无谓重建。
    正文没变(chunker 只看 body),chunks/FTS/向量都无需动。"""
    from kb.scanner import file_hash
    full = VAULT_ROOT / doc_path
    try:
        text = full.read_text(encoding="utf-8")
    except OSError:
        return
    conn.execute("UPDATE documents SET content_hash=? WHERE path=?",
                 (file_hash(text), doc_path))


# ── 「这一篇没有正文」的两档 ────────────────────────────────────────────────────
# 都是 store.doc_body() 返回空串,但下一步完全不同,所以不能合成一句话:
#   NO_BODY_EMPTY  md 正文自己就是空的 —— 多半是抓取失败/转载 stub 留下的壳,
#                  重抓原文有机会补上,该进「需回填」清单。
#   NO_BODY_NO_TEXT md 正文有内容,只是一个文字都没有(整篇是一张长海报图、
#                  一张图里套着的表格、纯嵌入块)。chunker 剥掉图片引用之后
#                  不剩字,于是一个 chunk 都不产生。**回填脚本救不了它** ——
#                  重抓回来还是同一张图,内容在像素里。
# 不分档的后果实测过:2026-06-29 那篇「阿里硬核少年技术节」是一张 1080x7355
# 的活动海报,正文容器里 1 个 <img>、0 个字。它被当成「正文为空,需回填」整整
# 半个月,每一轮同步重报一次、把 extract 阶段标红,而回填每次都在白跑。
NO_BODY_EMPTY = "empty"
NO_BODY_NO_TEXT = "no_text"


def body_gap(path: str, body: str) -> str:
    """正文为空时回答「空成哪一档」;正文非空返回空串。

    **判据是「md 正文照 chunker 那套规则剥完还剩不剩字」,不是「md 正文是不是
    空的」,也不是「有没有图片」。** 用的就是 chunker 自己那个 strip_markup ——
    「为什么一个 chunk 都没有」只能由决定 chunk 存不存的那条规则来回答,另写一份
    必然漂。按图片特判同样不行:图里套着的表格、纯嵌入块是同一种形态。

    剥完**还剩字**却一个 chunk 都没有是第三种形态(正文标题里的字被 chunker
    当成小节标题、小节正文清空之后整段丢掉了):那是**真有文字**,落终态等于
    把看得见的内容藏起来、从此不再提取 —— 一律按最保守的 EMPTY 办(继续进
    「需回填」清单,维持本档出现之前的行为)。读不到文件同理落 EMPTY。

    **「空没空」要在剥掉阅读装饰之后问,整个判定只认 normalize_body 之后那份
    正文。** 索引看到的就是它(`scanner.Doc.body`),所以「为什么一个 chunk
    都没有」也只能问它;拿装饰前的原文判空,等于这一个函数的两半各用一份口径
    —— 而 `wikilib.linkify_doc` **对正文为空的壳子照样追加图谱尾区**,于是
    「什么都没有」被那行 `<!-- kb-graph -->` 顶成非空、落进 no_text 终态:
    一篇真的需要回填的空文章从此不再提醒、也不再提取(2026-10-09 第 2 轮审查
    实测,真走一遍 linkify_doc 的空壳 pending=[]、no_body_hash 等于
    content_hash)。图片那一档不受影响:尾区剥掉之后 `![](…)` 还在。
    """
    if body.strip():
        return ""
    from kb.chunker import strip_markup
    from kb.scanner import normalize_body, parse_frontmatter
    try:
        raw = (VAULT_ROOT / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return NO_BODY_EMPTY
    _fm, md_body = parse_frontmatter(raw)
    md_body = normalize_body(md_body)              # 先剥双链与图谱尾区
    if not md_body.strip():
        return NO_BODY_EMPTY                       # 真的什么都没有
    return NO_BODY_EMPTY if strip_markup(md_body).strip() else NO_BODY_NO_TEXT


def mark_no_body(conn, path: str, version: str) -> bool:
    """记下「**这一版**内容确认没有可提取的文字」,让它离开提取队列。

    存的是判定时读到的那份 content_hash,所以这不是永久封死:回填脚本补上正文、
    用户手改 md、源内容变化都会让 index 写进一个新的 content_hash,下一轮
    pending_docs 立刻又选得到它(口径见那个查询的 NOT 子句)。

    **判据待在 UPDATE 自己的 WHERE 里,不许在 Python 那边先读一次 content_hash
    再比。** 判定是在工作线程里做的,落终态在主线程,这中间另一条连接的索引完全
    可能把这一篇换成一版有正文的内容(提取不持维护锁,这是合法交错)。原来那句
    `SET no_body_hash = content_hash` 无条件复制当下的版本号,于是终态被盖到了
    **新**版上:新正文既不提取也不提醒,后续同内容的索引也不会让它回队列
    (2026-10-09 审查实测:库里正文是真的,而 pending=[]、extracted_at=null、
    no_body_hash 与新 content_hash 相等)。先读后写在两个线程之间压根不成立 ——
    SQLite 把写入串行化,判据只有待在写语句里才和这次写入同生共死。

    返回「写进去了没有」:0 行 = 这期间内容换版了,**这一轮不记终态**,
    也别把新内容排除出队列,下一轮按新版重新判一次。
    """
    cur = conn.execute(
        "UPDATE documents SET no_body_hash = ? WHERE path = ? AND content_hash = ?",
        (version, path, version))
    return cur.rowcount > 0


def pending_docs(conn, limit: int = 0) -> list:
    """还没提取过的、需要走 LLM 的文档(pre_tagged 来源除外)。

    排掉「这一版内容已经确认没有文字可提取」的那些(见 mark_no_body)。比的是
    hash 而不是一个标记位:内容一变就自动回到队列。`no_body_hash <> ''` 不能省
    —— 少了它,万一某篇的 content_hash 也是空串,两个空串相等会把它悄悄排掉。

    **feeds 那几类来源(rss/social/web/digest)默认不提取**,只放行显式开了
    `subscriptions.extract` 的订阅(见 `docs/decisions/extract-optin.md`)。绿泡泡
    和别的非 feeds 来源不受这道闸影响,照旧全量进队列。一个条目可能同时属于
    几条订阅,判据是**任一归属订阅开着就提取** —— 漏提取可恢复(开关一开就自动
    回队列),静默多提取则会往标签里灌噪声。

    这个函数是**唯一口径**:一处改动同时治提取队列和面板那个「当前待处理 N 项」
    (`pipeline.pending_counts` 数的就是它)。

    feeds.db 读不到时**抛**,不静默放行也不静默拦死 —— 调用方那三条路各自
    都看得见(管道落红、面板那个数显示不出来、重试计划 500 改成报原因)。
    只有「候选里压根没有 feeds 来源的文档」那一档不去碰 feeds.db:全新安装、
    纯绿泡泡的库本来就没有那个文件,为它抛是在报一个不存在的故障。
    """
    pre_tagged_sources = {s.name for s in SOURCES if s.pre_tagged}
    ph = ",".join("?" * len(pre_tagged_sources))
    base = f"""
        FROM documents
        WHERE extracted_at IS NULL AND source NOT IN ({ph})
          AND NOT (no_body_hash <> '' AND no_body_hash = content_hash)
    """
    args = list(pre_tagged_sources)
    feed_kinds = sorted(_feed_kinds())
    kph = ",".join("?" * len(feed_kinds))
    # 先问一句「这一批候选里有没有 feeds 来源的」。有才去碰 feeds.db ——
    # 这一句顺带让纯绿泡泡的库完全不付跨库的代价(实测整条判据 5ms,但
    # 这个函数坐在面板每次刷新那条路上)。
    has_feed = feed_kinds and conn.execute(
        f"SELECT 1 {base} AND source IN ({kph}) LIMIT 1", args + feed_kinds).fetchone()
    gate = ""
    if has_feed:
        gate = f"""
          AND (source NOT IN ({kph}) OR path IN (
                SELECT {_FEED_DOC_PATH}
                FROM {_FEEDS_ALIAS}.subscriptions s
                JOIN {_FEEDS_ALIAS}.subscription_items si ON si.subscription_id = s.id
                WHERE s.extract = 1))
        """
        args = args + feed_kinds
    sql = f"SELECT path, source, title, account {base} {gate} ORDER BY published DESC"
    if not has_feed:
        rows = conn.execute(sql, args).fetchall()
    else:
        with _attached_feeds(conn):
            rows = conn.execute(sql, args).fetchall()
    return rows[:limit] if limit > 0 else rows


def pending_promo_docs(conn, sources: list[str], limit: int = 0,
                       accounts: list[str] | None = None,
                       recheck: bool = False) -> list:
    """已提取过、但还没做软广判定的文档(存量补判用)。

    新入库的文章在提取时就顺带判了,这个查询只会捞出「软广判定上线之前」
    提取的那批存量;补完一次就永远是空的。

    `recheck=True` 时连已判过的一起捞 —— 判定口径改了之后要让存量跟上,
    靠的就是这条路(改口径而不重判,等于新文章一套标准、存量另一套)。
    它**覆盖**已有的 promo_level,所以默认是关的,而且调用方(补判脚本)还要求
    必须同时给 `accounts`:无目标的全库重判是 9000 多篇、一次把所有历史结论
    连人工核对过的那些一起盖掉,不该是一个标志位就能触发的事。

    `accounts` 给了就只看这几个号。空列表当没给(别把它解释成「哪个号都不要」:
    那会让调用方的一次参数失误静默变成「一篇都不判」,而脚本照旧报成功)。
    """
    where = [f"source IN ({','.join('?' * len(sources))})"]
    args = list(sources)
    if not recheck:
        where.append("promo_level = ''")
    if accounts:
        where.append(f"account IN ({','.join('?' * len(accounts))})")
        args += list(accounts)
    sql = (f"SELECT path, title, account FROM documents"
           f" WHERE {' AND '.join(where)} ORDER BY published DESC")
    rows = conn.execute(sql, args).fetchall()
    return rows[:limit] if limit > 0 else rows
