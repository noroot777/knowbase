# -*- coding: utf-8 -*-
"""project_url 是不是指向代码托管站——**全仓库只有这一份口径**。

原来图谱那边(`scripts/wiki/graph_data.py`)按 `repo_host()` 的白名单判「有没有
仓库」,图鉴这边聚合多条记录的 url 时却只认字面量 `github.com`,两层口径不一致:
一个工具在 A 文里被写成官网、B 文里给了 Hugging Face 仓库时,聚合结果取官网,
图谱的「只看有仓库」再按这个 url 判就把它筛掉了(2026-09-22 第 1 轮审查发现)。
"""

from __future__ import annotations

from urllib.parse import urlsplit

# 代码托管站。「只看有仓库」筛的必须是这个,不能退化成「有任意 http 网址」——
# project_url 里官网/文档站/应用商店/博客都有(opencode.ai、prettier.io…),
# 按有没有 URL 筛的话开关名不副实。判定按解析出的 hostname 精确比对或后缀匹配,
# 不用字符串包含:`github.com.evil.cn` 那种也会命中包含判断。
REPO_HOSTS = frozenset({
    "github.com", "gitlab.com", "gitee.com", "bitbucket.org", "codeberg.org",
    "sourceforge.net", "git.sr.ht", "gitcode.com", "gitcode.net",
    "huggingface.co", "hf.co", "modelscope.cn", "gitea.com", "framagit.org",
})


def safe_url(u: str) -> str:
    """只放行 http(s)。project_url 是 LLM 从公众号正文里读出来写进 frontmatter
    的,`javascript:` 这种点一下就执行。"""
    u = (u or "").strip()
    return u if u[:7].lower() == "http://" or u[:8].lower() == "https://" else ""


def repo_host(u: str) -> str:
    """URL 指向代码托管站时返回归一后的主机名,否则空串。"""
    u = safe_url(u)
    if not u:
        return ""
    try:
        host = (urlsplit(u).hostname or "").lower()
    except ValueError:
        return ""
    host = host[4:] if host.startswith("www.") else host
    if host in REPO_HOSTS:
        return host
    return next((h for h in REPO_HOSTS if host.endswith("." + h)), "")
