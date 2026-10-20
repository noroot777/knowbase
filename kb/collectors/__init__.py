# -*- coding: utf-8 -*-
"""来源采集器契约:知识库怎么从外部世界拿到文章。

知识库主干只认这一层抽象,不认识绿泡泡、RSS 或任何具体来源——换来源、换
采集方式(公众平台爬取 / 抓包 / 手工投喂)都只动实现,管道一行不改。

一个采集器负责两步:

    fetch()        远端 → 采集器本地。慢,受对方反爬与限流约束。
    materialize()  采集器本地 → obs md。落进 vault 之后就是知识库自己的事。

两步都允许是空操作:一步到位的采集器把 fetch 写成 no-op 即可;反过来,
采集器要是自带常驻库(先落库再转 md),两步就各司其职。

两个方法都直接调 kb.pipeline_state._stage 汇报进度,与其它阶段一致;
失败通过 _stage(..., "error", ...) 表达而不是抛异常——管道要的是"这一阶段红了
但后面接着跑",不是整条链断掉。

fetch 的返回值没有意义。**materialize 的返回值是这一轮的成果回执**:它这一次
往 vault 新落了哪几篇,每一篇带着它的身份(见 `Fetched`),交给 kb.fetch_rounds
记成「今天第 N 次抓取抓了多少」。说不出来就返回 None —— 账本据此报「不可确认」,
面板上写「条数读不出来」。

回执这件事不许由上层旁观着猜:管道一度在物化前后各取一次全 vault 快照、拿差集
当成果,而差集分不出"谁写的"——那段时间里别的进程(实时 saver、手工导入、另一个
采集器)落下的文件会被算进这一轮(第 3 轮审查实测)。采集器自己知道它写了哪几个
文件,这是别人都拿不到的事实,所以只能由它交出来。宁可返回 None 让面板说
「读不出来」,也不要一个看着确定、实际上掺了别人成果的数。

**回执里的身份有两个,缺一个就答不全。** 光有「写了哪几个路径」只够回答「这次
多了多少」;要分得清「这一次新抓到的」和「之前抓到、这一次才补导进来的」,还得
知道每一篇是**哪一次业务抓取发现**的 —— 而那件事只有采集器自己的持久化记录
记得住(订阅侧 `feed_items.fetch_round`、公众号侧 `article_fetch_rounds`)。
路径当不了这个身份:它随标题变,也不区分「头一回落下」和「正文补抓重写一遍」。
口径与两个数在补导场景下为什么必然不等,见 docs/decisions/daily.md。

**交回执的那一刻是「文件已经落进 vault」,不是「这一篇的后续记账也成了」。**
一篇 md 替换成功之后,它就会被索引、会进当天日报取材;采集器自己那边随后的
确认(feeds.db 的 exported_hash、本地库的已导标记)失败只意味着"下一轮还会再
导一次",不许让这一篇从回执里消失——消失之后重导时它已经不是新文件,这一篇
永远补不回账(第 1 轮审查实测:vault 多了 1 篇,tooltip 永久写 0 条且自称确定)。
整段物化半途炸掉、清单只剩一半时返回 None,不要返回那半份冒充完整。

新增来源 = 写一个实现 + 在 _REGISTRY 里注册。想让"订阅了哪些号/源"能在
面板上增删,再实现一份 TargetManager(可选,见下);想让面板上那份失败明细里的
单条内容能被用户当场排除掉,再实现一份 ItemDropping(可选,见
docs/decisions/failure-drop.md)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import NamedTuple, Protocol, runtime_checkable
from kb.source_types import feed_types


class Fetched(NamedTuple):
    """回执里的一条成果:谁、哪一次抓到的、这一次落在 vault 哪儿。

    `content_id` 是**稳定内容身份**,写成 `"<采集器>:<它自己的主键>"`
    (`rss:9f3c…`、`wechat_local:2247…`)。它必须跨轮次、跨改名、跨重导出都
    不变 —— 账本靠它取并集去重,一条成果被重放、被两条通道各报一次、被 fsync
    失败后重试追加,折出来的条数都得一样。**路径当不了这个身份**:标题一改
    文件名就变,同一篇会连着几天被报成"刚抓到"。

    `fetch_id` 是**发现它的那一次业务抓取**(`fetch_rounds.round_key()`)。它等于
    当前轮次 = 这一次新抓到的;不等且非空 = 之前抓到、这一次才补导进来的;
    **空串 = 说不清**(存量内容、手工导入链接、采集器还没来得及记)。空串不许
    当成「不是本次」—— 那是把"不知道"写成了"确定不是",而面板上那句分解会据此
    说出一个它其实不知道的事实。

    `path` 是这一次写下去的 vault 相对路径,只给账本做**取材范围过滤**用
    (`fetch_rounds.in_scope`:衍生树、停用的树、skip_dirs 不算)。范围是知识库
    自己的事,不交给每个采集器各判一遍。
    """

    content_id: str
    fetch_id: str
    path: str


@dataclass(frozen=True)
class SourceStatus:
    """采集器健康状况,给管道面板显示。

    online 与 ready 分开:采集器进程在(online)但授权过期(not ready)是常态,
    面板要区别对待——前者抓取阶段直接跳过,后者要提示用户去续期。
    """

    online: bool
    ready: bool = False
    message: str = ""                       # 面板上的一句话,已经是成品文案
    hint: str = ""                          # 不 ready 时怎么处置
    console_url: str = ""                   # 采集器自己的管理页(扫码续期等),没有则不显示
    targets: tuple[str, ...] = ()           # 可筛选的子集(公众号名 / RSS 源名…)

    def as_dict(self) -> dict:
        """给 HTTP 层用。字段刻意扁平:前端只显示不解析,换采集器不用改前端。"""
        return {
            "online": self.online,
            "ready": self.ready,
            "message": self.message,
            "hint": self.hint,
            "console_url": self.console_url,
        }


@dataclass(frozen=True)
class FetchScope:
    """一次采集的范围。全空 = 增量拉最新。"""

    targets: tuple[str, ...] = field(default=())    # 空 = 全部
    since: str = ""                                 # YYYY-MM-DD
    until: str = ""
    force: bool = False                         # 手动重试可跳过来源退避

    @property
    def ranged(self) -> bool:
        """是否为指定区间的补抓(而非日常增量)。"""
        return bool(self.since and self.until)


@dataclass(frozen=True)
class Coverage:
    """一次采集**真跑成了哪几个目标** —— 结清旧失败的唯一依据。

    请求的范围不是执行的范围:退避期内的那几条压根不取文、缺配置的那几条不执行、
    预算用尽的那几条留到下一轮、采集期间被改指的那一条结果作废。**拿请求范围当
    覆盖范围的后果是静默丢数据**:订阅 A 抓取失败后半小时内再跑一次同样范围的普通
    同步,它一次都没被取文(`deferred`),而那一轮以「检查 0/1 个来源」绿色收尾 ——
    这份绿色会把 A 今天早些时候那笔失败结清,面板上「重试失败项」当场消失,A 的
    `last_error` 却还在原处(审查 R1 实测)。

    - `targets`:这一轮**真跑过而且成了**的那几个目标身份(失败的不算 —— 它要留在
      账上等重试;没轮到的更不算)。
    - `whole`:这一步把这条来源**整个**跑了一遍、而且一个目标都没剩下。只有这一档
      才敢把这个来源这一步的旧账清空。
    - 两样都空/假 = **零覆盖**:这一轮一个目标都没真跑成,旧账一格都不许动。

    **报不出来(返回 `None`)就退回请求范围**(存量行为):这是可选能力,不实现它
    的采集器照旧按「这一轮计划了谁」记账。消费方在 `kb/pipeline_results.py`。
    """

    targets: tuple[str, ...] = ()
    whole: bool = False


@runtime_checkable
class SourceCollector(Protocol):
    """采集器契约。实现见 kb.collectors.wechat_local。"""

    name: str                               # 稳定标识,配置里用
    label: str                              # 面板上的人话名字
    range_hint: str                         # 区间补抓的注意事项(留空则面板用通用文案)

    def status(self, targets: tuple[str, ...] = ()) -> SourceStatus:
        """现在抓不抓得动。给了 targets 就**只对这几个目标作答**。

        **整条通道有能力,不等于这一次要抓的那几个有能力。** 「重试失败项」只重抓
        上一轮失败的那几条,而就绪在有些来源那边是逐个订阅的(X 账号各自的取法)
        —— 拿整条通道的就绪放行一次只抓 A 的重试,等于用 B 能抓证明 A 能抓,而 A
        必定再失败一次,那一轮白跑还会把手上那份诊断顶掉(见 `pipeline_retry`
        的 `_BLOCKED_PREFIX`)。

        `targets` 用订阅的 id 或名字,口径同 `FetchScope.targets`(真正去抓的那
        一批就是按它挑的)。**逐个目标没有就绪这个概念的来源原样忽略它**,照旧答
        整条通道 —— 那不是糊弄:本机库那条路的前提是"采集器装没装好、那个库读不
        读得出来",跟抓哪几个号确实无关。

        `targets` 里带着这次范围内**启用着的**那几个,好让调用方分得清"这几个抓
        不动"和"这几个压根不在订阅里"(后者答不了,照旧放行)。
        """
        ...

    def fetch(self, scope: FetchScope) -> Coverage | None:
        """把远端内容拉到本地。

        返回**这一轮真跑成了哪几个目标**(`Coverage`),说不出来返回 `None` ——
        那一档按请求范围记账(存量行为,理由和事故形态在 `Coverage` 里)。
        """
        ...

    def materialize(self) -> frozenset[Fetched] | None:
        """落 md。返回这一轮新写的那几篇(见 `Fetched`),说不出来返回 None。

        判据是"文件已经进了 vault",不是"后续记账也成了"(见模块开头)。
        """
        ...

    def pending(self) -> int | None:
        """还有多少条没落进 vault。算不出返回 None(面板显示空白而非 0)。"""
        ...


# ── 目标管理(可选能力)────────────────────────────────────────────────────────


class CollectorError(RuntimeError):
    """采集器操作失败。

    message 已经是给用户看的成品文案(带处置建议),HTTP 层原样透传、
    前端原样显示——知识库不解读来源的失败原因,否则"凭证怎么续期"
    这类只有采集器知道的知识又会渗回主干。
    """


class DropIncomplete(CollectorError):
    """永久排除已提交，但已导出内容还需要重试清理。"""


class CollectorTimeout(CollectorError):
    """采集器调用超时，不能据此断言操作没有生效。"""


@dataclass(frozen=True)
class Target:
    """来源里的一个采集目标:一个公众号、一个 RSS 源、一个频道…"""

    id: str                                 # 采集器内部标识(fakeid / feed url…)
    name: str                               # 档案键:各处引用它,不随对方改名而变
    # 目标在来源那边的当前名字。跟 name 分开是因为对方会改名(公众号尤其常见),
    # 而 name 一旦被文章、导出目录引用就动不了了。空 = 没核对过或就等于 name。
    display_name: str = ""
    enabled: bool = True                    # 是否参与采集
    detail: str = ""                        # 副标题(最近采集时间 / 简介),成品文案
    # 现在能不能采到它。None = 这种来源没有"逐个目标就绪"的概念(RSS 拉就完了),
    # 面板据此完全不显示就绪那一列;有的来源则是逐个授权的(绿泡泡要每个号
    # 各自的文章凭证),差哪几个必须能一眼看出来,否则只能抓完看谁失败。
    ready: bool | None = None
    state: str = ""                         # 就绪状态的成品文案("凭证有效"/"缺凭证")
    # 就绪状态的色调,给面板上那颗小圆点用:ok 绿 / warn 黄 / bad 红。
    # 跟 ready 分开是因为 ready 要参与"多少个可采"的计数,只能是是非题,
    # 而中间状态(凭证放太久、没验证过)既不该计入可采、也不该跟"压根没有"同色。
    tone: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name,
                "display_name": self.display_name, "enabled": self.enabled,
                "detail": self.detail, "ready": self.ready,
                "state": self.state, "tone": self.tone}


@runtime_checkable
class TargetManager(Protocol):
    """采集器可以额外实现这套方法,把"订阅了哪些号"变成面板上可维护的东西。

    没实现的采集器一样能用(面板不显示管理入口)——目标集合本来就不是每种
    来源都有的概念,比如"抓某个固定 API"就只有一个隐含目标。

    失败一律抛 CollectorError,不返回错误码:这几个是用户点一下等结果的
    同步操作,与 fetch/materialize 那种"红一格但管道接着跑"的语义不同。
    """

    def list_targets(self) -> list[Target]: ...

    def search_targets(self, query: str) -> list[Target]:
        """按关键词找还没订阅的候选。不支持搜索的来源返回空列表。"""
        ...

    def add_target(self, name: str, target_id: str = "") -> str:
        """加入订阅,返回一句成品结果文案。target_id 来自 search_targets。"""
        ...

    def remove_target(self, name: str) -> str: ...

    def set_target_enabled(self, name: str, enabled: bool) -> str: ...


def target_manager(collector: SourceCollector | None = None) -> TargetManager | None:
    """当前采集器支持目标管理就返回它,否则 None。"""
    c = collector or active_collector()
    return c if isinstance(c, TargetManager) else None


# ── 「重试失败项」的范围收窄(可选能力)──────────────────────────────────────


@runtime_checkable
class RetryNarrowing(Protocol):
    """能回答「上一轮失败的那几个目标,现在还要重抓哪些」的采集器。

    名单是采集器自己在失败那一刻留下的(见 `pipeline_segments.note_retry_targets`),
    这里只回答"它们现在还要不要抓" —— 中间用户可能改过设置、也可能已经单独同步
    成功了,而这两件事只有采集器分得清。

    **不支持这个能力不是缺陷,是另一种真实情况**:公众号的失败是文章级的,而它的
    `targets` 是账号级的,压根对不上 —— 返回 None(没有这个方法)的来源一律按原
    范围重跑,比收窄成一个对不上的账号名单安全。
    """

    def retry_targets(self, note: dict) -> list[str]:
        """note 形如 {"targets": [稳定身份...], "at": 记名单的时刻}。"""
        ...


def retry_narrower(collector: SourceCollector) -> RetryNarrowing | None:
    """这个采集器能不能把重试范围收窄到失败项;不能返回 None。"""
    return collector if isinstance(collector, RetryNarrowing) else None


# ── 目标的稳定身份(可选能力)────────────────────────────────────────────────


@runtime_checkable
class TargetIdentity(Protocol):
    """能把一次请求里的目标**归一到稳定身份**的采集器。

    一次请求里的目标可以写名字也可以写编号(`FetchScope.targets` 的口径就是两种都
    收),而**记账只能有一种键**:同一条订阅在「请求里叫 A」和「失败名单里叫 63」
    两种写法下记成两格,后来成功清掉其中一格、另一格的红就永远留着 —— 页面说这一
    步做完了,又说今天还有一步没补上,点下去后端回一句「没有要补的来源」
    (第 3 轮审查 R3 实测)。所以记账之前先过这一道。

    **不支持这个能力不是缺陷**:公众号那条路的目标就是账号名,它本身已经是稳定
    身份,没有第二种写法。没有这个方法的来源一律把请求里那几个原样记下来。
    """

    def stable_targets(self, targets) -> list[str]:
        """`targets` 是请求里那几个(名字或编号都可能),返回稳定身份。

        **一个都匹配不上的那几个要原样留着**(可能刚被删掉或改了名)—— 丢掉等于把
        那一格的欠账从账上摘掉,而漏记是静默丢数据。
        """
        ...


def target_identity(collector: SourceCollector) -> TargetIdentity | None:
    """这个采集器能不能把目标归一到稳定身份;不能返回 None。"""
    return collector if isinstance(collector, TargetIdentity) else None


# ── 单条内容的「别再抓了」(可选能力)─────────────────────────────────────────


@runtime_checkable
class ItemDropping(Protocol):
    """能把**某一条内容**从采集范围里永久去掉的采集器。

    面板上那份失败明细里,有一类条目说的是「这一篇一直缺正文」。它可能真的
    已经没有内容了(原文被删、只有图片),而管道会按退避时间一轮一轮地重试它 ——
    用户点开原文核对完,得能当场说一句「这条别再抓了」,否则这几十条会永远挂在
    那里,把真正要处理的问题挤出视野。

    **语义是「永久不再采集」,不是「这一轮跳过」**:实现必须把这条的身份记成
    黑名单,再次被发现时照样拦住。做不到的来源别实现这个协议 —— 面板按
    「有没有这个能力」决定显不显示那颗按钮,假装支持的后果是用户删了一条、
    下一轮它又回来了。

    **幂等**:同一条删两次要照样返回成功(用户会连点,两个标签页也会各点一次)。
    失败一律抛 `CollectorError`(同 TargetManager:这是点一下等结果的同步操作)。
    """

    def drop_item(self, ref: dict) -> str:
        """`ref` 是失败明细里那条的 `drop` 字段,返回一句成品结果文案。"""
        ...


def item_dropper(collector: SourceCollector) -> ItemDropping | None:
    """这个采集器能不能把单条内容永久排除;不能返回 None。"""
    return collector if isinstance(collector, ItemDropping) else None


@runtime_checkable
class ItemAliases(Protocol):
    """同一篇内容的其他可靠身份；不能依赖仍存在的暂存行。"""

    def item_aliases(self, ref: dict) -> tuple[str, ...]: ...


def item_aliases(collector: SourceCollector, ref: dict) -> tuple[str, ...]:
    identities = [str(ref.get('id') or '')]
    if isinstance(collector, ItemAliases):
        identities.extend(collector.item_aliases(ref))
    return tuple(dict.fromkeys(value for value in identities if value))


@runtime_checkable
class DocumentFailures(Protocol):
    """从已入库文档核实失败条目的采集身份；不属于自己的文档返回 None。"""

    def document_failure(self, document: dict, reason: str) -> dict | None:
        ...


def document_failure(document, reason: str) -> dict:
    """提取阶段保留原文；采集器核实身份后才附上永久排除入口。

    身份查询失败不能盖掉原始故障，也不能据标题或路径猜出可删除的身份。
    """
    document = dict(document)
    result = {"title": document.get("title") or "未命名内容", "reason": reason}
    if document.get("url"):
        result["url"] = document["url"]
    for channel in _REGISTRY:
        try:
            collector = get_collector(channel)
            if isinstance(collector, DocumentFailures):
                if detail := collector.document_failure(document, reason):
                    return detail
        except Exception:
            continue
    return result


# ── 注册表 ──────────────────────────────────────────────────────────────────────
# 目前只有一个来源。多来源时这里改成 list,管道对每个采集器跑一遍即可
# ——fetch/materialize 之后的阶段(索引/提取/条目化)本来就是来源无关的。

_DEFAULT_CHANNEL = "wechat_local"

_REGISTRY: dict[str, str] = {
    # name -> "模块路径:属性名",延迟导入:采集器实现可能拖着 requests 等依赖,
    # 而 kb.cli / kb.mcp_server 这些不碰管道的入口不该为此付启动成本
    "wechat_local": "kb.collectors.wechat_local:collector",
    **{kind: "kb.collectors.feeds:create_collector" for kind in feed_types()},
}

# ── 退役通道名的别名 ──────────────────────────────────────────────────────────
# 公众号曾经有两条通道二选一:wechat_oa 用逐号文章凭证(凭证靠 mitm 代理从绿泡泡
# 客户端流量里截)、wechat_local 读本机已经同步下来的推送。它们抓的是同一批号、
# 写进同一套订阅和文章库,差别只在"从哪儿发现文章",所以主干只留了后一条,
# 前一条整条搬去 wechat-oa 分支。
#
# 别名表不能跟着一起删:盘上那些"抓哪几个来源"的配置(定时范围、日报范围)是按
# 通道名存的,老配置里写着的正是 wechat_oa。当成"不认识的通道"丢掉的话,公众号
# 从此不再定时同步、不再进日报,而面板上完全看不出少了什么。
_RETIRED_CHANNELS: dict[str, str] = {"wechat_oa": "wechat_local"}


def resolve_channel(name: str) -> str:
    """把存下来的通道名换成现在认得的那个(认得的原样返回)。"""
    return _RETIRED_CHANNELS.get(name, name)


def retired_channels() -> dict[str, str]:
    """退役通道名 → 接替它的那条。给浏览器那一侧用。

    `kb-sync-channel` / `kb-sync-channels` 这些选择存在 localStorage 里,后端那套
    `resolve_channel` 管不到。前端不该自己写一份 `wechat_oa` 的字面量——两份清单
    迟早漂,而漂的后果是升级后打开新建同步时公众号被静默地漏掉。
    """
    return dict(_RETIRED_CHANNELS)


def known_channels(names) -> list[str]:
    """盘上存的来源名 → 当前认得出的那些(先 resolve,再去重保序)。

    读存量配置用它;写配置那一侧要的是"认不出就报错",别用这个。
    """
    out: list[str] = []
    seen: set[str] = set()
    for name in names or []:
        resolved = resolve_channel(str(name))
        if resolved in _REGISTRY and resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def get_collector(name: str | None = None) -> SourceCollector:
    """按名字取采集器。name 为空时取 KB_COLLECTOR 环境变量,再回落到公众号那条。"""
    key = resolve_channel(name or os.getenv("KB_COLLECTOR", "").strip()
                          or _DEFAULT_CHANNEL)
    try:
        target = _REGISTRY[key]
    except KeyError:
        raise ValueError(
            f"未知采集器 {key!r},已注册: {', '.join(sorted(_REGISTRY))}"
        ) from None
    module_path, _, attr = target.partition(":")
    from importlib import import_module

    resolved = getattr(import_module(module_path), attr)
    return resolved(key) if attr == "create_collector" else resolved


def active_collector(channel: str | None = None) -> SourceCollector:
    """当前生效的采集器。管道各阶段的唯一入口。"""
    return get_collector(channel)


def wechat_channel() -> str:
    """公众号那条通道的 id。

    留着这个名字是因为"默认范围里的绿泡泡是哪条"有好几个读取方(定时同步、
    日报),它们不该各写一个字面量。
    """
    return _DEFAULT_CHANNEL


def _channel_info(key: str) -> dict:
    c = get_collector(key)
    return {"id": key, "label": getattr(c, "channel_label", c.label),
            "hint": getattr(c, "channel_hint", ""), "range_hint": c.range_hint}


def collection_channels() -> list[dict]:
    """可以勾选的来源通道。"""
    return [_channel_info(key) for key in _REGISTRY]


def channel_labels() -> dict[str, str]:
    """通道 id → 人话名字,**含已经退役的那些**。

    历史运行记录里写着的可能正是一条退役通道("上次来源:…"),面板得能把它
    显示成人话,而不是一个裸 id。退役的那条指向接替它的通道,名字自然一致。
    """
    labels = {key: _channel_info(key)["label"] for key in _REGISTRY}
    return {**{old: labels[new] for old, new in _RETIRED_CHANNELS.items()
               if new in labels}, **labels}
