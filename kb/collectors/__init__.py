# -*- coding: utf-8 -*-
"""来源采集器契约:知识库怎么从外部世界拿到文章。

知识库主干只认这一层抽象,不认识微信、RSS 或任何具体来源——换来源、换
采集方式(公众平台爬取 / 抓包 / 手工投喂)都只动实现,管道一行不改。

一个采集器负责两步:

    fetch()        远端 → 采集器本地。慢,受对方反爬与限流约束。
    materialize()  采集器本地 → obs md。落进 vault 之后就是知识库自己的事。

两步都允许是空操作:一步到位的采集器把 fetch 写成 no-op 即可;反过来,
采集器要是自带常驻库(先落库再转 md),两步就各司其职。

两个方法都直接调 kb.pipeline_state._stage 汇报进度,与其它阶段一致;
返回值没有意义,失败通过 _stage(..., "error", ...) 表达而不是抛异常
——管道要的是"这一阶段红了但后面接着跑",不是整条链断掉。

新增来源 = 写一个实现 + 在 _REGISTRY 里注册。想让"订阅了哪些号/源"能在
面板上增删,再实现一份 TargetManager(可选,见下)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from kb.source_types import feed_types


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


@runtime_checkable
class SourceCollector(Protocol):
    """采集器契约。实现见 kb.collectors.wechat_local。"""

    name: str                               # 稳定标识,配置里用
    label: str                              # 面板上的人话名字
    range_hint: str                         # 区间补抓的注意事项(留空则面板用通用文案)

    def status(self) -> SourceStatus: ...

    def fetch(self, scope: FetchScope) -> None: ...

    def materialize(self) -> None: ...

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
    # 面板据此完全不显示就绪那一列;有的来源则是逐个授权的(微信要每个号
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
