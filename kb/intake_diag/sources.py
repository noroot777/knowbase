# -*- coding: utf-8 -*-
"""**来源级的问题:连不上,或者「这一轮没读完」。**

**硬约束在 `docs/decisions/intake-diagnostics.md` 第一节 —— 改这块之前先读那一份。**

**来源连接问题不是一篇文章。** 它不套文章那四步进度(发现链接 → 补齐正文 → 翻译
→ 入库),走自己的四步「访问来源 → 读取时间线 → 检查覆盖 → 结束检查」;它**不提供
「不再收录」**(那颗按钮的射程是一条内容,拿它停用整条订阅是把单篇排除悄悄放大),
批量勾选里也要禁掉。来源复查恢复**不等于**发现了新文章,更不计为新文章入库。

**两列答的是两件事,不许混。**
- `subscriptions.last_error` —— 这一轮**失败**了:一条内容都没取到,退避已经打上。
- `subscriptions.last_note` —— 这一轮**成功但没读完**(`kb/feeds/schema.py` 的
  `ADDED_COLUMNS` 里写着):抓到的照旧入库,只是没有证据覆盖到原同步下界,所以**水位不前移**,下一轮
  按原下界重叠重抓、按 URL 去重。把它记成失败会让面板显示「连接失败」并触发退避,
  而连接好得很。

**X 那两类绝不许说成「掉登录」。**
- 启动限流(`x_page_state.XSessionThrottled`):启动请求被限流、页面应用没能启动,
  **本轮没读到这个账号的时间线**;不是登录过期、也不是安全验证。出路是等后端那份
  会话冷却(`x_cooldown`:45 秒起,反复撞上翻倍到最多 180 秒),而且
  `x_browser` **已经会**等冷却、并在预算允许时把受影响账号排到队尾重试一次 ——
  文案不许写成「完全没有自动重试」,也不许承诺「冷却结束就会成功」。
- 覆盖未完成(`last_note`):浏览器时间线最多滚 40 次、每账号 50 秒预算,连续 3 次
  没长出新内容又没到底也算没读完。现有实现**重新导航账号页、不是从上次像素位置
  续滚**,所以那颗按钮叫「重试此范围」,**不许承诺断点续传**。

**分类从产生那句话的工厂现取,不手抄关键词。** `_NEEDLES` 里的字符串由
`x_page_state` 那几个工厂函数在 import 时算出来 —— 改了那边的文案,这边自动跟上,
不会留下一份对不上的关键词表。认不出来一律落进**通用档**(「来源连接失败」+ 原话
照搬),通用档**一个字都不提重新登录**:判不准时多说一句「去登录」就是把限流说成
掉登录,而那正是要防的那件事。

**`last_note` 不过限流那道关键词。** 它那句话里**会原样嵌着限流那段文案**(实测:
某账号的 note 是「滚动次数用完,没有读到上次的位置;有 67 条折叠推文暂缓补全(X
限制了访问频率,页面应用没能启动…),留到下次同步再试」)—— 拿关键词去搜它会把一个
「成功但没读完」判成「启动限流」,于是屏幕上说「本轮没读到这个账号」,而它其实读到了
一堆内容。按列分流就没有这个问题。
"""
from __future__ import annotations

from kb.feeds import registry as reg, x_page_state as xstate
from kb.intake_diag import warrant

#: 「这一轮没读完」那句话里,时间线覆盖那一档会出现的字样(产生处在
#: `kb/feeds/x_browser.py`:滚动次数用完 / 单账号秒数用完 / 不再长出新内容)。
#: 认不出来不影响分组(都在覆盖未完成那一档),只影响标题念哪一句。
_COVERAGE_WORDS = ('没有读到上次的位置', '不在时间线底部')

#: 失败原话 → 问题类型。**字样由工厂现算**,不手抄。
_NEEDLES = (
    ('x_boot', str(xstate.session_throttled())),
    ('x_blocked', str(xstate.access_blocked())),
)

#: 每一档的屏幕文案。`state` 进列表行的状态位,`label` 是详情里那句结论。
PROBLEMS = {
    'x_boot': {
        'state': '来源受限',
        'label': '页面应用没能启动（限流，等冷却）',
        'explanation': '启动请求被限流，页面应用没能启动，本轮没有读到这个账号的时间线。'
                       '不是登录过期，也不是安全验证——不要反复登录。原同步下界保留，'
                       '已取得的内容照旧在库里。',
        'advice': '等后端那份会话冷却过去再重试这一个账号。冷却从 45 秒起，反复撞上会翻倍'
                  '到最多 180 秒；同步本来就会在同一轮里等它，并在预算允许时把受影响的'
                  '账号排到队尾重试一次。冷却结束也不保证这一次就能读到。',
        'step': 0,
    },
    'x_blocked': {
        'state': '需人工确认',
        'label': '对方要求安全验证',
        'explanation': '这一轮被一道安全验证挡住并已停止，不是限流、也不是账号没有新内容。'
                       '原同步下界保留。',
        'advice': '在这条通道对应的浏览器里完成那道验证，再回来重试这一个账号。'
                  '反复同步不会让验证自己过去。',
        'step': 0,
    },
    'connect': {
        'state': '来源受限',
        'label': '来源连接失败',
        'explanation': '这一轮一条内容都没取到，退避已经打上：下一轮自动同步会按退避时间'
                       '自己再试一次。原同步下界保留，已入库的内容不受影响。',
        'advice': '原话在下面的证据里。确认来源地址和网络之后可以单独重试这一个来源，'
                  '那一下会跳过退避。',
        'step': 0,
    },
    'coverage': {
        'state': '覆盖未完成',
        'label': '没有读到上次位置（覆盖未完成）',
        'explanation': '这一轮同步成功了，抓到的内容照旧入库，但没有证据覆盖到原同步下界，'
                       '所以水位不前移。下一轮按原下界重叠重抓、按原文链接去重，'
                       '不会重复入库。',
        'advice': '可以现在就按原同步下界重试这一个账号。它会重新打开账号页从头读，'
                  '不是从上次停下的位置接着滚——所以同样的预算或加载障碍下，'
                  '这一次仍然可能读不完。',
        'step': 2,
    },
    'partial': {
        'state': '覆盖未完成',
        'label': '本轮没有全部读完（覆盖未完成）',
        'explanation': '这一轮同步成功了，抓到的内容照旧入库，但有一部分没有取齐，'
                       '所以水位不前移，下一轮按原下界重叠重抓。',
        'advice': '可以现在就按原同步下界重试这一个来源。原话在下面的证据里。',
        'step': 2,
    },
}

#: 来源级问题自己那四步(**不套文章那四步**)。
SOURCE_STEPS = ('访问来源', '读取时间线', '检查覆盖', '结束检查')


def _kind_of_failure(reason: str) -> str:
    for name, needle in _NEEDLES:
        if needle and needle in reason:
            return name
    return 'connect'


def _kind_of_note(note: str) -> str:
    return 'coverage' if any(word in note for word in _COVERAGE_WORDS) else 'partial'




def problems(kind: str) -> list[dict]:
    """这个来源里**此刻有问题**的那几条订阅。一条订阅最多出现两次(失败 + 没读完)。

    **停用的订阅照旧列出来,但不给重试出口**:它上一次确实失败过,把它从屏幕上
    抹掉等于让人再也看不到那笔红;而单独同步一条停用的订阅会被 `/{kind}/sync`
    以 400 拒掉(那是对的 —— 停用就是「这条先别跑」),所以这里如实标上
    `actionable: False`,按钮由前端禁掉并说清原因。通道没配好(`blocked`)
    同理 —— 那是同一个端点的另一句 400。
    """
    out: list[dict] = []
    settings = reg.get_settings(reg.check_kind(kind))
    for source in reg.subscriptions(kind):
        # **「待配置」也得先说出来。** 这一条的出口是 `/{kind}/sync`,而那个端点
        # 在通道没配好时以 400 拒掉(`reg.configuration_error`)。不先问一遍的
        # 后果是:确认框照常承诺「冷却后重试这一个账号」,点下去却弹一句「浏览器
        # 通道还没有登录过 X」—— 而这块面板存在的理由之一恰恰是**不要**把限流
        # 引到「再登一次」上去。判据仍然只有一份,这里只是把它提前问出来。
        blocked = reg.configuration_error(source, settings)
        shared = {
            'kind': kind, 'source_id': source['id'], 'name': source['name'],
            'url': source['url'], 'enabled': bool(source['enabled']),
            'actionable': bool(source['enabled']) and not blocked,
            'blocked': blocked,
            'failures': source['failures'], 'retry_at': source['retry_at'],
            'last_attempt': source['last_attempt'], 'last_success': source['last_success'],
            # **下界按这一轮真正会用的那条通道取**(`warrant.source_floor`),不按
            # 固定顺序试三条取第一个非空值 —— 两条通道都留过水位时那样取到的是
            # 另一条的下界,而屏幕上这句话承诺的是这一轮会用的那个(审查第 1 轮 R2)。
            'since': warrant.source_floor(source, kind, settings),
            'channel': warrant.source_channel(kind, settings),
            # **这条订阅的定义改到第几版了**(只增、不复用,见 `kb/feeds/schema.py`
            # 的 `rev`)。它是那张执行凭证的身份:`source_id` 和 `problem_id` 都
            # 认不出「人在确认框停留期间把这条订阅改指到另一个账号」——编号没变、
            # 问题类型也没变,票却必须换(审查第 2 轮 R3)。口径在
            # `kb.intake_diag.actions._source_scope`。
            'rev': int(source['rev'] or 0),
            # **这一条的整份执行描述**(订阅编号 + 代次 + 实际通道 + 当前通道的
            # 下界 + 跑不跑得起来)。票按它签,起跑那一下把**同一份**交给
            # `intake_api.start_sync`,再由它交到执行器手里重算一次 —— 口径只有
            # `warrant.source_sig` 一份。
            'sig': warrant.source_sig(source, kind, settings),
        }
        if reason := (source['last_error'] or ''):
            name = _kind_of_failure(reason)
            out.append({**shared, 'id': f"source:{source['id']}:{name}",
                        'problem': name, 'evidence': reason, **PROBLEMS[name]})
        if note := (source['last_note'] or ''):
            name = _kind_of_note(note)
            out.append({**shared, 'id': f"source:{source['id']}:{name}",
                        'problem': name, 'evidence': note, **PROBLEMS[name]})
    return out


def counts(kinds=None) -> dict[str, int]:
    """每个来源有几条来源级问题。读不出来的来源记 0 并把原因交给调用方。"""
    return {kind: len(problems(kind)) for kind in (kinds or reg.KINDS)}
