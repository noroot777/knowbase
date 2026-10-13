# -*- coding: utf-8 -*-
"""外文订阅条目的中文化:抓进来是英文,落进知识库之前先译成中文。

为什么放在导出之前、而不是阅读时现翻:vault 的 markdown 是事实来源,索引、
提取、检索、日报全都读它。阅读时现翻只有那一块屏幕是中文,搜"智能体"仍然
搜不到一篇讲 agent 的英文文章,日报也还是拿英文素材写。

判定不花钱(`registry.text_language` 只看字符构成),只有判成外文的条目才调
模型;一条译一次,结果落在 feed_items 的 title_zh/body_zh 上长期复用。

失败不挡路,但也不抢在前头:还排在翻译队列里的条目这一轮先不导出(见
`queued_sql`),等译完再落盘;重试 `MAX_TRIES` 次仍然译不动的才按原文导出
——那多半不是偶发抖动,而是这条内容本身有问题,留着原文比一直烧钱强,更比
把它挡在知识库外面强。
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from kb.feeds import registry as reg
from kb.pipeline_state import _state

# 一轮同步最多译多少条:存量几千条英文的话,一次全译会把一次同步拖成几小时、
# 还一次性烧掉一大笔钱。分批消化,每轮同步啃一截。
MAX_ITEMS = int(os.environ.get('KB_TRANSLATE_MAX_ITEMS', '40'))
# 一条正文最多译多少字:超出部分不译,末尾注明并指向原文。RSS 全文条目偶尔
# 上万字,整篇译一遍的钱和时间都不划算,而知识库要的是能被检索到的中文正文。
MAX_BODY = int(os.environ.get('KB_TRANSLATE_MAX_BODY', '12000'))
CHUNK = 3000            # 单次请求的正文上限,按段落切,不切断句子
WORKERS = 3             # 并发度:中转站限流下 3 条并行已经够快,再高容易 429
MAX_TRIES = 3

_TRUNCATED = '\n\n> 原文较长，以上为前 {n} 字的中文翻译，完整内容请打开原文。'
NOTE = '> 本文由英文原文自动翻译，专有名词保留原文；如有歧义请对照原文。'

_RULES = """把下面的内容翻译成简体中文。

要求:
- 只输出译文本身,不要解释、不要前言、不要代码围栏。
- 保持原有的段落与换行结构,不要合并或拆分段落。
- 产品名、公司名、人名、代码、命令、URL、缩写保留原文,不要音译。
- 不增不减不总结:原文说什么就译什么。"""

_WITH_TITLE = _RULES + """

严格按下面的格式输出:
###TITLE###
(标题的译文)
###BODY###
(正文的译文)

标题:
{title}

正文:
{body}"""

_BODY_ONLY = _RULES + """

正文:
{body}"""

# 标题单独译:正文为空的条目(social 那类短动态)用它,正文回包里漏了标题
# 分隔标记时也用它补一次。
_TITLE_ONLY = _RULES + """

只译下面这一行标题,直接输出译文本身,不要加标记:
{title}"""


def _model():
    from kb import llm, llm_stages
    m = llm_stages.stage_model('translate')
    if not llm.has_key(m.provider):
        raise RuntimeError(f'{m.provider} 还没配 API key,无法翻译外文内容')
    return m


def _call(prompt: str, model) -> str:
    from kb import llm
    out = llm.chat(prompt, model, max_tokens=8192, timeout=180).strip()
    if not out:
        raise RuntimeError('模型返回了空译文')
    return out


def _split(body: str) -> list[str]:
    """按段落切成不超过 CHUNK 的块。单段超长时硬切,总比整条译不动强。"""
    chunks: list[str] = []
    for para in body.split('\n\n'):
        while len(para) > CHUNK:
            chunks.append(para[:CHUNK])
            para = para[CHUNK:]
        if not chunks or len(chunks[-1]) + len(para) + 2 > CHUNK:
            chunks.append(para)
        else:
            chunks[-1] += '\n\n' + para
    return [c for c in chunks if c.strip()]


def _parse_titled(out: str) -> tuple[str, str]:
    """拆 ###TITLE###/###BODY### 两段。没有分隔标记时整段当正文、标题回空串
    ——一行格式标记不能当标题写进 vault。空标题不代表"这一条译完了",
    由调用方补问一次(见 `translate_item`)。"""
    if '###BODY###' not in out:
        return '', out.strip()
    head, _, body = out.partition('###BODY###')
    return head.replace('###TITLE###', '').strip(), body.strip()


def _title_only(title: str, model) -> str:
    """单独译一行标题。译不出来就抛错,让整条走失败重试。"""
    if not title.strip():
        return ''
    for line in _call(_TITLE_ONLY.format(title=title), model).splitlines():
        line = line.replace('###TITLE###', '').replace('###BODY###', '').strip()
        if line:
            return line
    raise RuntimeError('标题没有译出来')


def translate_item(item, model) -> tuple[str, str]:
    """返回 (标题译文, 正文译文)。**标题和正文都得有译文才算这一条译完了。**

    标题单独兜一道:模型偶尔漏掉 ###TITLE### 分隔标记,那时只解析得出正文。
    原来这种回包按"成功,标题留原文"存下来,而存下来就不再进翻译队列——页面
    上标着"已译为中文",标题却永久是英文(2026-09-30 复审实测)。所以缺了
    就补问一次;补不到就抛错,整条按失败计次、下一轮重试。

    正文为空的条目(social 那类只有一行标题的动态)不走带正文的那套格式:
    问它译一段空正文只会拿回空译文,白白耗掉三次重试额度。
    """
    original = item['title'] or ''
    if _has(item, 'body_zh') and not _has(item, 'title_zh') and original:
        # 存量半成品(见 `_requeue_partial`):正文已经是中文,只差标题那一行。
        # 整篇重译一遍是把同一份正文再买一次,而它已经在 vault 里了。
        return _title_only(original, model), item['body_zh']
    body = (item['body'] or '')[:MAX_BODY]
    truncated = len(item['body'] or '') > MAX_BODY
    parts = _split(body)
    if not parts:                                # 只有标题
        return _title_only(original, model), ''
    title_zh, out = _parse_titled(_call(
        _WITH_TITLE.format(title=original or '(无标题)', body=parts[0]), model))
    pieces = [out]
    for extra in parts[1:]:
        pieces.append(_call(_BODY_ONLY.format(body=extra), model))
    text = '\n\n'.join(p for p in pieces if p)
    if not text.strip():
        raise RuntimeError('模型返回了空译文')
    if truncated:
        text += _TRUNCATED.format(n=MAX_BODY)
    if original and not title_zh:
        title_zh = _title_only(original, model)
    return title_zh, text


def display(item) -> tuple[str, str]:
    """落盘/展示用的标题与正文:有译文用译文,没有就用原文。

    这是「译文优先」的唯一口径 —— 导出、面板、阅读器各判各的话,同一条内容
    在 vault 里是中文、在面板上是英文,用户根本不知道库里到底存了什么。
    """
    title = item['title_zh'] if _has(item, 'title_zh') else ''
    body = item['body_zh'] if _has(item, 'body_zh') else ''
    return (title or item['title']), (body or item['body'])


def _has(item, column: str) -> bool:
    try:
        return bool(item[column])
    except (IndexError, KeyError):      # 老 Row 对象(迁移前取出来的)没有这列
        return False


def queued_sql() -> tuple[str, list]:
    """"还排在翻译队列里"的判据,只有这一份。

    这些条目**不按英文导出**:一轮只译 MAX_ITEMS 条,一次进来 41 条英文的话
    第 41 条会先以英文写进 vault,而索引、提取、检索、日报读的全是 vault 里
    那份英文。等下一轮译完再落盘,晚一轮但是中文的。队列有出口——试满
    MAX_TRIES 次仍然失败的就不在这个集合里了,那时按原文导出,免得一条
    永远译不动的内容永远进不了知识库。

    判据是 `translated_at`(`_save` 只在整条译成时写它),不是"有没有 body_zh"
    ——只有标题的短动态译完之后 body_zh 仍然是空串,拿它判的话这一条译完了
    还赖在队列里,每轮重译一遍。

    **还排在正文补抓队列里的条目不算待翻译**(见 `kb.feeds.fulltext`):它们
    此刻手里那份正文是 feed 给的跳转壳,正文马上要被整条换掉。不排掉的话同一
    条内容买两次模型调用 —— 先按壳译一遍,补上正文再译一遍,而第一份译文随后
    就被 `fulltext._save` 丢掉。翻译是这条链上最贵的一步,顺序不能反。
    """
    from kb.feeds import fulltext      # 函数内 import:两个模块互相认识对方的口径
    cond, args = fulltext.queued_sql()
    return (f"lang='en' AND translated_at='' AND translate_tries<? AND NOT ({cond})",
            [MAX_TRIES, *args])


def available() -> bool:
    """翻译这一步现在能不能真的发生(配了 key、解析得出模型)。

    来源面板据此决定"没有译文"该怎么显示:能翻就是"还在等翻译,尚未入库",
    翻不了就是"这条只有原文",而后者的原文此刻已经按原样进了 vault
    (`materialize()` 传 `hold_untranslated=not skipped`)。
    """
    try:
        _model()
        return True
    except Exception:                      # noqa: BLE001 - 配置问题一律当"翻不了"
        return False


def pending(kind: str, source_id: int | None = None,
            only_manual: bool = False) -> int:
    """还有多少条排在翻译队列里。

    两个可选的收窄各有用处,口径和 `translate_pending` 共用同一份判据:
    `source_id` 给面板那颗按钮算射程(它承诺的是哪一条订阅),`only_manual`
    给重试那一轮算「这一轮还剩几条」。
    """
    cond, args = queued_sql()
    if only_manual:
        cond += ' AND translate_manual=1'
    extra, eargs = _retry_where(source_id)
    with reg.db() as c:
        return c.execute(f'SELECT count(*) FROM feed_items WHERE kind=?'
                         f' AND {cond}{extra}',
                         (kind, *args, *eargs)).fetchone()[0]


# 「手动重试」的射程:判成外文、还没有译文、而且**试过至少一次**的条目。
#
# **为什么含试满 `MAX_TRIES` 的那一批** —— 它们正是面板上**看不见**的那一半。
# 试满额度的条目按设计离开队列、按原文导出(见 `queued_sql`),于是 vault 里
# 躺着英文、条目上一个标记都没有、顶上那句「N 条还在等翻译」也不数它们,而那
# 批英文已经被索引、被打了标签。更要紧的是**系统里原来没有任何入口能把它们放
# 回队列**:`queued_sql` 的 `translate_tries<MAX_TRIES` 把它们排除在外,所以面
# 板上的「同步」和「重试失败项」点多少次都选不到它们 —— 中转站一轮 503 就能造
# 出上百条(2026-10-13 实测 rss 42 条 + social 66 条),而这是一个**只能靠改库
# 才能解除的终态**。
#
# **判据是「试过至少一次」,不是「有错误信息」。** `translate_error` 被 `_save`
# 成功时清空过,库里也确实有 `translate_tries=3` 而错误是空串的存量条目
# (2026-10-13 实测 social 2 条);按错误信息筛会把它们永久留在库外,而那正是
# 这颗按钮唯一要救的东西。
#
# **也不含 `translate_tries=0` 的那些** —— 那是正常积压(一轮只译 MAX_ITEMS
# 条),它们下一轮本来就会被取走,额度一条没动过,放回队列是空操作。
RETRY_SQL = "lang='en' AND translated_at='' AND translate_tries>0"


def _retry_where(source_id: int | None) -> tuple[str, list]:
    """手动重试的 WHERE,按订阅收窄那一半只有这一份(计数和写回共用)。

    面板上那行提示跟着当前筛选走(`registry.items` 的 `where` 带 source_id),
    所以按钮的射程也得跟着收 —— 否则筛到一条订阅时显示「3 条译不动」、点下去
    却把整个来源的上百条一起放回队列,代价是用户没点过的那一大笔模型调用。
    """
    if not source_id:
        return '', []
    return (' AND key IN (SELECT item_key FROM subscription_items'
            ' WHERE subscription_id=?)', [source_id])


def stuck(kind: str, source_id: int | None = None) -> dict:
    """译不动的内容有多少条、其中几条已经按原文入库、最近一次是什么原因。

    给面板那行提示用。它原来只说「N 条还在等翻译」,对「刚抓回来还没轮到」和
    「这一批已经失败两次、再失败一次就永久按英文入库」说的是同一句话,而两者
    的下一步完全相反:前者等下一轮就好,后者得有人去看一眼为什么。

    `shipped` 单独数一档:那批条目**不在**「还在等翻译」的计数里(它们已经离开
    队列、按英文入库了),不单独说出来的话面板上没有任何东西提到它们的存在。

    **`exhausted` 和 `shipped` 是两个问题,别合成一个。** 前者问「射程里有几条
    已经离开队列」(`queued` 选不到它们,所以总量要把它们加回来),后者问「其中
    几条**真的按原文落盘了**」—— 面板上那句「已按原文写进知识库」只能用后者。
    两者在「最后一次翻译失败之后、导出之前」那个窗口里不同(第 1 轮审查实测:
    `translate_tries` 满、`exported_hash=''`,而临时 vault 里一篇文章都没有),
    混用就是对一篇压根不在知识库里的文章说它已经入库了。
    反过来**射程不许改用 `shipped`**:那一档照样会被 `requeue_failed` 放回去
    (`manual_sql` 不看导出状态,那是对的),按它算就又变成「说 3 条、动 4 条」。
    """
    extra, eargs = _retry_where(source_id)
    # `exhausted` 要和点名集合(`manual_sql`)逐字同一个射程 —— 它是确认框那个
    # 数字的第二段,而点名集合决定实际译谁。两边口径一漂就是「说一个数、动另一
    # 批」:排除补抓队列而确认里算了它,就会少译一条(如实,但数字不准);反过来
    # 算了而不排除,就是多花一笔用户没确认的钱。
    from kb.feeds import fulltext
    body_cond, body_args = fulltext.queued_sql()
    with reg.db() as c:
        row = c.execute(
            f'SELECT count(*) AS failed,'
            f' sum(translate_tries>=? AND NOT ({body_cond})) AS exhausted,'
            f" sum(translate_tries>=? AND exported_hash<>'') AS shipped"
            f' FROM feed_items WHERE kind=? AND ({RETRY_SQL}){extra}',
            (MAX_TRIES, *body_args, MAX_TRIES, kind, *eargs)).fetchone()
        # 原因取**最近抓回来的那一条**的非空错误:一轮整批失败时它们的原因
        # 一模一样,而要紧的是最近一次发生了什么,不是最早那次。
        why = c.execute(
            f'SELECT translate_error FROM feed_items WHERE kind=? AND ({RETRY_SQL})'
            f"{extra} AND translate_error<>'' ORDER BY fetched_at DESC, key LIMIT 1",
            (kind, *eargs)).fetchone()
    return {'failed': row['failed'] or 0, 'shipped': row['shipped'] or 0,
            'exhausted': row['exhausted'] or 0,
            'reason': (why['translate_error'] if why else '')[:300],
            # 还排在队列里的有几条。**按订阅收窄、不跟搜索词走** —— 它是面板上
            # 那颗按钮的射程说明,而顶上那个 `translating` 跟着搜索词变(它回答
            # 的是「这一屏列出来的内容里有几条扣着没入库」,不是按钮会动谁)。
            'queued': pending(kind, source_id)}


def manual_sql() -> tuple[str, list]:
    """「这一轮用户点名要译的」的射程,只有这一份。

    这个来源(或这一条订阅)里**所有还没有译文、而且现在就译得了的**外文条目。

    **比 `RETRY_SQL` 宽,这是有意的 —— 它决定的是「这一轮译谁」,不只是「谁要
    回到队列」。** 按钮有两种点法:失败过的那批要放回队列(额度清零),而只是还
    没轮到的普通积压(`translate_tries=0`)要的是「现在就译,别等下一轮」。后者
    额度没动过、清零是空操作,可**标记**一点都不是空操作:`translate_pending`
    在重试那条路上只译带标记的条目(见 `only_manual`),不标就是一条都不译。

    **但正文还在补抓队列里的条目一个都不许标记** —— 这是付费边界的一半。
    原来含它们,依据是"取数那一刻选不到,标记无害"。那个依据**错了**:补抓和
    翻译在**同一轮**里先后跑(见 `kb/collectors/feeds.py` 的 `materialize`),
    补抓成功的那一下它就离开补抓队列、进了翻译队列,于是带着标记在同一轮被取走
    —— 而确认框那个数字是按翻译队列算的,压根没算它(2026-10-14 第 2 轮审查实测:
    确认 1 条、实际调了 2 次模型,两条都译成了中文)。不需要并发、不需要上一轮
    失败就能发生。

    补抓成功之后 `fulltext._save` 会把译文和额度一起清掉,所以它是一条全新的
    待译条目 —— 下一轮常规同步正常轮到它。**少译一条是如实的**(确认框说的
    就是 1 条),多译一条是花用户没确认过的钱。
    """
    from kb.feeds import fulltext      # 函数内 import:两个模块互相认识对方的口径
    cond, args = fulltext.queued_sql()
    return f"lang='en' AND translated_at='' AND NOT ({cond})", list(args)


# 点名标记的清场语句。`requeue_failed`(开工前作废上一轮)和 `clear_manual`
# (收工后收掉这一轮)共用这一份 —— 两头问的是同一个问题:「库里还留着谁的标记」。
_CLEAR_MANUAL = ('UPDATE feed_items SET translate_manual=0'
                 ' WHERE kind=? AND translate_manual=1')


def requeue_failed(kind: str, source_id: int | None = None) -> int:
    """把这个来源(或这一条订阅)里还没译的条目点名放到队首。返回点了几条。

    三件事缺一不可:
    ①**额度清零** —— 试满 `MAX_TRIES` 的那批否则永远选不到(见 `RETRY_SQL`);
    ②**置 `translate_manual`** —— 它同时承担两件事:
      - **排序**:取数是「失败过的优先」(`translate_tries DESC`),而第①步恰好
        把这一批变成「从没试过」、排到队尾。队列里积压超过一轮上限
        (`MAX_ITEMS`)时,用户点完按钮、等完一整轮同步,库里一个字都不会变:
        实测点按钮那一刻队列里 246 条,恢复的 108 条排在第 139 位往后,下一轮
        取的 40 条里一条都没有它们。那是「按钮没用」里最难查的一种 —— 它什么
        都不报错。
      - **射程**:重试那一轮只译带标记的条目。**这一半是付费边界**:标记是唯一
        跟得到 `translate_pending` 取数那一步的东西 —— 面板上筛着一条订阅、
        确认框承诺「这条订阅的 1 条」,而取数按整个 kind 取 40 条,于是另一条
        订阅的内容也被译了,实际调用和费用超出用户确认的范围
        (2026-10-13 第 1 轮审查实测:`requeued=1` 而 `pending=2`,两条都拿到
        了译文)。`crawl_filter` 到不了那一步 —— `crawl_channels` 只留
        `channel`/`accounts` 两个键,而 `materialize()` 调的是整个 kind 的
        `translate_pending`。

    ③**作废上一轮留下的点名** —— 见下面那段。

    **不动 `exported_hash`**:那批英文此刻确实就在 vault 里,清掉它等于让一篇
    **已经能读、能搜到**的文章连同入口一起消失,而译文还没到手。等 `_save`
    真拿到译文时它自己会清(口径同 `fulltext._save`)。
    """
    extra, eargs = _retry_where(source_id)
    cond, cargs = manual_sql()
    with reg.db() as c:
        # **先把上一轮的点名作废,而且和这一次的点名在同一个事务里。**
        # 标记是一次性的,可收它的那句 `finally`(`clear_manual`)只在进程活着时
        # 跑得到 —— 进程被 kill、机器断电的那一轮,标记照旧留在库里,而且和本次
        # 写下的那些**长得一模一样**。取数那一侧要的是「标记为 1 且属于本次范围」,
        # 同一条订阅下的残留**两个条件都满足**。
        #
        # 单靠 `manual_sql` 排除补抓队列挡不住它:那一道只管「这一次标记谁」,
        # 管不了「上一次标记过谁」。按本次 `source_id` 收窄也只挡得住**跨订阅**的
        # 残留(2026-10-14 第 3 轮审查实测:在 `fetch_pending` 处注入
        # `os._exit(73)` —— 那一刻两条都还是可译的、都被合法标记了 —— 重开后给
        # 这条订阅打开「补抓原文正文」,其中一条进了补抓队列,于是确认框按翻译
        # 队列算出「1 条」;而补抓就排在翻译前面,它补完正文离开补抓队列的那一下,
        # 旧标记还在身上,两条一起被取走译掉)。
        #
        # 清场的射程是**整个 kind**,不是本次那个范围 —— 范围之外的残留同样是
        # 上一轮的遗物,留着只会让下一次常规同步继续把它排在队首。射程不含
        # `kind` 之外的行:别的来源各有自己的那一轮。
        #
        # 这一道让「库里的 `translate_manual=1` 全都是本轮写的」成为一个不变量,
        # 所以不需要再存一份运行身份或候选 key 集合 —— 管道是单例,而这两句和
        # 紧跟着的取数在同一个执行体里,中间没有别的写入方。
        c.execute(_CLEAR_MANUAL, (kind,))
        cur = c.execute(
            "UPDATE feed_items SET translate_tries=0, translate_error='',"
            f' translate_manual=1 WHERE kind=? AND ({cond}){extra}',
            (kind, *cargs, *eargs))
        return cur.rowcount or 0


def clear_manual(kind: str) -> int:
    """把这个来源的点名标记全部收掉。返回收了几条。

    标记是**一次性**的:它说的是「用户点过那一下」,不是这条内容的属性。一轮重试
    跑完就该收掉 —— 不收的话下一次常规同步仍然只盯着这批(排序上压过所有新内容),
    而且**付费边界会漂**:那时筛着另一条订阅点重试,上一轮残留的标记会把别人的
    条目一起带进 `only_manual` 的射程。

    译成的那条由 `_save` 自己清;这里收的是**没译成**的那些(失败、作废、这一轮
    没轮到)。所以它必须排在整轮翻译之后,而且无论成败都要跑。

    **它不是唯一的清场,也挡不住进程被 kill 那一档** —— 那时这一句压根跑不到,
    开工前 `requeue_failed` 里那次清场才是兜底的那一道。两头共用 `_CLEAR_MANUAL`。
    """
    with reg.db() as c:
        cur = c.execute(_CLEAR_MANUAL, (kind,))
        return cur.rowcount or 0


# 复判的长度上限:判定规则收紧的只是"短文本"那一档,够长的条目在旧规则下
# 就走不到那一档,不必每轮重新数一遍字符。
_RECHECK_CHARS = 400


def _label_unknown(kind: str) -> None:
    """老库里的条目没判过语种(lang='')。判定纯本地,一次把它们标完。

    顺带复判一次短条目:2026-09-30 之前"字母少于 12 个一律算中文"把
    「Hello world」这种短外文永久挡在翻译队列外面,只改判定规则的话存量那批
    仍然一辈子是英文。判定不花钱,标签变了才写盘,所以这一趟稳定状态下是
    空转。
    """
    with reg.db() as c:
        rows = c.execute('SELECT key,title,body,lang FROM feed_items WHERE kind=? AND'
                         " (lang='' OR (lang='zh' AND length(title)+length(body)<?))",
                         (kind, _RECHECK_CHARS)).fetchall()
        for r in rows:
            lang = reg.text_language(r['title'], r['body'])
            if lang != (r['lang'] or ''):
                c.execute('UPDATE feed_items SET lang=? WHERE key=?', (lang, r['key']))


# 「半成品」的唯一口径:整条判成外文、原标题不空、正文有译文而标题没有。
# 这形态是 2026-09-30 之前那版解析器留下的——模型漏掉 ###TITLE### 分隔标记时,
# 整段当正文、空标题照样存下来。修了写入路径救不了**已经在库里的那批**:它们
# `translated_at` 非空,新的队列判据永远选不到,面板还按"任一字段有译文"标成
# 「已译为中文」,于是英文标题永久留着、再同步也不补(第 3 轮审查实测)。
PARTIAL_SQL = "lang='en' AND title<>'' AND title_zh='' AND body_zh<>''"


def _requeue_partial(kind: str) -> int:
    """把存量半成品清回待翻译状态,让下一趟只补一次标题。返回清回几条。

    幂等:清掉 `translated_at` 之后这一条就不再匹配 `PARTIAL_SQL` 里那个
    "已经完成过"的前提了(它进了队列,由正常流程收尾)。`translate_tries` 保持
    原样——补译标题失败照样计次,试满 `MAX_TRIES` 仍然拿不到标题的按原文留着,
    不会无限烧钱。
    """
    with reg.db() as c:
        cur = c.execute("UPDATE feed_items SET translated_at='' WHERE kind=? AND"
                        f" translated_at<>'' AND ({PARTIAL_SQL})", (kind,))
        return cur.rowcount or 0


def input_version(item) -> tuple[str, str]:
    """这一次翻译请求所依据的那份输入:标题 + 正文。写回时拿它当判据。

    **口径只有这一份。** 取快照那头和写回那头各写一遍的话,哪天多比一列、
    少比一列,两处就各漂各的 —— 而这道判据漏一次就是让一份作废的译文重新
    成为知识库内容。

    标题也要比,不是只比正文:半成品那条路(见 `PARTIAL_SQL`)的请求输入就
    只有标题,而正文换掉时它手里那份 `body_zh` 同样是旧正文的译文。
    """
    return (item['title'] or '', item['body'] or '')


def _save(key: str, title_zh: str, body_zh: str, seen: tuple[str, str]) -> bool:
    """写回译文。返回**写进去了没有** —— 0 行 = 这期间原文换过了。

    **判据是「这一条还是我发请求时那份标题和正文吗」,而且待在 UPDATE 自己的
    WHERE 里,不许在 Python 这边先 SELECT 一次比对。** 先查后写在两个线程/
    进程之间压根不成立(翻译跑在 3 个工作线程上,而换正文的那两个写入方 ——
    `fulltext._save` 和 `scripts/feed_fulltext_recheck` —— 可能在另一个进程里),
    而 SQLite 把写入串行化:判据待在写语句里才和那次写入同生共死。口径同
    `scripts/feed_fulltext_recheck._downgrade`、`sync._claim_write`、
    `store.mark_no_body` 和 collector 那条 UPSERT。

    **不这么写就是让作废的旧译文重新成为知识库内容,而且不可恢复**:模型请求
    要几十秒到几分钟,这期间复查(或者一次成功补抓)完全可能把这一条的正文整
    份换掉并清空译文 —— 旧回包随后按 key 盖回去,`translated_at` 跟着重新设上,
    于是这一条**再也不进翻译队列**,导出和阅读又优先用 `body_zh`:正文是新的、
    中文却是旧内容的译文,后面每一轮同步都把这段文字重新写进 vault
    (2026-10-09 第 4 轮审查三个隔离库稳定复现:复查把一段版权声明换成了正确
    的标题与链接,而迟到的旧回包把「版权所有,网站使用条款和隐私声明。」写了
    回去,再同步三轮也清不掉)。

    **0 行什么都不动,这正是我们要的末态**:`translated_at` 没写上,这一条还
    待在队列里(它此刻多半在补抓队列,等正文定下来再译);`exported_hash` 也
    没清,vault 里那份不会被这份过期译文重写。
    """
    with reg.db() as c:
        # exported_hash 清空 = 让导出阶段重写这篇 md。译完不重导的话,vault 里
        # 留着的还是那份英文,索引/提取/日报读到的也还是英文,等于没译。
        cur = c.execute("""UPDATE feed_items SET title_zh=?,body_zh=?,translated_at=?,
            translate_error='',translate_tries=0,translate_manual=0,exported_hash=''
            WHERE key=? AND title=? AND body=?""",
            (title_zh, body_zh, reg.now(), key, *seen))
        return bool(cur.rowcount)


def _fail(key: str, error: Exception, seen: tuple[str, str]) -> bool:
    """记一次失败。返回**记上了没有** —— 判据同 `_save`,理由是另一半:

    一份基于旧正文的请求失败了,那是**旧内容**的毛病,不该记在换过来的新内容
    头上。记上去就是拿别人的错误推进这一条的重试计数(`MAX_TRIES` 到顶就按
    原文导出,等于让新正文少几次翻译机会),面板上那行「补译失败」说的也是一
    件跟当前内容无关的事。
    """
    with reg.db() as c:
        cur = c.execute('UPDATE feed_items SET translate_error=?,'
                        'translate_tries=translate_tries+1'
                        ' WHERE key=? AND title=? AND body=?',
                        (str(error)[:600] or type(error).__name__, key, *seen))
        return bool(cur.rowcount)


def translate_pending(kind: str, limit: int | None = None, progress=None,
                      only_manual: bool = False,
                      source_id: int | None = None) -> dict:
    """把这个来源里待翻译的条目译成中文。返回一份成品摘要给调用方汇报。

    没配 key / 没有可用模型时直接跳过(skipped 带原因),不报错也不重试计数
    ——那是配置问题,不是这些条目有问题,按失败记会把重试次数白白耗光。

    **`only_manual` 和 `source_id` 一起才是付费边界,缺一不可。** 面板上那颗
    「重新翻译」按钮的射程跟着当前选中的订阅走,而这两个参数是那个射程唯一跟得到
    取数这一步的东西:收窄只到 `requeue_failed`(它按 `source_id` 点名)的话,
    这里照旧按整个 kind 取 `MAX_ITEMS` 条,于是用户确认「这条订阅的 1 条」却为
    另一条订阅的内容也付了钱(2026-10-13 第 1 轮审查实测)。`crawl_filter`
    到不了这里 —— `crawl_channels` 只留 `channel`/`accounts` 两个键。

    **`source_id` 这一道是为了不把旧标记当成本次授权。** `translate_manual`
    是落在库里的持久标记,而清它那一步**不保证跑得到**:同一轮里补抓阶段抛异常、
    进程被 kill、机器断电,标记就留在那儿。下一次重试筛着另一条订阅点下去,
    只按标记取数就会把上一轮残留的那些一起译掉 —— 用户确认 1 条、实际译 2 条,
    多出来的那条属于他这次压根没选的订阅(2026-10-14 第 2 轮审查实测:在补抓
    的正文保存处注入 `OSError`,随后筛另一条订阅重试,两条都译成了中文)。
    判据因此是**本次点的那个范围**,不是「上一轮应该清掉了」。

    **常规同步两个都不传**:那时照旧按「点名的 → 失败过的 → 新的」排序取数,
    只是顺序上让残留的点名条目先走,不把别的内容关在门外。
    """
    # stale:这一轮的回包作废了几条(请求发出之后原文被换掉了,见 `_save`)。
    # 它既不算成功也不算失败 —— 什么都没坏,只是这份译文不是当前内容的译文;
    # 统计要如实反映**实际写回结果**,按"调了几次模型"报的话这一轮会说"已翻译
    # N 条"而库里一个字都没改。
    summary = {'done': 0, 'failed': 0, 'left': 0, 'stale': 0, 'skipped': '',
               'errors': [], 'failed_keys': []}
    _label_unknown(kind)
    _requeue_partial(kind)
    cond, cargs = queued_sql()          # 队列判据只有这一份,别在这里另写一遍
    if only_manual:
        cond += ' AND translate_manual=1'
    # 本次点名的那个范围。**和标记是两道,不是一道** —— 见上面 docstring。
    scope, sargs = _retry_where(source_id)
    cond += scope
    cargs = [*cargs, *sargs]
    with reg.db() as c:
        # **失败过的优先**。原来是 `ORDER BY translate_tries`(升序),于是译
        # 不动的那些永远排在"从没试过"的后面:实测队列里 833 条 tries=0 压着
        # 63 条 tries=1,按一轮 40 条算要 21 轮同步、三周才轮得到重试一次,而
        # 这期间每一轮导出都把这 63 条原样报成「补译失败」并判红 —— 一个三周
        # 不变的红点,说的还不是这一轮发生的事(2026-10-08 实测)。
        # 失败优先不会让它们赖着:`translate_tries<MAX_TRIES` 是硬出口,试满
        # 就按原文导出、离开队列。而 tries=0 的那些只是正常积压,晚一轮无损。
        rows = [dict(r) for r in c.execute(
            f'SELECT * FROM feed_items WHERE kind=? AND ({cond})'
            # **手动重试排在最前面**,压过「失败过的优先」:那颗按钮是用户的
            # 显式指令,而第二段排序恰好把刚放回队列的那批排到队尾(额度清零
            # = 从没试过)。队列积压超过一轮上限时,点按钮等一轮同步会是库里
            # 一个字都没变 —— 见 `requeue_failed`。
            # `key` 收尾是为了定序:`fetched_at` 只精确到秒(`registry.now`),
            # 一批抓回来的几百条时间戳一模一样,前几段排完仍然没有定序 —— 取
            # 哪 40 条就交给 SQLite 的扫描顺序了,不可复现也没法测。
            ' ORDER BY translate_manual DESC, translate_tries DESC,'
            ' fetched_at DESC, key LIMIT ?',
            (kind, *cargs, max(1, limit or MAX_ITEMS))).fetchall()]
    # 剩几条要按**这一轮的射程**数:只译点名条目时拿整个来源的队列长度去减,
    # 报出来的「留到下一轮」里混着压根不在这一轮射程里的内容。
    summary['left'] = max(0, pending(kind, source_id,
                                     only_manual=only_manual) - len(rows))
    if not rows:
        return summary
    try:
        model = _model()
    except Exception as e:
        summary['skipped'] = str(e)
        summary['left'] = pending(kind, source_id, only_manual=only_manual)
        return summary

    def one(item):
        """调模型。失败就地记错(时机和写回判据都跟着这一条自己走)。

        第四个值是「这一条的写回落下了吗」:失败那一侧由 `_fail` 现场回答,
        成功那一侧的实际写入方是主循环里的 `_save`,所以这里给 True。
        """
        if _state.get('stop_requested'):
            return item, None, None, True
        try:
            return item, *translate_item(item, model), True
        except Exception as e:                       # noqa: BLE001 - 逐条隔离
            return item, None, e, _fail(item['key'], e, input_version(item))

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='translate') as pool:
        for item, title_zh, result, recorded in pool.map(one, rows):
            if isinstance(result, Exception) and recorded:
                summary['failed'] += 1
                # key 也记下来:导出那头要靠它把"本轮新失败"和"此前失败、还排着
                # 队"分开报,拿 failed 这个计数是分不出来的(见 `sync._export`)。
                summary['failed_keys'].append(item['key'])
                summary['errors'].append({'title': item['title'][:120], 'reason': str(result)[:300]})
            elif isinstance(result, Exception):      # 原文已经换过,这次失败不算它的
                summary['stale'] += 1
            elif result is None:                     # 收到停止请求,剩下的留给下一轮
                summary['left'] += 1
            elif _save(item['key'], title_zh, result, input_version(item)):
                summary['done'] += 1
            else:
                summary['stale'] += 1                # 作废,不写成功标记也不重导出
            if progress:
                progress(summary['done'] + summary['failed'], len(rows))
    return summary


def summarize(summary: dict) -> str:
    if summary['skipped']:
        return f"英文内容未翻译({summary['skipped']})"
    if not (summary['done'] or summary['failed'] or summary['left']
            or summary.get('stale')):
        return ''
    text = f"已翻译 {summary['done']} 条英文内容"
    if summary['failed']:
        text += f",{summary['failed']} 条翻译失败(仍在重试,暂不入库)"
    if summary.get('stale'):
        # 不混进 failed:那会让面板判红,而这不是故障。也不混进 left:那句话
        # 说的是"这一轮没轮到",而这一条是译过了、结果作废了。
        text += f",{summary['stale']} 条的原文在这期间换过、旧译文作废(下一轮按新正文重译)"
    if summary['left']:
        text += f",{summary['left']} 条留到下一轮译完再入库"
    return text
