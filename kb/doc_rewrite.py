# -*- coding: utf-8 -*-
"""改一篇已索引 md 的展示层,再登记「它还是索引过的那一份」—— 整段一把锁。

**硬约束在 `docs/decisions/maintenance-lock.md` 第一节 —— 动这块之前先读那一份。**

`documents.content_hash` 那一句写的是**「这份文件已经索引过了」**:索引判「要不要
重建」只看它(`store.doc_needs_reindex`)。所以登记它等于签一张收据,而签错的后果
不可恢复 —— 别人那份还没索引的改动从此永远进不了 chunks,磁盘上读出来是新的、
搜索里是旧的,**再同步、再索引都不会自己纠正**(2026-10-14 审查用真·独立子进程
在两条旁路上都复现了)。

**签这张收据要三样证明,而且全在 `register_written` 这一个边界上** —— 不在某个
调用者里自证(原来「核对磁盘上还是我写的那一份」写在 `Rewriting._settle` 里,
于是那道证明只有两条旁路有,主回写那条路压根没有):

1. **登记的是本次变换自己写出去的那一份**(调用方交付写出快照,退出前再读一次
   磁盘比对)—— 管「别人插进来改了这个文件」。光有「库里记的还是我动手之前那
   一份」(第 3 样)不够:那只证明**库**没换版,证明不了**磁盘**还是我写的那一份。
   **「刚读到」不能充当「刚写出」**:两处旁路实测过在「写完文件 → 刷 hash」之间
   被另一个进程换掉整篇正文,WHERE 合法命中、登记下去的却是那份未索引新文件的
   hash;主回写那条路更直接 —— 它本来就是事后重读磁盘来当凭据。
2. **这一份现在重新索引一次,库里不会变**(`index_projection_matches`)——
   管「变换自己越界改了索引看得见的东西」。比的是 `store.upsert_document` 真正
   写进库的每一样,**不是**模型输入那三样:后者漏掉 doc_tags / url / published /
   source / 小节标题(审查按字段逐个实测,五种外来编辑全被盖了章)。
3. **库里记的还是动手之前那个 hash** —— 条件 UPDATE 自己的 WHERE,不在 Python
   那边先 SELECT 一次。

**①② 对「外来编辑改了索引投影」那一组互为备份**(任一道单独拿掉,那五个字段
照旧拒得住),但各有自己独有的射程,所以回归里各有一条定点红:① 独有的是「外来
编辑没动索引投影」(② 对它没意见,可收据不该盖到别人那份上);② 独有的是「变换
自己写进去的东西还没落库」(① 对它是瞎的,文件就是我写的那一份)。

**异常契约:这里只保证「不把失败说成成功」,不保证「什么都不抛」。** 登记那几步
自己不抛(0 行、判据不符都走返回值),但 SQL 失败、`commit` 失败、以及读磁盘时
的 UTF-8 解码失败都会原样抛出去 —— 读失败只捕获 `OSError`。退出时还有一档:
写回已经抛了、收尾这一步再抛的话,`finally` 里的新异常会盖掉原来那个。

**①② 对大多数外来编辑互为备份,所以「某一道还在不在」看不出来 —— 想分辨得让
另一道闭嘴。** 外来进程插在**写回之后**那一瞬:① 当场拦下(磁盘不是我写出去的
那一份),② 压根轮不到说话;插在**写入函数读文件之前**:那个改动被带进了我们
自己写出去的全文,① 合法通过,只有 ② 拦得住。回归里两种时点各有一组,不然一道
坏掉照样全绿(自测踩过两次:注入拿掉其中一道,整组绿着)。

**各自独有的射程**:① 管「外来编辑没动索引投影」(② 对它没意见,可收据不该盖到
别人那份上);② 管「变换自己写进去的东西还没落库」「越界改了索引看得见的东西」
(文件就是我写的那一份,① 对它是瞎的)。

**① 自己也是两件事叠起来的,顺序别倒**:登记用的 hash 取自**本次写出去的那一
份**,不是退出时重读磁盘的那一份 —— 主回写原来正是重读磁盘来算,于是别人换上去
的内容被盖了章。核对那一步(`on_disk != written`)在这之上再挡一层:少了它,登记
会把一个**磁盘上从未存在过**的 hash 写进库(我写的那份已经被覆盖了),`indexed`
还谎报一句「登记上了」。

**整段在维护锁里**(读 → 变换 → 写回 → 登记 → 提交):把正文整篇换掉的那几条路
(订阅存盘与导出、索引、存量清理)全程持着**同一把**锁,所以它们压根进不来;上面
那两道证明管的是**不认这把锁的那种写入**(手工编辑、外部工具)——两层各自有射程,
缺哪一层哪一种交错就漏过去。

**一份快照,不是两份。** `rewriting()` 锁内读一次,变换、写回、登记全按它算。
原来 `wiki_daily.step_linkify` 是另一个形状:`linkify_doc` 自己读一次文件算新文本,
外面再读一次算 `before` —— 两次读之间别人的一次写入就让「动手之前那份 hash」指向
一份我们压根没变换过的内容。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path


def md_index_input(text: str, md_path: Path) -> str:
    """一份 md 文本在**索引眼里**那组模型输入的指纹(标题 / 账号 / 分块正文)。

    **这是软广补判那一道的判据,不是登记「已索引」那一道。** 它回答的是「磁盘
    上这一篇喂给模型的那组输入,还是索引里那一组吗」—— 够用来判「能不能固化
    一个判定」(判定只基于这三样)。

    标题和账号按 `scanner.frontmatter_input` 读(**和索引 upsert 进库的是同一份
    口径**),正文按 `normalize_body` → `split_chunks` 过一遍再拼回来(**和
    `cli.cmd_index` 同一对函数**)。所以「这个指纹没变」= 模型看到的那组输入一个
    字都没变。

    **它证明不了「重新索引一次库里不会变」,所以不许拿它签「已索引」的收据。**
    索引 upsert 进库的还有 `doc_tags` / `url` / `published` / `source` /
    `chunk.heading`,而这里把正文**拼成一串**比,分块边界不同也会算成相同。
    那张收据的判据是 `index_projection_matches`(整份索引投影)——审查按字段逐个
    实测过,只比这三样时五种外来编辑全被盖了章。
    """
    from kb.chunker import split_chunks
    from kb.extract import input_fingerprint
    from kb.scanner import (frontmatter_input, normalize_body,
                            parse_frontmatter)

    fm, body = parse_frontmatter(text)
    title, account = frontmatter_input(fm, md_path)
    chunked = "\n".join(c.text for c in split_chunks(normalize_body(body)))
    return input_fingerprint(title, account, chunked)


def index_input_matches(conn, doc_path: str, text: str) -> bool:
    """这份文本那组索引输入,和索引里**此刻**那一组是同一组吗。

    **只有软广补判用它**(`extract_promo.disk_input_indexed`:磁盘上这一篇喂给
    模型的那组输入,进过索引吗 —— 判的是「能不能固化一个判定」)。登记
    `content_hash` 那张收据**不用这一道**,它要的是整份索引等价
    (`index_projection_matches`):模型输入这三样只是索引写进库的一部分,拿它
    签「已索引」会漏掉 doc_tags / url / published / source / 小节标题,而且会把
    分块边界不同当成相同。两道判据各有射程,别互相顶替。

    库里压根没这一行时返回 False:谈不上「进过索引」。
    """
    from kb import store
    from kb.extract import VAULT_ROOT, input_fingerprint

    snap = store.doc_input_snapshot(conn, doc_path)
    if snap is None:
        return False
    return (md_index_input(text, VAULT_ROOT / doc_path)
            == input_fingerprint(snap["title"], snap["account"], snap["body"]))


def source_name_of(doc_path: str, fallback: str) -> str:
    """这篇 md 所属来源的 `src.name` —— 按**路径**认,认不出来才用 `fallback`。

    `doc_from_text` 只在 frontmatter 没写 `source` 时用它兜底。`config.SOURCES`
    里每一条都是 `root = VAULT_ROOT / "source" / name`、`name` 就是那级目录名
    (`tests/test_doc_rewrite_roster.py` 钉着这个等价关系,将来加一条 root 形状
    不同的来源会在那儿红),所以生产里的路径一律推得出来 —— 连「外来编辑把
    `source:` 那行删掉」都判得出(推出来的 `src.name` 和库里记的那个值不同时)。

    **不去遍历 `config.SOURCES` 比 root**:那张表在模块加载时就按当时的
    `VAULT_ROOT` 把 root 算死了,而夹具一律打桩 `extract.VAULT_ROOT` —— 比 root
    会在测试里全部失配,于是判据变成「永远不一致」、合法回写从此一次都登记不上,
    而症状只是「每轮索引都在白重建」,看不出是判据坏了。

    **推不出来就退回库里那个值**(`fallback`,即上一次索引写进去的
    `documents.source`)。那时这一档退化成「fm 写了 source 就比它、没写就算
    一致」:漏的是「fm 原来写的 source 和 `src.name` 不同、这次又被外来编辑删掉
    那行」这一窄档。宁可漏这一档,也不能在「路径不是 `source/x/...` 形状」时
    一律判不一致 —— 那会把合法回写全拦掉(误报的代价是每轮白重建,漏报才是
    丢数据,可这里的误报是**永久**拦住,等于另一种丢)。
    """
    parts = doc_path.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "source" else fallback


def index_projection_matches(conn, doc_path: str, text: str) -> str:
    """这份文本**现在**重新索引一次,库里会变吗。空串 = 不会变,非空 = 哪儿会变。

    这才是「登记 `content_hash` 即声明已索引」那张收据要的判据:比的是
    `store.upsert_document` 真正写进库的**每一样** —— `documents` 的
    source/title/account/published/url、`doc_tags`、以及 chunks 的
    (seq, heading, text) 序列(`chunks_fts` 的两列都由 title 和 heading+text
    派生,比了这几样就够)。投影本身用 `scanner.doc_from_text` 算,**和索引
    走同一个函数**,不自己再读一遍 frontmatter。

    **为什么不能只比模型输入那三样**(标题/账号/拼接正文,`index_input_matches`
    那一份):索引写进库的不只那三样。审查用独立子进程逐个字段实测过,
    外来编辑只改 `doc_tags` / `url` / `published` / `source` / `chunk.heading`
    任意一样,那三样都分毫不差 —— 收据照签,而连跑两趟 `kb index` 都报「未变 1」,
    新标签查不到、小节标题搜不到、原文链接一直是旧的,再同步也不恢复。
    拼接正文还会把**分块边界**不同当成相同(只比 text 的拼接结果),所以这里比
    的是逐块的 (seq, heading, text),不是拼起来的那一串。

    **标签这一样不是集合全等,分两段判,而且这是本函数最容易判错的地方:**

    - **普通标签只比「文件上有的,库里都有吗」这一个方向。** 反方向(库里有、
      文件没有)是**合法状态**:`kb extract` 不带 `--writeback` 时提取结果只落
      `doc_tags`、不回写文件。按全等判它,拒绝登记的代价**不是**「白重建一次」
      —— `upsert_document` 重建 doc_tags 是 `DELETE` 全部再**只从文件**重建,
      那些库里专有的提取标签当场永久没了,`extracted_at` 非空也不会重新提取。
      退让的那一半是「外来编辑**删掉**了 md 上一行标签」:我们照旧登记,库里
      那行留着(查询多捞到一条),等这一篇正文真的变了再重建纠正 —— 比永久吞掉
      一批提取标签轻。
    - **`flag/软广` 那一行按落库那份判定归一化**,规则和索引自己用的是同一份
      (`store.promo_tag_wanted`):hard 就该有、none/soft 就该没有、**还没判过
      (`''`)就跟着文件**。原来这里无条件把它从两边排除,于是还没判过的那一篇
      上,外来编辑增删 md 那行 flag 判据完全看不见 —— 收据照签,而真实索引这时
      是跟着文件的,按这个标签筛选从此停在旧结果上。

    **一个写事务里读完所有表。** 跨三张表没法把判据塞进某一句的 WHERE,所以
    靠 `BEGIN IMMEDIATE`:读和随后那句条件 UPDATE 同属一个事务、同一个快照。
    锁序仍是维护锁 → SQLite 写锁(调用方已经持着维护锁)。
    """
    from kb.chunker import split_chunks
    from kb.config import Source
    from kb.extract import VAULT_ROOT
    from kb.extract_promo import PROMO_TAG
    from kb.scanner import doc_from_text
    from kb.store import promo_tag_wanted

    row = conn.execute(
        "SELECT source, title, account, published, url FROM documents"
        " WHERE path=?", (doc_path,)).fetchone()
    if row is None:
        return "库里压根没有这一行,谈不上「已索引」"

    doc = doc_from_text(
        text, VAULT_ROOT / doc_path,
        Source(name=source_name_of(doc_path, row["source"] or ""),
               root=VAULT_ROOT),
        rel_path=doc_path)
    for col in ("source", "title", "account", "published", "url"):
        if (row[col] or "") != getattr(doc, col):
            return f"磁盘上的 {col} 和索引里那一个不一样了"

    in_file = set(doc.tags)
    in_db = {r["tag"] for r in conn.execute(
        "SELECT tag FROM doc_tags WHERE path=?", (doc_path,))}
    # **普通标签只比一个方向:文件上有的,库里得都有。** 反方向(库里有、文件
    # 没有)是**合法状态** —— `kb extract` 不带 `--writeback` 是明确支持的操作,
    # 提取出的 topic/entity 只落 `doc_tags`、压根不回写文件。判成不一致的后果
    # 不是「白重建一次」:`upsert_document` 重建 doc_tags 是 `DELETE` 全部再
    # **只从文件**重建,那些库里专有的提取标签当场永久没了,而 `extracted_at`
    # 非空、这一篇不会被重新提取回来(2026-10-14 审查实测:一次双链化或一次
    # 软广回写之后,两轮索引下来只剩 `source/*`,按主题和实体过滤全空)。
    if (in_file - {PROMO_TAG}) - in_db:
        return "磁盘上那组标签还没进 doc_tags"
    # `flag/软广` 那一行单独判,规则同 `store._align_promo_tag`(抽在
    # `store.promo_tag_wanted` 里):判过就按判定、还没判过就跟着文件。
    level = (conn.execute("SELECT promo_level FROM documents WHERE path=?",
                          (doc_path,)).fetchone()["promo_level"] or "")
    if promo_tag_wanted(level, PROMO_TAG in in_file) != (PROMO_TAG in in_db):
        return "磁盘上那行软广标记和索引重建之后该有的对不上"

    chunks = [(c.seq, c.heading, c.text) for c in split_chunks(doc.body)]
    indexed = [(r["seq"], r["heading"], r["text"]) for r in conn.execute(
        "SELECT seq, heading, text FROM chunks WHERE path=? ORDER BY seq",
        (doc_path,))]
    if chunks != indexed:
        return "磁盘上那组分块(小节标题或正文)和索引里那一组不一样了"
    return ""


def register_written(conn, doc_path: str, written: str, expect: str) -> str:
    """登记「磁盘上这一份已经索引过了」。空串 = 登记了,非空 = 没登记的原因。

    **`written` 必须是调用方本次真正写出去的那一份,不许是事后重读磁盘的那一份**
    —— 这是三样证明里的第一样,而「刚读到」证明不了「刚写出」。审查用独立子进程
    实测过:事后重读的形状下,别人插在「写完文件 → 刷 hash」之间换上去的内容被
    盖了章。**所有登记都必经这一个边界**,三样一起证:

    ① 磁盘上此刻还是 `written`(退出前再读一次比对);
    ② `written` 现在重新索引一次库里不会变(`index_projection_matches`);
    ③ 库里记的还是动手之前那个 hash(条件 UPDATE 自己的 WHERE,不在 Python
       那边先 SELECT 一次)。

    **登记的 hash 取自 `written`,不取磁盘那一份** —— 这一条和 ① 是一对:只有
    ① 没这一条,登记下去的就是别人那份的 hash(原缺陷的形态);只有这一条没 ①,
    库里会留一个**磁盘上从未存在过**的 hash,而 `indexed` 还谎报一句登记上了。

    **0 行不是失败,正是想要的末态**:这一篇继续带着「待索引」,下一轮重建一次
    就对了。**所以「判据不符」一律走返回值、不抛** —— 调用方照自己的记账把原因
    打出来即可。**但这不等于这个函数不抛**:读磁盘的 UTF-8 解码失败、那几句
    SQL 失败都会原样飞出去(读失败只捕获 `OSError`),见模块说明那条异常契约。

    **「误报」在这里不是一律良性的。** 普通标签那一段之所以只比一个方向,正是
    因为拒绝登记会把这一篇交给下一轮重建,而重建 `doc_tags` 是**破坏性**的
    (`DELETE` 全部再只从文件重建)——详见 `index_projection_matches`。
    """
    from kb.extract import VAULT_ROOT
    from kb.scanner import file_hash

    try:
        on_disk = (VAULT_ROOT / doc_path).read_text(encoding="utf-8")
    except OSError as e:
        return f"写回之后读不出来({e})"
    if on_disk != written:
        # 两档都在这儿:别人在这之后改了它,或者我们那次写回自己抛在了半路。
        return "磁盘上不是本次写回的那一份(写回没成,或被改了)"
    if not conn.in_transaction:
        # 读几张表 + 那句条件 UPDATE 同属一个事务、一个快照。调用方已经在写事务
        # 里时(区间里顺手写过别的列)沿用它那一个:它早就持着写锁,同样是一个
        # 快照,再 BEGIN 会抛「transaction within a transaction」。
        conn.execute("BEGIN IMMEDIATE")
    reason = index_projection_matches(conn, doc_path, written)
    if reason:
        return reason
    cur = conn.execute(
        "UPDATE documents SET content_hash=? WHERE path=? AND content_hash=?",
        (file_hash(written), doc_path, expect))
    if cur.rowcount != 1:
        return "库里记的已经不是我动手之前那一份 content_hash 了"
    return ""


class Rewriting:
    """一个协调区间里的一篇 md。`rewriting()` 造它,调用方只读几个字段 + `write()`。

    `text` 是**锁内读到的那一份快照**(这一篇已经不在了、或者读不出来时是 None,
    原因在 `reason` 里);`changed` / `indexed` / `reason` 在退出协调区间之后才是
    末态。
    """

    __slots__ = ("path", "full", "text", "before", "new_text",
                 "changed", "indexed", "reason")

    def __init__(self, doc_path: str, full: Path):
        self.path, self.full = doc_path, full
        self.text: str | None = None
        self.before = ""
        self.new_text: str | None = None
        self.changed = False       # 文件真的被改写了
        self.indexed = False       # content_hash 登记成功(= 这一份已索引)
        self.reason = ""           # 跳过或没登记的原因

    def write(self, new_text: str) -> None:
        """把变换后的**整份**文本写回。

        **只许真写回一次**:写两次就有两份快照了,而登记那一步只证明得了最后
        那一份 —— 中间那一份算过的 `before` 就成了一个没人验过的值。

        和原文逐字相同时什么都不做(`changed` 留 False,也不算用掉那一次):文件
        没变就没有收据要签,也不该让调用方的计数把它数成「改了一篇」。
        """
        if self.text is None:
            raise RuntimeError(f"锁内读不出来,没有可变换的快照:{self.path}")
        if self.new_text is not None:
            raise RuntimeError(f"一个协调区间只许写回一次:{self.path}")
        if new_text == self.text:
            return
        # **先记下要写的那一份,再写。** 顺序反过来不安全:`write_text` 抛在
        # 截断之后(磁盘满、权限中途变了)时文件已经坏了,而 `new_text` 还是
        # None —— 收尾那一步就什么也不核对、直接放过去了。记在前面的话它会读到
        # 一份对不上的内容,如实留着「待索引」。
        self.new_text = new_text
        self.full.write_text(new_text, encoding="utf-8")
        self.changed = True

    def _settle(self, conn) -> None:
        """收尾:登记 + 在锁里提交。**判据不符不抛**(走 `reason`)—— 调用方的
        异常正在往外飞的时候也会走到这里(理由同 `extract.writeback_extraction`
        那个 finally:已经改掉的文件先登记上,漏登记才是坏的那一侧)。

        **但它确实可能抛**:`register_written` 里读磁盘的解码失败、那几句 SQL、
        以及这里的 `commit` 都会飞出去。写回已经抛了、这一步再抛的话,`finally`
        里的新异常会盖掉原来那个 —— 异常契约见模块说明,不在这儿假装不抛。
        """
        if self.new_text is not None:
            # **三样证明全在 `register_written` 里,这里不自己判。** 原来核对
            # 「磁盘上还是我写的那一份」那一步写在这儿,于是那道证明只在这**一个**
            # 调用者里成立:主回写走的是另一条路(事后重读磁盘),压根没有这一道。
            self.reason = register_written(
                conn, self.path, self.new_text, self.before)
            self.indexed = not self.reason
        # 在锁里提交:攥着 SQLite 写事务走出锁的话,下一个拿到维护锁的清理会卡在
        # 它自己的 DELETE 上(SQLite 的写锁不认这把锁)。调用方在区间里顺手写的
        # 别的列(`backfill_youtube_summaries` 的 summary)跟着这一次落盘。
        conn.commit()


def _default_on_wait(waited: float) -> None:
    """等维护锁时**默认出声**(第一次立刻,之后每 60 秒一次,由锁那边定节奏)。

    这两条旁路原来压根不取锁,所以也从来不会停下来等。现在会了:管道正在同步或
    索引时,它们最长等 `KB_MAINT_LOCK_WAIT`(默认 900 秒)。**一声不出地等 15 分钟
    和卡死长得一模一样** —— wiki 阶段的输出是直接给人看的,linkify 那一步看着
    停住了,人只能去猜是死循环还是在等谁。
    """
    print(f"[rewrite] 等维护锁:另一个 kb.db 维护任务正在跑(已等 {int(waited)} 秒)",
          flush=True)


@contextmanager
def rewriting(conn, doc_path: str, on_wait=_default_on_wait):
    """协调一次「只改展示层」的改写:整段在维护锁里,全程同一份快照。

    用法(两条旁路都是这个形状):

        with rewriting(conn, path) as doc:
            if doc.text is None:
                continue                      # 这一篇已经不在了,或者读不出来
            new_text = 变换(doc.text)          # 只许改不进索引的那些东西
            if new_text is not None:
                doc.write(new_text)
        # 到这儿 doc.changed / doc.indexed / doc.reason 已经是末态

    **锁拿到手之后先确认这一篇还在**(`extract._still_indexed`)——等锁的这段时间
    里存量清理很可能正是把它删掉了,而这里是「整份读改写」:那次写回会把 md 文件
    **重新创建出来**,下一轮索引合法地把它收回库里,而 feeds 行已经没了,重跑清理
    只会说「无事可做」。不抛异常:文章是用户主动清理掉的,不是一次故障。

    **退出时登记 `content_hash` 要两样证明**(写回的那一份还在磁盘上、它那组索引
    输入还是索引里那一组),不满足就**保留待索引**而不是认证,`doc.reason` 说一句
    为什么。理由见模块说明。

    **取锁之前先把调用方手里那个写事务落盘**,退出时在锁里提交 —— 锁序是
    维护锁 → SQLite 写锁,反过来会让持锁的清理卡在它自己的 DELETE 上。

    **等不到锁时 `LockBusy` 原样往外抛,而且这篇一个字节都没动过**(连读都在锁
    里)—— 「等不到锁」的语义就是「什么都没做」,调用方据此自己收场(两条旁路都
    是:把这一步收住,别把整趟带崩)。等的时候默认出声,见 `_default_on_wait`。
    """
    from kb.extract import VAULT_ROOT, _still_indexed
    from kb.maintenance import maintenance_lock
    from kb.scanner import file_hash

    if conn.in_transaction:
        conn.commit()                 # 锁序:见 docstring,这件事由这里负责
    doc = Rewriting(doc_path, VAULT_ROOT / doc_path)
    with maintenance_lock(on_wait=on_wait):
        if not _still_indexed(conn, doc_path):
            doc.reason = "这一篇已经不在库里或 vault 里了"
        else:
            try:
                doc.text = doc.full.read_text(encoding="utf-8")
            except OSError as e:
                doc.reason = f"读不出来({e})"
            else:
                doc.before = file_hash(doc.text)
        try:
            yield doc
        finally:
            doc._settle(conn)
