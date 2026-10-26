// ── 基础:$ / tab 切换 / meta / 过滤 chips / 链接工具 / 阅读器 / 搜索 ────────
const $ = id => document.getElementById(id);
let tagFilters = [], entityFilters = [];

function switchTab(t) {
  const regularTabs = ['gallery', 'search', 'ask', 'fav', 'daily', 'subs', 'pipe'];
  // 老深链 #wechat/#social/#rss(来源目录里的模块 id)一律落到「我的订阅」的对应视图
  if (typeof subsCatalog !== 'undefined' && subsCatalog.some(m => m.id === t)) { subsPendingKind = t; t = 'subs'; }
  if (!regularTabs.includes(t)) return;
  const previousScroll = window.scrollY;
  for (const k of regularTabs) {
    $(k + 'panel').style.display = t === k ? '' : 'none';
    $('tab-' + k).classList.toggle('on', t === k);
  }
  document.body.classList.toggle('chat-mode', t === 'ask');
  if (typeof updateWorkspaceTab === 'function') updateWorkspaceTab(t, previousScroll);
  if (typeof applySubsReadingLayout === 'function') applySubsReadingLayout();
  if (t === 'pipe') { refreshPipe(true); loadPipeHistory(); }
  if (t === 'daily') loadDailyPanel();
  if (t === 'ask') initChat();
  if (t === 'fav') loadFavPanel();
  if (t === 'search') loadMeta().catch(() => {});
  if (t === 'subs') { const kind = subsPendingKind; subsPendingKind = ''; openSubsPanel(kind); }
}

async function loadMeta() {
  const [stats, facets] = await Promise.all([
    fetch('/api/stats').then(r => r.json()),
    fetch('/api/facets').then(r => r.json()),
  ]);
  $('stats').textContent =
    `${stats.documents.toLocaleString()} 篇内容已收录`;
  const fill = (sel, items, label) => {
    const selected = sel.value;
    while (sel.options.length > 1) sel.remove(1);
    for (const it of items) {
      const o = document.createElement('option');
      o.value = it.value;
      const name = sel.id === 'f-source' && typeof sourceModules !== 'undefined' ? sourceModules.get(it.value)?.label : '';
      o.textContent = `${name || (label ? it.value.replace(label, '') : it.value)} (${it.n})`;
      sel.appendChild(o);
    }
    if ([...sel.options].some(o => o.value === selected)) sel.value = selected;
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
    tagFilters.map(t => `<span class="chip">${esc(t)}<button data-rm-tag="${esc(t)}" aria-label="移除主题 ${esc(t)}">×</button></span>`).join('') +
    entityFilters.map(e => `<span class="chip">${esc(e)}<button data-rm-ent="${esc(e)}" aria-label="移除实体 ${esc(e)}">×</button></span>`).join('');
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
// 模型标识给人看的形式:接口里传的是带家的 "openai:gpt-5.5"(同一个 id 两家中转站
// 都可能有,裸 id 会被后端猜错家),但界面上只想看见模型名。老数据是裸 id,原样显示。
const modelLabel = s => String(s || '').replace(/^(anthropic|openai):/, '');
// 只放行安全协议:被索引的 url 来自外部内容,javascript: 之类点一下就执行
const safeUrl = u => /^(https?:|obsidian:|\/|#)/i.test(String(u || '')) ? String(u) : '#';
// Markdown → HTML 的唯一出口。条目正文、模型回答都是外部内容,而 marked 从 v5 起
// 不带 sanitizer:原始 <img onerror> 和 javascript: 链接会被原样输出。一律过 DOMPurify。
// 兜底是"失败关闭":sanitizer 没加载出来就退化成纯文本,绝不放行未净化的 HTML。
// 两个库都随包在 /static/vendor/,正常不会走到兜底;真走到了在 console 留一句,
// 否则"回答变纯文本"这种静默降级根本查不出是脚本没加载。
let _mdWarned = false;
function mdHtml(src, linkContext = null) {
  const md = String(src || '');
  if (!window.DOMPurify || !window.marked) {
    if (!_mdWarned) {
      _mdWarned = true;
      console.warn('[kb] marked/DOMPurify 未加载(marked=%s, DOMPurify=%s),Markdown 降级为纯文本',
        typeof window.marked, typeof window.DOMPurify);
    }
    return `<div style="white-space:pre-wrap">${esc(md)}</div>`;
  }
  if (linkContext !== null) {
    const links = marked.lexer(String(linkContext)).links;
    const lexer = new marked.Lexer();
    lexer.tokens.links = links;
    return DOMPurify.sanitize(marked.parser(lexer.lex(md)));
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
// soft 软广的角标。hard 是直接抛弃(压根不会出现在结果里),soft 保留但要让人
// 一眼看出「这篇有推广倾向」——判定理由在 documents.promo_reason,鼠标悬停看。
// 只认 'soft':档位是后端算好的(含衍生条目按 url 继承),前端不做二次判断。
function promoBadge(h) {
  return h && h.promo_level === 'soft'
    ? '<span class="badge promo" title="疑似软广:厂商通稿/带推广的推荐,内容仍保留,但检索里已降权">软广?</span>'
    : '';
}function docAHtml(h, labelHtml, cls = '', hash = '') {
  return `<a ${cls ? `class="${cls}"` : ''} href="${docLink(h)}${hash}" data-doc="1"` +
    ` data-path="${encodeURIComponent(h.path)}"` +
    ` data-title="${esc(h.title)}" data-url="${esc(safeUrl(h.url || ''))}" data-obs="${esc(obsLink(h))}"` +
    ` title="${esc(h.title)}${h.account ? ' — ' + esc(h.account) : ''}">${labelHtml}</a>`;
}

function openReader(a) {
  const frame = $('reader-frame');
  const generation = resetReaderNavigation();
  readerLoadListener = () => bindReaderScrollAway(generation);
  frame.addEventListener('load', readerLoadListener);
  navigateReader(a.getAttribute('href'));
  setReaderHeader(a);
  // 从哪一边滑出来。默认右边;日报正文里的译文入口要左边 —— 出处编号就在正文
  // 那一列里,抽屉从同一侧出来,眼睛不用横跨整屏去追它。**要在 display 之前设**:
  // 滑入动画是跟着 class 走的 CSS 动画,先显示再换 class 的话这一次还是按旧的
  // 那一边播完。
  $('reader').classList.toggle('from-left', a.dataset.side === 'left');
  $('reader-mask').style.display = '';
  $('reader').style.display = '';
}
function setReaderHeader(a) {
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
}

// 读正文时让出位置:正文在 iframe 里,所以绑的是它那份 window,而「这块还在不在
// 页面上」得看 iframe 元素自己(跨文档量不着)。头收起之后右上角留一颗常驻的
// 关闭按钮 —— 原本唯一的出口就在那条头里。机制见 kb/static/js/scrollaway.js。
let readerAway = null;
let readerLoadGeneration = 0;
let readerLoadListener = null;
let readerLastDocument = null;
let readerRequestedDocument = null;
let readerPendingDestination = null;
let readerNavigationId = 0;
let readerRecovery = null;
const READER_RECOVERY_WINDOW_MS = 5000;
function navigateReader(url) {
  const frame = $('reader-frame');
  readerRequestedDocument = new URL(url, location.href).href;
  readerPendingDestination = null;
  frame.dataset.readerNavigation = String(++readerNavigationId);
  frame.src = url;
}
function recordReaderNavigation(event) {
  const frame = $('reader-frame');
  const message = event.data;
  if (event.source !== frame.contentWindow || event.origin !== location.origin
      || message?.type !== 'reader-navigation' || $('reader').style.display === 'none'
      || message.navigationId !== frame.dataset.readerNavigation) return;
  let source;
  try { source = new URL(message.documentUrl); } catch { return; }
  if (source.origin !== location.origin
      || (readerLastDocument && source.href.split('#')[0] !== readerLastDocument.split('#')[0])) return;
  if (message.destination === null) { readerPendingDestination = null; return; }
  let destination;
  try { destination = new URL(message.destination); } catch { return; }
  if (!/^https?:$/.test(destination.protocol)) return;
  readerLastDocument ||= source.href;
  readerPendingDestination = destination.href;
}
window.addEventListener('message', recordReaderNavigation);
function resetReaderNavigation() {
  readerLoadGeneration++;
  if (readerLoadListener) $('reader-frame').removeEventListener('load', readerLoadListener);
  readerLoadListener = null;
  readerLastDocument = null;
  readerRequestedDocument = null;
  readerPendingDestination = null;
  readerRecovery = null;
  delete $('reader-frame').dataset.readerNavigation;
  $('reader-destination').style.display = 'none';
  setReaderUnavailable(false);
  return readerLoadGeneration;
}
function setReaderUnavailable(unavailable) {
  const frame = $('reader-frame');
  const restoreFocus = document.activeElement === frame || document.activeElement === document.body;
  frame.hidden = unavailable;
  frame.inert = unavailable;
  if (unavailable) frame.setAttribute('tabindex', '-1');
  else frame.removeAttribute('tabindex');
  $('reader-error').hidden = !unavailable;
  if (unavailable && restoreFocus) $('reader-close').focus({ preventScroll: true });
}
function recoverReaderDocument(doc) {
  const destination = readerPendingDestination || doc?.URL || readerRequestedDocument;
  if (!/^https?:/i.test(destination)) { setReaderUnavailable(true); return; }
  $('reader-open').href = destination;
  if (!readerLastDocument || destination === readerLastDocument) {
    setReaderUnavailable(true);
    return;
  }
  const link = $('reader-destination');
  link.href = destination;
  link.style.display = '';
  const recoveryUrl = readerLastDocument.split('#')[0];
  const now = Date.now();
  if (readerRecovery?.blocked || (readerRecovery?.url === recoveryUrl
      && now - readerRecovery.time < READER_RECOVERY_WINDOW_MS)) {
    readerRecovery.blocked = true;
    setReaderUnavailable(true);
    return;
  }
  readerRecovery = { url: recoveryUrl, time: now, blocked: false };
  $('reader-close').focus({ preventScroll: true });
  navigateReader(readerLastDocument);
  setReaderUnavailable(false);
  window.open(destination, '_blank', 'noopener');
}
function syncReaderHeader(doc) {
  const links = [...doc.querySelectorAll('.meta a[href]')];
  setReaderHeader({ getAttribute: () => doc.URL, dataset: {
    title: doc.querySelector('.doc-title')?.textContent || doc.title,
    path: encodeURIComponent(doc.documentElement.dataset.readerPath),
    obs: links.find(link => link.href.startsWith('obsidian:'))?.href || '#',
    url: links.find(link => /^https?:/i.test(link.href))?.href || '',
  } });
}
function bindReaderScrollAway(generation = readerLoadGeneration) {
  if (generation !== readerLoadGeneration) return;
  const previous = readerAway;
  readerAway = null;
  previous?.release();
  if ($('reader').style.display === 'none') return;      // about:blank,关掉那一下
  const frame = $('reader-frame');
  let doc;
  try { doc = frame.contentDocument; } catch {}
  if (typeof doc?.documentElement?.dataset?.readerPath !== 'string') {
    recoverReaderDocument(doc);
    return;
  }
  try {
    readerLastDocument = doc.URL;
    readerPendingDestination = null;
    setReaderUnavailable(false);
    syncReaderHeader(doc);
    readerAway = bindScrollAway(frame.contentWindow, ['reader-head'],
      { visible: frame, host: $('reader') });
  } catch { /* An external document cannot be measured from here. */ }
}

function closeReader() {
  const previous = readerAway;
  readerAway = null;
  try { previous?.release(); }
  finally {
    $('reader').style.display = 'none';
    $('reader-mask').style.display = 'none';
    resetReaderNavigation();
    $('reader-frame').src = 'about:blank';
  }
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
// 时间段弹层点外面收起(弹窗本体不关)
document.addEventListener('click', e => {
  const pop = $('pipe-range-pop');
  if (pop.style.display !== 'none' && !pop.contains(e.target) && !$('pipe-range-btn').contains(e.target)) {
    closePipeRange();
  }
});

let searchAbort = null;
let searchVersion = 0;
/** 这一屏搜索结果的原始命中,按卡片上的 `data-hit` 下标取。
 *  「收藏此段」要用它:卡片里那段 `.snippet` 是**命中那一篇正文的一截真原文**,
 *  收藏得记下它出自哪一篇(path/标题/链接)。和 DOM 一起整块换,所以下标不会错位
 *  (注册在 `kb/static/js/snip_cards.js`)。*/
let searchHits = [];
const SEARCH_WELCOME = $('results').innerHTML;
// 条目化衍生条目的标题本身就是「名字:摘要」,照渲染的话同一句话在标题正下方
// 又出现一遍 —— 一屏三条结果里有三条在复读自己。标题已经包含摘要时就不出这一行。
function dupSummary(h) {
  const s = (h.summary || '').trim();
  if (!s) return true;
  return (h.title || '').replace(/\s+/g, '').includes(s.replace(/\s+/g, ''));
}

async function doSearch() {
  const q = $('q').value.trim();
  if (searchAbort) searchAbort.abort();
  const version = ++searchVersion;
  if (!q && !tagFilters.length && !entityFilters.length && !$('f-source').value && !$('f-account').value) {
    $('results').innerHTML = SEARCH_WELCOME;
    $('results').setAttribute('aria-busy', 'false');
    $('search-submit').innerHTML = icon('search') + '搜索';
    $('q').focus(); return;
  }
  searchAbort = new AbortController();
  $('results').setAttribute('aria-busy', 'true');
  $('search-submit').textContent = '搜索中…';
  $('results').innerHTML = '<div class="empty"><span class="spin"></span>搜索中…</div>';
  const p = new URLSearchParams({ q: q || '*' });
  if ($('f-source').value) p.set('source', $('f-source').value);
  if ($('f-account').value) p.set('account', $('f-account').value);
  tagFilters.forEach(t => p.append('tag', t));
  entityFilters.forEach(e => p.append('entity', e));
  let res;
  try {
    const response = await fetch('/api/search?' + p, { signal: searchAbort.signal });
    if (!response.ok) throw new Error(await apiFail(response, '搜索失败'));
    res = await response.json();
    if (version !== searchVersion) return;
  } catch (e) {
    if (e.name === 'AbortError' || version !== searchVersion) return;
    $('results').innerHTML = `<div class="empty-state"><h3>搜索暂时不可用</h3><p>${esc(e.message)}</p><button onclick="doSearch()">重试搜索</button></div>`;
    return;
  } finally {
    if (version === searchVersion) {
      $('results').setAttribute('aria-busy', 'false');
      $('search-submit').innerHTML = icon('search') + '搜索';
    }
  }
  if (!res.hits.length) {
    $('results').innerHTML = '<div class="empty-state"><span class="empty-icon">' + icon('search') + '</span><h3>没有找到相关内容</h3><p>试试更短的关键词，或减少来源与主题限制。</p><button class="ghost" onclick="clearSearchFilters()">清空筛选条件</button></div>';
    return;
  }
  // 命中里的标题/账号/摘要/片段/实体都是被索引的外部内容,拼进 innerHTML 前一律转义
  searchHits = res.hits;
  $('results').innerHTML = `<p class="search-count">找到 ${res.hits.length} 条相关内容 · 点击标题阅读全文</p>` + res.hits.map((h, i) => `
    <div class="hit" data-hit="${i}">
      <h3>${docA(h, h.title)}</h3>
      <div class="meta">
        <span>${esc(h.account || '')}</span><span>${esc((h.published || '').slice(0, 10))}</span>
        <span class="badge">${esc(h.source || '')}</span>
        ${promoBadge(h)}
        ${h.matched_by.map(m => `<span class="badge ${m === 'vec' ? 'vec' : ''}">${m === 'vec' ? '语义' : '关键词'}</span>`).join('')}
        <a href="${esc(obsLink(h))}" style="color:var(--muted)" title="在 Obsidian 中打开">Obsidian</a>
        ${h.url ? `<a href="${esc(safeUrl(h.url))}" target="_blank" rel="noopener" style="color:var(--muted)">原文${arrow('ext')}</a>` : ''}
        <button class="favbtn ${favSet.has(h.path) ? 'on' : ''}" data-path="${encodeURIComponent(h.path)}"
              onclick="toggleFav(this)">${favSet.has(h.path) ? '★ 已收藏' : '☆ 收藏'}</button>
      </div>
      ${dupSummary(h) ? '' : `<div class="summary">${esc(h.summary)}</div>`}
      <div class="snippet">${esc((h.best_chunk || '').slice(0, 160))}${
        (h.best_chunk || '').length > 160 ? '<span class="snip-more">…</span>' : ''}</div>
      ${h.entities.length ? `<div class="ents">${h.entities.map(e =>
        `<button class="ent" data-ent="${esc(e)}">${esc(e)}</button>`).join('')}</div>` : ''}
    </div>`).join('');
}

function clearSearchFilters() {
  tagFilters = []; entityFilters = [];
  $('f-source').value = ''; $('f-account').value = '';
  renderChips(); doSearch();
}
