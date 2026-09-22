#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图谱归类的**提问那一侧**:提示词、调模型、解析回包。

从 taxonomy.py 里拆出来,理由和 taxo_tables.py 一样——那个文件越过了 700 行上限,
而「怎么问模型」是里面最独立的一块:不碰缓存、不碰锁、没有模块级可变状态。

`taxonomy` 把这几个名字原样 import 过去,所以测试里那句 `T._llm = fake` 照旧有效
(`classify` 是按 taxonomy 的模块全局去找 `_llm` / `_parse` / `default_model` 的,
打桩打在哪个模块上就用哪个)。**这也是 `CACHE_DIR` 这类被测试改写的模块变量不能
这么搬的原因**:那种搬走之后 `T.CACHE_DIR = tmp` 只改到一份副本。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PROMPT = """\
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


def llm(prompt: str, model, timeout: int = 300, max_tokens: int = 8192) -> str:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from kb.llm import chat

    return chat(prompt, model, max_tokens=max_tokens, timeout=timeout)


def default_model():
    """用哪家的哪个模型由设置面板的「管道 · 图谱归类」阶段决定,别在这儿写死。"""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from kb.llm_stages import stage_model

    return stage_model("taxonomy")


def parse(out: str) -> dict[int, str]:
    """{编号: code}。解析不出来的条目直接缺席——调用方按「这批少回了一截」处理。"""
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
