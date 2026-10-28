# -*- coding: utf-8 -*-
"""正文补抓这一族的**判据**:谁还排着队、谁的正文还没定下来、谁挂起了、谁试满了,
以及一次补抓的**工作身份**。

从 `kb/feeds/fulltext.py` 搬出来**只因为 700 行上限**(口径同
`kb/feeds/translate_scope.py`)。分界线是「问的是哪个问题」:这里只回答「哪些条目
算在里面、这一次动的是哪一版」(纯判据 + 计数 + 身份),抓取本体、取数循环、写回、
挂起出口和存量清理留在 `fulltext.py`。所以这里一次网络请求都没有。

**名字一律由 `fulltext` 原样再导出**,调用点和直测入口一个字都不用改 ——
`fulltext.queued_sql()` / `fulltext.MAX_TRIES` / `fulltext.work_version(row)`
照旧成立。`MAX_TRIES` 跟着判据走(它是判据的一部分);`MAX_ITEMS` 留在那边
(它管一轮抓几条,而且有回归在打桩)。

**硬约束见 `docs/decisions/fulltext.md`;搁置那一道见
`docs/decisions/intake-diagnostics.md`。**
"""
from __future__ import annotations

from kb.feeds import registry as reg
# 「这一行此刻是哪一版」那份口径摆在 `grab_store` 里(两个版本身份 —— 内容身份
# `item_version` 和工作身份 `work_version` —— 要摆在一起对照着读)。
from kb.feeds.grab_store import work_version as _content_version
from kb.intake_diag import shelf
from kb.source_types import link_body_sql

#: 确定失败的重试额度。试满就离开补抓队列,按 feed 原本那份壳走完翻译和导出 ——
#: 留着原文比把它永远挡在知识库外面强(理由见 `queued_sql` 的 ③ ④)。
MAX_TRIES = 3


def _eligible(ref: str = 'feed_items.key') -> str:
    """**「这一条现在还属于某条开着补抓的订阅」—— 补抓这一族判据只有这一份。**

    条目和归属订阅都须开放 `SourceType.body_from_link`,旧库误开的开关不授予资格。

    一条内容可能同时属于聚合源和单号源,**任意一条开着就算有资格**(补到的正文对
    另一条同样有用);反过来,关掉最后一条开着补抓的归属、或者把订阅整条删掉
    (`registry` 删订阅时刻意保留已收录的条目),这一条就再也不该等全文了。

    **抽成函数是因为它曾经只补在三个消费方身上、漏掉了第四个。** 先补的是
    `held()` / `release()`,后来实测到的是 `unsettled_sql()` 的挂起那半边没有:
    于是关掉补抓之后那一条落进一个**没有出口的夹缝** —— 翻译那边当它「正文还没
    定下来」永久排除(英文摘要永远不译成中文),待办那边当它「没资格」不列出,
    人工恢复也以 `released=0` 拒绝。三处各判一遍就会这样,所以元规则那条「口径
    只有一份」在这里不是风格问题。

    `ref` 只有两个写死的取值(`feed_items.key` 给拼进别人 WHERE 的那几段和复查
    写回,`fi.key` 给 `held()` 和复查脚本带别名的 SELECT)—— **不接外部输入**。
    """
    item = ref.rsplit('.', 1)[0]
    return (f"({link_body_sql(item)} AND EXISTS(SELECT 1 FROM subscription_items si"
            " JOIN subscriptions s ON s.id=si.subscription_id"
            f" WHERE si.item_key={ref} AND s.fulltext=1 AND {link_body_sql('s')}))")


def work_version(row) -> tuple[str, int, str, str, int]:
    """补抓这条链上的**工作身份**:这一世、这一代、这一版内容、这一个抓取目标。

    取样和提交都过这一个口径(`fetch_pending` 取数、`_save` / `_fail` 写回),
    **五道一起**。前两道是 `grab_store.work_version` 那一份原样(`content_hash`
    + 只增不复用的 `content_rev`),各挡什么在那边的说明里;后三道是这条链自己的。

    **第三道是抓取目标(`fulltext_url`)。** 它是「问题与待办」里那颗「换链接」
    改的那一列(`kb.intake_diag.actions.relink`),而换链接**不碰内容** ——
    `content_hash` 和 `content_rev` 一个都不动,于是前两道认不出这次改动。序列:
    worker 读到旧地址和旧内容身份 → 人换成新地址 → 旧请求回来 → `_save` 照旧
    通过,把**旧地址**那一页的正文写进去、`fulltext_at` 一置,自动补抓此后再也
    不碰这一条 —— 新地址一次都没去过,而面板上这一条显示「正文已补齐」。失败那
    一侧同样:旧地址的超时和错误被记在新地址头上,还扣掉一次重试额度
    (审查第 1 轮 F05)。

    **第四道是这一世(`life`)、第五道是这一代(`fulltext_gen`),它们管的是
    前三道**都可以复原**这件事**(审查第 2 轮 R2 实测):
    - `fulltext_url` 改成 B 再改回 A,那个字符串逐字复原,**旧请求重新获得写回
      权** —— 实测改回 A 之后,换地址之前发出去的那次 `_save` 又写进去了。所以
      `relink` 每次都给 `fulltext_gen` 加一:地址可以来回改,代次只会往前走。
    - 一条内容被存量清理删掉、下一轮又从**同一个链接**收回来时,`key` 一个字
      没变,而 `content_hash`(同一份摘要)、`content_rev`(新行从 0 起)、
      `fulltext_url`(新行是空串)**三道可以逐字相同** —— 上一世那个在路上的请求
      回来就盖在新收录的这一行上。`life` 不可复用(见 `kb/feeds/schema.py`),
      认的就是「还是我取样那一行吗」。
    - 人工放回队列(`fulltext.release` / `reopen`)把额度和待办清掉 = 发了一轮
      **新的**工作机会,所以它也加一代:那之前发出去的请求回来不许把刚清掉的
      挂起重新写上、不许扣掉刚还回去的额度。

    **可复原的输入值不是一次工作的身份** —— 这一条和 `shelf` 那边「本地可重复的
    状态不是身份」是同一句话,元规则在 CLAUDE.md。

    **这里不动 `content_rev`**(换地址、还额度都不是换内容):别人手上那份**内容**
    工作资格不该因此作废 —— 比如同一时刻跑着的手动重抓,它写回的是用户自己抓到
    的正文,和这一条去哪儿补抓、还剩几次额度无关。
    """
    return (*_content_version(row), row['fulltext_url'] or '',
            row['life'] or '', int(row['fulltext_gen'] or 0))


def queued_sql() -> tuple[str, list]:
    """"还排在正文补抓队列里"的判据,只有这一份。

    五个条件:①它属于某条开了补抓的订阅(一条内容可能同时属于聚合源和单号源,
    任意一条开了就抓 —— 补到的正文对另一条同样有用);②还没抓到过
    (`fulltext_at` 空);③**没有挂起**(`fulltext_hold` 空);④确定失败的重试
    额度没用完;⑤**没有被人搁置**(`kb.intake_diag.shelf`)。

    ⑤ 是「问题与待办」面板上那两颗按钮(暂缓 / 不再收录)的承重处:不带它的话,
    人点完「暂缓」之后下一轮同步照旧去抓它 —— 按钮只是在屏幕上改了个字。
    判据同源见 `shelf.free_sql`;**`unsettled_sql` 刻意不带它**,理由在那边。

    **③ 和 ④ 是两件不同的事,这是审查第 1 轮点出来的那条。** ④ 是「对方真的
    拦着/真的连不上,试到第三次就认了」的预算;③ 是「我**没能判断**这一页是不
    是正文」—— 后者一次就挂起、不扣 ④ 的预算,因为再敲同一扇门不会让我们
    知道得更多,而扣掉预算的后果是它三轮之后**静默**退回摘要、面板上再没有任何
    痕迹。挂起的出路是人看一眼(`held()` / `release()`),不是再试两次。

    队列的出口因此有两个:挂起(等人)和 ④ 试满 `MAX_TRIES`(按 feed 原本那份壳
    走完翻译和导出 —— 留着原文比把它永远挡在知识库外面强)。

    `feed_items.key` 写全名是因为这段会被拼进别人的 WHERE 里(`registry.items`、
    `sync._export`),那些语句的 FROM 子句就是 feed_items,不带别名。
    """
    return (f"{_eligible()} AND {shelf.free_sql()} AND fulltext_at=''"
            " AND fulltext_hold='' AND fulltext_tries<?"), [MAX_TRIES]


def unsettled_sql() -> tuple[str, list]:
    """"这一条的正文还没定下来" —— **翻译那道闸门问的是这个,不是「还排着队吗」**。

    两者差一个挂起态:挂起的条目**不在**补抓队列里(它等的是人,不是下一轮同步),
    但它手里那份正文仍然是 feed 给的跳转壳,人点一次 `release()` 就可能被整条换
    掉。拿 `queued_sql` 当翻译闸门的后果实测到过(就在把挂起做出来的那一轮):那
    一条一挂起就当场进了翻译队列,壳被译一遍、按文章导出,而人点一次恢复、正文真
    补上之后 `_save` 把那份译文丢掉重译 —— **同一条内容买两次模型调用**,翻译是
    这条链上最贵的一步。

    **导出那一侧刻意不用它**:挂起的条目照旧按壳导出,和「试满额度降级出队」完全
    一样的待遇 —— 留着原文比把它永远挡在知识库外面强,而正文后来真补上时
    `content_rev` / `extract_stale` 会让它重新导出。

    **资格那一道排在整个并集外面,不是只排在「还排着队」那半边。** 写成
    `queued_sql() OR (at='' AND hold<>'')` 的后果实测过:关掉补抓(或者删掉最后一条
    开着补抓的归属)之后,左半边因为没资格变假、右半边只看 hold 仍然为真,于是这一条
    **永久**被判成「正文还没定下来」—— 英文摘要再也不译成中文,而 `held()` /
    `release()` 那两道已经按资格把它排除了,面板上看不到、人点恢复也拒绝。一条
    答不出任何出口的夹缝。判据同源见 `_eligible`。
    """
    return (f"{_eligible()} AND fulltext_at=''"
            " AND (fulltext_hold<>'' OR fulltext_tries<?)"), [MAX_TRIES]


def pending(kind: str) -> int:
    cond, args = queued_sql()
    with reg.db() as c:
        return c.execute(f'SELECT count(*) FROM feed_items WHERE kind=? AND {cond}',
                         (kind, *args)).fetchone()[0]



def held_sql(alias: str = 'feed_items') -> str:
    """**「挂起等人看一眼」的判据,只有这一份**(`held()` 和收录诊断面板共用)。

    抽成函数的理由和 `_eligible` 一模一样:面板要按同一档计数、同一档列条目,
    各写一遍就会在「面板说有 3 条待确认、点恢复回 0 条」上分叉。三条判据连同
    「没被搁置」一起 —— 人点过暂缓的那一条不该再出现在「等着人做」的清单里。

    `alias` 不接外部输入,理由同 `_eligible` 的 `ref`。
    """
    return (f"fulltext_hold<>'' AND fulltext_at='' AND {_eligible(alias + '.key')}"
            f" AND {shelf.free_sql(alias)}")


def exhausted_sql(alias: str = 'feed_items') -> tuple[str, list]:
    """**「确定失败、重试预算用完」的判据,只有这一份。**

    它和挂起是两件事(见 `queued_sql` 的 ③ ④):这一档是「对方真拦着 / 真连不上,
    试到第三次就认了」,于是离开队列、按 feed 原本那份壳走完导出 —— 面板上**原来
    一个痕迹都没有**(排查那天生产库里 RSS 226 条、网页专栏 1 条,只能靠手写 SQL
    问出来)。**不许和挂起、和「还没轮到」统称「抓取中」**:三者的下一步完全不同
    (等一轮 / 人看一眼 / 人决定要不要再给一轮额度)。

    `fulltext_tries>=MAX_TRIES` 写成 `>=` 而不是 `=`:存量库里有过被手工改大的
    值,按相等判会把它们永久漏掉(漏报是静默丢数据,多报顶多多列一条)。

    这一档在面板上怎么分组、怎么计数见 `docs/decisions/intake-diagnostics.md`。
    """
    return (f"{_eligible(alias + '.key')} AND {shelf.free_sql(alias)}"
            f" AND {alias}.fulltext_at='' AND {alias}.fulltext_hold=''"
            f" AND {alias}.fulltext_tries>=?"), [MAX_TRIES]

