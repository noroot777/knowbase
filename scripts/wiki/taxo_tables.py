#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两张图谱的**固定分类表**,以及表本身的数据结构(`Cat` / `Taxonomy`)。

单独一个文件是因为 `taxonomy.py` 写到 714 行、越过了 700 行上限;分类表是里面
最独立的一块——纯数据、没有模块级可变状态,谁也不会去改它的绑定,搬出来最安全
(缓存目录 `CACHE_DIR` 那种被测试改写的模块变量就不能搬,搬了 `T.CACHE_DIR = tmp`
只会改到一份副本,另一边照旧读生产目录)。

`taxonomy.py` 里 `from taxo_tables import *` 过去的那几个名字仍然照旧可用
(`T.PROJECTS` / `T.SKILLS` / `T.Taxonomy` / `T.Cat` / `T.TAXONOMIES`)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import blake2b

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
        """分类表指纹:**任何会改变判定的东西**变了就让整份缓存作废、全部重判。

        不能只哈希 code/group/sub。`hint` 是类目说明、`extra_rules` 是 skill
        「本体是软件还是 skill」那条成员资格规则,两者都原样进 prompt;改了它们
        判定就该变,签名不动的话旧结论被永久沿用,页面上还是老那批成员
        (2026-09-22 第 1 轮审查发现)。`fallback` 决定「判不出来落哪」、
        `item_zh` 决定 prompt 里怎么称呼被判的东西,一并算进去。
        """
        h = blake2b(digest_size=8)
        h.update(f"{self.item_zh}\0{self.fallback}\0{self.extra_rules}\0".encode())
        for c in self.cats:
            h.update(f"{c.code}\0{c.group}\0{c.sub}\0{c.hint}\0".encode())
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
