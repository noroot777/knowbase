# -*- coding: utf-8 -*-
"""手动收录的**库里那一条**:它此刻是什么状态、欠哪一步、怎么修。

从 `kb/feeds/grab.py` 拆出来(那一份写到 673 行,本文件这一族正好是「读与修」
那一半,`tests/test_grab_store.py` 早就是按这条边界分的)。**硬约束在
`docs/decisions/link-grab.md` 第一节**,这里只记各函数自己的那几句。

抓取与入库在 `grab.py`,它把这几个名字**再导出**(`grab.known`、`grab.owing`、
`grab.replace`、`grab.reclaim_export`、`grab.KIND` 照旧可用)—— 所有调用方和
文档都按那几个名字引用,改口会把一堆注释和决策文档一起带脏。

`KIND` 定义在这里而不是 `grab.py`:`known` 的内容质量那一问要用它,而
`grab.py` 要 import 本模块 —— 反过来写就是循环 import。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from kb.feeds import grab_page, registry as reg

#: 这一类来源的 id。`kb.source_types.SOURCE_TYPES` 里那一条。
KIND = 'inbox'


#: 替换时「变了没有」和「这是哪一版」都按这几列算 —— 它们**就是导出负载**:
#: vault 里那一篇的 frontmatter 和正文全从这几列来(`sync._export` 的 meta + 正文)。
#: 少算一列的后果不是「晚一点更新」:只改标题那一次 `content_hash` 不变 →
#: `exported_hash` 仍然相等 → 导出一条都选不中,文件和索引永远停在旧标题,而
#: `known` 按两个 hash 相等报 `done`、面板照旧说「全文检索里搜得到了」(第 4 轮
#: 审查顺序执行即复现:列表里已经是新标题,搜新标题 0 条、搜旧标题 1 条)。
_VERSION_FIELDS = ('title', 'body', 'author', 'published')


def item_version(fields: dict) -> str:
    """这一条**此刻这一版**的身份(`feed_items.content_hash` 在替换这条路上的口径)。

    **口径只有一份**,两处消费:`replace` 的早退(「和库里那一份一样吗」)、以及
    它写进去的 `content_hash`(导出闸门 `exported_hash<>content_hash` 的那一端)。
    各写一遍的话其中一处迟早少算一列 —— 「早退少算」把一次真实更新说成「和它
    一样」,「hash 少算」把一次真实更新藏在导出闸门外面,**两头都不报错**。

    和另外两份口径的分工:`registry.put_items` 哈希的是**整份 entry**(首次入库,
    那时还没有「哪几列会被导出」这一问);`fulltext.body_hash` 是**正文自己的**
    指纹(补抓那条路只换正文)。三份算的东西不同,所以**不许拿它们互相比较** ——
    `replace` 的 CAS 比的是库里那一列的**原值**(口径无关),不是重算一遍再比。
    """
    payload = {k: fields.get(k) or '' for k in _VERSION_FIELDS}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()


def work_version(row) -> tuple[str, int]:
    """这一行**此刻是哪一版** —— 凡是「先读一眼 → 等网络/等模型 → 再写回」的工作,
    取样和提交都过这一个口径。

    **两个入口共用它,只给一个带判据就是只修了一半:** 后台那一轮正文补抓
    (`fulltext.fetch_pending` → `_save`/`_fail`)和手动收录那颗「重新抓一次」
    (`grab_api._recover` → `replace`)是同一个模式 —— 都先拿一份快照,再去原文链接
    等几十秒,最后把结果写回来。只补抓那头带判据的后果实测过:两个页面先后重抓同一
    条链接,一个还在等,另一个把标题改成 BETA 又改回 ALPHA(两次都换成了)—— 等第一
    个回来时 `content_hash` 已经复原,它那份**更早**的抓取又被接受,文章换成旧内容、
    面板照旧报「已经在知识库里了」,md 和全文检索跟着落了它。

    **`content_rev` 是充分那一道** —— 一个**只增、不复用**的代次,换内容的两个写入
    方自己加一(`grab_store.replace`、`_save`)。它挡的是**内容指纹回到原值**那一
    类:手动重抓把标题改成 BETA、再改回 ALPHA 之后,`content_hash` 逐字回到取样那
    一版 —— 可这期间**两次更新都已经被接受过**,而按 hash 相等判的那一版补抓又拿
    回了交付权,把用户刚抓到的那一版换成它那份旧内容(审查用 Event 卡在补抓返回处
    确定性复现:面板最后说「这条已经在知识库里了」,而 md 和全文检索里都是旧正
    文)。**指纹不碰撞也会自然重复,所以这不是"再哈希一列、换个更长的摘要"能解决
    的问题** —— 要的是一个不可复用的身份。

    **`content_hash` 那一道是冗余带,别删。** 它多挡的是「不碰代次的写入方换掉了
    内容」那一类:存量复查的 `scripts/feed_fulltext_recheck._downgrade`(它自己的
    CAS 是 `AND body=?`)和 `scripts/x_article_backfill`(它自己的 CAS 是
    `content_hash`)都换内容、都不动 `content_rev`。**如实记一笔:去掉它一条用例
    都不红** —— 那两个写入方必然换这一列,而它们之后那几种末态都还能自己收回来
    (降级过的条目下一次 scan 照旧选得中,回填过的条目正文非空、不再进那个脚本的
    射程),代价只是多抓一轮。留着的理由是**判据宁可多拒**(元规则:漏报是静默丢
    数据,误报顶多重抓一次),以及哪天出现一个只换内容、不碰代次的新写入方时,这
    一道还兜着。`_save` 自己也加一是**同一条口径的一致写法**(换内容就加一),不
    是因为它那一侧另有可达的交错。

    不比的那几列按它们各自的问题留在原处(元规则:口径只有一份,但两个不同的问
    题不许合成一份):`translate._save` 比的是**它这趟翻译的输入**(标题+正文),
    `sync._export` 的收尾确认比的是**导出负载**(文件内容只由那四列决定,所以
    内容回到原值时确认旧文件是对的),`retire_stale` 比的是**那笔待办属于哪一版
    内容**(它做的事按 path 清,与代次无关)。
    """
    return (row['content_hash'], row['content_rev'])


def _indexed(path: str) -> bool:
    """vault 里那一篇**当前这一版**进全文索引了没有 —— 也就是「现在搜得到它吗」。

    **判据是版本,不是「`documents` 里有这条路径」。** 后一问只答得出「以前索引
    过某一版」:导出把新正文写进同一个路径之后那一行照旧在,于是「库里还是上一
    版」被当成走完了,后半条链再也没人补,而面板上写着「已经在知识库里了」
    (2026-10-16 第二轮审查)。

    口径借现成那一份,这里一个字都不自己拼(元规则「口径只有一份」):
    `scanner.file_hash` 算的就是 `documents.content_hash`,
    `store.doc_needs_reindex` 就是索引自己判「这一篇要不要重建」那一问。

    **只读打开 kb.db,一个字节都不写。** `store.connect()` 这一路走不得:它建表、
    跑迁移,那是写 kb.db 的入口(要先取维护锁,见 `maintenance-lock.md`),而这里
    只要回答一句话。地址用 `db_uri` + `?mode=ro`(口径同
    `extract_optin.attached_feeds`:路径里的 `#`/`?` 不会被当成 fragment/query
    截断、而且物理上建不出任何东西)。`config` 那两个路径**现读**,不绑死在默认
    参数上 —— 否则测试指不到临时库(理由同 `fulltext._drop_extraction`)。

    **判不准一律答「没进」**:代价是多跑一轮 export/index(两个阶段对已经走完的
    条目都是空转),而答反了用户就以为搜得到。
    """
    if not path:
        return False
    from kb import config, store          # 延迟导入:别让 kb/feeds 在导入期拖进 sqlite_vec
    from kb.scanner import file_hash, read_md_text
    if not config.DB_PATH.exists():
        return False
    try:
        text = read_md_text(config.VAULT_ROOT / path)
    except OSError:
        return False                       # vault 里还没有这一篇(导出没跑/文件没了)
    try:
        conn = sqlite3.connect(store.db_uri(config.DB_PATH) + '?mode=ro', uri=True, timeout=10)
    except sqlite3.Error:
        return False
    try:
        conn.row_factory = sqlite3.Row     # `doc_needs_reindex` 按列名读
        return not store.doc_needs_reindex(conn, path, file_hash(text))
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _stored(path: str) -> bool:
    """vault 里那一篇**还在吗**。

    单独一问,因为它和「导出过没有」是两件事:清理脚本、同步盘、手动删都会把
    文件拿走,而库里那两个 hash 仍然相等 —— 那时 `sync._export` 的待办判据
    (`exported_hash<>content_hash`)选不中它,补跑一轮什么都不会写(见
    `reclaim_export`)。
    """
    from kb import config

    try:
        return bool(path) and (config.VAULT_ROOT / path).is_file()
    except OSError:
        return False


def known(url: str) -> dict | None:
    """这条地址是不是已经在库里了。返回那一条或 None。

    返回里把**三件事各自的事实**都交出去,`done` 是它们的合:
    - `exported`:导出过,而且导出的是库里现在这一版(`exported_hash == content_hash`
      —— 一次正文补抓、一轮翻译回写都会把 `content_hash` 推新);
    - `stored`:vault 里那一篇**还在**(`_stored`);
    - `indexed`:vault 里当前那一份进了全文索引(`_indexed`)。

    **分开交而不是只回一个布尔**:调用方要据此决定「这一条还欠哪一步、那一步
    能不能靠补跑一轮补上」(`reclaim_export`、`owing`、`/api/grab/state`)。
    只回一个 `done=False` 的那一版,所有欠账都只能用同一个动作去撞 —— 实测
    形态:文件被删而 hash 仍相等,补跑的 export 一个字不写、index 正确地把旧
    索引收掉,两个阶段双 ok,而界面照着阶段结果宣布「已写入知识库」
    (2026-10-16 第 3 轮审查)。

    **「`feed_items` 里有这一行」只说明内容暂存下来了。** 后半条链(导出 →
    索引 → 提取)跑没跑是另一件事:占不到管道、服务在保存之后重启、导出或索引
    自己失败,都会留下一条「在库里但搜不到」的记录。把这两件事混成一句「已经在
    知识库里了」就是谎报 —— 用户搜不到,而再投一次只会再得到同一句话,后半条链
    永远没人补。**「以前走完过」也不算**:版本推新之后那一篇仍然欠一轮导出和索引。

    **这不是一道互斥**:真正防重复的是 `put_items` 的
    `ON CONFLICT(key) DO NOTHING`(同一类来源)—— 这里多答一句是为了跨来源的
    那种重复(一条推文已经被「大V观点」收过了),否则用户会得到第二篇一模一样的
    文章躺在另一棵树下。

    **只问 `feed_items`**:它覆盖全部订阅类来源(含手动收录自己)。公众号那棵树在
    kb.db 里、不在这张表上,所以一篇真能直接读到正文的绿泡泡文章(`classify` 不
    再按域名拒它)这里答不出「收过了」,它会在收件桶里再落一份 —— 代价和取舍写在
    `classify` 的注释里。
    """
    with reg.db() as c:
        row = c.execute('SELECT key,kind,title,author,published,path,url,body,content_kind,'
                        # `content_rev` 跟着交出去:快照要能回答「我读到的是哪一版」,
                        # 不然显式重抓那条链提交时只比得了指纹(见 `work_version`)。
                        'content_hash,exported_hash,content_rev,x_post_meta'
                        ' FROM feed_items WHERE url=? LIMIT 1', (url,)).fetchone()
    if not row:
        return None
    seen = dict(row)
    # **「写进去了」和「写进去的是一篇文章」是两件事。** 后者由内容自己回答,
    # 随时重算得出(`body_confirmed`),所以不必在表上多存一列标记 —— 老库里
    # 那些以前漏放存下的坏内容也因此一样救得回来(见 `replace`)。
    #
    # **这一问只对「我们按文章正文收下的网页」成立。** 那道判据的门槛借的是正文
    # 提取器的 `MIN_CHARS`(400),而推文、动态、订阅源给的摘要本来就短 ——
    # 拿它去判一条三十个字的推文,等于把所有短内容都说成坏内容:重复投递时会
    # 白跑一次重抓,面板上还写着「收下的那一份看不出是正文」。
    # 别的来源另有自己的「壳 / 空正文」判据(`body-gap.md`、`fulltext`),这里
    # 不替它们判;所以这一问限在**手动收录自己**那一类、且 `content_kind` 是
    # `content`(网页正文)的条目上。
    seen['content_ok'] = (seen['kind'] != KIND or seen['content_kind'] != 'content'
                          or grab_page.body_confirmed(seen['body']))
    seen['exported'] = bool(seen['path'] and seen['exported_hash']
                            and seen['exported_hash'] == seen['content_hash'])
    seen['stored'] = _stored(seen['path'])
    seen['indexed'] = seen['stored'] and _indexed(seen['path'])
    seen['done'] = seen['exported'] and seen['stored'] and seen['indexed']
    return seen


def owing(seen: dict) -> str:
    """这一条还欠哪一步 —— 一句人话。走完了返回空串。

    **只有这一份口径**:重复投递那句提示、`/api/grab/state` 的回答、界面收尾那
    句话都用它。各写一遍的话,其中一处迟早把「文件不见了」说成「还在排队」。
    """
    if not seen:
        return '这条还没收进来'
    if not seen.get('content_ok'):
        # **排在最前面:这一条欠的不是哪一步,是内容本身。** 后面几步全做完也
        # 只是把一份看不出是正文的东西写进了知识库(报告原话:已导出、已索引
        # 不是内容质量证明)。出路是重抓 + 条件替换,不是再跑一轮导出。
        return '收下的那一份看不出是正文'
    if not seen.get('exported'):
        return '还没写成 vault 里那一篇'
    if not seen.get('stored'):
        return 'vault 里那一篇不见了'
    if not seen.get('indexed'):
        return '还没进全文索引'
    return ''


def replace(seen: dict, entry: dict) -> bool:
    """库里那一份看不出是正文 → **用这次重抓到的内容把它换掉**。返回换没换成。

    这是 `DO NOTHING` 之下唯一的出路。手动收录这条路上「错存」有两种来路:以前
    那一版判据漏放的(1832 字的登录提示页实测过)、以及那一次抓到的本来就是一道
    我们认不出的墙。两种都不可能靠「再投一次」自愈 —— 查重会先命中,
    `put_items` 一个字节都不会动,于是用户永远拿到同一句「已经收过了」。

    **谁来叫它,是两个互不依赖的入口**(`grab_api._recover`):库里那一份看不出是
    正文,或者**人明确要求重抓**。后者不许以前者为前提 —— 质量那道判据只认得出
    我们认得出的坏内容,它漏放的那些(段段各不相同的会员说明页、历史那些
    `content_kind='post'` 的 X 残片)从此 `content_ok` 恒真,恢复要是也架在它上面
    就一辈子换不回来(第 3 轮审查实测)。

    **只在确认得了的内容上替换**(调用方保证:能走到这里的 `entry` 刚过完
    `gate_reason` 那六道),而且**判据待在 UPDATE 自己的 WHERE 里** ——
    `content_hash` 要还是我们读到的那一版;`rowcount == 0` 说明这期间别人已经
    动过它(一轮正文补抓、一次存量复查、另一条并发投递),那时不覆盖是对的,
    不是失败(元规则「0 行不是失败」)。

    **写哪几列照 `fulltext._save` 那一份口径**(元规则「口径只有一份」),它把
    换正文该连带的事都想过了(**只有 `content_hash` 算法不照它** —— 这条路还会
    换标题、作者、日期,口径见 `item_version`):`content_hash` 跟着内容变(导出闸门是
    `exported_hash<>content_hash`,所以**不许清 `exported_hash`** —— 那是「这一篇
    入没入过库」的事实来源,清掉会让面板上一篇正读得好好的内容连标题一起消失);
    `extract_stale=1` 留一个待办(知识库里那份摘要、主题、实体是按旧内容算的);
    译文整组作废重排(旧译文译的是那张墙)。`content_kind` 不动 —— 一条推文换了
    正文还是推文。

    seen 必须携带 x_post_meta,如同 content_hash/content_rev 一样不能以缺省值补造。
    known 与回填候选交出的生产快照都满足这一约束;缺列的调用方必须修复取样口径。
    """
    if not seen or not seen.get('key') or not entry.get('body'):
        return False
    # **「变了没有」和「这是哪一版」用同一套列**(`_VERSION_FIELDS`):它们就是
    # 导出负载。早退只比 title/body 的后果是作者、发布时间单独更新被说成「和它
    # 一样」;hash 只算正文的后果是只改标题那一次压根不产生导出欠账(第 4 轮
    # 审查两头都实测到)。entry 没给的那几项沿用库里原值 —— 重抓到的页面可能
    # 读不出作者或日期,那不是「它被清空了」。
    fresh = {'title': entry.get('title') or seen['title'], 'body': entry['body'],
             'author': entry.get('author') or seen.get('author') or '',
             'published': entry.get('published') or seen.get('published') or ''}
    if all(fresh[k] == (seen.get(k) or '') for k in _VERSION_FIELDS):
        # **「一样」按内容比,不按 hash 比。** 三处的 hash 口径本来就不同
        # (`put_items` 哈希整份 entry、`fulltext.body_hash` 只哈希正文、这里是
        # 导出负载),拿它们相等当「没变」会让每次重抓都白推一轮导出和索引。
        return False
    # **判据是取样那一刻的工作身份**(`work_version(seen)`),**代次跟着加一**:
    # 这一次更新被接受之后,一切更早的工作就永远不该再有交付权。只加一、不核对的
    # 后果实测过 —— 两个页面先后重抓同一条链接,等着的那一个回来时指纹已经复原,
    # 它那份更早的抓取照旧被接受(见 `work_version`)。`seen` 一律是 `known()` 交出
    # 来的那份快照,缺了代次就该当场抛,不许退回「只比指纹」。
    with reg.db() as c:
        cur = c.execute("""UPDATE feed_items SET title=?,body=?,author=?,published=?,
            content_hash=?,lang=?,x_post_meta=?,extract_stale=1,content_rev=content_rev+1,
            title_zh='',body_zh='',translated_at='',translate_error='',translate_tries=0
            WHERE key=? AND content_hash=? AND content_rev=?""",
            (fresh['title'], fresh['body'], fresh['author'], fresh['published'],
             item_version(fresh), reg.text_language(fresh['title'], fresh['body']),
             entry.get('x_post_meta', seen['x_post_meta']),
             seen['key'], *work_version(seen)))
        return cur.rowcount > 0


def reclaim_export(seen: dict) -> bool:
    """vault 里那一篇不见了 → **把这笔导出欠账落实下来**,让下一轮 export 真的会
    选中它。返回「这一笔落下了没有」。

    `sync._export` 的待办判据是 `exported_hash<>content_hash`。文件被删掉(清理
    脚本、同步盘、手动删)而两个 hash 还相等时:补跑那一轮 export 一个字都不会
    写、index 反而正确地把它那行旧索引收掉 —— 两个阶段都 ok,而那一篇从此搜不
    到,再投一次又得到同一句话,**反复补跑永远撞不到它**(2026-10-16 第 3 轮审查
    实测)。所以这一步不是「再起一轮」,是先把欠账写成 export 认得的形状。

    **判据待在写语句自己的 WHERE 里**(元规则:别在 Python 那边先 SELECT 一次):
    `rowcount == 0` 说明别人已经动过它、或者两个 hash 本来就不相等(那时 export
    自己就会选中它),都不是失败。

    **清水位不是固化终态,所以这一笔可以放心写**:最坏的后果是把一篇其实还在的
    md 重写一遍 —— 内容由库里那一行算出来、逐字节相同,`content_hash` 没变,索引
    也不会重做。反过来「不清」的代价是那一篇永久搜不到。
    """
    if not seen or not seen.get('path') or not seen.get('exported'):
        return False                       # 没导出过的那一档 export 本来就会选中
    if seen.get('stored'):
        return False                       # 文件还在:欠的不是导出
    with reg.db() as c:
        cur = c.execute("UPDATE feed_items SET exported_hash=''"
                        ' WHERE url=? AND path=? AND exported_hash=content_hash',
                        (seen['url'], seen['path']))
        return cur.rowcount > 0
