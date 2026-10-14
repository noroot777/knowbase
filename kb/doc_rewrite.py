# -*- coding: utf-8 -*-
"""改一篇已索引 md 的展示层,再登记「它还是索引过的那一份」—— 整段一把锁。

**硬约束在 `docs/decisions/maintenance-lock.md` 第一节 —— 动这块之前先读那一份。**

`documents.content_hash` 那一句写的是**「这份文件已经索引过了」**:索引判「要不要
重建」只看它(`store.doc_needs_reindex`)。所以登记它等于签一张收据,而签错的后果
不可恢复 —— 别人那份还没索引的改动从此永远进不了 chunks,磁盘上读出来是新的、
搜索里是旧的,**再同步、再索引都不会自己纠正**(2026-10-14 审查用真·独立子进程
在两条旁路上都复现了)。

**签这张收据要两样证明,少一样就能造出上面那个末态:**

1. **登记的是本次变换自己写出去的那一份**(写回之后再读一次比对)——
   管「别人插进来改了这个文件」。光有「库里记的还是我动手之前那一份」(条件
   UPDATE 的 WHERE)不够:那只证明**库**没换版,证明不了**磁盘**还是我写的那一份。
   实测过两处旁路脚本在「写完文件 → 刷 hash」之间被另一个进程换掉整篇正文,
   WHERE 合法命中、登记下去的却是那份未索引新文件的 hash。
2. **磁盘上那组索引输入(标题 / 账号 / 规范化后分块的正文)还是索引里那一组** ——
   管「变换自己越界改了索引看得见的东西」。这一道才是「我只改了展示层」这句话的
   **判据**,不是调用方的自述;口径和软广补判那道
   (`extract_promo.disk_input_indexed`)是同一份(`index_input_matches`)。

**两道射程交叠,但哪一道都不多余。** 整篇正文被换掉那一档两道都抓得住(② 发现
磁盘上那组输入变了);**只改 frontmatter 的外来编辑只有 ① 抓得住** —— ② 比的是
**模型输入那三样**,而索引 upsert 进库的不只那三样:`doc_tags`、`url`、`published`
都按 frontmatter 重建,别人只加了一行 tag 那种改动,那三样分毫不差、② 完全看不
出来,认证下去那行 tag 就永远进不了 `doc_tags`,按标签过滤的查询从此照旧捞不到
它。**反过来越界的变换只有 ② 抓得住**:那次写出来的文件**就是**我写的那一份,
① 对它是瞎的。

**① 自己也是两件事叠起来的,顺序别倒**:登记用的 hash 取自 `new_text`(本次写出
去的那一份),**不是**退出时重读磁盘的那一份 —— 原来那条旁路正是重读磁盘来算,
于是别人换上去的内容被盖了章。核对那一步(`on_disk != new_text`)在这之上再挡
一层:少了它,登记会把一个**磁盘上从未存在过**的 hash 写进库(我写的那份已经被
覆盖了),`doc.indexed` 还谎报一句「登记上了」。

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

    标题和账号按 `scanner.frontmatter_input` 读(**和索引 upsert 进库的是同一份
    口径**),正文按 `normalize_body` → `split_chunks` 过一遍再拼回来(**和
    `cli.cmd_index` 同一对函数**)。所以「这个指纹没变」= 现在重新索引一次,
    chunks / FTS / 向量和模型看到的那组输入一个字都不会变。

    **判据是这三样,不是整个文件的 hash。** 后者会把「只改了别的 frontmatter
    字段」(翻译回写 summary、tag 回写、wiki 追加图谱尾区,都是常态)也算成「跟
    索引不一致」,于是一次索引输入压根没动的合法改动永远登记不上,下一轮索引白
    重建一次。

    **反过来:这三样没变不等于「索引什么都不会改」** —— `doc_tags` / `url` /
    `published` 也按 frontmatter 重建。所以它只是登记那张收据的其中一道证明,
    另一道(「登记的是本次写出去的那一份」)管的正是这个缺口,见模块说明。
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

    两个消费点共用它:登记 `content_hash` 之前那道证明(`register_indexed`)、
    软广补判落库之前那道「磁盘上那组输入进索引了吗」
    (`extract_promo.disk_input_indexed`)。各写一遍的话,两处对「索引跟上了没有」
    的回答会各漂各的 —— 而它们判的后果一头是「认证已索引」、一头是「固化一个
    判定」,都不可恢复。

    库里压根没这一行时返回 False:谈不上「进过索引」。
    """
    from kb import store
    from kb.extract import VAULT_ROOT, input_fingerprint

    snap = store.doc_input_snapshot(conn, doc_path)
    if snap is None:
        return False
    return (md_index_input(text, VAULT_ROOT / doc_path)
            == input_fingerprint(snap["title"], snap["account"], snap["body"]))


def register_indexed(conn, doc_path: str, text: str, expect: str) -> str:
    """登记「磁盘上这一份已经索引过了」。返回空串 = 登记了,非空 = 没登记的原因。

    `text` 是调用方**刚写出去**(或刚从磁盘读到)的那一份,`expect` 是动手之前库
    里记着的那个 hash —— 判据待在 UPDATE 自己的 WHERE 里,不在 Python 那边先
    SELECT 一次。两道证明的理由见模块说明。

    **0 行不是失败,正是想要的末态**:这一篇继续带着「待索引」,下一轮重建一次就
    对了(误报的代价只是白重建一次,漏报是静默丢数据)。**所以这里一律不抛**:
    调用方照自己的记账把原因打出来即可。
    """
    from kb.scanner import file_hash

    if not index_input_matches(conn, doc_path, text):
        return "磁盘上那组索引输入已经和索引里那一组不一样了"
    cur = conn.execute(
        "UPDATE documents SET content_hash=? WHERE path=? AND content_hash=?",
        (file_hash(text), doc_path, expect))
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
        """收尾:核对 + 登记 + 在锁里提交。**自己不抛** —— 调用方的异常正在往外飞
        的时候也会走到这里(理由同 `extract.writeback_extraction` 那个 finally:
        已经改掉的文件先登记上,漏登记才是坏的那一侧)。
        """
        if self.new_text is not None:
            try:
                on_disk = self.full.read_text(encoding="utf-8")
            except OSError as e:
                self.reason = f"写回之后读不出来({e})"
            else:
                if on_disk != self.new_text:
                    # 两档都在这儿:别人在这之后改了它,或者我们那次写回自己
                    # 抛在了半路。两档都不许登记。
                    self.reason = "磁盘上不是本次写回的那一份(写回没成,或被改了)"
                else:
                    self.reason = register_indexed(
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
