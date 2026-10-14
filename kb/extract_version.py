# -*- coding: utf-8 -*-
"""「这次结果针对的那组输入还是当前那一组吗」—— 提取结果和它的输入绑在一起。

从 `kb/extract.py` 拆出来的一块(700 行上限逼出来的第四次拆分,手法同
`kb.extract_optin` / `kb.extract_promo` / `kb.extract_writeback`)。这里只有
**判「还是不是同一组输入」那一族**:指纹怎么算、从哪儿取样、两道判据、落库前
那道断言,外加「这一篇此刻还在不在」。协调(取维护锁、`BEGIN IMMEDIATE`、
落库、回写)仍在 `kb.extract.commit_extraction`。

**要用 `VAULT_ROOT` 一律函数内 `from kb.extract import VAULT_ROOT`**,不在模块
级把它绑成自己的全局:夹具一律打桩 `extract.VAULT_ROOT`(那是生产里唯一的那
一份),绑成本模块的全局就等于在测试里读真实 vault(理由同
`kb.extract_writeback`)。
"""
import hashlib

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


def input_fingerprint(title: str, account: str, body: str) -> str:
    """喂给模型的那**一组**输入的指纹:标题 + 账号 + 正文。口径只有这一份。

    **判定针对的不只是正文。** 软广那一档按「这个号在卖什么」读文末那段,标题
    和账号跟正文一起进 prompt(`extract_llm` 的两个模板),所以「这个结论还是
    针对当前那一份输入吗」必须把三样一起钉上。只钉正文的后果实测过:提取管道
    把标题取自待提取名单、正文取自稍后那个快照,两次读之间另一条连接的索引把
    这一篇整篇换掉,于是模型收到**旧标题配新正文**,判出来的 hard 落在库里 ——
    而按正文指纹比的那道判据看不出任何异常(正文确实是当前那一份),`extracted_at`
    一落这一篇就掉出所有待办:一篇已经改成普通教程的文章被永久当广告隐藏,
    再同步、再索引都不会自己纠正(2026-10-14 审查复现)。

    分隔符用 `\x00`:`normalize_body` 过的正文里不会有它,所以三段拼不出歧义。
    """
    return body_fingerprint("\x00".join((title, account, body)))


def model_input(conn, path: str) -> dict:
    """一篇文章喂给模型的那组输入 + 它的指纹,**三条提取路径的唯一取样点**。

    返回 `doc_input_snapshot` 那四样(title / account / version / body)再加一个
    `seen`(= `input_fingerprint`)。三条路(管道 `_extract_one`、`kb extract`、
    `scripts/extract_parallel.py`)一律从这里取,不要在外面自己拼 —— 各拼一遍
    的话,少传一样给模型(实测:`kb extract` 和并发脚本都没把 account 传进去,
    于是同一篇文章在三条路上判出不同结果)或者少钉一样进指纹,都只在其中一条
    路上出事,而回归枚举另外两条照样全绿。

    **四样必须同属一版**,所以底下那层是一条语句一个快照(见
    `store.doc_input_snapshot`);「这一版还是不是当前那一版」由写入侧回答
    (`commit_extraction` → `_input_moved`、`mark_no_body` 的条件写)。

    这一篇已经不在库里时给一份全空的快照 —— 和原来 `doc_body_version` 对不存在
    的文档返回 `("", "")` 是同一个末态:`body_gap` 把它归到「正文空」,主线程
    按那一档落终态(或者由锁内那道 `_still_indexed` 拦掉),不在这里另造一条路。
    """
    from kb import store
    snap = store.doc_input_snapshot(conn, path) or {
        "path": path, "title": "", "account": "", "version": "", "body": ""}
    snap["seen"] = input_fingerprint(snap["title"], snap["account"],
                                     snap["body"])
    return snap


def _input_moved(conn, path: str, input_seen: str, *,
                 file_must_match: bool = True) -> str:
    """这次结果针对的那组输入还是**当前**那一组吗 —— 变了就返回一句原因。

    **口径只有这一份,三个消费点**:落库前 `_require_current_input` 拿它抛
    `StaleBody`,回写前 `writeback_extraction` 在同一把维护锁里拿它判一次布尔,
    软广补判的交付入口拿它判 ②(见下面 `file_must_match`)。
    两处各写一遍的话,拦下来的和写回去的不是同一批。

    **比的是整组输入(标题 + 账号 + 正文),不是只比正文**(`input_fingerprint`)。
    标题和账号同样进 prompt,换掉任何一样这个结论就不再是针对当前那篇文章的了。

    两道各管一种交错时序,少哪一道哪一种就漏过去:

    ①**索引还没扫到当前这份 md**(`documents.content_hash` 对不上文件)。这是
      「导出完成、索引还没跟上」那一档:`fulltext.retire_stale` 在导出之后就把
      这一篇排回了待提取集合,而提取读的是**索引里**那份正文(`store.doc_body`
      拼的是 chunks)—— 旁路的一轮 `kb extract` 正好落在这个缝里,就会拿旧壳
      再提一遍。那一刻 chunks 压根没变,所以 ② 对它是瞎的。
    ②**索引里那组输入已经和我读到的不是同一组了**。这是「旧正文的提取请求
      在途、模型回包晚于全文更新」那一档:中间那次 index 已经把 chunks 换成
      新正文,`content_hash` 跟着对上了文件 —— 所以 ① 对它是瞎的。

    **拦不住的后果是永久的**:落库会把 `extracted_at` 设上,这一篇从此再也不进
    `pending_docs`,摘要、主题、实体永久停在那个跳转壳上,再同步、再索引都不会
    自己纠正(2026-10-09 第 2 轮审查两种时序各三次稳定复现)。

    **`file_must_match=False` 只关掉 ①,不关 ②。** 软广补判要的是"这个判定针对
    的正文还是当前那一份吗",而 ① 回答的是另一个问题:"整个**文件**都已经索引
    过了吗" —— 它为回写 tag 服务(旧 tag 贴到新全文上就摘不掉了),所以把只改
    frontmatter 的那一类合法改动也算进"版本不对"。补判拿 ① 当判据会把一次正文
    压根没动的改判拦掉,而它真正要防的「磁盘上那组输入还没进索引」由
    `kb.extract_promo.disk_input_indexed` 更精确地回答(它按索引的口径把磁盘上
    的标题/账号/正文各读一遍再和库里比,**别的** frontmatter 字段的差异不影响
    结论)。

    **"读不出来"和"真的不在了"是两档,不是一档。**

    文件真被清理掉(`FileNotFoundError`)照旧放行:那一档本来就有人管 ——
    `writeback_extraction` 在锁内复查「这一篇还在吗」,落进库的判定随后被外键
    级联一并带走(真半途失败也由下一轮 `remove_missing_documents` 收掉),末态
    干净。在这里改判成作废只是把它换了个形状,代价却是实打实的:回写压根不会
    被调到,**于是"回写在锁里、读也不许读"那组回归对它不再敏感**(把回写那把锁
    去掉也照样全绿)。「这一篇还在不在」不是这道判据的问题。

    **别的读取失败一律拒。** 没权限、IO 出错、这一刻正被别人换着写 —— 文件
    都还在,而 ① 答不出「索引扫到当前这份 md 了吗」。**「没读到」不等于「证明
    了没变」**:拿这一档当放行,旧判定就连同旧摘要和 `extracted_at` 一起固化到
    新正文上。实测过的末态是库里 `promo_level=hard` + 旧稿的摘要、chunks 却是
    新版普通教程:这一篇从普通搜索里消失、点开却是新内容,`kb index` + `kb
    extract` 连跑都不恢复(`pending_docs` 已经空了),连「免费修复标签」那条路
    都会照着错的 `hard` 把广告标签补到新版教程上(2026-10-14 审查三次稳定
    复现)。误报的代价只是这一篇下一轮重提一次。

    **② 问在 ① 前面,因为它只读库、一定问得出结果。** 反过来排的话,一次读取
    失败会把这道**能**答出来的判据一起跳过 —— 那正是上面那个末态的来路:
    `except OSError` 直接 return,而那一刻库里的指纹明明已经能证明输入换了。
    """
    # 一条语句一个快照(`model_input` 取样时也是):拆成几句读的话,② 比的
    # 标题和正文可能来自两版,一边对得上一边对不上,结论取决于读的顺序。
    if model_input(conn, path)["seen"] != input_seen:
        return "喂给模型的输入已更新(标题/账号/正文),提取结果作废"
    if file_must_match:
        from kb import store
        from kb.extract import VAULT_ROOT
        from kb.scanner import file_hash
        try:
            text = (VAULT_ROOT / path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""               # 见 docstring:这一档交给回写那道锁内复查
        except OSError as e:
            return f"磁盘上这一份 md 读不出来({e}),证不明索引已经扫到它,提取结果作废"
        if store.doc_needs_reindex(conn, path, file_hash(text)):
            return "索引还没扫到当前这份 md,提取结果作废"
    return ""


def _require_current_input(conn, path: str, input_seen: str) -> None:
    """落库之前确认模型输入那一组还是当前那一组,对不上抛 `StaleBody`
    (判据见 `_input_moved`)。

    **这道检查本身给不了保证,得由 `commit_extraction` 把它和写入圈进同一个
    维护锁 + 写事务里。** 单独跑只回答"检查的那一瞬间正文还是不是我读到的
    那份",答不了"我写进去的那一瞬间还是不是" —— 两者之间那个窗口有一份实测
    后果,见 `commit_extraction` 的说明。
    """
    reason = _input_moved(conn, path, input_seen)
    if reason:
        raise StaleBody(f"{reason}: {path}")


def _still_indexed(conn, doc_path: str) -> bool:
    """这一篇此刻还在 vault、也还在库里吗。**只在持着维护锁时问才有意义。**"""
    from kb.extract import VAULT_ROOT

    if not (VAULT_ROOT / doc_path).exists():
        return False
    return conn.execute("SELECT 1 FROM documents WHERE path=? LIMIT 1",
                        (doc_path,)).fetchone() is not None
