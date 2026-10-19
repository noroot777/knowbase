# -*- coding: utf-8 -*-
"""扫描 obs vault,解析每篇 md 的 frontmatter 与正文。

产出 Doc 对象流,供索引层消费。增量逻辑靠 content_hash:
文件内容没变的文档,索引层直接跳过。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml

from kb.config import SOURCES, VAULT_ROOT, Source
from kb.scan_status import ScanReport, stat_signature


@dataclass
class Doc:
    path: str                # 相对 VAULT_ROOT 的路径(索引主键)
    source: str              # 来源标识
    title: str
    account: str             # 公众号名/频道名
    published: str
    url: str
    tags: list[str] = field(default_factory=list)
    body: str = ""           # 去 frontmatter 后的正文
    content_hash: str = ""
    meta: dict = field(default_factory=dict)  # 完整 frontmatter(project/kind/summary 等)
    body_version: str = ""


_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)

# wiki 双链与图谱尾区只是阅读层装饰(linkify 后处理产物),索引/向量必须
# 看到与 linkify 前完全一致的正文:[[页|原文]]→原文,[[页]]→页,
# <!-- kb-graph --> 之后整段丢弃。这保证 linkify 不改变 chunk 与 embedding。
_GRAPH_FOOTER_RE = re.compile(r"\n<!-- kb-graph -->.*\Z", re.S)
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+)\|([^\[\]]+)\]\]|\[\[([^\[\]|]+)\]\]")


def normalize_body(body: str) -> str:
    """索引视角的正文:剥掉双链语法与图谱尾区。"""
    body = _GRAPH_FOOTER_RE.sub("", body)
    return _WIKILINK_RE.sub(lambda m: m.group(2) or m.group(3), body)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """拆 frontmatter 和正文。没有 frontmatter 时返回 ({}, 全文)。"""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = {}
    return fm, text[m.end():]


def file_hash(text: str) -> str:
    """一份 md 文件的指纹 —— `documents.content_hash` 存的就是它。

    口径只有这一份:索引按它判「这一篇要不要重建」(`store.doc_needs_reindex`)、
    回写改完 frontmatter 按它刷新(`extract.refresh_content_hash`)、提取结果落库
    前按它确认「索引已经扫过当前这份 md」(`extract._require_current_input`)。
    各写一遍的话,三处对「索引跟上了没有」的回答会各漂各的。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def read_md_text(full: Path) -> str:
    """读一份 md 的文本。**解码一律 `errors="replace"`。**

    版本号(下面那个 `body_version`)是按读到的文本算的,所以报版本的那几处和
    落库前再算一次的那一处必须用同一种解码:一处严格、一处 replace 的话,只要
    文件里有一个坏字节,两头就会算出两个版本,把一条好收藏误拒成"原文已更新"。
    """
    return full.read_text(encoding="utf-8", errors="replace")


def body_version(text: str) -> str:
    """一份 md 的**正文**版本号(frontmatter 改了不算数)。

    口径只有这一份:`/api/doc`、图鉴条目详情、独立阅读页都按它告诉前端「你现在
    看到的是哪一版正文」,片段收藏落库前按**盘上此刻**那一份再算一次,两个数
    相等才落 `origin=doc`(`kb/web/favorites_api.py` 的 `_doc_version_now`)。
    少了这个数,核对和写入之间就没有共同事实:核对读到的是旧译文、写入时盘上
    已是新译文,收藏照旧落成 doc,点回去 0 个高亮(审查实测)。

    **不用 `file_hash`**(那是整份文件、`documents.content_hash` 存的那个):
    提取回写只动 frontmatter(补 tags、写 extracted_at),正文一个字没变 ——
    按整份文件算的话,回写那一刻的收藏会被误拒、白退到站外原文。

    和 `kb.extract_version.body_fingerprint` 算的是同一个数却各写一份:那一族
    判「模型结论针对的输入还是不是当前这份」,这一族判「收藏绑的目标还是不是
    用户看过的那份」—— 两个判据各自演进,不互为前提。

    事故形态、实测证据和回归在 `docs/decisions/fav-snippets.md`(硬约束一节)。
    """
    return body_version_of(parse_frontmatter(text)[1])


def body_version_of(body: str) -> str:
    """同一个数,但输入是**已经拆出来的正文**(frontmatter 之外那一截)。

    给「文件还没写下去、却要说出它的正文版本号」那一头用:订阅条目面板摊的是
    feeds.db 里那一行的正文,而阅读页回跳读的是导出的那一篇 md —— 两份语料分属
    两处,想证明「屏幕上这一份就是盘上这一份」只能**按同一行内容算出那一篇 md 的
    正文版本号**(`kb.feeds.exported.item_body_md` 就是那一截的唯一定义),再跟盘上
    此刻那一份比。拿「响应这一刻读盘算出来的版本号」冒充是不成立的:那个数证明
    的是刚读到的文件,不是屏幕上那份语料(审查实测:先改稿、再打开订阅/搜索,
    旧正文带着新版本号,核对必然相等、弱档重新放行,点回去高亮改稿新写的那一节)。
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def frontmatter_tags(fm: dict) -> list[str]:
    """索引会从一份 frontmatter 里读到的 tags 列表。

    抽出来是因为有第二个消费点:回写摘掉 `flag/软广` 之后,要确认「索引下次
    读到的那一份里它真的不在了」(`kb.extract_promo.promo_tag_in_md`)。那道
    确认必须和索引用同一份口径 —— 各写一遍的话,摘除那边按自己的理解判「摘
    干净了」,而索引按另一套读法照旧把它读回 `doc_tags`。
    """
    tags = fm.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    return [str(t).strip() for t in tags if str(t).strip()]


def frontmatter_input(fm: dict, md_path: Path) -> tuple[str, str]:
    """索引会从一份 frontmatter 里读到的 title / account —— 也是**模型输入**那两样。

    抽出来是因为有第二个消费点:软广补判要在落库之前确认「磁盘上这一篇喂给
    模型的那组输入,还是索引里那一份吗」(`kb.extract_promo.disk_input_indexed`)。
    磁盘可以领先于索引(导出完成、索引还没跟上),而这两样都进 prompt —— 各写
    一遍读法的话,那道确认按自己的理解说「一致」,索引随后按另一套读法把别的
    值 upsert 进库,判定就固化在了一份没人判过的输入上。

    `title` 的兜底是文件名:frontmatter 里没有 title 的 md 在索引里就叫这个,
    判定看到的也是这个。
    """
    account = str(fm.get("account") or fm.get("channel") or "").strip()
    title = str(fm.get("title") or "").strip() or md_path.stem
    return title, account


def doc_from_text(text: str, md_path: Path, src: Source,
                  rel_path: str | None = None) -> Doc:
    """一份 md 文本在**索引眼里**的那篇 Doc —— upsert 进库的每一样都出自这里。

    从 `_doc_from_file` 原样抽出来(它现在只管读文件)。抽的理由:登记「这份
    文件已经索引过了」那张收据要回答的是「这份文本**现在**重新索引一次,库里
    会不会变」(`kb.doc_rewrite.index_projection_matches`)。各写一遍读法的话,
    那道判据按自己的理解说「一致」,索引随后按另一套读法把别的值 upsert 进库,
    收据就签在一份没人索引过的东西上 —— 而签错不可恢复。**口径只有这一份,
    两个消费方都过它。**

    **`rel_path` 给了就直接用,不去 `relative_to(VAULT_ROOT)` 算。** 生产里
    `scanner.VAULT_ROOT` 和 `extract.VAULT_ROOT` 是同一个值,可夹具各打桩哪一个
    全凭各自的需要(`tests/promo_delivery_fixture.py` 只打 `extract` 那一份)——
    让这个函数去比两个模块级常量,等于把判据押在「这两处在每个夹具里都一起被
    打桩」上,而失配时它抛的是 `ValueError: not in the subpath`:登记那一步整个
    炸掉,看上去像是回写坏了。调用方手里本来就有那个相对路径。
    """
    fm, body = parse_frontmatter(text)

    tags = frontmatter_tags(fm)

    # channel(youtube) 和 account(gzh) 语义相同,归一到 account。这两样的读法
    # 还有第二个消费点,所以在 frontmatter_input 里(别在这儿内联回来)。
    title, account = frontmatter_input(fm, md_path)
    url = str(fm.get("url") or fm.get("youtube") or "").strip()
    published = str(fm.get("published") or fm.get("publish_time") or "").strip()

    return Doc(
        path=rel_path if rel_path is not None
        else str(Path(os.path.abspath(md_path)).relative_to(Path(os.path.abspath(VAULT_ROOT)))),
        source=str(fm.get("source") or src.name),
        title=title,
        account=account,
        published=published,
        url=url,
        tags=tags,
        body=normalize_body(body),
        content_hash=file_hash(text),
        meta=fm,
        body_version=body_version(text),
    )


def _doc_from_file(md_path: Path, src: Source) -> Doc:
    before = stat_signature(md_path.stat())
    text = md_path.read_text(encoding="utf-8")
    if before != stat_signature(md_path.stat()):
        raise ValueError("扫描期间文件发生变化")
    if not text.strip():
        raise ValueError("文件为空,可能尚未写完")
    if text.startswith('---\n'):
        match = _FM_RE.match(text)
        if match is None:
            raise ValueError("frontmatter 尚未写完")
        metadata = yaml.safe_load(match.group(1))
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("frontmatter 不是映射")
    return doc_from_text(text, md_path, src)


def iter_md(sources: list[Source] | None = None, *,
            report: ScanReport | None = None) -> Iterator[tuple[Source, Path]]:
    """遍历该扫的那些 md 文件,**只给路径,不读正文**。

    「哪些树要扫、树里跳过哪些子目录」只有这一份实现。拆出来是因为"枚举该扫
    哪些文件"和"把文件读成 Document"是两件事:要枚举的人(本函数)不该为了拿
    一个路径清单去读上万篇正文。目前只有 `scan_all` 过它。
    """
    report = report if report is not None else ScanReport()
    report.start(sources if sources is not None else SOURCES, VAULT_ROOT)
    yield from report.iter_md()


def scan_all(*, report: ScanReport | None = None) -> Iterator[Doc]:
    """流式读文档,在 report 中如实记录完成状态、覆盖范围及读失败。

    文件读坏、来源不可达和目录变化都不是删除证明。清理只消费完整的凭据,
    且只授权无异常、未停用、未排除范围中经再次核验确实不存在的路径。
    """
    report = report if report is not None else ScanReport()
    for src, md_path in iter_md(report=report):
        try:
            doc = _doc_from_file(md_path, src)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
            report.fail(src, md_path, error)
        else:
            yield doc
    report.complete = True
