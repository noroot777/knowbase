# -*- coding: utf-8 -*-
"""预览页注入的 CSS / JS 常量。

从 wechat_preview.py 里抽出来的——那个文件 886 行,超过本仓库 700 行的上限,
而这一坨是纯字符串常量,跟解析逻辑放一起只是让文件变长。搬走不改一个字符。
wechat_preview 仍然把它们再导出一遍,原来 `from src.utils.wechat_preview import
_PREVIEW_AI_CSS` 这样的写法照旧能用。
"""

from __future__ import annotations

# 微信 tencent_portfolio_light.css 中代码块核心样式（外链 CSS 未加载时兜底）
_CODE_SNIPPET_CSS = """
.code-snippet,.code-snippet__fix,.code-snippet *,.code-snippet__fix *{max-width:100%!important;box-sizing:border-box}
.code-snippet{margin:10px 0;display:block;overflow-x:auto;font-size:14px;padding:1em 1em 1em 3em;color:#333;position:relative;background-color:#fafafa;border:1px solid #f0f0f0;border-radius:2px;counter-reset:line;white-space:normal}
.code-snippet code{text-align:left;font-size:14px;display:block;white-space:pre-wrap;position:relative;font-family:Consolas,Liberation Mono,Menlo,Courier,monospace}
.code-snippet__fix{font-size:14px;margin:10px 0;display:flex;color:#333;position:relative;background-color:rgba(0,0,0,.03);border:1px solid #f0f0f0;border-radius:2px;line-height:26px}
.code-snippet__fix pre{overflow-x:auto;padding:1em 1em 1em 0;white-space:normal;flex:1;margin:0}
.code-snippet__fix code{text-align:left;font-size:14px;display:block;white-space:pre;position:relative;font-family:Consolas,Liberation Mono,Menlo,Courier,monospace}
.code-snippet__fix .code-snippet__line-index{flex-shrink:0;height:100%;padding:1em;margin:0;list-style:none;counter-reset:line}
.code-snippet__fix .code-snippet__line-index li{list-style:none;text-align:right}
.code-snippet__fix .code-snippet__line-index li:before{min-width:1.5em;text-align:right;counter-increment:line;content:counter(line);display:inline;color:rgba(0,0,0,.15)}
.code-snippet__comment,.code-snippet__quote,.code-snippet__meta{color:#afafaf;font-style:italic}
.code-snippet__keyword,.code-snippet__selector-tag,.code-snippet__subst,.code-snippet__regexp,.code-snippet__link,.code-snippet__built_in,.code-snippet__builtin-name{color:#ca7d37}
.code-snippet__template-variable,.code-snippet__tag .code-snippet__attr,.code-snippet__type,.code-snippet__class .code-snippet__title,.code-snippet__tag,.code-snippet__name,.code-snippet__attribute{color:#0e9ce5}
.code-snippet__title,.code-snippet__section,.code-snippet__selector-id,.code-snippet__symbol,.code-snippet__bullet{color:#d14}
body[data-weui-theme="light"] .code-snippet,
body[data-weui-theme="light"] .code-snippet__fix{color:#333}
body[data-weui-theme="light"] .code-snippet__fix .code-snippet__line-index li:before{color:rgba(0,0,0,.15)}
"""

# 文章标题 + 原创/作者/公众号/时间/属地（依赖微信 CSS 变量与外链，预览内联兜底）
_ARTICLE_HEADER_CSS = """
/* preview-header-v1 */
body[data-weui-theme="light"]{
  --weui-LINK:#576b95;
  --weui-FG-1:rgba(0,0,0,.55);
  --weui-FG-2:rgba(0,0,0,.3);
  --weui-FG-HALF:rgba(0,0,0,.9);
}
.rich_media_title{font-size:22px;line-height:1.4;font-weight:400;margin-bottom:12px;color:var(--weui-FG-HALF);word-wrap:break-word}
.rich_media_meta_list{margin-bottom:22px;line-height:20px;font-size:0;word-wrap:break-word}
.rich_media_meta_list em{font-style:normal}
.rich_media_meta{display:inline-block;vertical-align:middle;margin:0 10px 10px 0;font-size:15px}
.rich_media_meta.icon_appmsg_tag{margin-right:4px;padding:1px 6px;font-size:12px;line-height:1.5;color:var(--weui-FG-2);background:rgba(0,0,0,.05);border-radius:2px}
.rich_media_meta.appmsg_title_tag{margin-right:8px}
.rich_media_meta_text,.rich_media_meta_list #publish_time{color:var(--weui-FG-2);font-style:normal}
.rich_media_meta_nickname a,.rich_media_meta_link{color:var(--weui-LINK);text-decoration:none}
.rich_media_meta_nickname a:hover{text-decoration:underline}
.rich_media_meta_nickname,.rich_media_meta_nickname a,#js_name,.wx-preview-account{cursor:pointer}
.rich_media_meta_nickname:hover,.rich_media_meta_nickname a:hover,#js_name:hover,.wx-preview-account:hover{opacity:.88}
#meta_content_hide_info{display:inline}
#js_ip_wording_wrp.rich_media_meta{display:inline-block}
"""

# 正文表格（微信编辑器 table/thead/th/td，外链样式未加载时兜底）
_ARTICLE_TABLE_CSS = """
/* preview-table-v1 */
#js_content table,#js_article table,.wx_preview_table{
  margin:16px 0;
  border-collapse:collapse;
  display:table!important;
  width:100%!important;
  max-width:100%;
  table-layout:auto;
  word-break:break-word;
  font-size:15px;
  line-height:1.6;
  color:rgba(0,0,0,.9);
}
#js_content table thead,#js_article table thead{display:table-header-group!important}
#js_content table tbody,#js_article table tbody{display:table-row-group!important}
#js_content table tr,#js_article table tr{display:table-row!important}
#js_content table th,#js_content table td,#js_article table th,#js_article table td{
  display:table-cell!important;
  vertical-align:top;
  padding:8px 10px;
  border:1px solid #e5e5e5;
  text-align:left;
  box-sizing:border-box;
}
#js_content table th,#js_article table th{
  font-weight:600;
  background-color:#f7f7f7;
  color:rgba(0,0,0,.9);
}
#js_content table tbody tr:nth-child(even) td,#js_article table tbody tr:nth-child(even) td{
  background-color:#fafafa;
}
#js_content table td code,#js_article table td code{
  font-family:Consolas,Menlo,monospace;
  font-size:14px;
  word-break:break-all;
}
.wx_preview_table_wrap{
  margin:16px 0;
  overflow-x:auto;
  -webkit-overflow-scrolling:touch;
}
.wx_preview_table_wrap>table{margin:0}
"""

# 阅读区背景：固定浅色（不受系统深色模式影响，避免代码块/表格与背景同色）
_PREVIEW_IMG_CSS = """
/* preview-img-v2 */
.wx-preview-img{
  display:block;
  max-width:100%!important;
  height:auto!important;
  min-height:48px;
  margin:12px auto;
  background:#e8e4de;
  border-radius:4px;
  object-fit:contain;
}
"""

_PREVIEW_SURFACE_CSS = """
/* preview-surface-v3 */
:root{color-scheme:light}
body{
  margin:0;
  padding:14px 12px 28px;
  background:#f2ede6!important;
  color:rgba(0,0,0,.88)!important;
}
#page-content,.rich_media_area_primary{
  max-width:667px;
  margin:0 auto;
  background:#fdfbf7!important;
  border-radius:12px;
  box-shadow:0 2px 14px rgba(26,18,46,.07);
  overflow:hidden;
  color:rgba(0,0,0,.88)!important;
}
.rich_media_area_primary_inner{padding:4px 18px 24px}
#js_content,#js_content p,#js_content section{color:rgba(0,0,0,.88)}
#js_row_immersive_stream_wrap{display:none!important;height:0!important;overflow:hidden!important;margin:0!important;padding:0!important}
#js_article_bottom_bar{
  max-width:667px;
  margin:12px auto 0;
  padding:0 4px;
}
body[data-weui-theme="light"] #js_content table th{background-color:#f0ebe3}
body[data-weui-theme="light"] #js_content table tbody tr:nth-child(even) td{background-color:#f7f4ef}
body[data-weui-theme="light"] .code-snippet,
body[data-weui-theme="light"] .code-snippet__fix,
body[data-weui-theme="light"] .code-snippet code,
body[data-weui-theme="light"] .code-snippet__fix code{color:#333!important}
body[data-weui-theme="light"] #js_content table,
body[data-weui-theme="light"] #js_content table th,
body[data-weui-theme="light"] #js_content table td{color:rgba(0,0,0,.9)!important}
body[data-weui-theme="light"] #js_content table td code{color:#333!important;background:transparent}
"""

# AI 摘要与标签（插在 meta 行下方）
_PREVIEW_AI_CSS = """
/* preview-ai-v1 */
.wx_preview_ai{
  margin:16px 0 20px;
  padding:14px 16px 16px;
  border-radius:12px;
  background:linear-gradient(135deg,#f5f0ff 0%,#fdf8f3 50%,#eef6ff 100%);
  border:1px solid rgba(139,92,246,.25);
  box-shadow:0 2px 14px rgba(99,102,241,.1);
}
.wx_preview_ai_head{margin-bottom:10px}
.wx_preview_ai_badge{
  display:inline-flex;
  align-items:center;
  gap:6px;
  padding:5px 12px 5px 10px;
  font-size:12px;
  font-weight:700;
  letter-spacing:.06em;
  color:#fff;
  background:linear-gradient(135deg,#a855f7 0%,#6366f1 100%);
  border-radius:999px;
  box-shadow:0 2px 10px rgba(99,102,241,.4);
}
.wx_preview_ai_badge::before{
  content:"✦";
  font-size:12px;
  line-height:1;
  opacity:.95;
}
.wx_preview_ai_summary{
  margin:0 0 12px;
  font-size:15px;
  line-height:1.7;
  color:rgba(0,0,0,.82);
}
.wx_preview_ai_tags{
  display:flex;
  flex-wrap:wrap;
  gap:8px;
}
.wx_preview_ai_tag{
  display:inline-flex;
  align-items:center;
  padding:4px 11px;
  font-size:12px;
  font-weight:600;
  border-radius:999px;
  line-height:1.3;
}
.wx_preview_ai_tag--0{background:#ede9fe;color:#5b21b6}
.wx_preview_ai_tag--1{background:#dbeafe;color:#1d4ed8}
.wx_preview_ai_tag--2{background:#fce7f3;color:#9d174d}
.wx_preview_ai_tag--3{background:#d1fae5;color:#047857}
"""

_PREVIEW_IMG_LIGHTBOX_JS = """
(function(){
  function normalizeLightboxSrc(raw){
    var src = (raw || '').trim();
    if (!src) return '';
    if (/^https?:\\/\\//i.test(src)) return src;
    if (src.charAt(0) === '/' && src.indexOf('/api/wechat-image') === 0) return src;
    return '';
  }
  function openParentLightbox(src){
    src = normalizeLightboxSrc(src);
    if (!src) return;
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ type: 'wx-preview-img-open', src: src }, '*');
      }
    } catch (e) {}
  }
  var selectors = '.wx-preview-img, #js_content img, #js_article img';
  document.querySelectorAll(selectors).forEach(function(img){
    if (!img || img.tagName !== 'IMG') return;
    img.style.cursor = 'zoom-in';
    img.addEventListener('click', function(e){
      e.preventDefault();
      e.stopPropagation();
      var src = img.getAttribute('src') || img.getAttribute('data-src') || '';
      openParentLightbox(src);
    });
  });
})();
"""
