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
from kb.scanner import body_version, read_md_text
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
    return _graph_file("graph.html")


# 分类图谱:开源项目 / skill 各一张,由 scripts/wiki/build_catgraph.py 生成。
# 名字在这里写死成白名单而不是拿 URL 片段去拼文件名:同目录下的 vault 根还放着
# 别的东西,一个 `/graph/{name}.html` 的写法就把整个 vault 顶层变成可读的了。
_CATGRAPHS = {"projects": "graph-projects.html", "skills": "graph-skills.html"}


@router.get("/graph/{name}")
def catgraph_page(name: str):
    fname = _CATGRAPHS.get(name)
    if fname is None:
        raise HTTPException(404, f"没有这张图谱:{name}")
    return _graph_file(fname)


def _graph_file(fname: str):
    f = VAULT_ROOT / fname
    if not f.is_file():
        raise HTTPException(
            404, f"{fname} 还没生成,先跑一次全流程同步(wiki 阶段会生成)")
    return FileResponse(f)


# 外链箭头,几何抄自 kb/static/js/icons.js 的 ARROW_PATHS.ext
_EXT_ARROW = (
    '<svg class="ia" width="1em" height="1em" viewBox="0 0 12 12" fill="none"'
    ' stroke="currentColor" stroke-width="1.4" stroke-linecap="round"'
    ' stroke-linejoin="round" aria-hidden="true">'
    '<path d="M2.9 9.1 9.1 2.9M4.9 2.9h4.2v4.2"/></svg>'
)

_DOC_TMPL = """<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="/static/vendor/marked.min.js"></script>
<script src="/static/vendor/purify.min.js"></script>
<style>
  /* 配色与 kb/static/css/base.css 的 :root 逐个对齐 —— 这页是 iframe 里独立
     渲染的产物,拿不到那份 token,只能抄。改那边记得改这里,否则点一条搜索
     结果去读原文就换了一套配色。 */
  body {{ font: 16px/1.8 -apple-system, "PingFang SC", "Noto Sans SC", sans-serif;
         background: #F3F5F4; color: #14202B; max-width: 760px;
         margin: 0 auto; padding: 40px 20px 80px; }}
  h1.doc-title {{ font: 900 24px/1.4 "Songti SC","STSong",serif; letter-spacing: .02em; margin-bottom: 6px; }}
  .meta {{ color: #66727C; font-size: 13px; margin-bottom: 28px;
           display: flex; gap: 14px; flex-wrap: wrap; }}
  .meta a {{ color: #17604F; text-decoration: none; }}
  #content img {{ max-width: 100%; border-radius: 8px; }}
  #content h1, #content h2, #content h3 {{ margin: 22px 0 10px; }}
  #content p, #content ul, #content ol, #content blockquote {{ margin-bottom: 12px; }}
  #content pre {{ background: #EAEEED; padding: 14px; border-radius: 9px; overflow-x: auto; }}
  #content code {{ background: #EAEEED; padding: 1px 5px; border-radius: 4px; font-size: 14px; }}
  #content blockquote {{ border-left: 3px solid #DCE3E0; padding-left: 14px; color: #3A4750; }}
  /* 行内箭头:与 kb/static/js/icons.js 的 ARROW_PATHS.ext 同一份几何(这页独立
     渲染,拿不到那个模块,只能抄)。别退回字符 ↗ —— 各字体粗细基线都不同。 */
  .ia {{ display: inline-block; vertical-align: -.14em; margin-left: 3px; opacity: .7; }}
</style></head><body>
<h1 class="doc-title">{title}</h1>
<div class="meta">{meta}</div>
<div id="content"></div>
<script>
const MD = {md_json};
const DOC_PATH = {path_json};
// 这一页摊的是**哪一版正文**(`scanner.body_version` 算的,和 `/api/doc` 同一
// 个口径)。收藏时原样回传,后端落库前按盘上此刻那一份再算一次,不等就拒 ——
// 这一页可以开着好几个钟头,正文在这期间被重译写回、被补抓换掉都不会通知它。
const DOC_VERSION = {version_json};
// 本地保存的文章正文是外部内容,marked 不做 sanitize,渲染前一律过 DOMPurify;
// 两个库任一没加载出来就退化成纯文本(失败关闭),不给未净化的 HTML 放行
// (漏判 marked 的话 marked.parse 直接抛 ReferenceError,正文整个空白)
const MD_OK = !!(window.DOMPurify && window.marked);
{literal_js}
document.getElementById('content').innerHTML = MD_OK
  ? DOMPurify.sanitize(marked.parse(MD))
  : '<div style="white-space:pre-wrap"></div>';
if (!MD_OK) document.querySelector('#content div').textContent = MD;
{snip_js}
</script>
</body></html>"""

# 正文里的尖括号一律按字面文字显示(纯常量、不走 str.format,大括号可以直接写):
#
# vault 里文章正文按契约**已经是纯文本** —— RSS 走 `kb/feeds/fetching.py` 的 plain/
# body_text,公众号走它自己的转换器,两边都把标签剥干净了。所以正文里剩下的
# `List<T>`、`<dependency>`、`<system-reminder>` 全是作者写在文章里的示例文字,
# 没有一处是要渲染的标记(实测 2.58 万篇 vault md 里有 39 篇正文带着**代码围栏之外**
# 的这种示例 —— 38 篇公众号 + 1 篇 RSS,围栏里的 marked 本来就当代码显示;
# wiki/daily/index 一篇都没有裸 HTML)。
#
# 而 marked 默认把它们当原始 HTML 交给 DOMPurify,于是**这些字在阅读页上直接消失**:
# `<T>` 被当成未知标签吃掉(`Use List<T>` 显示成 `Use List`)、整段只有
# `<script>alert(1)</script>` 的示例被净化删掉之后正文一片空白。订阅面板的展开阅读
# 用 textContent 显示,同一篇在那边是完整的 —— 换个阅读入口内容就少一截
# (2026-10-08 第 3 轮审查实测两种样本)。
#
# **不是关掉净化**:DOMPurify 照旧过一遍,这里只改「原始 HTML 怎么渲染」—— html
# token 转义成字面文字,于是压根没有原始 HTML 送到净化器面前,比原来更严。
# 代价是正文里真有一段想渲染的 HTML 时也只显示文字,这正是要的:上面那条契约说
# 它不该存在,而「示例文字静默消失」比「示例文字照显示」坏得多。
#
# **只改这一页。** `kb/static/js/core.js` 的 mdHtml 渲染的是模型写的 markdown
# (问答回答、日报正文)和条目正文,不是剥过标签的文章原文;而且日报出处链接那套
# 裁决器(`kb/daily_refs.py`)的方言是按它逐位对齐的(`html=True`),动它等于让
# 那份对齐失效。
#
# **可是"收藏此段"落库之前要核的正是这一页会看到的文本**,所以 app 那一侧另有一份
# **同规则的副本**(`kb/static/js/snip.js` 的 `snipDocRenderer`,挂在一个独立的
# marked 实例上、不碰全局)—— **下面这三行改一处就要改两处**:只差这一条规则,
# 图鉴里选中一句跨过 `<strong>` 的话就会核过、落成 origin=doc、点回去 0 个高亮。
_DOC_LITERAL_JS = r"""
if (window.marked) marked.use({ renderer: { html(t) {
  // v12 给的是字符串,v13 起给 token 对象;拿错了会抛异常、正文整个空白
  const s = typeof t === 'string' ? t : (t && t.text) || '';
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
} } });
"""

# 片段收藏脚本(纯常量、不走 str.format,所以里面可以放心用大括号):
# 选中正文文字浮出「☆ 收藏此段」;URL 带 #kbq=… 时按 quote+前后文锚点定位高亮。
# 这一页是脱离 kb-web 也要能用浏览器直接打开的独立产物,所以 CSS 和图标内联。
# **同一套按钮和高亮在 app 那边由 `kb/static/css/snip.css` + `kb/static/js/snip.js`
# 提供**(日报、问答、订阅阅读器、图鉴条目共用那一份)—— 色值、圆角、锚点口径
# 三样改一处就要同步另一处,不然用户点一条收藏回原文就换一套配色,或者这边存下
# 的锚点那边对不上。下面那两个十六进制对应 base.css 的 `--ink` / `--brand-soft`
# 与 `--brand-deep`。
_DOC_SNIP_JS = r"""
(() => {
  const content = document.getElementById('content');
  document.head.insertAdjacentHTML('beforeend', `<style>
    #snipbtn { position: fixed; z-index: 10; padding: 6px 13px;
      font-size: 12.5px; background: #14202B; color: #fff; border-radius: 9px;
      cursor: pointer; user-select: none; box-shadow: 0 4px 14px rgba(0,0,0,.28);
      white-space: nowrap; }
    #kbtoast { position: fixed; z-index: 11; top: 16px; left: 50%;
      transform: translateX(-50%); padding: 8px 16px; font-size: 13px;
      background: #14202B; color: #fff; border-radius: 10px;
      box-shadow: 0 4px 14px rgba(0,0,0,.28); max-width: 88vw; }
    ::highlight(kb-quote) { background: #DFF0E9; color: #0E4A3B; }
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

  // 正文所有 text node 串成一条**压平空白**的全文(一段连续空白当一个空格),
  // 并记下每个字出自哪个节点的哪一位:选区偏移和锚点定位共用同一坐标系。
  // 这一套和 app 那边 snip.js 的 snipTextIndex/snipOffsetAt/snipOffsetRange
  // 逐位相同 —— 同一条收藏可能在那边选、在这边点回去,**差一个空格就对不上**。
  // 压平是必需的:那边摊的可能是订阅阅读器的 textContent(段落之间两个换行),
  // 这边是同一份 md 渲染出来的一串 <p>(串起来只剩一个换行)。
  const WS = /\s/;
  function collapse(t) { return String(t == null ? '' : t).replace(/\s+/g, ' '); }
  // 截断留下的「半个字」掐掉:JS 字符串按 UTF-16 存,emoji 一类字占两个单元,
  // 按单元截会把它切成两半,而孤立的那一半 UTF-8 编不出来 —— 后端 sqlite3 在
  // INSERT 那一句当场抛,整条收藏变成 500(审查实测:emoji 后面隔 29 个字母,
  // 选中再后面那句普通中文,这一页和 app 都收藏失败)。口径同 snip.js 的
  // `snipWhole`/`SNIP_LOW_HALF`,两头各一份,改一处要改两处。
  const LOW_HALF = /^[\uDC00-\uDFFF]/, HIGH_HALF = /[\uD800-\uDBFF]$/;
  function whole(t) {
    let v = String(t == null ? '' : t);
    if (LOW_HALF.test(v)) v = v.slice(1);
    if (HIGH_HALF.test(v)) v = v.slice(0, -1);
    return v;
  }
  // **元素边界也是换行。** `<br>`、段落/列表/表格边界在文本节点里一个字符都不占,
  // 屏幕上却是分行的:照文本节点串存,选中的两行当场接成一行(审查实测)。
  // 坐标系插一个空格位、取原文插一个换行,**同一份名单**管两处 —— 口径同
  // snip.js 的 `SNIP_BREAKS`/`snipBreaks`/`snipFragText`,两头各一份,
  // 改一处要改两处。名单宁可宽:多算一处只是两头都多一个分隔(匹配照旧成立),
  // 漏一处就是那一行的换行永久丢掉。
  const BREAKS = new Set([
    'BR', 'P', 'DIV', 'PRE', 'BLOCKQUOTE', 'HR',
    'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
    'UL', 'OL', 'LI', 'DL', 'DT', 'DD',
    'TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'TD', 'TH', 'CAPTION',
    'SECTION', 'ARTICLE', 'HEADER', 'FOOTER', 'ASIDE', 'NAV', 'MAIN',
    'FIGURE', 'FIGCAPTION', 'DETAILS', 'SUMMARY', 'ADDRESS', 'FIELDSET',
  ]);
  function breaks(el) { return !!el && el.nodeType === 1 && BREAKS.has(el.tagName); }
  // 一棵子树 → 屏幕上那份文字(不压平:真换行真缩进留着,元素边界补换行)
  function fragText(node) {
    let out = '';
    (function walk(parent) {
      for (const n of parent.childNodes) {
        if (n.nodeType === 3) { out += n.nodeValue; continue; }
        if (n.nodeType !== 1) continue;
        const br = breaks(n);
        if (br) out += '\n';
        walk(n);
        if (br && n.childNodes.length) out += '\n';   // 空元素(<br>)只算一处
      }
    })(node);
    return out;
  }
  // 一个 Range → 屏幕上那份文字。**先问浏览器**:当前选区正好是这一段时
  // (压平一遍逐字相同),`Selection.toString()` 就是权威答案 —— 元素边界算几个
  // 换行、`<pre>` 里的缩进怎么留,那是 CSS 的规则,自己照标签名重算一遍总会在
  // 某一类节点上差一个字(两个 <p> 之间 HTML 源里那个换行文本节点:渲染里看不见、
  // 文本节点串里在)。对不上就退回 `fragText`。口径同 snip.js 的 `snipShown`。
  function shown(range) {
    const frag = fragText(range.cloneContents());
    const sel = getSelection();
    if (sel && !sel.isCollapsed && sel.rangeCount) {
      const t = sel.toString();
      if (t && collapse(t).trim() === collapse(frag).trim()) return t;
    }
    return frag;
  }
  function textIndex() {
    const map = [], nodes = []; let text = '', space = false;
    // 一处显示分隔占一个空格位;前面已经是空白就只标记,不另占位置(坐标系
    // 「一段连续空白只占一位」这条不许破,否则 quote 压平之后对不上)
    const sep = () => {
      if (!text) return;
      if (space) { if (map.length) map[map.length - 1].sep = true; return; }
      map.push({ node: null, off: 0, sep: true });
      text += ' ';
      space = true;
    };
    (function walk(parent) {
      for (const n of parent.childNodes) {
        if (n.nodeType === 1) {
          const br = breaks(n);
          if (br) sep();
          walk(n);
          if (br) sep();
          continue;
        }
        if (n.nodeType !== 3) continue;
        const v = n.nodeValue;
        nodes.push({ node: n, from: text.length });
        for (let i = 0; i < v.length; i++) {
          const ws = WS.test(v[i]);
          if (ws && space) continue;
          map.push({ node: n, off: i });
          text += ws ? ' ' : v[i];
          space = ws;
        }
      }
    })(content);
    return { map, nodes, text };
  }
  // DOM 位置 → 全文偏移:这个位置之前已经有多少个字(选区两端同一个函数)
  function offsetAt(idx, node, offset) {
    const it = idx.nodes.find(x => x.node === node);
    if (!it) return -1;
    let i = it.from;
    while (i < idx.map.length && idx.map[i].node === node && idx.map[i].off < offset) i++;
    return i;
  }
  function offsetRange(idx, start, end) {
    // 两头往里收过分隔位(元素边界插出来的那一格没有自己的 text node)。
    // 口径同 snip.js 的 `snipOffsetRange`。
    let a = start, b = end;
    while (a < b && idx.map[a] && !idx.map[a].node) a++;
    while (b > a && idx.map[b - 1] && !idx.map[b - 1].node) b--;
    const first = idx.map[a], last = idx.map[b - 1];
    if (!first || !last || b <= a) return null;
    const range = document.createRange();
    range.setStart(first.node, first.off);
    range.setEnd(last.node, last.off + 1);
    return range;
  }
  // 锚点收成规范形态 + 在压平全文里找它(找法见下面的 seek:按候选集合算,
  // 位置说不清就如实说没找到 —— 同一句话出现两次时静默挑第一处,是把一句
  // 「没找到」换成一个落在错地方的高亮)。口径同 snip.js 的 snipAnchor/snipSeek
  // 和后端 `favorites_api._anchor`。
  // 外侧空白去掉、接缝那一头留着:接缝上的空白是位置信息(引文首尾挪过来的),
  // 外侧那一个在另一种渲染里经常差一个(正文末尾在 textContent 里后面跟着换行,
  // 渲染成 HTML 之后那儿什么都没有)。口径同 snip.js 的 snipAnchor。
  function anchorOf(spec) {
    let p = collapse(spec && spec.p).replace(/^ +/, '');
    let q = collapse(spec && spec.q);
    let s = collapse(spec && spec.s).replace(/ +$/, '');
    if (q.startsWith(' ') && !p.endsWith(' ')) p += ' ';
    if (q.endsWith(' ') && !s.startsWith(' ')) s = ' ' + s;
    return { p, q: q.trim(), s };
  }
  // **按「候选集合」算,不按「档位顺序」挑。** 前文那一边(p+q)和后文那一边
  // (q+s)各给出它认的**全部**位置:0 个 = 这一边的证据在这份文本里用不上
  // (这一页把 `## 标题` 渲染成 <h2>,井号没了 —— 从订阅阅读器那边采来的前文
  // 跨过一个标题就整段失配,而后文那边分毫不差);多个 = 这一边自己定不了位,
  // 可它仍然把范围缩到了那几处。**这两件事不许压成同一个值** —— 压成同一个
  // 哨兵之后,前文明明说"只可能是第一处或第二处"的那个集合就丢了,只剩后文
  // 那个"唯一命中",于是静默高亮一处前文已经排除掉的地方(审查实测:三处一字
  // 不差的重复句,收藏第二处、只改写后文,这一页高亮落在偏移 429 而收藏的是
  // 326)。两边都有候选就取交集(交集正是整段 p+q+s 命中的那几处),一边用不上
  // 就只听另一边,两边都用不上才退到裸引文;**最后一律要求只剩一处** —— 剩两处
  // 是正文自己分不出的两处,剩零处是两边的证据矛盾,两种都如实说没找到。
  // **弱档(只听一侧、退到裸引文)还要先证明「这一篇就是采集时那一篇」**
  // —— `spec.same`,由 `locateQuote` 按 `#kbq=` 里的 `v`(库里的
  // `favorites.doc_version`)和这一页的 `DOC_VERSION` 比出来。一侧**天生没有**
  // (引文顶在正文开头/末尾)照旧只听另一侧;一侧**有证据却一处也对不上**时,
  // 分两种:版本号相等 = 文件没动过,那只能是两种渲染摊出来的文本不一样(弱档
  // 正是为它存在的);不等或者说不清 = 这一篇改过,那时另一侧的「唯一命中」证明
  // 不了它就是原处 —— 三处一字不差的重复句,收藏第二处、后来只把它的后文改掉,
  // 前文那一边仍然唯一地指向前一处,于是静默高亮一处早该排除的地方(审查实测)。
  // **裸引文那一档还要一个条件:采集那一刻这段引文在这一篇里就只有一处**
  // (`spec.u === 1`,库里那一列是 `favorites.quote_unique`)。「现在只剩一处」
  // 证明不了「这一处就是当时那一处」—— 当时有两处的话,原处完全可能正是被改稿
  // 换掉的那一处,而剩下的这一处是采集时就明确不是的另一处(审查实测:删掉原处
  // 之后高亮落在第一处)。拿不到那个数(老收藏,`u` 没有)= 不知道,那一档一律
  // 不给用:漏报顶多是一句如实的「没找到」,误报是静默跳到另一段话。
  // **「两侧都还在、现在正好一处」同样不足以定身份。** 采集那一刻整段锚点就分不出
  // 两处(前后各两百字也一字不差)时,那一刻谁也说不出用户选的是第几处;后来原处
  // 被整节删掉,剩下的那一处成了唯一命中 —— 采信它等于高亮一处从没被收藏过的文字
  // (审查用生产函数实测:采集偏移 740,删掉第二节之后回查到 244)。所以采集那头
  // 把「这条锚点定不定得住」也随收藏落库(`favorites.anchor_exact` → `#kbq=` 里的
  // `e`),记着「定不住」的哪一档都不给用;老收藏没记下(没有 `e`)按原样放行。
  // **例外是「这一页的正文整份就是这段引文」**:两侧天生都没有、而这份文本里除了
  // 这段引文什么都没有 —— 没有第二处可冒认,`same` 和 `u` 都不必再问。
  // 口径同 snip.js 的 snipSeek/snipAll,两头各一份,改一处要改两处。
  const MAX_HITS = 64;   // 数到这个数就不往下数;数不完的那一边一律当说不清
  function hitsOf(text, needle) {
    const out = [];
    if (!needle) return out;
    for (let at = text.indexOf(needle); at >= 0; at = text.indexOf(needle, at + 1)) {
      out.push(at);
      if (out.length > MAX_HITS) break;
    }
    return out;
  }
  function seek(text, spec) {
    const a = anchorOf(spec);
    if (!a.q) return null;
    const same = !!(spec && spec.same);
    // 「这份正文整份就是这段引文」= 没有别处可冒认,位置只能是这一处(见上)
    const whole = !a.p && !a.s && text.trim() === a.q;
    // 采集那一刻这条锚点就定不住的话,这一刻的「正好一处」是别处被删掉了(见上)
    if (!whole && Number(spec && spec.e) === 0) return null;
    // 一侧的三种状态**不许压成两种**:null = 这条锚点天生没有这一侧;
    // [] = 有这一侧、在这份文本里一处也对不上;非空 = 它认的那几处。
    const byP = a.p ? hitsOf(text, a.p + a.q).map(at => at + a.p.length) : null;
    const byS = a.s ? hitsOf(text, a.q + a.s) : null;   // q+s 的起点就是引文的起点
    // 「有证据却一处也对不上」= 这份语料不是采集时那一份,除非证明过(见上)
    if (((byP && !byP.length) || (byS && !byS.length)) && !same) return null;
    const hitP = byP || [], hitS = byS || [];
    const bare = whole || (same && Number(spec && spec.u) === 1)
      ? hitsOf(text, a.q) : [];
    const cand = hitP.length && hitS.length ? hitP.filter(at => hitS.includes(at))
      : hitP.length ? hitP
        : hitS.length ? hitS
          : bare;
    const over = xs => xs.length > MAX_HITS;
    if (over(hitP) || over(hitS) || over(cand)) return null;
    return cand.length === 1 ? [cand[0], cand[0] + a.q.length] : null;
  }
  // 前后文从 30 字起、不唯一就加长,取第一档唯一的。同 snip.js 的
  // `SNIP_CTX_STEPS`/`snipContext` —— 两头各存一份,改一处要改两处。
  // `exact` = 这条锚点按回跳的口径落回的正是这一处。**它要传出去**:三档都分不
  // 出来时存照旧存,但得当场说一句"点回去可能定位不到这一处"(app 那一侧同口径,
  // 见 snip.js 的 `locatable`)—— 不说的话,用户要到点回去那一刻才发现。
  const CTX_STEPS = [30, 80, 200];
  /** 这段引文在这份文本里是不是只有一处(1/0)。采集那一刻存进
   *  `favorites.quote_unique`,回跳那一刻裸引文那一档按它决定能不能采信。*/
  function uniqueIn(text, quote) {
    return hitsOf(text, collapse(quote).trim()).length === 1 ? 1 : 0;
  }
  function contextOf(text, a, b) {
    const quote = text.slice(a, b);
    let first = null;
    for (const n of CTX_STEPS) {
      // 窗口两头可能切在一个字中间,削掉那半个(见 whole)
      const ctx = { p: whole(text.slice(Math.max(0, a - n), a)),
                    s: whole(text.slice(b, b + n)) };
      if (!first) first = { ...ctx, exact: false };
      // 判据走 seek 自己 —— 回跳用的就是它(候选集合 + 外侧空白那一套)。
      // `u` 按**这一刻这一篇里**算:采集和核对都发生在现在,「只有一处」就是
      // 「只有一处」;回跳那一刻才有"原处可能已经不在了"这件事(见 seek)。
      // `same: true` 是事实而不是假设:这份 text 正是这一刻这一页的正文,而存下去
      // 的 `doc_version` 就是它 —— 回跳那一刻版本号还相等的话,那一头判的档位和
      // 这儿一样。少了它,这儿算出"定得到"、点回去却是一句"没找到"。
      const hit = seek(text, { q: quote, p: ctx.p, s: ctx.s, same: true,
                               u: uniqueIn(text, quote) });
      if (hit && hit[0] === a) return { ...ctx, exact: true };
      if (a - n <= 0 && b + n >= text.length) break;
    }
    return first;
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
  // 「选完了」三种输入方式各报一次,一处也不许少:鼠标 mouseup、触摸 touchend
  // (手机上长按选词没有 mouseup)、键盘/辅助技术 keyup(Shift+方向键选字全程
  // 没有 mouseup)。口径同 snip.js 的 `snipSettle`,两头各一份。
  const settle = e => {
    if (e.target === btn) return;
    setTimeout(showBtn, 10);   // 等浏览器结算好选区
  };
  document.addEventListener('mouseup', settle);
  document.addEventListener('touchend', settle);
  document.addEventListener('keyup', e => {
    if (e.key === 'Shift' || e.key.startsWith('Arrow') || e.key === 'Home'
        || e.key === 'End' || e.key === 'PageUp' || e.key === 'PageDown'
        || (e.key === 'a' && (e.ctrlKey || e.metaKey))) settle(e);
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
    let a = offsetAt(idx, range.startContainer, range.startOffset);
    let b = offsetAt(idx, range.endContainer, range.endOffset);
    let quote, prefix = '', suffix = '', locatable = false, unique = 0;
    if (a >= 0 && b > a) {
      // 引文两头的空白挪进前后文,这样压平之后 prefix+quote+suffix 仍是正文里
      // 连着的一段(后端 `_anchor` 同一条规则)
      while (a < b && idx.text[a] === ' ') a++;
      while (b > a && idx.text[b - 1] === ' ') b--;
      // 端点落在一个字中间时往里收一格(见 whole):引文和前后文的边界都得是整字
      if (a < b && LOW_HALF.test(idx.text[a])) a++;
      if (b > a && HIGH_HALF.test(idx.text[b - 1])) b--;
      // **引文按原文存**:压平过的那一份只是匹配表示,存它等于把选中的多行代码
      // 接成一行(换行和缩进就再也找不回来了)。`offsetRange` 给的正是 [a,b)
      // 在 DOM 里的范围,`shown()` 把它变成屏幕上那份文字 —— 压一遍就等于
      // `idx.text.slice(a, b)`,匹配照旧。同 snip.js 的 `snipSpec`/`snipShown`。
      const dom = offsetRange(idx, a, b);
      quote = whole((dom ? shown(dom) : idx.text.slice(a, b)).trim());
      const ctx = contextOf(idx.text, a, b);
      prefix = ctx.p;
      suffix = ctx.s;
      locatable = !!ctx.exact;
      unique = uniqueIn(idx.text, quote);
    } else {
      // 选区端点不在 text node 上的少见情况:锚点留空,靠全文搜索。
      // `same: true` 同上面 contextOf:这一刻读的就是这一页的正文,存下去的
      // `doc_version` 就是它 —— 少了这一个字,这一档(两侧都空)永远算不出
      // "定得到",屏幕上于是永远多一句"点回去可能分不出是第几处"。
      quote = whole(sel.toString().trim());
      unique = uniqueIn(idx.text, quote);
      locatable = !!seek(idx.text, { q: quote, p: '', s: '', u: unique, same: true });
    }
    btn.textContent = '…';
    try {
      const r = await fetch('/api/favorites', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: DOC_PATH, quote, prefix, suffix,
                               // 采集那一刻这段引文在这一篇里是不是只有一处 ——
                               // 回跳那一刻裸引文那一档按它决定能不能采信(见 seek)
                               quote_unique: unique,
                               // **采集那一刻这条锚点定不定得住**:定不住的(这一篇
                               // 里有几处连前后文都一样)往后哪一档都不给用 ——
                               // 「现在正好剩一处」只说明别处被删掉了(见 seek)。
                               // 屏幕上那句提示说的是同一件事,两者同一个判据。
                               anchor_exact: locatable ? 1 : 0,
                               doc_version: DOC_VERSION }),
      });
      // 409 = 这一页上这份正文已经旧了。**这一头没有退路可降级**(页面上压根
      // 没有另一条回跳路线),所以如实说一句怎么办 —— 后端那句话只说"不是你
      // 核对过的那一版"。
      if (r.status === 409) {
        throw new Error('这一篇的正文刚更新过(页面上这份是旧的),刷新一下再收藏');
      }
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
      sel.removeAllRanges();
      // 分不出是第几处时如实说一句(口径同 app 那一侧的 `!spec.locatable`)
      toast(locatable ? '✓ 片段已收藏,「收藏 · 待试」里可回看'
        : '✓ 片段已收藏 —— 这一篇里有几处一模一样的文字,点回去可能定位不到这一处');
    } catch (err) {
      toast('收藏失败: ' + err.message);
    }
    hideBtn();
    btn.textContent = '☆ 收藏此段';
  });

  // ── #kbq= 锚点 → 高亮 + 滚动定位 ──
  let lastEl = null;
  // **每一次定位请求先把上一次的结果清掉**,早退的那几条分支也算(没有 kbq、
  // 参数坏了、引文空、找不到、范围建不出来)。同一页上从一条找得到的收藏换到
  // 一条找不到的,旧那一句照旧亮着、`load` 那一下还会把页面滚回它那儿 ——
  // 用户看到的是「这条收藏指到那一句」,而那是上一条的位置。口径同 snip.js 的
  // `locateSnip`(它开头就是 `clearSnipHighlight()`)。
  function clearQuote() {
    if (window.CSS && CSS.highlights) CSS.highlights.delete('kb-quote');
    lastEl = null;
  }
  function locateQuote() {
    clearQuote();
    const m = location.hash.match(/kbq=([^&]*)/);
    if (!m) return;
    let spec;
    try { spec = JSON.parse(decodeURIComponent(m[1])); } catch { return; }
    if (!spec || !spec.q) return;
    const idx = textIndex();
    // **`same` 在这儿定**:`v` 是收藏那一刻核对过的正文版本,`DOC_VERSION` 是这
    // 一页此刻的。相等 = 这一篇没动过,锚点一侧失配只能是两种渲染的差别,弱档
    // 可以采信;不等、或者老收藏压根没记下版本 = 证明不了,弱档一律不给用
    // (口径同 `snip.js` 的 `snipSeek`,两头各一份,改一处要改两处)。
    const hit = seek(idx.text, { ...spec, same: !!spec.v && spec.v === DOC_VERSION });
    if (!hit) { toast('没找到这段收藏(原文可能已更新)'); return; }
    const range = offsetRange(idx, hit[0], hit[1]);
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


# 正文是嵌进页面内联 <script> 里的,所以 `<` 一律编码成 `\u003c` —— 不是只防
# `</script>` 那一种写法。HTML 的脚本数据状态里,`<!--` 之后再出现 `<script` 会把
# 分词器推进「双重转义」:那之后的 `</script>` 只把状态退回一层、**不结束这个
# script 元素**,于是页面末尾的 `</script></body></html>` 全被当成脚本内容吞掉 ——
# 整段脚本一行都没执行:`MD` 没定义、上面那个字面 renderer 没注册、正文渲染、纯文本
# 降级和片段收藏全都没发生,阅读页只剩标题(2026-10-08 第 4 轮审查实测:一篇正文写着
# `示例开头写 <!-- <script>。` 的纯文本文章,连它后面那个普通段落一起看不见;同一个
# 开头放进 markdown 代码围栏里照样复现 —— 故障发生在 markdown 解析**之前**,围栏
# 保护不了 HTML 文档里的内嵌 JSON)。
#
# `<` 全编码掉之后分词器再也离不开脚本数据状态,原来那条 `</` 特判被它涵盖;
# `\u003c` 是合法的 JSON 转义,`JSON.parse` 回来还是同一个字符串,md 一个字节不变。
def _script_json(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


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

    # 解码走 `read_md_text`:版本号是按读到的文本算的,报版本的这几处和落库前
    # 再算一次的那一处必须同一种解码(见 `scanner.read_md_text`)。
    text = read_md_text(full)
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
                f'<a href="{html_mod.escape(safe)}" target="_blank" rel="noopener">绿泡泡原文{_EXT_ARROW}</a>'
            )
    obs = (f"obsidian://open?vault={quote(VAULT_NAME)}"
           f"&file={quote(doc_path.removesuffix('.md'))}")
    meta_parts.append(f'<a href="{obs}">在 Obsidian 打开</a>')

    md_json = _script_json(body)
    path_json = _script_json(doc_path)
    # **按整份 text 算,不按上面剥掉 frontmatter 之后那一份**:后端落库前是对着
    # 盘上那一整个文件算的,少剥一行都对不上(两头都过 `scanner.body_version`)。
    version_json = _script_json(body_version(text))
    return HTMLResponse(
        _DOC_TMPL.format(
            title=html_mod.escape(title), meta=" · ".join(meta_parts),
            md_json=md_json, path_json=path_json, version_json=version_json,
            literal_js=_DOC_LITERAL_JS, snip_js=_DOC_SNIP_JS,
        ),
        # 动态渲染页,改版后旧缓存会让新脚本(片段收藏)不生效
        headers={"Cache-Control": "no-cache"},
    )
