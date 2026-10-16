# -*- coding: utf-8 -*-
"""HTTP 层的公共小工具:静态目录、vault 名、时间戳。

各 router 模块共用,单独拎出来避免互相 import 成环。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from kb.config import VAULT_ROOT, ScheduleUnreadable
from kb.scanner import body_version, file_hash, read_md_text

_STATIC = Path(__file__).resolve().parent.parent / "static"

# obsidian://open 深链需要 vault 名(obs 目录名)
VAULT_NAME = VAULT_ROOT.name


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_doc_text(path: str) -> str:
    """读 vault 里那一篇 md 的文本;读不出来按 HTTP 三分法抛。

    **「不在了」和「读不出来」要分开报。** 索引里有行、文件此刻读不出来(权限、
    网盘没挂上、IO 错)不等于这一篇不存在 —— 报成 404 会让调用方把一次**暂时的
    失败**当成一个**已经证明了的结论**:片段收藏那一头据此告诉用户「这篇还没进
    检索索引」并静默丢掉 path。

    **口径只有这一份**,两个消费方都过它:`/api/doc` 按它报正文版本号,片段落库
    前按它再算一次(`favorites_api`)。各写一遍的话,一处把"文件不在了"报成 503、
    另一处报成 404,而前端是按状态码决定退回哪一档、怎么跟用户说的。
    解码一律 `errors="replace"`,理由见 `scanner.read_md_text`;
    这一道的事故形态在 `docs/decisions/fav-snippets.md`(硬约束一节)。
    """
    try:
        return read_md_text(VAULT_ROOT / path)
    except FileNotFoundError:
        raise HTTPException(404, "文件不在了(可能已移动)")
    except OSError as e:
        raise HTTPException(503, f"原文暂时读不出来:{e.strerror or e}")


def indexed_doc_version(path: str, indexed_hash: str) -> str:
    """盘上那一篇此刻的正文版本号 —— **只在它还是建索引时那一份时才报**,否则空串。

    给检索命中用。搜索卡摊的那一截(`best_chunk`)是**建索引那一刻**从这一篇切下来
    的,存在 `chunks.text` 里;而收藏那一头要拿版本号证明「屏幕上这一份就是盘上
    这一份」(口径在 `kb/static/js/snip.js` 的 `sameCorpus`)。所以这个数必须和
    **摊出来那截语料同源**:`documents.content_hash` 记着建索引时这份文件的指纹
    (`scanner.file_hash`),和此刻盘上这一份逐字节相等,才说明卡片上那一截就是从
    现在这份正文里切出来的 —— 那时报它的正文版本号才成立。

    **响应这一刻读盘算出来的版本号冒充不了它**(上一版就是这么写的):那个数证明的
    是「刚读到的文件」,不是「屏幕上那份语料」。审查实测:建完索引之后在盘上把那一节
    换掉、**不重建索引**,再打开搜索 —— 返回的还是旧 `best_chunk`,却带着新文件的
    版本号,于是核对那一步两个数必然相等、弱档重新放行,收藏返回 200,点回去高亮的
    是改稿新写的那一节。

    索引落后时如实回空串(「说不出」),收藏那一头照旧不采信弱档:绑得住那一篇、
    当场说一句「点回去可能定位不到这一处」。文件读不出来、索引里压根没有这一行
    (`indexed_hash` 空)同样回空串,一个异常都不抛 —— 一页几十条,其中一篇刚移走
    是常态,抛出去就是整页列不出来。
    """
    if not path or not indexed_hash:
        return ""
    try:
        text = read_md_text(VAULT_ROOT / path)
    except OSError:
        return ""
    return body_version(text) if file_hash(text) == indexed_hash else ""


def unreadable_config(e: ScheduleUnreadable) -> HTTPException:
    """读不出配置 → 503 + 原因。**不许回 200 带一份缺省值。**

    回 200 的那一版里,界面没法把「读不出来」和「用户关掉了」分开:两处都写
    「未启用」、来源说明还断言「这一类来源目前不会自动同步」,而盘上那份其实开着
    (第 1 轮实测把配置文件 chmod 000)。请求失败时「不知道」那一档前端本来就有
    (`schedules.js` 读不到就把那条留成 null、`source_forms.js` 照它说"没法确定")。

    **两条定时共用这一句**(日报和页头):措辞在两处各写一份的话,改一处就会出现
    "一条说得清、另一条还在说未启用"。
    """
    return HTTPException(503, f'{e}。修好或删掉这个文件之后再试。')
