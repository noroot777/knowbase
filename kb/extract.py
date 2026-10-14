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

# 软广那一族的落库与交付(700 行上限逼出来的第二次拆分,手法同上面的
# `extract_optin`)。**原样再导出**:`extract.PROMO_TAG` / `extract.save_promo` /
# `extract.sync_promo_tag` 这些写法一个字不用改,调用方函数内 import 的也照旧
# 拿到被打桩的那一份。判定怎么下仍然只有 `extract_llm` 那一份口径。
# 回写那三件碰 md 文件的事(700 行上限逼出来的第三次拆分)。**原样再导出**:
# `extract.writeback_tags` / `extract.refresh_content_hash` 这些写法一个字不用改,
# 打桩的也照旧打得到。协调(取锁、确认这一篇还在、按判定对齐软广那行)留在本
# 文件的 `writeback_extraction`。
from kb.extract_writeback import (  # noqa: F401  (原样再导出)
    refresh_content_hash, remove_writeback_tag, writeback_tags)
# 输入版本判据那一族(700 行上限逼出来的第四次拆分,手法同上面两块)。
# **原样再导出**:`extract.StaleBody` / `extract.model_input` / `extract._input_moved`
# 这些写法一个字不用改,打桩 `extract._require_current_input` 的也照旧打得到
# —— 本文件里那几处调用的就是这个全局名。
from kb.extract_version import (  # noqa: F401  (原样再导出)
    StaleBody, _input_moved, _require_current_input, _still_indexed,
    body_fingerprint, input_fingerprint, model_input)
from kb.extract_promo import (  # noqa: F401  (原样再导出)
    PROMO_TAG, PromoTagNotAligned, align_promo_tag, commit_promo,
    disk_input_indexed, mark_promo_tag_pending, promo_tag_in_md,
    promo_tag_owed, promo_tag_pending_docs, save_promo, sync_promo_tag)


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


def save_extraction(conn, path: str, data: dict,
                    input_seen: str | None = None) -> None:
    """把一篇的提取结果落库。`input_seen` 给了就先校验模型输入(见 `StaleBody`)。

    **提取是「读正文 → 等几十秒模型 → 落库」,正文在这中间可能整篇换掉。**
    补抓把跳转壳换成全文正是这么一条路:它会把这一篇送回待提取集合,于是
    「模型读走旧壳 → 期间正文换新并重新索引 → 旧结论落库」成了常规时序,
    而落库会把 `extracted_at` 设上 —— 这一篇从此再也不进 `pending_docs`,
    摘要、主题、实体永久停在那个壳上,再同步也不会自己纠正(2026-10-09
    第 2 轮审查两种交错时序各三次稳定复现)。所以落库之前比一次指纹,
    对不上就整份丢掉。

    判据有两道,**各管一种交错时序、缺一不可**(都在 `_input_moved`)。校验和这
    一串写入之间还有个窗口,**那由 `commit_extraction` 把两头圈进同一个维护锁
    + 写事务里关掉** —— 三条读正文的提取路径一律走它,不要在外面自己拼。
    不校验的那几条路(`input_seen=None`)是压根不读索引正文的:`parse_pretagged`
    直接解析 frontmatter,软广补判走自己那个交付入口(`commit_promo` 在落库
    之前把磁盘侧和索引侧两道一起判掉)—— 它们没有"基于哪一组输入"可言。
    """
    if input_seen is not None:
        _require_current_input(conn, path, input_seen)
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
    promo = data.get("promo")
    if isinstance(promo, dict):
        if promo.get("level"):
            # 不传 version_seen:这条路的正文版本由 commit_extraction 在维护锁 +
            # 写事务里校验(`_require_current_input`),比单条语句的条件写更严。
            # **不往上透传「md 里的 flag 该摘了」那个信号了**:它只在「这一次恰好
            # 改判了」时为真,一次回写失败之后就再也没有第二次机会。md 那行现在
            # 按库里的判定幂等对齐(`extract_promo.align_promo_tag`),不看信号。
            save_promo(conn, path, promo, now)
        # **待办不看「这次写进去了没有」,只看「接下来要对齐、而库里有明确
        # 判定」。** 带着 promo 字段来的这一类,回写那一步一定会去对齐 md 那行
        # (`writeback_extraction` 的 `has_promo`),而 `save_promo` 的两档
        # False 都留着一份明确判定要对齐 —— 兜底 none 复用了旧的 hard、换过版
        # 的那一篇库里还是上一份判定。把登记挂在「写进去了」上,这两档就都没有
        # 兜底:实测过兜底那一档,回写撞上一次 PermissionError 之后末态是
        # 库里 hard + md 里没那行 + 待办 0,`sync-tags` 报「待对齐 0 篇」,而
        # `extracted_at` 已经非空 —— 这一篇掉出所有候选,不花钱的那条修复路和
        # 默认重提都不会再选中它(2026-10-14 审查,三条提取入口各实测一次)。
        # 登记在**提取那个事务里**(`commit_extraction` 的 `BEGIN IMMEDIATE`),
        # 所以「提交完 DB、写 md 之前进程没了」也留得住;对齐成功后由
        # `align_promo_tag` 清掉。多登记一次是幂等的,漏登记没人补。
        if promo_tag_owed(conn, path):
            mark_promo_tag_pending(conn, path, True)


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


def writeback_extraction(conn, doc_path: str, data: dict,
                         input_seen: str | None = None) -> bool:
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

    **给了 `input_seen` 就在锁内再确认一次「这份 tag 算的还是当前那组输入吗」**
    (`_input_moved`,和落库那道同一份口径)。正常路径上 `commit_extraction` 已经
    把落库和回写圈在同一个锁区间里、中间没有窗口,所以这一道是**这个公开入口
    自己的契约**:谁在锁外隔着一段时间拿一份基于旧正文的结果来回写,都该被挡
    下来 —— 旧 tag 一写进新全文就摘不掉了(`writeback_tags` 只追加),而它还顺手
    刷了 content_hash,下一次索引未必重建(2026-10-09 第 3 轮审查实测:新全文的
    frontmatter 里混着 `topic/link-only` + `topic/ai-coding` 两代主题)。
    同样返回 False、不抛异常 —— 判定要么已经落库、要么由落库那道挡掉了,这里
    没有任何坏事发生,下一轮按新正文重提会写进对的那一批。
    """
    tags = extraction_tags(data)
    # 带着一个 promo 结论来的,那行 flag 就得按库里的判定对齐一次 —— 不管这次
    # 判的是哪一档、也不管上一次是哪一档(理由见 `extract_promo.align_promo_tag`)。
    # pre_tagged 那条路产物里压根没有 promo 字段,它不碰这一行。
    has_promo = isinstance(data.get("promo"), dict)
    if not tags and not has_promo:
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
        if input_seen is not None and _input_moved(conn, doc_path, input_seen):
            return False            # 见 docstring:旧 tag 贴到新全文上就摘不掉了
        try:
            # **留着原文,不只留它的 hash。** 登记那道边界要拿它回答「文件自己
            # 那几句话(标签、summary、project/kind)动没动」—— 只有 hash 的话,
            # 「库里有、md 上没有」的两种来源分不开(合法的「提取只落库没回写」
            # 和外来编辑刚删掉那行标签),判据只能二选一地放过一种。
            before: str | None = (VAULT_ROOT / doc_path).read_text(
                encoding="utf-8")
        except OSError:
            # 读不出来就交 None:登记那边一律拒绝(第三样证明本来也会 0 行,
            # `hash(before)` 取不出来)。下面那两步本来也会抛、异常才是这一档的
            # 主路径 —— 这里只保证万一走到刷新,也不会替谁签一张「已索引」的收据。
            before = None
        # **记住最后一次真正写出去的那一份整文。** 登记那道边界要它当凭据
        # (`doc_rewrite.register_written` 的第一样证明),事后再读一次磁盘是不
        # 行的:那一份可能已经是别人的了。两步都可能写文件,后写的那一份才是
        # 磁盘上该有的,所以用 `or written` 往后盖、不是往前。
        written: str | None = None
        try:
            if tags:
                written = writeback_tags(doc_path, tags)
            if has_promo:
                written = align_promo_tag(conn, doc_path) or written
        except (OSError, PromoTagNotAligned):
            # **待办不在这里记** —— 它在判定那个事务里就落下了
            # (`save_promo` → `mark_promo_tag_pending`),所以进程在这中间被杀
            # 也留得住;这里只是让异常飞出去。**两步一起圈进这个 try**:hard
            # 那一行是 `writeback_tags` 写的,它抛的时候 md 里同样缺着那行,
            # 而 `align_promo_tag` 压根没机会跑 —— 清待办那一步因此不会发生,
            # 这正是我们要的末态。topic/entity 跟着一起丢不要紧(下一轮重提会
            # 补回来),promo 那行丢了没人补:这一篇已经掉出所有默认候选。
            raise
        finally:
            # **先把已经改掉的文件登记上,再让异常飞出去。** 对齐那一步抛的时候
            # `writeback_tags` 可能已经写过文件了,hash 不刷新只会让下一轮索引
            # 白重建一次(良性);反过来漏刷才是坏的。
            if written is not None:
                reason = refresh_content_hash(conn, doc_path, before,
                                              written)
                if reason:
                    # **没登记上要出声。** 文件安全地留着「待索引」、下一轮重建
                    # 就补上了(良性),可这一轮的输出里不说一句的话,「磁盘长期
                    # 领先于索引」就只能从「每轮都在重建同几篇」里猜。
                    print(f"回写: {doc_path} 改完没登记已索引({reason}),"
                          f"留待下一轮重建")
                # 在锁里提交:攥着 SQLite 写事务走出锁的话,下一个拿到维护锁的
                # 清理会卡在它的 DELETE 上(SQLite 的写锁不认我们这把锁)。
                conn.commit()
        return written is not None


def commit_extraction(conn, path: str, data: dict, input_seen: str, *,
                      writeback: bool = True, on_error=None) -> bool:
    """交付一次提取的结果:校验模型输入 → 落库 → 提交 → 回写 md,整段在
    「维护锁 → kb.db 写锁」里。返回回写有没有改文件。

    **这个协调区间就是它存在的理由。** `save_extraction` 里那道版本校验只回答
    "检查的那一瞬间输入还是不是我读到的那组",答不了"我写进去的那一瞬间还是
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

    **原来那条兜底论证不成立,记在这儿备查**:`_require_current_input` 曾写着
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
            save_extraction(conn, path, data, input_seen)
            conn.commit()
        finally:
            if conn.in_transaction:
                conn.rollback()
        if not writeback:
            return False
        try:
            return writeback_extraction(conn, path, data,
                                        input_seen=input_seen)
        except Exception as e:
            if on_error is None:
                raise
            on_error(e)
            return False


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
