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
  $('chips').innerHTML =
    tagFilters.map(t => `<span class="chip">${t}<b onclick="rmTopic('${t}')">×</b></span>`).join('') +
    entityFilters.map(e => `<span class="chip">${e}<b onclick="rmEntity('${e}')">×</b></span>`).join('');
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
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
// 页内阅读链接:点击滑出面板,href 保留 /doc 路径,中键/Cmd 点仍可新标签打开
// hash 传 #kbq=…(片段定位锚点),doc 页加载后据此高亮滚动
function docA(h, label, cls = '', hash = '') {
  return `<a ${cls ? `class="${cls}"` : ''} href="${docLink(h)}${hash}" data-doc="1"` +
    ` data-path="${encodeURIComponent(h.path)}"` +
    ` data-title="${esc(h.title)}" data-url="${esc(h.url || '')}" data-obs="${esc(obsLink(h))}"` +
    ` title="${esc(h.title)}${h.account ? ' — ' + esc(h.account) : ''}">${label}</a>`;
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
  $('results').innerHTML = res.hits.map(h => `
    <div class="hit">
      <h3>${docA(h, h.title)}</h3>
      <div class="meta">
        <span>${h.account}</span><span>${(h.published || '').slice(0, 10)}</span>
        <span class="badge">${h.source}</span>
        ${h.matched_by.map(m => `<span class="badge ${m === 'vec' ? 'vec' : ''}">${m === 'vec' ? '语义' : '关键词'}</span>`).join('')}
        <a href="${obsLink(h)}" style="color:var(--muted)" title="在 Obsidian 中打开">Obsidian</a>
        ${h.url ? `<a href="${h.url}" target="_blank" style="color:var(--muted)">原文↗</a>` : ''}
        <span class="favbtn ${favSet.has(h.path) ? 'on' : ''}" data-path="${encodeURIComponent(h.path)}"
              onclick="toggleFav(this)">${favSet.has(h.path) ? '★ 已收藏' : '☆ 收藏'}</span>
      </div>
      ${h.summary ? `<div class="summary">${h.summary}</div>` : ''}
      <div class="snippet">${h.best_chunk.slice(0, 160)}…</div>
      ${h.entities.length ? `<div class="ents">${h.entities.map(e =>
        `<span class="ent" onclick="addEntity('${e.replace(/'/g, "\\'")}')">${e}</span>`).join('')}</div>` : ''}
    </div>`).join('');
}
