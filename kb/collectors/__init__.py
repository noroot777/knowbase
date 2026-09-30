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
    """采集器契约。实现见 kb.collectors.wechat_oa。"""

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

    def refresh_readiness(self) -> str:
        """重新探测各目标的就绪状态,返回一句成品结果文案。

        对逐个目标授权的来源,授权是在采集器外面拿到的(微信是人去 Mac 微信里
        打开文章、由抓包工具截下凭证),知识库这边只能"再看一眼有没有变"。
        不区分目标就绪与否的来源直接返回空串即可。
        """
        ...


def target_manager(collector: SourceCollector | None = None) -> TargetManager | None:
    """当前采集器支持目标管理就返回它,否则 None。"""
    c = collector or active_collector()
    return c if isinstance(c, TargetManager) else None


# ── 注册表 ──────────────────────────────────────────────────────────────────────
# 目前只有一个来源。多来源时这里改成 list,管道对每个采集器跑一遍即可
# ——fetch/materialize 之后的阶段(索引/提取/条目化)本来就是来源无关的。

_REGISTRY: dict[str, str] = {
    # name -> "模块路径:属性名",延迟导入:采集器实现可能拖着 requests 等依赖,
    # 而 kb.cli / kb.mcp_server 这些不碰管道的入口不该为此付启动成本
    "wechat_oa": "kb.collectors.wechat_oa:collector",
    "wechat_local": "kb.collectors.wechat_local:collector",
    **{kind: "kb.collectors.feeds:create_collector" for kind in feed_types()},
}

# ── 绿泡泡的两条通道:互斥,选哪条是设置项 ────────────────────────────────────────
# wechat_oa(逐号文章凭证)和 wechat_local(本机已同步的推送)抓的是同一批公众号,
# 写进同一套订阅、同一个文章库、同一份导出,差别只在"从哪儿发现文章"。两条同时开
# 只会把同一批号抓两遍,所以它不是"每次同步现勾"的范围,而是一个设置:选中的那条
# 出现在同步范围里,另一条根本不列出来(见 collection_channels)。
WECHAT_CHANNELS: tuple[str, ...] = ("wechat_local", "wechat_oa")
# 默认本机数据库:它不需要 wxdown、不需要逐号文章凭证,开箱就能用。
_DEFAULT_WECHAT = "wechat_local"
# 设置落在 secrets.json(它早就不只装 key —— 各阶段的模型分配也在里面),
# 借 kb.llm 那套「两把锁 + 唯一临时文件 + rename」的读改写,不另起一套。
_SETTINGS_SECTION = "collectors"


def wechat_channel() -> str:
    """当前生效的公众号采集通道。没配过 / 配了个不认识的值都回落到本机数据库。"""
    from kb import llm

    section = llm.read_settings().get(_SETTINGS_SECTION)
    value = (section or {}).get("wechat") if isinstance(section, dict) else None
    return value if value in WECHAT_CHANNELS else _DEFAULT_WECHAT


def set_wechat_channel(name: str) -> str:
    """把公众号采集通道存进设置,返回存下来的那条。"""
    if name not in WECHAT_CHANNELS:
        raise ValueError(f"未知的公众号采集通道 {name!r},"
                         f"可选: {', '.join(WECHAT_CHANNELS)}")
    from kb import llm

    def mutate(data: dict) -> None:
        section = data.get(_SETTINGS_SECTION)
        if not isinstance(section, dict):
            section = {}
            data[_SETTINGS_SECTION] = section
        section["wechat"] = name

    llm.update_settings(mutate)
    return name


def resolve_channel(name: str) -> str:
    """把存下来的通道名带到当前设置上(非微信通道原样返回)。

    盘上那些"抓哪几个来源"的配置(日报范围、定时范围)是按通道名存的。设置里把
    绿泡泡换到另一条通道之后,存着的旧名字必须跟着走——当成"不在可选列表里"
    丢掉的话,公众号从此不再同步,而面板上完全看不出少了什么。
    """
    return wechat_channel() if name in WECHAT_CHANNELS else name


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
    """按名字取采集器。name 为空时取 KB_COLLECTOR 环境变量,再回落到设置里那条。"""
    key = name or os.getenv("KB_COLLECTOR", "").strip() or wechat_channel()
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


def _channel_info(key: str) -> dict:
    c = get_collector(key)
    return {"id": key, "label": getattr(c, "channel_label", c.label),
            "hint": getattr(c, "channel_hint", ""), "range_hint": c.range_hint}


def collection_channels() -> list[dict]:
    """可以勾选的来源通道。绿泡泡两条互斥,只列设置里选中的那条。"""
    active = wechat_channel()
    return [_channel_info(key) for key in _REGISTRY
            if key not in WECHAT_CHANNELS or key == active]


def channel_labels() -> dict[str, str]:
    """全部通道 id → 人话名字,**含没被选中的那条**。

    历史运行记录里写着的可能正是另一条绿泡泡通道("上次来源:…"),
    面板得能把它显示成人话,而不是一个裸 id。
    """
    return {key: _channel_info(key)["label"] for key in _REGISTRY}


def wechat_channel_options() -> list[dict]:
    """设置面板上那两个候选(顺序即默认在前)。"""
    return [_channel_info(key) for key in WECHAT_CHANNELS]
