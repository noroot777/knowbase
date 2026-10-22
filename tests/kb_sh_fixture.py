# -*- coding: utf-8 -*-
"""命令级测试的共享夹具:「跑 `kb.sh` 要带上哪些文件」只算一次,四处都过它。

四个命令级测试各自把 `kb.sh` 拷进一个临时目录跑**真脚本**(uv 走 PATH 上的桩):
`test_kb_web_start`、`test_kb_grab_cmd`、`test_kb_graph_guard`、`test_kb_code_stale`。
`kb.sh` 顶层 source 进来的文件**必须一起拷过去** —— 否则隔离环境里脚本一加载就炸,
而**红出来的话指不到「夹具没拷全」这件事**:实测一次按 700 行上限拆文件的改动让
25 条用例红在四个互不相关的文件上(图谱闸门、grab 命令、code_stale 提示、web start),
看起来像 `kb.sh` 整个坏了。`test_kb_grab_cmd` 里那条注释早就写下过同一个形态
(「少拷一个,隔离环境里那条命令一 import 就炸」)—— 它当时按实例修在了自己那一处,
于是下一处照样踩。所以这一份是**结构级**的:夹具清单和脚本里的 source 行同源。

**清单现算,不许改成手工维护的列表。** 口径同 `tests/tiers.py` 的 probes 名单:手工
列表会静默过期 —— 下一个按行数上限拆文件的人不会记得来登记,而症状又是一片指不到
原因的红。守卫在 `tests/test_egress_env.py`(钉住那条正则认得出脚本里的实际写法)。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KB_SH = REPO / 'kb.sh'
#: 顶层的 `. "$ROOT/..."`。只认这一种写法,别的写法由守卫那条红拦住。
_SOURCE_RE = re.compile(r'^\s*\.\s+"\$ROOT/([^"]+)"', re.M)


def sourced() -> list[str]:
    """`kb.sh` source 进来的那些仓库内相对路径。"""
    return _SOURCE_RE.findall(KB_SH.read_text(encoding='utf-8'))


def install(root: Path, *extra: str) -> Path:
    """把 `kb.sh` 和它 source 的文件拷进 `root`,返回那份 `kb.sh` 的路径。

    `extra` 给那些**某一条命令**才用到的仓库内文件(比如 grab 那四件套)—— 它们不是
    加载时就要,所以不进 `sourced()`,但同样少拷一个就炸。
    """
    root.mkdir(parents=True, exist_ok=True)
    target = root / 'kb.sh'
    shutil.copy2(KB_SH, target)
    target.chmod(0o755)
    for rel in [*sourced(), *extra]:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dest)
    return target
