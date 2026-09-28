# -*- coding: utf-8 -*-
"""全局配置:vault 路径、数据库位置、来源定义。

设计原则:所有"哪里有内容"的知识集中在这一个文件;
新增一种来源(博客/RSS/播客)只需在 SOURCES 加一项。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from kb.source_types import feed_types

# obs vault 根
VAULT_ROOT = Path(os.environ.get("KB_VAULT_ROOT", str(Path.home() / "obs" / "new-start")))

# 项目数据目录(索引数据库等,不进 git)
DATA_DIR = Path(os.environ.get("KB_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DB_PATH = DATA_DIR / "kb.db"

# 本地 embedding 模型(bge-m3:中英混合效果好,1024 维 dense)
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024

# 模型跑在子进程里(见 kb/embed_worker.py:MPS 显存池只涨不退,进程内收不回)。
# 空闲这么多秒后子进程自杀,把 ~3.7 GB 还给系统;下次要用重新起,冷启动 3.2 秒。
# 设 0 或负数 = 不自动回收(worker 一直留着)。
EMBED_IDLE_SECONDS = int(os.environ.get("KB_EMBED_IDLE_SECONDS", "600"))
# 应急开关:置 1 退回「模型直接加载在本进程」的老行为。子进程路径出问题时能
# 保住检索可用,代价是这个进程的内存再也降不下来。
EMBED_IN_PROCESS = os.environ.get("KB_EMBED_IN_PROCESS", "") == "1"
# 一批编码的「B × 字符数²」预算,决定批量索引的峰值内存(注意力张量随长度平方涨,
# MPS 显存池会涨到见过的最大批次那个规模)。单位是 B×L² 的乘积,写成
# 「几条 × 多长 × 多长」便于估算。
# 2026-09-08 实测(1536 条真实 chunk,p50 长度 632):
#   32×768²(原值) 峰值 17.0 GB  14.7 chunk/s   ← 36 GB 的机器上会打到 swap
#    8×768²        峰值  8.7 GB   9.0 chunk/s   ← 曲线拐点,取它
#    4×768²        峰值  8.0 GB   8.8 chunk/s   ← 再降换不来多少内存了
# 短 chunk 不受影响(200 字以内三档都是满批 32);预算只在长 chunk 上收紧批次。
EMBED_BATCH_BUDGET = int(os.environ.get("KB_EMBED_BATCH_BUDGET",
                                        str(8 * 768 * 768)))
# 单次编码请求最多等多久。宽到正常绝不会碰到(一批 128 条实测 ~15 秒),它防的是
# worker 卡死或管道错位——那种情况下父进程会攥着锁永久阻塞,搜索和索引一起冻住。
EMBED_REQUEST_TIMEOUT = float(os.environ.get("KB_EMBED_REQUEST_TIMEOUT", "600"))


@dataclass
class Source:
    """一种内容来源(obs 里的一棵树)。"""
    name: str                 # 来源标识,与 frontmatter source 字段对应
    root: Path                # 扫描根目录
    # 该来源是否已有 entity/topic 标签(dr-youtube-dig 的管线自带,直接解析;
    # 没有的来源走 Claude 提取)
    pre_tagged: bool = False
    # 跳过的子目录名(相对 root 的一级目录)
    skip_dirs: frozenset[str] = field(default_factory=frozenset)
    # 由其它来源条目化/转写出来的衍生树,内容与原文重叠。
    # 文章级枚举("这段时间有什么文章/信息")未显式指定来源时只看非衍生来源,
    # 避免同一内容以原文+条目两种形态重复出现;实体级枚举不受影响
    derived: bool = False


SOURCES: list[Source] = [
    *(Source(name=kind, root=VAULT_ROOT / "source" / kind) for kind in feed_types()),
    Source(
        # claude-dr 生成的 AI 日报(英文博客/X/HN/AI 资讯,每天一篇)。
        # 内容是总结形态,但这些信息在本地只有此形态且与其它来源不重叠,按原始来源对待;
        # deep-dives 是单 URL 深挖(质量参差)、_preview 是 prompt 调试预览稿,都不入库
        name="dr",
        root=VAULT_ROOT / "source" / "dr",
        skip_dirs=frozenset({"deep-dives", "_preview"}),
    ),
    Source(
        name="dr-gzh",
        root=VAULT_ROOT / "source" / "dr-gzh",
    ),
    Source(
        name="dr-youtube-dig",
        root=VAULT_ROOT / "source" / "dr-youtube-dig",
        pre_tagged=True,
        skip_dirs=frozenset({"_graph"}),  # 聚合页不是文档本体
    ),
    Source(
        # daily-report 视频原字幕的项目条目转写
        # (scripts/video_projects_export.py 产物,一个项目一个 md,自带标签)
        name="dr-video-projects",
        root=VAULT_ROOT / "source" / "dr-video-projects",
        pre_tagged=True,
        derived=True,
    ),
    Source(
        # 公众号文章的工具/项目条目化
        # (scripts/gzh_projects_export.py 产物,一个工具一个 md,自带标签)
        name="dr-gzh-projects",
        root=VAULT_ROOT / "source" / "dr-gzh-projects",
        pre_tagged=True,
        derived=True,
    ),
    Source(
        name="gzh",  # 实时 saver 的树(gzh_saver.py 产物,无 tags)
        root=VAULT_ROOT / "source" / "gzh",
    ),
]

# 原始来源名(非衍生),文章级枚举未指定 source 时的默认扫描范围
PRIMARY_SOURCE_NAMES: list[str] = [s.name for s in SOURCES if not s.derived]

# ── 来源组 ────────────────────────────────────────────────────────────────────
# 同一内容域的原始来源 + 条目化衍生树打包成组:查"视频里的工具"必须同时覆盖
# dr-youtube-dig(概览摘要)和 dr-video-projects(逐工具条目),单选其一都会漏。
SOURCE_GROUPS: dict[str, list[str]] = {
    "gzh": ["dr-gzh", "dr-gzh-projects", "gzh"],
    "video": ["dr-youtube-dig", "dr-video-projects"],
    "dr": ["dr"],
    **{kind: [kind] for kind in feed_types()},
}

# 组的自然语言别名(问答规划/工具参数里用户或模型可能给的写法)
SOURCE_ALIASES: dict[str, str] = {
    "公众号": "gzh", "微信": "gzh", "wechat": "gzh", "公众号系": "gzh",
    "视频": "video", "视频系": "video", "youtube": "video", "yt": "video",
    "日报": "dr", "daily": "dr", "ai日报": "dr",
    "大v观点": "social", "大v": "social", "推特": "social", "x": "social",
    "rss订阅": "rss",
    **{spec.label.lower(): kind for kind, spec in feed_types().items()},
}

def expand_sources(value) -> list[str] | None:
    """把 source 参数归一成来源名列表:None=不过滤;组名/别名展开;
    具体来源名原样保留;列表逐项展开去重(保序)。未知名字原样传下去
    (SQL 过滤自然得到空集,比吞掉错误直观)。"""
    if value is None or value == "":
        return None
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for it in items:
        key = str(it).strip()
        if not key:
            continue
        g = SOURCE_ALIASES.get(key.lower(), key.lower())
        if g in SOURCE_GROUPS:
            # 组名优先:"gzh"/"dr" 同时是来源名,但按组展开永远是超集,语义更符合意图
            out.extend(SOURCE_GROUPS[g])
        else:
            out.append(key)
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))] or None
