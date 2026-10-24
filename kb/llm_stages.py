# -*- coding: utf-8 -*-
"""各个用到大模型的阶段,分别用哪家供应商的哪个模型。

**硬约束在 `docs/decisions/llm-stages.md` 第一节 —— 改这块之前先读那一份。**

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
    rec: str = ""                       # 推荐档位(见 _TIERS),面板上那行「推荐」
    rec_why: str = ""                   # 一句话说明为什么推荐这一档


# 顺序即面板上的展示顺序:先问答链路,再管道链路。
STAGES: tuple[Stage, ...] = (
    Stage("ask", "问答 · 回答", "对话里综合资料写答案,也是其他阶段的兜底模型",
          legacy="default_model", env="KB_ASK_MODEL", auto="first_with_key",
          rec="main", rec_why="要把几十条检索结果读成一个答案,这步换便宜模型最容易看出来"),
    Stage("plan", "问答 · 意图规划", "把问题解析成检索条件,活轻,用便宜的就行",
          legacy="plan_model", env="KB_PLAN_MODEL", auto="cheap", prefer_last=True,
          rec="cheap", rec_why="只是把一句话拆成关键词和过滤条件,每次提问都跑,越快越好"),
    Stage("agent", "问答 · 研究(工具循环)",
          "agent 模式下反复调检索工具找资料;要走 Anthropic 的 tools 接口,只能选 Claude",
          providers=("anthropic",), legacy="agent_model", env="KB_AGENT_MODEL",
          auto="sonnet",
          rec="main", rec_why="要自己决定再查一轮还是收工,便宜模型往往查一次就交差"),
    Stage("fav", "收藏 · 分诊与行动卡", "给收藏的文章分诊、提炼「怎么上手」的行动卡",
          legacy="fav_model", env="KB_FAV_MODEL", auto="follow_ask",
          rec="main", rec_why="一天也就几条,产出是要照着做的步骤,不值得在这儿省"),
    Stage("extract", "管道 · 提取打标", "读正文提 实体/主题/一句话摘要,每篇一次",
          default="gpt-5.5", env="KB_EXTRACT_MODEL",
          rec="cheap", rec_why="按固定字段抽取、每篇都跑,是管道里调用次数最多的一步"),
    Stage("gate", "管道 · 条目化判定", "先判这篇是不是「工具/项目介绍型」,只看开头,量大",
          default="gpt-5.5", env="KB_GATE_MODEL",
          rec="cheap", rec_why="只看开头答一个是/否,量最大,便宜模型足够"),
    Stage("transcribe", "管道 · 条目化拆条目", "把整篇拆成一个个工具条目,吃全文,最贵的一步",
          default="gpt-5.6-luna", env="KB_TRANSCRIBE_MODEL",
          rec="main", rec_why="图鉴里每个条目的质量上限就在这一步,拆错了后面全是错的"),
    Stage("wiki", "管道 · wiki 摘要与综述", "生成每日摘要页和实体页的「近期动态」",
          default="gpt-5.6-luna", env="KB_WIKI_MODEL",
          rec="main", rec_why="产出是给人读的成段文字,便宜模型的流水账一眼看得出来"),
    Stage("skill_tag", "脚本 · skill 标签回填", "批量判定项目该不该打「skill工具」标签",
          default="gpt-5.6-luna", env="KB_SKILL_TAG_MODEL",
          rec="cheap", rec_why="批量判一个标签打不打,答案只有两种"),
    Stage("taxonomy", "管道 · 图谱归类", "给开源项目/skill 条目分门别类,一个条目判一次后长期复用",
          default="gpt-5.5", env="KB_TAXONOMY_MODEL",
          rec="main", rec_why="要在 45 个子类里挑一个,判完长期缓存,判错得改分类表才会重判"),
    Stage("translate", "来源 · 英文内容翻译", "把 RSS/大V 抓到的英文标题正文译成中文,一条一次后长期复用",
          default="gpt-5.5", env="KB_TRANSLATE_MODEL",
          rec="main", rec_why="译文当成正文长期躺在库里,索引、检索、日报读的都是它,译歪一次就永久错"),
    Stage("daily", "日报 · 当日综述", "把当天入库的全部内容写成一份日报,每天一次",
          default="gpt-5.6-luna", env="KB_DAILY_MODEL",
          rec="main", rec_why="一天只跑一次,产出是每天要读的成段文字,便宜模型的流水账一眼看得出来"),
)

_BY_KEY = {s.key: s for s in STAGES}

# 「便宜模型」的识别词:规划这种解析活用不上旗舰
_CHEAP_HINTS = ("haiku", "mini", "flash", "lite", "nano", "spark")

# ── 推荐档位 ─────────────────────────────────────────────────────────────
# 面板上「推荐」那一行按档位在**实际可选的模型**里挑一个具体 id 出来,注册表里
# 不写死 id:每家中转站开出来的型号不一样(内置默认表写的是 claude-sonnet-4-5,
# 用户那边可能只有 claude-sonnet-4-6),写死一个十有八九不在候选里,那行就成了
# 一句指不到任何选项的废话。命中顺序即偏好顺序。
_TIERS: dict[str, tuple[str, ...]] = {
    "cheap": _CHEAP_HINTS,
    "main": ("sonnet", "gpt-5.6", "gpt-5.5", "gpt-5", "gpt-4", "opus"),
}
# 一个候选都匹配不上时退回这句(没配 key、中转站只开了些别的型号)
_TIER_LABEL = {
    "cheap": "便宜快的那一档(haiku / mini 这类)",
    "main": "主力那一档(sonnet / gpt-5.5 这类)",
}


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
    set_stages({key: {"provider": provider or "", "model": model or ""}})


def set_stages(updates: dict[str, dict]) -> None:
    """先校验全部阶段,再在同一个配置事务内保存;任一项非法则整组不落盘。"""
    entries = {}
    for key, value in updates.items():
        st = stage(key)
        model = (value.get("model") or "").strip()
        entry = None
        if model:
            provider = (value.get("provider") or "").strip() or None
            resolved = llm.parse_model(model, provider=provider)
            if resolved is None:
                raise ValueError(f"认不出模型 {model} 属于哪家,请显式指定供应商")
            if st.providers and resolved.provider not in st.providers:
                raise ValueError(f"「{st.label}」只支持 {'/'.join(st.providers)}")
            entry = {"provider": resolved.provider, "model": resolved.model_id}
        entries[key] = entry

    def mutate(data: dict) -> None:
        stages = data.get("stages")
        stages = dict(stages) if isinstance(stages, dict) else {}
        for key, entry in entries.items():
            if entry is None:
                stages.pop(key, None)
            else:
                stages[key] = entry
            legacy = stage(key).legacy
            if legacy:
                data.pop(legacy, None)
        if stages:
            data["stages"] = stages
        else:
            data.pop("stages", None)

    llm.update_settings(mutate)


def _candidates(st: Stage) -> list[tuple[str, str]]:
    """该阶段可选的 (provider, model_id),受 providers 限制。"""
    provs = st.providers or tuple(llm.PROVIDERS)
    return [(p, m) for p in provs for m in llm.configured_models(p)]


def recommended(st: Stage, candidates: list[tuple[str, str]] | None = None) -> str:
    """该阶段推荐用哪个模型,返回 "provider:model_id";挑不出来返回空串。

    在这个阶段允许的候选里按档位关键词挑第一个命中的,**已配 key 的那几家优先**
    ——推荐一个用不了的型号等于没推荐。挑不出来时面板退回 `_TIER_LABEL` 那句话。
    candidates 可将候选缩到某个预设的供应商和模型系列,阶段本身的供应商限制仍生效。
    """
    hints = _TIERS.get(st.rec)
    if not hints:
        return ""
    cands = _candidates(st) if candidates is None else candidates
    cands = [(provider, model) for provider, model in cands
             if not st.providers or provider in st.providers]
    pool = [c for c in cands if llm.has_key(c[0])] or cands
    for hint in hints:
        for prov, model in pool:
            if hint in model.lower():
                return f"{prov}:{model}"
    return ""


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
            # 推荐:具体 id(挑得出来时)+ 档位说明 + 一句为什么
            "recommend": recommended(st),
            "rec_tier": _TIER_LABEL.get(st.rec, ""),
            "rec_why": st.rec_why,
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
