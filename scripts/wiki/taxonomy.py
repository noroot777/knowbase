#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图谱归类:给条目化出来的开源项目 / skill 分门别类。

两张图谱(开源项目、skill)各有一套固定分类表,**类别是写死的**而不是让模型
自由发挥:自由标签每轮都会漂(同一个项目今天叫「命令行工具」明天叫「CLI」),
一漂图谱的分区就换一副样子,"经常看"的上层视角就没了。固定表 + 模型只选
code,分区因此稳定,改类目时 bump `Taxonomy.sig()` 会自动触发全量重判。

判定结果落 `data/taxonomy_{key}.json`,**按 norm 长期复用**:一个项目判一次,
之后每天重生成图谱都是零 LLM。新条目进来才判新增的那几个(build 脚本默认
带 `--max-new` 上限,日常一天也就一两批)。

用哪家的哪个模型由设置面板的「管道 · 图谱归类」阶段决定(kb.llm_stages 的
"taxonomy")。批量判定是「给 40 条 name+summary 选 code」这种活,便宜模型够用,
内置默认取的是实测一批 12 秒的那个。
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from hashlib import blake2b
from pathlib import Path
from threading import Lock

REPO = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO / "data"


# ── 分类表 ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Cat:
    """一个类目。sub 为空 = 单层分类(pill 直接挂在大类下)。"""
    code: str
    group: str
    sub: str = ""
    hint: str = ""

    @property
    def label(self) -> str:
        return self.sub or self.group


@dataclass(frozen=True)
class Taxonomy:
    key: str                       # 缓存文件名与图谱标识
    item_zh: str                   # 提示词里怎么称呼被判的东西
    cats: tuple[Cat, ...]
    fallback: str                  # 判不出/还没判的 code(必须在 cats 里)
    extra_rules: str = ""          # 附加判定规则(如 skill 的「不是 skill 判 X」)
    _by_code: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_code", {c.code: c for c in self.cats})
        assert self.fallback in self._by_code, f"fallback {self.fallback} 不在分类表里"

    def get(self, code: str) -> Cat:
        return self._by_code.get(code) or self._by_code[self.fallback]

    def groups(self) -> list[str]:
        """大类顺序 = 分类表里的出现顺序。"""
        out: list[str] = []
        for c in self.cats:
            if c.group not in out:
                out.append(c.group)
        return out

    def sig(self) -> str:
        """分类表指纹:类目改了就让整份缓存作废、全部重判。"""
        h = blake2b(digest_size=8)
        for c in self.cats:
            h.update(f"{c.code}\0{c.group}\0{c.sub}\0".encode())
        return h.hexdigest()

    def prompt_table(self) -> str:
        lines = []
        for c in self.cats:
            name = f"{c.group} / {c.sub}" if c.sub else c.group
            lines.append(f"{c.code} = {name}" + (f" —— {c.hint}" if c.hint else ""))
        return "\n".join(lines)


# 开源项目:两层。大类是"我平时按什么维度找项目",子类细到能一眼看出用途。
PROJECTS = Taxonomy(
    key="projects",
    item_zh="开源项目",
    fallback="J0",
    cats=(
        Cat("A1", "AI 编程", "编程 Agent 与 CLI", "Claude Code/Codex/aider 这类会自己写代码的终端 agent"),
        Cat("A2", "AI 编程", "IDE 与编辑器扩展", "VSCode/JetBrains 插件、编辑器本体"),
        Cat("A3", "AI 编程", "Agent 配置与增强", "规范/记忆/上下文/子代理/skill 包/启动脚本等给编程 agent 加料的"),
        Cat("A4", "AI 编程", "代码质量与测试", "审查、重构、测试生成、静态分析、调试"),
        Cat("A5", "AI 编程", "模型接入与网关", "API 中转、模型路由、额度调度、多模型代理"),
        Cat("B1", "Agent 与自动化", "Agent 框架与 SDK", "写 agent 用的框架、运行时、协议实现"),
        Cat("B2", "Agent 与自动化", "多 Agent 与工作流编排", "角色分工、任务编排、workflow 引擎"),
        Cat("B3", "Agent 与自动化", "MCP 服务与工具", "MCP server/client、工具市场"),
        Cat("B4", "Agent 与自动化", "浏览器与 GUI 操控", "浏览器自动化、电脑操作、手机操控"),
        Cat("B5", "Agent 与自动化", "流程自动化与机器人", "n8n 类流程编排、定时任务、IM 机器人、爬取之外的日常自动化"),
        Cat("C1", "模型与推理", "模型本体与权重", "开源发布的大模型/多模态模型本身"),
        Cat("C2", "模型与推理", "推理部署与服务化", "推理引擎、本地跑模型、模型服务框架"),
        Cat("C3", "模型与推理", "训练微调与数据合成", "训练框架、微调、RL、数据集构建"),
        Cat("C4", "模型与推理", "评测与基准", "benchmark、评测集、打分工具"),
        Cat("C5", "模型与推理", "量化压缩与加速", "量化、蒸馏、算子与显存优化"),
        Cat("D1", "知识与检索", "RAG 与向量检索", "检索增强、向量库、embedding 管线"),
        Cat("D2", "知识与检索", "笔记与知识库", "笔记软件、个人知识管理、第二大脑"),
        Cat("D3", "知识与检索", "文档解析与 OCR", "PDF/网页/表格解析、版面识别、格式转换"),
        Cat("D4", "知识与检索", "抓取采集与爬虫", "爬虫、订阅采集、数据抓取"),
        Cat("D5", "知识与检索", "搜索与问答", "搜索引擎、站内检索、问答机器人"),
        Cat("E1", "内容创作", "图像生成与编辑", ""),
        Cat("E2", "内容创作", "视频生成与剪辑", ""),
        Cat("E3", "内容创作", "语音合成与识别", "TTS、ASR、语音克隆、实时语音"),
        Cat("E4", "内容创作", "音乐与音频处理", ""),
        Cat("E5", "内容创作", "数字人与直播", ""),
        Cat("E6", "内容创作", "3D 与游戏", "3D 生成、建模、游戏开发与引擎"),
        Cat("E7", "内容创作", "设计与创意生成", "海报/PPT/UI 稿/品牌设计一类的生成工具"),
        Cat("F1", "应用与效率", "终端与命令行", "终端本体、shell 增强、通用 CLI 小工具"),
        Cat("F2", "应用与效率", "桌面与移动应用", "面向普通用户的客户端 App"),
        Cat("F3", "应用与效率", "浏览器扩展与脚本", ""),
        Cat("F4", "应用与效率", "办公文档与表格", "文档协作、表格、待办、项目管理"),
        Cat("F5", "应用与效率", "翻译阅读与信息流", "翻译、RSS、稍后读、播客与资讯聚合"),
        Cat("F6", "应用与效率", "自托管服务与网盘", "私有部署的网盘/影音/家庭服务器一类"),
        Cat("G1", "开发基础设施", "前端与 UI 组件", "前端框架、组件库、样式方案"),
        Cat("G2", "开发基础设施", "后端与数据库", "后端框架、数据库、消息队列、存储"),
        Cat("G3", "开发基础设施", "DevOps 与云原生", "CI/CD、容器、K8s、部署运维平台"),
        Cat("G4", "开发基础设施", "网络与代理", "代理、内网穿透、网络调试"),
        Cat("G5", "开发基础设施", "安全与隐私", "安全审计、渗透、加密、隐私保护"),
        Cat("G6", "开发基础设施", "监控与可观测", "日志、指标、追踪、告警"),
        Cat("H1", "学习与资料", "教程课程与电子书", ""),
        Cat("H2", "学习与资料", "Awesome 清单与资源集", "精选合集、导航站、资源索引"),
        Cat("H3", "学习与资料", "论文与研究实现", "论文开源实现、研究项目"),
        Cat("H4", "学习与资料", "提示词与技巧集", "prompt 合集、系统提示词、玩法技巧"),
        Cat("I1", "机器人与硬件", "具身智能与机器人", ""),
        Cat("I2", "机器人与硬件", "嵌入式与智能硬件", "单片机、边缘设备、IoT、AI 硬件"),
        Cat("J0", "其他", "", "确实不属于上面任何一类,或信息太少判不出来"),
    ),
)

# skill:单层。368 个条目摊到十几个大类刚好,再分子类每格就只剩个位数了。
_SKILL_RULES = """\
先判它到底是不是 skill:用户拿到手的就是 SKILL.md 指令集、技能包/技能合集、
CLAUDE.md 一类规范文件,给 Claude Code/Codex 等 AI 编程工具或 Agent 直接装上用的,
才算 skill。独立软件/平台/服务/模型只是沾边的一律判 X:平台内置技能功能或带
Skill 市场、Agent 框架带技能机制、普通工具"也能作为 skill 安装"、帮你生成或管理
skill 的软件(它产出 skill,但本体是软件)、"技能"泛指人的技能。
判 X 的例子:Claude Code(是编程工具本体)、豆包(是 AI 助手)、book-to-skill
(本体是转换软件)、腾讯云 ADP(平台带 Skill 市场)。
判不是 X 的例子:superpowers(工程规范 Skill)、stop-slop(去 AI 味的技能包)、
awesome-claude-skills(技能合集)。"""

SKILLS = Taxonomy(
    key="skills",
    item_zh="给 AI Agent 用的 skill(技能包)",
    fallback="S0",
    extra_rules=_SKILL_RULES,
    cats=(
        Cat("S1", "工程规范与流程", "", "TDD/开发铁律/提交规范/项目脚手架/协作流程"),
        Cat("S2", "代码质量与调试", "", "代码审查、重构、测试、排错、性能优化"),
        Cat("S3", "设计与前端实现", "", "设计系统、审美品味、网页与 UI 生成"),
        Cat("S4", "图表与可视化", "", "流程图、架构图、数据图表、白板"),
        Cat("S5", "文档与写作", "", "写作风格、文案、技术文档、格式转换、出版排版"),
        Cat("S6", "演示与汇报", "", "PPT、演示文稿、报告、看板"),
        Cat("S7", "研究与学习", "", "学术论文、文献核验、调研、把书/课程蒸馏成技能"),
        Cat("S8", "数据与分析", "", "数据处理、表格分析、爬取清洗、报表"),
        Cat("S9", "多媒体创作", "", "视频、图像、音频、播客、自媒体内容生产"),
        Cat("S10", "办公与生活", "", "邮件、日程、会议、报销、生活助手一类日常自动化"),
        Cat("S11", "安全与运维", "", "安全审计、渗透测试、逆向、部署运维"),
        Cat("S12", "Agent 能力扩展", "", "记忆、上下文工程、浏览器操作、多 Agent 协作、模型与成本调度"),
        Cat("S13", "Skill 合集与元工具", "", "技能合集/插件集/市场,以及写 skill、管 skill 的 skill"),
        Cat("S0", "其他", "", "是 skill 但不属于上面任何一类"),
        Cat("X", "不是 skill", "", "本体是软件/平台/服务/模型,不该出现在 skill 图谱里"),
    ),
)

TAXONOMIES = {t.key: t for t in (PROJECTS, SKILLS)}


# ── 缓存 ───────────────────────────────────────────────────────────────────────

_io_lock = Lock()


def cache_path(taxo: Taxonomy) -> Path:
    return CACHE_DIR / f"taxonomy_{taxo.key}.json"


def load_raw(taxo: Taxonomy) -> tuple[dict[str, str], dict[str, str]]:
    """({norm: code}, {norm: 名字})。分类表指纹对不上(类目改过)就当空缓存,
    触发全量重判。名字只是给人翻这份 JSON 时对得上号,判定不读它。"""
    p = cache_path(taxo)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    if raw.get("sig") != taxo.sig():
        return {}, {}
    items = raw.get("items")
    if not isinstance(items, dict):
        return {}, {}
    codes, names = {}, {}
    for k, v in items.items():
        if isinstance(v, dict) and isinstance(v.get("c"), str):
            codes[k] = v["c"]
            names[k] = str(v.get("n") or "")
    return codes, names


def load_cache(taxo: Taxonomy) -> dict[str, str]:
    return load_raw(taxo)[0]


def save_cache(taxo: Taxonomy, codes: dict[str, str], names: dict[str, str]) -> None:
    """写盘用「唯一临时文件 + rename」:截断重写会让并发读者读到半截 JSON,
    而半截 JSON 被 load_cache 吞成空缓存,下一次 build 就会把几千条重判一遍。"""
    p = cache_path(taxo)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"sig": taxo.sig(),
            "items": {k: {"c": v, "n": names.get(k, "")} for k, v in sorted(codes.items())}}
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    with _io_lock:
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(p)


# ── LLM 批量判定 ───────────────────────────────────────────────────────────────

_PROMPT = """\
下面是一批{item_zh},请逐条判断它属于哪个类目。

类目表(只能从这些 code 里选):
{table}

判定要求:
- 按这个东西**本身是什么/主要用来做什么**归类,不要被它顺带支持的能力带偏。
- 每条只给一个 code,拿不准就选最接近的那个;确实判不出来才给 {fallback}。
{extra}
条目(格式「编号. 名字 :: 简介」):
{items}

只输出 JSON 数组,不要解释、不要代码围栏,每个元素 {{"id": <编号>, "c": "<code>"}}。"""


def _llm(prompt: str, model, timeout: int = 300, max_tokens: int = 8192) -> str:
    import sys
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from kb.llm import chat

    return chat(prompt, model, max_tokens=max_tokens, timeout=timeout)


def default_model():
    import sys
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from kb.llm_stages import stage_model

    return stage_model("taxonomy")


def _parse(out: str) -> dict[int, str]:
    out = re.sub(r"^```(?:json)?|```$", "", out.strip(), flags=re.M).strip()
    try:
        arr = json.loads(out)
    except json.JSONDecodeError:
        # 整段解析不了时退回逐对象扫描:模型偶尔在数组后面多写一句话
        arr = [json.loads(m.group(0)) for m in re.finditer(r"\{[^{}]*\}", out)]
    got = {}
    for x in arr:
        if isinstance(x, dict) and "id" in x and "c" in x:
            try:
                got[int(x["id"])] = str(x["c"]).strip()
            except (TypeError, ValueError):
                continue
    return got


def classify(taxo: Taxonomy, items: list[tuple[str, str, str]], *,
             model=None, workers: int = 6, batch: int = 40,
             max_new: int | None = None, log=print) -> dict[str, str]:
    """items = [(norm, name, summary)],**按重要性排好序**(热度高的在前)。

    返回 {norm: code},只含已判定的(缓存命中 + 本轮新判)。没判到的留给调用方
    落到 fallback:`--max-new` 截断和模型漏判都会留下尾巴,这时候宁可显示成
    「未归类」,也不要瞎猜一个类目混进分区里。
    """
    codes, names = load_raw(taxo)
    names.update({n: nm for n, nm, _ in items})
    pending = [it for it in items if it[0] not in codes]
    if max_new is not None and len(pending) > max_new:
        log(f"  [{taxo.key}] 待判 {len(pending)} 条,本轮只判前 {max_new} 条(按热度)")
        pending = pending[:max_new]
    if not pending:
        log(f"  [{taxo.key}] 全部命中缓存({len(codes)} 条)")
        return codes

    model = model or default_model()
    valid = {c.code for c in taxo.cats}
    log(f"  [{taxo.key}] 新判 {len(pending)} 条,模型 {model},{workers} 并发")
    batches = [pending[i:i + batch] for i in range(0, len(pending), batch)]

    def run(chunk: list[tuple[str, str, str]]) -> dict[str, str]:
        lines = "\n".join(
            f"{i}. {nm} :: {(sm or '(无简介)')[:140]}"
            for i, (_n, nm, sm) in enumerate(chunk))
        prompt = _PROMPT.format(
            item_zh=taxo.item_zh, table=taxo.prompt_table(), fallback=taxo.fallback,
            extra=(taxo.extra_rules + "\n") if taxo.extra_rules else "", items=lines)
        try:
            got = _parse(_llm(prompt, model))
        except Exception as e:                      # 单批失败不该带崩整轮
            log(f"  [{taxo.key}] 一批失败({type(e).__name__}: {e}),留到下次")
            return {}
        return {chunk[i][0]: c for i, c in got.items()
                if 0 <= i < len(chunk) and c in valid}

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(run, batches):
            codes.update(got)
            done += 1
            if done % 5 == 0 or done == len(batches):
                save_cache(taxo, codes, names)      # 中途落盘:被打断也不白跑
                log(f"  [{taxo.key}] {done}/{len(batches)} 批,已判 {len(codes)} 条")
    save_cache(taxo, codes, names)
    return codes
