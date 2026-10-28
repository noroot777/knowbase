# -*- coding: utf-8 -*-
"""本轮的限流熔断:**哪个上游说了「请求过多」,这一轮就不再往它发下一个请求。**

**硬约束在 `docs/decisions/youtube-intake.md` 第一节 —— 改这块之前先读那一份。**

**为什么这件事不能待在某个 `except` 里。** 「这一次接住了一条 429」和「这一轮之前撞没撞过
429」是两件事,退出某个循环也不等于不再发请求。审查实测的三种漏法一条都不经过那个 `except`:

1. 逐条解析撞 429 → 后路 flat 照旧起一个进程;它要是成功了,限流只留在被丢弃的原因列表里,
   外层接着查下一个频道(`walls` 是空的,面板上看不出这一轮挨过罚)。
2. 同一个进程先印出一条正常视频、再因 429 退出 → 转成「部分成功」,限流只剩一句说明文字。
3. 发现阶段退出之后 → 处理阶段无条件开跑,去 probe 前一个**发现成功**的频道的视频,
   把那条本来正常的视频也带成 `failed` 和退避。

所以判据不在任何一个 `except` 里:它在**请求入口**(`media.ytdlp`、`llm_request.request` 各有
一道 `guard`)和**读诊断那一处**(`witness` —— 成功那一次也过它)。一轮里任何一次读取确认了
限流,后面每个入口都拒绝起跑;已经取到的条目照旧收下,没处理的一律留着下轮再来。

**生命周期就是一轮同步**(`opened`)。跨轮不留冷却 —— 立刻再同步一次照旧可能撞上,那是
另一件事,这里没做。轮外(面板上点一次「检查登录态」)压根没有熔断状态:`guard` 和 `trip`
都是空操作,行为一个字不变。同步正跑着的时候点检查会共用这一轮的账 —— 它自己撞上的 429
同样算这一轮撞上了(本来就是同一个出口),而这一轮已经收手时它也不再发请求,两边都对。

**本地中断不算限流**(`media.Interrupted`):人点了停止、或者预算用尽,那是我们自己的决定,
不许报成「出口限流」。它不是 `ToolFailure`,一个字都进不了这本账。

**「没发出去」和「发出去失败了」是两种结果,不是同一种**(`NotAttempted`)。拒绝起跑只说明
这一轮先前撞过,被拒的那条视频、那个频道**什么毛病都没有** —— 混成一种的那一版把它们
一起记成失败、各推一次半小时退避(审查实测:元数据已经拿到、只是同时带了一条 429 警告,
页面上那条候选显示「处理失败,待重试」、频道显示「连接失败」,而 `last_error` 自己写着
「没有再发起新的请求」)。所以拒绝有自己的类型,**消费方按类型认,不比对提示文字**。
"""

import threading
from contextlib import contextmanager

from kb.youtube import login
from kb.youtube.diagnosis import LOCAL, MODEL, YOUTUBE, ToolFailure, excerpt

#: 每个上游各自的说法。**按真实上游说话**:YouTube 那一档可以说「换出口有用」,
#: 对模型配额说这句话是错的。详细的下一步 YouTube 侧由 `login.MESSAGES` 另说一遍
#: (那是按频道归并的那句),这里只说「这一轮为什么不往下做了」。
NOTICES = {
    YOUTUBE: '出口被 YouTube 限流（429，请求过于频繁），这一轮不再发起新的读取，未处理的视频留待下轮',
    MODEL: '内容模型服务返回了限流（429，请求过于频繁），这一轮不再继续调用模型，'
           '未处理的视频留待下轮；这不是 YouTube 的限流，换出口没有用',
}

#: 模型服务说「请求过多」的特征。**和 YouTube 那一套(`login._LIMIT`)分开写,不许复用** ——
#: 两边的措辞不同源,混成一份之后就再也分不出是谁限的。单独一个 `429` 不收:别处的数字
#: (token 数、时长)也会撞上,而这里误报的代价是整轮不再调模型。
_MODEL_LIMIT = ('error code: 429', 'status 429', '429 too many requests',
                'too many requests', 'rate limit', 'rate_limit')


class Breaker:
    """一轮的熔断账。**只记,不猜**:哪个上游说过限流、原话是什么。

    第一笔说了算(`setdefault`):同一轮里后面那些refusal 的原文没有新信息,
    而人要看的是最早那一条真原话。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.hits = {}

    def trip(self, origin, evidence=''):
        with self._lock:
            self.hits.setdefault(origin, excerpt(evidence, 400))


#: 当前这一轮。`None` = 轮外(面板上的单次检查走这一档)。
_ROUND = None


@contextmanager
def opened():
    """开一轮。**嵌套时按栈还原**,不把外面那一轮的账清掉。"""
    global _ROUND
    previous, _ROUND = _ROUND, Breaker()
    try:
        yield _ROUND
    finally:
        _ROUND = previous


def trip(origin, evidence=''):
    """记一笔。**轮外是空操作** —— 没有轮次就没有「本轮」可言。"""
    if _ROUND is not None:
        _ROUND.trip(origin, evidence)


def tripped(origin=None) -> bool:
    """本轮撞上限流了没有。`origin=None` 表示任何上游都算。"""
    if _ROUND is None:
        return False
    return bool(_ROUND.hits) if origin is None else origin in _ROUND.hits


def notice(origin=None) -> str:
    """面板上那句话,没撞上就是空字符串。**撞上哪个上游说哪个。**"""
    for hit in ([origin] if origin else list(_ROUND.hits if _ROUND else ())):
        if tripped(hit):
            return NOTICES.get(hit, '')
    return ''


class NotAttempted(ToolFailure):
    """**这一次请求压根没发出去** —— 本轮先前撞过限流,入口直接拒绝起跑。

    和「发出去了、失败了」分开成两个类型,因为**后果完全不同**:没发出去的那一次,
    这条视频和这个频道一个毛病都没有,不该记失败、不该推退避、不该进「重试失败项」。
    混成一种的那一版把元数据已经取到的候选记成「处理失败,待重试」、把发现成功的频道
    记成「连接失败」,两边各推一次半小时退避(审查实测)。

    照旧是 `ToolFailure`:分档、完整诊断、上游都原样带着,面板上说的还是限流。
    **消费方一律按类型认**(`except NotAttempted`),不许比对提示文字 —— 文案会改。

    **只在这条目标上「一次都没发出去」时它才是结论。** 同一个频道、同一条视频上已经
    发生过真实失败(第一条路撞了 429、人工字幕撞了 429),那条真实失败说了算:它确实
    没拿到东西,原文也得留得住、该记的失败和退避照记。拿这个类型顶替掉真实失败等于
    把「撞上的那一个」也说成「没轮到」,面板上再也看不出这一轮是怎么停的。
    """


def guard(origin=YOUTUBE):
    """这一轮还能不能往这个上游发请求。**撞过就当场抛,一个进程都不起。**

    抛的是 `NotAttempted`(见那个类):带着 `code`/`origin`,所以上层照旧分得清档,
    同时让消费方认得出「这一次没发出去」。原话里写明是「这一轮已经撞上」,
    不是这一次又撞了一回 —— 后者是假话。
    """
    if tripped(origin):
        raise NotAttempted(f'这一轮已经撞上限流，没有再发起新的请求：{NOTICES.get(origin, "")}',
                           _ROUND.hits.get(origin, ''),
                           'rate_limited' if origin == YOUTUBE else '', origin)


def witness(noise, origin=YOUTUBE) -> str:
    """读完一次工具诊断,**顺手把限流记进本轮**,并把分档返回给调用方当 `code` 用。

    **正路只有一处**:`media.run` 的那个 `finally` —— 所有 yt-dlp 子进程共用的完成边界,
    成功、半截、失败都过。各个调用点也还调它(要那个分档),`trip` 第一笔说了算,不会算两笔。

    **成功那一次也要过这一道。** 429 很多时候只是一条警告:进程退 0、条目也列出来了,
    而出口已经在挨罚(`login.CAUTIONS` 里「这一轮算侥幸过的」说的就是它)—— 接着发下一个
    请求只是把惩罚加深,而且下一个多半就过不去了。
    **本机工具那一档一个字都不分类**(`origin=LOCAL`):ffmpeg 报的 `permission denied` 会被
    `login.classify` 判成「读不出 cookie」,而它和 cookie 一点关系都没有。

    **事实走 `login.limited`,不走分档的返回值。** 分档只能给出一种结论,而 cookie 那一档
    排在限流前面 —— 同一份输出既报 cookie 解密失败又报 429 时,照分档判就一笔都记不下来
    (审查实测:两个频道的详细列举都带这两条,这一轮照旧查完 `checked=2`)。
    """
    if origin == YOUTUBE and login.limited(noise):
        trip(origin, noise)
    return login.classify(noise) if origin == YOUTUBE and noise else ''


def model_limited(text) -> bool:
    """模型服务这条错误是不是「请求过多」。口径只有这一份,消费方都过它。"""
    lowered = str(text or '').lower()
    return any(mark in lowered for mark in _MODEL_LIMIT)


def caught(error):
    """把刚接住的这条异常过一遍熔断账,**按它自己报的上游分流**。

    正路是请求入口那两道(`guard` / `witness`):这里多认一手是**冗余带** —— 哪天某条路
    绕开了 `witness`(新加一条路径、或者被打桩替掉),「撞上就收手」这件事不该跟着一起丢。
    漏报是静默把惩罚加深,误报顶多让这一轮早收手、下一轮重来。

    YouTube 那一档认两样:异常自己带的分档(`login.coded`,它对非 YouTube 的上游一律返回
    `unknown`),以及原文里有没有出现过 429(`login.limited`)—— **后者是独立事实**,
    带 `code` 的那种(打桩造的、`guard` 抛的)诊断可能是空的,只看原文会漏。
    模型服务的 429 走不到这条分支,那正是审查点名的那半边。
    """
    origin = getattr(error, 'origin', YOUTUBE)
    evidence = getattr(error, 'diagnostic', '') or str(error)
    if origin == MODEL:
        if model_limited(str(error)):
            trip(MODEL, str(error))
    elif origin != LOCAL and (login.coded(error) == 'rate_limited' or login.limited(evidence)):
        trip(YOUTUBE, evidence)
