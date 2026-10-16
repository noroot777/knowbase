# -*- coding: utf-8 -*-
"""沙箱那两份 md 正文模板(图鉴条目、日报),从 `tests/serve_snippets.py` 搬出来。

那一头到了行数上限,而这两份只是**带占位符的纯文本**:句子常量、摆盘、manifest
仍在那一头,改正文里某一句话仍然是改那一头的常量。两份模板都用 `str.format`,
所以正文里**不许出现裸的 `{` `}`**(要写就写 `{{` `}}`)。
"""

from __future__ import annotations

# frontmatter 的键按 scripts/gzh_projects_export.py 来:`project` 缺了
# `kb/projects.py` 的 `_load_entry` 直接判成"不是条目"、整个图鉴里看不到它。
_TOOL_MD = """---
title: {name}
source: dr-gzh-projects
account: 某个公众号
published: {published}
url: https://example.org/tool-post
article_title: 一篇讲这个工具的文章
project: {name}
project_url: https://github.com/example/snipkit
kind: project
summary: 本地把文章拆成条目的小工具
tags:
  - entity/snipkit
  - topic/skill工具
---

> 整理自公众号文章 [一篇讲这个工具的文章](https://example.org/tool-post) · 某个公众号 · {day}

{snip},不连任何外部服务。

{vdel}。

{vfresh}。{vstale}。{vagain}。{vkept}。{keptnext}

{vhtml}。

{twinlead}{vtwin}。中间隔着一句话。乙节里又说一遍:{vtwin}。

{movelead}{vmove}。

{movespare}

{sidelead}{sideq}{sidetaila}

{sidelead}{sideq}{sidetailb}

后面还有一段,交代它和别的工具怎么搭。
"""

_DAILY_MD = """---
date: {day}
docs: 1
total: 1
late: 0
model: stub/none
generated: {generated}
sources:
  rss: 1
window_since: {since}
window_until: {until}
---

# 日报 · {day}

> 覆盖 {since} → {until} 发布的内容

## 今天的重点

{snip},它不依赖任何外部服务。

## 两处一样的话

{lead1}{dup}。

{lead2}

{dup}

## 换日那一刻

{slow}。

## 一段可以直接跑的代码

```python
{code}
```

## 一字不差的两段

{ambi}

## 一个 emoji 隔着三十个字母

{wide}

## 两边证据矛盾的两处

{conf}

## 两侧候选不相交的三处

{disj}

## 又说了一遍

{ambi}

## 采集那会儿有几处

{once}

## 素材清单

1. [一条把书变成技能的流水线](https://example.org/skill-pipeline) — 某个博客
"""
