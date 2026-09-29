# -*- coding: utf-8 -*-
"""各个用到大模型的阶段,分别用哪家供应商的哪个模型。

一个「阶段」= 代码里一处会调大模型的活:问答回答、意图规划、研究工具循环、
收藏分诊、提取打标、条目化 gate/拆条目、wiki 综述、标签回填。以前这些散在
各文件里写死字面量(`EXTRACT_MODEL = "gpt-5.5"`),换一个要翻五个文件;
现在统一在 STAGES 注册,面板里每个阶段一个下拉。

**provider 是显式存下来的,不是从 id 猜的**:同一个 id 在两家中转站都可能
存在(`_provider_of` 按前缀猜 `gpt-*` 归 openai),猜错就是拿 A 家的 key 去
打 B 家的站,报 401 还看不出为什么。面板里的选项按供应商分组,选谁就存谁。

配置落在 kb.llm 的 `data/secrets.json` 的 `stages` 段:
    {"stages": {"extract": {"provider": "openai", "model": "gpt-5.5"}}}
没配的阶段走 Stage.default(内置模型 id),default 为空的走 _auto()——
两条兜底都原样保留了这套设置加进来之前的行为,不配等于没变。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from kb import llm


@dataclass(frozen=True)
class Stage:
    key: str
    label: str                          # 面板上的阶段名
    hint: str                           # 一句话说明这步在干什么
    default: str | None = None          # 内置兜底模型 id(None = 见 _auto)
    providers: tuple[str, ...] = ()     # 限定供应商,空 = 两家都行
    legacy: str | None = None           # secrets.json 里的旧顶层键(兼容老配置)
    env: str | None = None              # 环境变量覆盖
    auto: str = ""                      # 未配置且无 default 时的自动挑选策略
    prefer_last: bool = False           # True = 自动挑选优先于「调用方当前所选模型」


# 顺序即面板上的展示顺序:先问答链路,再管道链路。
STAGES: tuple[Stage, ...] = (
    Stage("ask", "问答 · 回答", "对话里综合资料写答案,也是其他阶段的兜底模型",
          legacy="default_model", env="KB_ASK_MODEL", auto="first_with_key"),
    Stage("plan", "问答 · 意图规划", "把问题解析成检索条件,活轻,用便宜的就行",
          legacy="plan_model", env="KB_PLAN_MODEL", auto="cheap", prefer_last=True),
    Stage("agent", "问答 · 研究(工具循环)",
          "agent 模式下反复调检索工具找资料;要走 Anthropic 的 tools 接口,只能选 Claude",
          providers=("anthropic",), legacy="agent_model", env="KB_AGENT_MODEL",
          auto="sonnet"),
    Stage("fav", "收藏 · 分诊与行动卡", "给收藏的文章分诊、提炼「怎么上手」的行动卡",
          legacy="fav_model", env="KB_FAV_MODEL", auto="follow_ask"),
    Stage("extract", "管道 · 提取打标", "读正文提 实体/主题/一句话摘要,每篇一次",
          default="gpt-5.5", env="KB_EXTRACT_MODEL"),
    Stage("gate", "管道 · 条目化判定", "先判这篇是不是「工具/项目介绍型」,只看开头,量大",
          default="gpt-5.5", env="KB_GATE_MODEL"),
    Stage("transcribe", "管道 · 条目化拆条目", "把整篇拆成一个个工具条目,吃全文,最贵的一步",
          default="gpt-5.6-luna", env="KB_TRANSCRIBE_MODEL"),
    Stage("wiki", "管道 · wiki 摘要与综述", "生成每日摘要页和实体页的「近期动态」",
          default="gpt-5.6-luna", env="KB_WIKI_MODEL"),
    Stage("skill_tag", "脚本 · skill 标签回填", "批量判定项目该不该打「skill工具」标签",
          default="gpt-5.6-luna", env="KB_SKILL_TAG_MODEL"),
    Stage("taxonomy", "管道 · 图谱归类", "给开源项目/skill 条目分门别类,一个条目判一次后长期复用",
          default="gpt-5.5", env="KB_TAXONOMY_MODEL"),
    Stage("translate", "来源 · 英文内容翻译", "把 RSS/大V 抓到的英文标题正文译成中文,一条一次后长期复用",
          default="gpt-5.5", env="KB_TRANSLATE_MODEL"),
    Stage("daily", "日报 · 当日综述", "把当天入库的全部内容写成一份日报,每天一次",
          default="gpt-5.6-luna", env="KB_DAILY_MODEL"),
)

_BY_KEY = {s.key: s for s in STAGES}

# 「便宜模型」的识别词:规划这种解析活用不上旗舰
_CHEAP_HINTS = ("haiku", "mini", "flash", "lite", "nano", "spark")


def stage(key: str) -> Stage:
    st = _BY_KEY.get(key)
    if st is None:
        raise KeyError(f"未知阶段: {key}")
    return st


def _stored() -> dict:
    raw = llm.read_settings().get("stages")
    return raw if isinstance(raw, dict) else {}


def configured(key: str) -> dict | None:
    """该阶段在面板里存下来的 {provider, model};没配返回 None。"""
    ent = _stored().get(key)
    if not isinstance(ent, dict):
        return None
    model = str(ent.get("model") or "").strip()
    prov = str(ent.get("provider") or "").strip()
    if not model:
        return None
    if prov not in llm.PROVIDERS:
        prov = llm.provider_of(model) or ""
    return {"provider": prov, "model": model} if prov else None


def set_stage(key: str, provider: str | None, model: str | None) -> None:
    """保存某阶段的模型。model 传空 = 清除该阶段的配置,回落到内置默认。

    provider 留空时按模型 id 猜一把(命令行/老调用方便),猜不出来才报错——
    面板永远显式传,因为猜错就是拿错家的 key。

    校验先做完,**改盘走 llm.update_settings**:它在锁内重读当前文件再改,
    面板里连着改两个阶段时两次保存才会叠加,而不是后一次把前一次整个盖掉。
    """
    st = stage(key)
    model = (model or "").strip()
    entry = None
    if model:
        # provider 留空时也认 "openai:gpt-5.5" 这种带前缀的写法(收藏页那个只传
        # id 的老下拉、命令行都走得通),都拿不到才按 id 前缀猜,还猜不出就报错
        m = llm.parse_model(model, provider=(provider or "").strip() or None)
        if m is None:
            raise ValueError(f"认不出模型 {model} 属于哪家,请显式指定供应商")
        if st.providers and m.provider not in st.providers:
            raise ValueError(f"「{st.label}」只支持 {'/'.join(st.providers)}")
        entry = {"provider": m.provider, "model": m.model_id}

    def mutate(data: dict) -> None:
        stages = data.get("stages")
        stages = dict(stages) if isinstance(stages, dict) else {}
        if entry is None:
            stages.pop(key, None)
        else:
            stages[key] = entry
        if stages:
            data["stages"] = stages
        else:
            data.pop("stages", None)
        # 旧顶层键留着只会跟新配置打架(读时优先级更低,但用户改不动它),一并清掉
        if st.legacy:
            data.pop(st.legacy, None)

    llm.update_settings(mutate)


def _candidates(st: Stage) -> list[tuple[str, str]]:
    """该阶段可选的 (provider, model_id),受 providers 限制。"""
    provs = st.providers or tuple(llm.PROVIDERS)
    return [(p, m) for p in provs for m in llm.configured_models(p)]


def _auto(st: Stage) -> llm.Model | None:
    """没配也没内置默认时的自动挑选。保留各阶段原来的挑法,行为不变。"""
    if st.auto == "first_with_key":
        with_key = [(p, m) for p, m in _candidates(st) if llm.has_key(p)]
        pick = with_key or _candidates(st)
        return llm.Model(*pick[0]) if pick else llm.Model("anthropic", "claude-sonnet-4-5")
    if st.auto == "cheap":
        cheap = [(p, m) for p, m in _candidates(st) if llm.has_key(p)
                 and any(t in m.lower() for t in _CHEAP_HINTS)]
        return llm.Model(*cheap[0]) if cheap else None
    if st.auto == "sonnet":
        pool = [(p, m) for p, m in _candidates(st) if llm.has_key(p)]
        if not pool:
            return None
        son = next((c for c in pool if "sonnet" in c[1].lower()), pool[0])
        return llm.Model(*son)
    return None


def resolve_stage(key: str, prefer: str | None = None) -> llm.Model | None:
    """该阶段最终用哪个模型。顺序:面板配置 > 环境变量 > 旧顶层键 > 内置默认
    > prefer(调用方当前所选)/自动挑选 > 回落问答默认模型。

    prefer 与自动挑选谁先由 Stage.prefer_last 定,两个阶段的原有语义不一样:
    研究阶段先认用户当前选的 Claude,规划阶段先挑便宜的、实在没有才用用户那个。

    返回 None 只有一种情况:限定了供应商(agent 阶段限 Anthropic)而那家
    没配 key——上层据此退回非 agent 管线。
    """
    st = stage(key)
    cfg = configured(key)
    if cfg:
        return llm.Model(cfg["provider"], cfg["model"])

    def ok(m):
        return m if m and (not st.providers or m.provider in st.providers) else None

    for raw in (os.environ.get(st.env) if st.env else None,
                llm.read_settings().get(st.legacy) if st.legacy else None,
                st.default):
        if not isinstance(raw, str) or not raw.strip():
            continue
        # 认不出家的 id 直接跳过下一条候选 —— 用 llm.resolve 会去问默认模型,
        # 而默认模型自己也在走这段解析,认不出来就成死循环了
        m = ok(llm.parse_model(raw))
        if m:
            return m
    picks = [_auto(st), ok(llm.parse_model(prefer)) if prefer else None]
    if not st.prefer_last:
        picks.reverse()
    for m in picks:
        if m:
            return m
    if st.providers:            # 限定供应商又挑不出来 = 那家没 key
        return None
    return resolve_stage("ask") if key != "ask" else None


def stage_model(key: str, prefer: str | None = None) -> llm.Model:
    """给调用方用的出口:直接丢给 llm.chat / chat_stream。

    解析不出来时抛 RuntimeError,而不是悄悄拿别的模型顶上——批量管道里
    静默换模型比报错更难查(产物风格变了都不知道是哪一步换的)。
    """
    m = resolve_stage(key, prefer)
    if m is None:
        st = stage(key)
        raise RuntimeError(
            f"「{st.label}」没有可用模型:它限定 {'/'.join(st.providers) or '任意'} "
            f"供应商,而那家还没配 API key")
    return m


def describe() -> list[dict]:
    """给设置面板:每个阶段的当前取值 + 来源 + 供应商是否已配 key。"""
    out = []
    for st in STAGES:
        cfg = configured(st.key)
        try:
            eff = resolve_stage(st.key)
        except Exception:
            eff = None
        source = "config" if cfg else _fallback_source(st)
        out.append({
            "key": st.key,
            "label": st.label,
            "hint": st.hint,
            "providers": list(st.providers),
            "default": st.default or "",
            "env": st.env or "",
            "configured": cfg,                       # None = 跟随默认
            "provider": eff.provider if eff else "",
            "model": eff.model_id if eff else "",
            "source": source,
            "ready": bool(eff and llm.has_key(eff.provider)),
        })
    return out


def _fallback_source(st: Stage) -> str:
    if st.env and os.environ.get(st.env):
        return "env"
    if st.legacy and llm.read_settings().get(st.legacy):
        return "legacy"
    if st.default:
        return "default"
    return "auto"
