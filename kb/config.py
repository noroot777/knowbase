# -*- coding: utf-8 -*-
"""全局配置:vault 路径、数据库位置、来源定义。

设计原则:所有"哪里有内容"的知识集中在这一个文件;
新增一种来源(博客/RSS/播客)只需在 SOURCES 加一项。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# obs vault 根
VAULT_ROOT = Path.home() / "obs" / "new-start"

# 项目数据目录(索引数据库等,不进 git)
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "kb.db"

# 本地 embedding 模型(bge-m3:中英混合效果好,1024 维 dense)
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024


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


SOURCES: list[Source] = [
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
        name="gzh",  # 实时 saver 的树(gzh_saver.py 产物,无 tags)
        root=VAULT_ROOT / "source" / "gzh",
    ),
]
