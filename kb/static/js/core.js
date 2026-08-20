// ── 基础:$ / tab 切换 / meta / 过滤 chips / 链接工具 / 阅读器 / 搜索 ────────
const $ = id => document.getElementById(id);
let tagFilters = [], entityFilters = [];

function switchTab(t) {
  for (const k of ['gallery', 'search', 'ask', 'fav', 'pipe']) {
    $(k + 'panel').style.display = t === k ? '' : 'none';
    $('tab-' + k).classList.toggle('on', t === k);
  }
  document.body.classList.toggle('chat-mode', t === 'ask');
  if (t === 'pipe') refreshPipe(true);
  if (t === 'ask') initChat();
  if (t === 'fav') loadFavPanel();
}

async function loadMeta() {
  const [stats, facets] = await Promise.all([
    fetch('/api/stats').then(r => r.json()),
    fetch('/api/facets').then(r => r.json()),
  ]);
  $('stats').textContent =
    `${stats.documents} 篇 · ${stats.vectors} 向量 · ${stats.entities} 实体 · 已提取 ${stats.extracted}`;
  const fill = (sel, items, label) => {
    for (const it of items) {
      const o = document.createElement('option');
      o.value = it.value;
      o.textContent = `${label ? it.value.replace(label, '') : it.value} (${it.n})`;
      sel.appendChild(o);
    }
  };
  fill($('f-source'), facets.sources);
  fill($('f-account'), facets.accounts);
  fill($('f-topic'), facets.topics, '');
  fill($('f-entity'), facets.entities);
}

function renderChips() {
  // chip 文案是被索引的外部内容(实体名来自公众号原文/LLM 提取),一律转义;
  // 删除按钮走 data 属性 + 事件委托,不再把值拼进 onclick 的 JS 字符串里
  $('chips').innerHTML =
    tagFilters.map(t => `<span class="chip">${esc(t)}<b data-rm-tag="${esc(t)}">×</b></span>`).join('') +
    entityFilters.map(e => `<span class="chip">${esc(e)}<b data-rm-ent="${esc(e)}">×</b></span>`).join('');
}
function addTopic(v) { if (v && !tagFilters.includes(v)) tagFilters.push(v); $('f-topic').value=''; renderChips(); doSearch(); }
function rmTopic(v) { tagFilters = tagFilters.filter(x => x !== v); renderChips(); doSearch(); }
function addEntity(v) { if (v && !entityFilters.includes(v)) entityFilters.push(v); $('f-entity').value=''; renderChips(); doSearch(); }
function rmEntity(v) { entityFilters = entityFilters.filter(x => x !== v); renderChips(); doSearch(); }

function obsLink(h) {
  return `obsidian://open?vault=${encodeURIComponent(h.vault)}&file=${encodeURIComponent(h.path.replace(/\.md$/, ''))}`;
}
function docLink(h) {
  return '/doc/' + h.path.split('/').map(encodeURIComponent).join('/');
}
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
// 只放行安全协议:被索引的 url 来自外部内容,javascript: 之类点一下就执行
const safeUrl = u => /^(https?:|obsidian:|\/|#)/i.test(String(u || '')) ? String(u) : '#';
// Markdown → HTML 的唯一出口。条目正文、模型回答都是外部内容,而 marked 从 v5 起
// 不带 sanitizer:原始 <img onerror> 和 javascript: 链接会被原样输出。一律过 DOMPurify。
// 兜底是"失败关闭":sanitizer 没加载出来就退化成纯文本,绝不放行未净化的 HTML。
function mdHtml(src) {
  const md = String(src || '');
  if (!window.DOMPurify || !window.marked) {
    return `<div style="white-space:pre-wrap">${esc(md)}</div>`;
  }
  return DOMPurify.sanitize(marked.parse(md));
}
// 页内阅读链接:点击滑出面板,href 保留 /doc 路径,中键/Cmd 点仍可新标签打开
// hash 传 #kbq=…(片段定位锚点),doc 页加载后据此高亮滚动
// label 一律当纯文本转义(标题来自公众号/LLM,不能当 HTML 拼);
// 确实要塞 HTML 片段的调用方用 docAHtml()
function docA(h, label, cls = '', hash = '') {
  return docAHtml(h, esc(label), cls, hash);
}
function docAHtml(h, labelHtml, cls = '', hash = '') {
  return `<a ${cls ? `class="${cls}"` : ''} href="${docLink(h)}${hash}" data-doc="1"` +
    ` data-path="${encodeURIComponent(h.path)}"` +
    ` data-title="${esc(h.title)}" data-url="${esc(safeUrl(h.url || ''))}" data-obs="${esc(obsLink(h))}"` +
    ` title="${esc(h.title)}${h.account ? ' — ' + esc(h.account) : ''}">${labelHtml}</a>`;
}

function openReader(a) {
  $('reader-frame').src = a.getAttribute('href');
  $('reader-title').textContent = a.dataset.title || '';
  $('reader-open').href = a.getAttribute('href');
  $('reader-obs').href = a.dataset.obs || '#';
  $('reader-src').style.display = a.dataset.url ? '' : 'none';
  if (a.dataset.url) $('reader-src').href = a.dataset.url;
  const fav = $('reader-fav');
  if (a.dataset.path) {
    fav.dataset.path = a.dataset.path;
    setFavBtn(fav, favSet.has(decodeURIComponent(a.dataset.path)));
    fav.style.display = '';
  } else {
    fav.style.display = 'none';
  }
  $('reader-mask').style.display = '';
  $('reader').style.display = '';
}
function closeReader() {
  $('reader').style.display = 'none';
  $('reader-mask').style.display = 'none';
  $('reader-frame').src = 'about:blank';
}
document.addEventListener('click', e => {
  const a = e.target.closest('a[data-doc]');
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
  e.preventDefault();
  openReader(a);
});
// 实体/过滤 chip 的点击走事件委托:值从 data 属性读,不经过 JS 字符串拼接
document.addEventListener('click', e => {
  const ent = e.target.closest('[data-ent]');
  if (ent) { addEntity(ent.dataset.ent); return; }
  const rmT = e.target.closest('[data-rm-tag]');
  if (rmT) { rmTopic(rmT.dataset.rmTag); return; }
  const rmE = e.target.closest('[data-rm-ent]');
  if (rmE) rmEntity(rmE.dataset.rmEnt);
});
// Escape 逐层关:阅读器(最高)→ 工具详情抽屉 → 管道时间段弹层 / 管道弹窗
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if ($('reader').style.display !== 'none') { closeReader(); return; }
  if ($('tool-drawer').style.display !== 'none') { closeToolDrawer(); return; }
  if ($('pipe-range-pop').style.display !== 'none') $('pipe-range-pop').style.display = 'none';
  else if ($('pipe-modal').style.display !== 'none') closePipeModal();
});
// 时间段弹层点外面收起(弹窗本体不关)
document.addEventListener('click', e => {
  const pop = $('pipe-range-pop');
  if (pop.style.display !== 'none' && !pop.contains(e.target) && !$('pipe-range-btn').contains(e.target)) {
    pop.style.display = 'none';
  }
});

async function doSearch() {
  const q = $('q').value.trim();
  if (!q && !tagFilters.length && !entityFilters.length) return;
  $('results').innerHTML = '<div class="empty"><span class="spin"></span>搜索中…</div>';
  const p = new URLSearchParams({ q: q || '*' });
  if ($('f-source').value) p.set('source', $('f-source').value);
  if ($('f-account').value) p.set('account', $('f-account').value);
  tagFilters.forEach(t => p.append('tag', t));
  entityFilters.forEach(e => p.append('entity', e));
  const res = await fetch('/api/search?' + p).then(r => r.json()).catch(() => ({hits: []}));
  if (!res.hits.length) { $('results').innerHTML = '<div class="empty">没有结果</div>'; return; }
  // 命中里的标题/账号/摘要/片段/实体都是被索引的外部内容,拼进 innerHTML 前一律转义
  $('results').innerHTML = res.hits.map(h => `
    <div class="hit">
      <h3>${docA(h, h.title)}</h3>
      <div class="meta">
        <span>${esc(h.account || '')}</span><span>${esc((h.published || '').slice(0, 10))}</span>
        <span class="badge">${esc(h.source || '')}</span>
        ${h.matched_by.map(m => `<span class="badge ${m === 'vec' ? 'vec' : ''}">${m === 'vec' ? '语义' : '关键词'}</span>`).join('')}
        <a href="${esc(obsLink(h))}" style="color:var(--muted)" title="在 Obsidian 中打开">Obsidian</a>
        ${h.url ? `<a href="${esc(safeUrl(h.url))}" target="_blank" rel="noopener" style="color:var(--muted)">原文↗</a>` : ''}
        <span class="favbtn ${favSet.has(h.path) ? 'on' : ''}" data-path="${encodeURIComponent(h.path)}"
              onclick="toggleFav(this)">${favSet.has(h.path) ? '★ 已收藏' : '☆ 收藏'}</span>
      </div>
      ${h.summary ? `<div class="summary">${esc(h.summary)}</div>` : ''}
      <div class="snippet">${esc((h.best_chunk || '').slice(0, 160))}…</div>
      ${h.entities.length ? `<div class="ents">${h.entities.map(e =>
        `<span class="ent" data-ent="${esc(e)}">${esc(e)}</span>`).join('')}</div>` : ''}
    </div>`).join('');
}
