#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分类图谱的页面外壳:把数据 + assets/catgraph.{css,js} 拼成单文件 HTML。

开源项目图谱和 skill 图谱共用这一份外壳,差别只在标题/主色/控件(项目图谱多
一个「只看有仓库」开关)。样式和脚本放在 assets/ 下的真文件里而不是塞进这段
字符串:编辑器能高亮、行数也压得住,构建时读进来内联,产物仍是单文件——
它要能脱离 kb-web 直接用浏览器打开。
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"

# 大类配色,按大类顺序发放。13 个够 skill 图谱那 13 个实义大类不重样;
# 「其他」不占色位,固定用 GRAY(见 catgraph.js 里发色的那几行)。
# 亮度统一压到和 --brand(#17604F 深松绿)同一档:原来这排是高饱和纯色
# (#0e7edb / #0f9d63 一类),在瓷白底上比正文还抢眼,一屏十三个大类的圆点
# 先跳出来,要看的项目名反而退到后面。色相保持不变,仍然一类一色。
PALETTE = ["#2C5F8D", "#6B4A8F", "#17604F", "#8C6A3F", "#9C3A5C",
           "#1D6E78", "#5E6B28", "#8A5A22", "#4A55A0", "#803A8C",
           "#8A6A14", "#A8437A", "#2B7A68"]
GRAY = "#66727C"

_TMPL = """<!DOCTYPE html>
{mark}
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--accent:{accent}}}
{css}
</style>
</head>
<body>
<svg id="edges"></svg>
<header>
  <div class="hrow">
    <div>
      <h1>{h1}</h1>
{sub}
    </div>
    <div class="controls">
      <input id="q" type="search" placeholder="搜名字 / 简介 / 主题…  (/)">
      <div class="seg" data-k="win" title="按最近一次被提到的时间筛">
        <button data-v="0" class="on">全部</button>
        <button data-v="90">90 天</button>
        <button data-v="30">30 天</button>
      </div>
      <div class="seg" data-k="heat" title="按被提到的总次数筛">
        <button data-v="1" class="on">全部热度</button>
        <button data-v="2">≥2 次</button>
        <button data-v="5">≥5 次</button>
      </div>
      <div class="seg" data-k="sort" title="每个类目里的排序">
        <button data-v="n" class="on">按热度</button>
        <button data-v="t">按最近</button>
        <button data-v="l">按名称</button>
      </div>
      {extra_ctl}
    </div>
  </div>
  <div id="nav"></div>
</header>
<main id="main"></main>
<div id="panel">
  <button class="close" onclick="kbReset()">✕</button>
  <div id="panelBody"></div>
</div>
<footer>{foot}</footer>
<script>
const DATA = {data};
const CFG = {cfg};
</script>
<script>
{js}
</script>
</body>
</html>
"""

_REPO_CTL = ('<button class="toggle" id="repoBtn" '
             'title="只看 project_url 指向代码托管站(GitHub/GitLab/Gitee…)的;'
             '官网、文档站、应用商店这类不算">只看有仓库</button>')

# 头部的机读标记:下一次生成拿它当「上一张图有多少节点」的基线,好判断这一轮
# 是不是塌了(全被归类踢掉、只剩零星几个)。读的是文件头两行,不必把 1.8 MB
# 的产物整个解析一遍。老产物没有这一行,读出 None,那一轮就只做绝对值检查。
_MARK = "<!-- catgraph nodes={nodes} classified={classified} built={built} -->"
_MARK_RE = re.compile(r"<!-- catgraph nodes=(\d+) classified=(\d+)")


def page_stats(path: Path) -> dict | None:
    """读已有图谱头部那行标记 → {"nodes": n, "classified": m};没有则 None。"""
    try:
        with path.open(encoding="utf-8") as f:
            head = f.readline() + f.readline()
    except OSError:
        return None
    m = _MARK_RE.search(head)
    return {"nodes": int(m.group(1)), "classified": int(m.group(2))} if m else None


def render(data: dict, *, title: str, h1: str, sub: str, accent: str,
           foot: str, other_group: str, app_base: str = "/", hot_cap: int = 30,
           repo_toggle: bool = False) -> str:
    cfg = {"colors": PALETTE, "gray": GRAY, "otherGroup": other_group,
           "appBase": app_base, "hotCap": hot_cap}
    meta = data["meta"]
    return _TMPL.format(
        title=html.escape(title), h1=h1, accent=accent,
        sub=f'      <div class="sub">{html.escape(sub)}</div>' if sub else '',
        mark=_MARK.format(nodes=meta["total"], classified=meta["classified"],
                          built=meta["built"]),
        css=(ASSETS / "catgraph.css").read_text(encoding="utf-8"),
        js=(ASSETS / "catgraph.js").read_text(encoding="utf-8"),
        # 数据里有文章标题这类外部文本,</script> 会当场截断脚本块
        data=json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
        cfg=json.dumps(cfg, ensure_ascii=False),
        extra_ctl=_REPO_CTL if repo_toggle else "",
        foot=foot,
    )


def foot_html(meta: dict, unit: str) -> str:
    """底栏统计。「显示中」由前端按筛选实时改写。"""
    un = meta["total"] - meta["classified"]
    parts = [f'显示中 <b id="shown">{meta["total"]}</b> / <b>{meta["total"]}</b> {unit}',
             f'近 30 天有提及 <b>{meta["hot30"]}</b>',
             f'来自 <b>{meta["arts"]}</b> 篇文章',
             f'<b>{meta["edges"]}</b> 条共现关联']
    if un:
        parts.append(f'待归类 <b>{un}</b>')
    parts.append(f'数据截至 {meta["built"]} &nbsp;·&nbsp; Esc 复位')
    return " · ".join(parts)
