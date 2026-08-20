# -*- coding: utf-8 -*-
"""页面路由:前端外壳、静态资源、知识图谱、本地文章阅读页。

/doc/<vault 相对路径> 在浏览器里直接读本地保存的文章:md 渲染成网页,
其余文件(assets 图片等)原样返回,md 里的相对图片链接因此自然可用。
"""

from __future__ import annotations

import html as html_mod
import json
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from kb import store
from kb.config import VAULT_ROOT
from kb.web.common import VAULT_NAME, _STATIC

router = APIRouter()


@router.get("/")
def index_page():
    return FileResponse(_STATIC / "index.html")


@router.get("/static/{fpath:path}")
def static_asset(fpath: str):
    """前端拆分出的 css/js 静态资源;每次请求从磁盘读,改动免重启。"""
    f = (_STATIC / fpath).resolve()
    if not f.is_relative_to(_STATIC) or not f.is_file():
        raise HTTPException(404, "static file not found")
    return FileResponse(f)


@router.get("/graph")
def graph_page():
    """交互式知识图谱(wiki 管道阶段生成的静态单文件)。"""
    f = VAULT_ROOT / "graph.html"
    if not f.is_file():
        raise HTTPException(404, "graph.html 还没生成,先跑一次全流程同步(wiki 阶段会生成)")
    return FileResponse(f)


_DOC_TMPL = """<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<script src="/static/vendor/purify.min.js"></script>
<style>
  body {{ font: 16px/1.8 -apple-system, "PingFang SC", "Noto Sans SC", sans-serif;
         background: #F2F4F3; color: #1a1a1a; max-width: 760px;
         margin: 0 auto; padding: 40px 20px 80px; }}
  h1.doc-title {{ font-size: 24px; margin-bottom: 6px; }}
  .meta {{ color: #66727C; font-size: 13px; margin-bottom: 28px;
           display: flex; gap: 14px; flex-wrap: wrap; }}
  .meta a {{ color: #17604F; text-decoration: none; }}
  #content img {{ max-width: 100%; border-radius: 8px; }}
  #content h1, #content h2, #content h3 {{ margin: 22px 0 10px; }}
  #content p, #content ul, #content ol, #content blockquote {{ margin-bottom: 12px; }}
  #content pre {{ background: #EAEEED; padding: 14px; border-radius: 9px; overflow-x: auto; }}
  #content code {{ background: #EAEEED; padding: 1px 5px; border-radius: 4px; font-size: 14px; }}
  #content blockquote {{ border-left: 3px solid #DFE5E5; padding-left: 14px; color: #555; }}
</style></head><body>
<h1 class="doc-title">{title}</h1>
<div class="meta">{meta}</div>
<div id="content"></div>
<script>
const MD = {md_json};
const DOC_PATH = {path_json};
// 本地保存的文章正文是外部内容,marked 不做 sanitize,渲染前一律过 DOMPurify;
// sanitizer 没加载出来就退化成纯文本(失败关闭),不给未净化的 HTML 放行
document.getElementById('content').innerHTML = window.DOMPurify
  ? DOMPurify.sanitize(marked.parse(MD))
  : '<div style="white-space:pre-wrap"></div>';
if (!window.DOMPurify) document.querySelector('#content div').textContent = MD;
{snip_js}
</script>
</body></html>"""

# 片段收藏脚本(纯常量、不走 str.format,所以里面可以放心用大括号):
# 选中正文文字浮出「☆ 收藏此段」;URL 带 #kbq=… 时按 quote+前后文锚点定位高亮。
_DOC_SNIP_JS = r"""
(() => {
  const content = document.getElementById('content');
  document.head.insertAdjacentHTML('beforeend', `<style>
    #snipbtn { position: fixed; z-index: 10; padding: 6px 13px;
      font-size: 12.5px; background: #1a1a1a; color: #fff; border-radius: 9px;
      cursor: pointer; user-select: none; box-shadow: 0 4px 14px rgba(0,0,0,.28);
      white-space: nowrap; }
    #kbtoast { position: fixed; z-index: 11; top: 16px; left: 50%;
      transform: translateX(-50%); padding: 8px 16px; font-size: 13px;
      background: #1a1a1a; color: #fff; border-radius: 10px;
      box-shadow: 0 4px 14px rgba(0,0,0,.28); max-width: 88vw; }
    ::highlight(kb-quote) { background: #DFF0E9; color: #17604F; }
  </style>`);
  // 隐藏态走行内样式:show 逻辑靠清空 style.display 露出,CSS 规则里不能写 display:none
  const btn = document.createElement('div');
  btn.id = 'snipbtn';
  btn.textContent = '☆ 收藏此段';
  btn.style.display = 'none';
  document.body.appendChild(btn);
  const toastEl = document.createElement('div');
  toastEl.id = 'kbtoast';
  toastEl.style.display = 'none';
  document.body.appendChild(toastEl);
  let toastTimer = null;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.style.display = '';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.style.display = 'none'; }, 2200);
  }

  // 正文所有 text node 串成一条全文,选区偏移和锚点定位共用同一坐标系
  function textIndex() {
    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
    const nodes = []; let text = '', n;
    while ((n = walker.nextNode())) { nodes.push({ node: n, start: text.length }); text += n.nodeValue; }
    return { nodes, text };
  }
  function offsetRange(idx, start, end) {
    const range = document.createRange();
    let started = false;
    for (const it of idx.nodes) {
      const nEnd = it.start + it.node.nodeValue.length;
      if (!started && start < nEnd) { range.setStart(it.node, start - it.start); started = true; }
      if (started && end <= nEnd) { range.setEnd(it.node, end - it.start); return range; }
    }
    return null;
  }

  // ── 选中 → 浮动收藏按钮 ──
  function hideBtn() { btn.style.display = 'none'; }
  function showBtn() {
    const sel = getSelection();
    if (!sel || sel.isCollapsed) return hideBtn();
    const range = sel.getRangeAt(0);
    if (!content.contains(range.commonAncestorContainer)) return hideBtn();
    if (sel.toString().trim().length < 4) return hideBtn();
    const r = range.getBoundingClientRect();
    btn.style.display = '';
    const w = btn.offsetWidth;
    btn.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2)) + 'px';
    btn.style.top = Math.max(8, r.top - 40) + 'px';
  }
  document.addEventListener('mouseup', e => {
    if (e.target === btn) return;
    setTimeout(showBtn, 10);   // 等浏览器结算好选区
  });
  document.addEventListener('selectionchange', () => {
    const sel = getSelection();
    if (!sel || sel.isCollapsed) hideBtn();
  });

  btn.addEventListener('mousedown', e => e.preventDefault());  // 别让点击清掉选区
  btn.addEventListener('click', async () => {
    const sel = getSelection();
    if (!sel || sel.isCollapsed) return hideBtn();
    const range = sel.getRangeAt(0);
    const idx = textIndex();
    const s = idx.nodes.find(x => x.node === range.startContainer);
    const e = idx.nodes.find(x => x.node === range.endContainer);
    let quote, prefix = '', suffix = '';
    if (s && e) {
      const a = s.start + range.startOffset, b = e.start + range.endOffset;
      quote = idx.text.slice(a, b);
      prefix = idx.text.slice(Math.max(0, a - 30), a);
      suffix = idx.text.slice(b, b + 30);
    } else {
      quote = sel.toString();  // 选区端点不在 text node 上的少见情况:锚点留空,靠全文搜索
    }
    btn.textContent = '…';
    try {
      const r = await fetch('/api/favorites', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: DOC_PATH, quote, prefix, suffix }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
      sel.removeAllRanges();
      toast('✓ 片段已收藏,「收藏 · 待试」里可回看');
    } catch (err) {
      toast('收藏失败: ' + err.message);
    }
    hideBtn();
    btn.textContent = '☆ 收藏此段';
  });

  // ── #kbq= 锚点 → 高亮 + 滚动定位 ──
  let lastEl = null;
  function locateQuote() {
    const m = location.hash.match(/kbq=([^&]*)/);
    if (!m) return;
    let spec;
    try { spec = JSON.parse(decodeURIComponent(m[1])); } catch { return; }
    if (!spec || !spec.q) return;
    const idx = textIndex();
    const p = spec.p || '', sfx = spec.s || '';
    let start = -1;
    let i = idx.text.indexOf(p + spec.q + sfx);
    if (i >= 0) start = i + p.length;
    else { i = idx.text.indexOf(spec.q); if (i >= 0) start = i; }
    if (start < 0) { toast('没找到这段收藏(原文可能已更新)'); return; }
    const range = offsetRange(idx, start, start + spec.q.length);
    if (!range) return;
    if (window.Highlight && CSS.highlights) {
      CSS.highlights.set('kb-quote', new Highlight(range));
    }
    lastEl = range.startContainer.parentElement;
    lastEl.scrollIntoView({ block: 'center' });
  }
  locateQuote();
  // 图片加载完布局会往下推,落定后再校准一次滚动位置
  addEventListener('load', () => { if (lastEl) lastEl.scrollIntoView({ block: 'center' }); });
  addEventListener('hashchange', locateQuote);
})();
"""


@router.get("/doc/{doc_path:path}")
def doc_page(doc_path: str):
    root = VAULT_ROOT.resolve()
    full = (root / doc_path).resolve()
    if root != full and root not in full.parents:
        raise HTTPException(403, "路径越界")
    if not full.is_file():
        raise HTTPException(404, "文件不存在")
    if full.suffix.lower() != ".md":
        return FileResponse(full)

    text = full.read_text(encoding="utf-8", errors="replace")
    body = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)

    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT title, account, published, url FROM documents WHERE path=?",
            (doc_path,),
        ).fetchone()
    finally:
        conn.close()

    title = (row["title"] if row and row["title"] else full.stem)
    meta_parts = []
    if row:
        if row["account"]:
            meta_parts.append(html_mod.escape(row["account"]))
        if row["published"]:
            meta_parts.append(html_mod.escape(row["published"][:10]))
        if row["url"]:
            # url 来自被索引的外部内容,只放行 http(s):javascript: 之类点一下就执行
            safe = row["url"] if re.match(r"^https?://", row["url"], re.I) else "#"
            meta_parts.append(
                f'<a href="{html_mod.escape(safe)}" target="_blank" rel="noopener">微信原文↗</a>'
            )
    obs = (f"obsidian://open?vault={quote(VAULT_NAME)}"
           f"&file={quote(doc_path.removesuffix('.md'))}")
    meta_parts.append(f'<a href="{obs}">在 Obsidian 打开</a>')

    md_json = json.dumps(body, ensure_ascii=False).replace("</", "<\\/")
    path_json = json.dumps(doc_path, ensure_ascii=False).replace("</", "<\\/")
    return HTMLResponse(
        _DOC_TMPL.format(
            title=html_mod.escape(title), meta=" · ".join(meta_parts),
            md_json=md_json, path_json=path_json, snip_js=_DOC_SNIP_JS,
        ),
        # 动态渲染页,改版后旧缓存会让新脚本(片段收藏)不生效
        headers={"Cache-Control": "no-cache"},
    )
