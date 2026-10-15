# -*- coding: utf-8 -*-
"""「这个路径算不算一次抓取的新内容」—— 范围口径只有这一份。

从 `kb.fetch_rounds` 里分出来的:那边是**当日抓取账本**(轮次、子轮、凭证、落盘
协议),这边只回答**哪几棵树算成果**。两件事的判据不相干,而账本那份文件顶着
单文件 700 行上限(CLAUDE.md 那条)。

`fetch_rounds.note_new` 是唯一的消费方:先 `_scope_prefixes()` 把那几棵树算一次,
再对回执里每条路径问一次 `in_scope()`。**算不出来(`None`)不许当成空范围** ——
那等于把「答不出来」写成「确定不算」,再让结束行把这一轮定成确定的 0 条。
"""
from __future__ import annotations

from pathlib import Path


def _scope_prefixes() -> list[tuple[tuple[str, ...], frozenset]] | None:
    """算「抓到的新内容」的那些树,各自的 vault 相对前缀 + 要跳过的子目录。

    一次算好交给 `in_scope` 复用:一轮回执有上百条路径,每条都把全部来源 resolve
    一遍是上千次 syscall,而这份清单一轮之内不会变。

    **没读全就交 `None`,不许交一份少了几棵树的清单。** `resolve()` 会因为权限、
    符号链接层数、目录临时不可用而抛 `OSError` —— 那时「这条路径算不算成果」是
    **答不出来**,不是「确定不算」。上一版把这两种压成一种(`return []` / 跳过
    那棵树),于是一次临时故障能把一份真回执筛成空集,再让结束行把这一轮定成
    确定的 0 条(第 2 轮审查静态项)。`ValueError` 是另一件事:那棵树**确定**
    不在 vault 底下,跳过它才是对的。
    """
    from kb.config import SOURCES, VAULT_ROOT

    try:
        root = Path(VAULT_ROOT).resolve()
    except OSError:
        return None
    out = []
    for src in SOURCES:
        if src.derived or not src.enabled:
            continue
        try:
            out.append((Path(src.root).resolve().relative_to(root).parts,
                        src.skip_dirs))
        except OSError:
            return None                 # 这棵树没读全 → 整份范围都不可信
        except ValueError:
            continue                    # 确定不在 vault 底下
    return out


def in_scope(rel: str, prefixes) -> bool:
    """这个路径**算不算「抓到的新内容」** —— 范围口径只有这一份。

    和日报取材认的那批树一致(`daily._scope`):非衍生、启用中、不在 `skip_dirs`
    里。回执由采集器交,但"哪些树算"是知识库自己的事,不能交给每个采集器各判
    一遍 —— 条目化衍生树(`dr-gzh-projects` 等)和原文是同一批内容,日报本来就
    排除它们,算进来的话一轮条目化就能让药丸写出三位数而日报那边一条新的都没有;
    停用的树同理不再进库。

    `prefixes` 由调用方先算好(`_scope_prefixes`)。**算不出来时不许拿空范围
    当答案** —— 那一档归「不可确认」,判据在 `note_new`。
    """
    parts = Path(rel).parts
    for prefix, skip in prefixes:
        if parts[:len(prefix)] != prefix:
            continue
        rest = parts[len(prefix):]
        return not (rest and rest[0] in skip)
    return False
