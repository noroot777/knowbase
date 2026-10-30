function favDate(value) { return String(value || '').slice(0, 10).replaceAll('-', '.'); }

function favSourceName(source) {
  return ({ 'dr-gzh': '公众号', 'dr-gzh-projects': '公众号条目', rss: 'RSS',
    video: '视频', 'dr-youtube-dig': '视频研究', social: '大V观点',
    'dr-video-projects': '视频条目', dr: '日报' })[source] || source || '其他来源';
}

// 「这条出自哪里」。有 account 就是它;日报正文、问答回答、没导出的订阅条目在
// `documents` 里压根没有一行(`d.source` 是 NULL),所以按它自己那一类说话 ——
// 不然卡片上写的是「其他来源」。**doc 不单独标**:它占绝大多数,标了就是每张卡上
// 一句什么也没区分的话(同「图鉴类型色别撞重点色」那条理由)。
function favWhere(item) {
  return item.account || (item.origin && item.origin !== 'doc'
    ? (SNIP_LABEL[item.origin] || item.origin) : '') || favSourceName(item.source);
}

function renderFavHeader(data) {
  const total = Object.values(data.counts).reduce((sum, count) => sum + count, 0);
  $('fav-total').textContent = `${total} 条`;
  for (const [status, label] of Object.entries(FAV_STATUSES)) {
    const button = $('fav-t-' + status);
    button.innerHTML = `${label}<span>${status === 'all' ? total : data.counts[status] || 0}</span>`;
    button.classList.toggle('on', status === favView);
    button.setAttribute('aria-pressed', String(status === favView));
  }
  const select = $('fav-source');
  const previous = select.value;
  const sources = [...new Set(favItems.map(item => item.source))].sort();
  select.innerHTML = '<option value="">全部来源</option>' + sources.map(source =>
    `<option value="${esc(source)}">${esc(favSourceName(source))}</option>`).join('');
  select.value = sources.includes(previous) ? previous : '';
}

function renderFavCollection() {
  const items = visibleFavs();
  pruneFavSelection(items);
  const counts = { all: favItems.length, quick: 0, deep: 0, quote: 0, fresh: 0 };
  favItems.forEach(item => counts[favCategory(item)]++);
  $('fav-filters').innerHTML = Object.entries(FAV_FILTERS).map(([key, [label, glyph]]) =>
    `<button class="${favFilter === key ? 'on' : ''}" onclick="filterFavs('${key}')" aria-pressed="${favFilter === key}">${icon(glyph)}${label}<span>${counts[key]}</span></button>`).join('');
  $('fav-result-count').textContent = `${items.length} 条${items.length !== favItems.length ? ` / 共 ${favItems.length} 条` : ''}`;
  $('fav-list').className = favLayout === 'list' ? 'fv-grid fv-compact' : 'fv-grid';
  const focus = document.activeElement?.closest('[data-fav-preview]')?.dataset.favPreview;
  $('fav-list').innerHTML = items.length ? items.map(favCardHtml).join('') : favEmptyHtml();
  if (focus) $('fav-list').querySelector(`[data-fav-preview="${focus}"]`)?.focus({ preventScroll: true });
  for (const layout of ['grid', 'list']) {
    $('fav-view-' + layout).classList.toggle('on', layout === favLayout);
    $('fav-view-' + layout).setAttribute('aria-pressed', String(layout === favLayout));
  }
  $('fav-batch').hidden = !favSelecting;
  $('fav-select-toggle').textContent = favSelecting ? '结束整理' : '批量整理';
  $('fav-select-toggle').setAttribute('aria-pressed', String(favSelecting));
  updateFavSelection();
}

function favCardHtml(item) {
  const category = favCategory(item);
  const action = item.action || {};
  const title = action.name || item.title;
  const description = favDescription(item);
  const [label, glyph] = FAV_FILTERS[category];
  return `<article class="fv-card fv-${category}${favActive === item.id ? ' is-active' : ''}${favSelected.has(item.id) ? ' is-selected' : ''}" data-fav-card="${item.id}">
    <div class="fv-card-top"><span class="fv-kind">${icon(glyph)}${label}</span>
      ${favSelecting ? `<input type="checkbox" aria-label="选择 ${esc(title)}" ${favSelected.has(item.id) ? 'checked' : ''} onchange="selectFav(${item.id},this.checked)">` : `<span class="fv-bookmark" aria-hidden="true">${icon('bookmark')}</span>`}</div>
    <button class="fv-card-preview" data-fav-preview="${item.id}" onclick="openFavDetail(${item.id})" aria-label="查看 ${esc(title)} 的详情" aria-pressed="${favActive === item.id}">
      <h3>${esc(title)}</h3><p class="fv-card-desc${item.quote ? ' is-quote' : ''}">${esc(description)}</p>
      ${action.name && action.name !== item.title ? `<span class="fv-article-title">${esc(item.title)}</span>` : ''}
    </button>
    ${item.note ? `<button class="fv-card-note" onclick="openFavDetail(${item.id})" aria-label="编辑 ${esc(title)} 的笔记">${icon('paper')}<span>${esc(item.note)}</span></button>` : ''}
    <div class="fv-card-meta"><span>${esc(favWhere(item))}</span><time datetime="${esc(item.created_at)}">${favDate(item.created_at).slice(5)}</time></div>
    <div class="fv-card-footer"><span class="fv-card-eta">${item.status !== 'inbox' ? `<span class="fv-state-${item.status}">${item.status === 'done' ? '✓ 已完成' : '已暂存'}</span>` : esc(action.eta || '')}</span>
      ${favDocA(item, `${esc(favOriginal(item).label)}${arrow('right')}`, 'fv-read-link', true)}</div>
  </article>`;
}

function favEmptyHtml() {
  const filtered = favFilter !== 'all' || $('fav-search').value.trim() || $('fav-source').value;
  if (filtered) return '<div class="fv-empty"><span>没有符合当前筛选的收藏。</span><button class="text-button" onclick="resetFavFilters()">清除筛选</button></div>';
  const text = {
    all: '还没有收藏，可在图鉴或阅读页添加收藏。',
    inbox: '没有待探索的收藏。',
    done: '没有已完成的收藏，可在收藏详情中标为完成。',
    archived: '没有暂存的收藏，暂存的内容可以随时放回待探索。',
  }[favView];
  return `<div class="fv-empty"><span>${text}</span><a href="#gallery" onclick="switchTab('gallery');return false">打开工具图鉴${arrow('right')}</a></div>`;
}

// 卡片/详情上那颗「打开」。**按有没有 path 分流,不按 origin 分**:有 path 的走
// 阅读页那条带 `#kbq=` 的链接,没有 path 的(日报正文、问答回答、没导出的订阅条目)
// 在 vault 里没有对应文件 —— 照旧拼 `/doc/` 得到的是 iframe 里一张 404,所以换成回
// 各自面板的回跳按钮。两条路都在 `fav.js`(`favDocAHtml` / `favJumpHtml` →
// `jumpFavOrigin`),**这儿不许再拼一遍锚点**:那一份要把 `u`/`e`/`v`/`m` 一起带过去,
// 少带一个数,回跳那一头就只知道「说不清」、弱档一律不采信(见
// `docs/decisions/fav-snippets.md`)。
function favDocA(item, label, cls = '', html = false) {
  return favDocAHtml(item, html ? label : esc(label), cls);
}

function openFavDetail(id) {
  favActive = id;
  renderFavCollection();
  renderFavInspector();
  const inspector = $('fav-inspector');
  inspector.querySelector('.fv-detail-title')?.focus({ preventScroll: true });
  if (matchMedia('(max-width: 760px)').matches) inspector.scrollIntoView({
    behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start',
  });
}

function closeFavDetail() {
  const id = favActive;
  favActive = null;
  renderFavCollection();
  renderFavInspector();
  const trigger = $('fav-list').querySelector(`[data-fav-preview="${id}"]`) || $('fav-search');
  trigger.focus({ preventScroll: !matchMedia('(max-width: 760px)').matches });
}

// 读正文时让出位置:头那一条(「收藏详情」+ 返回)是 sticky 的,往下读时收起来。
// **这一处每次重绘都是新 DOM**,所以绑定跟着渲染走,渲染前先把上一份解掉;
// 收起之后右上角那颗常驻返回按钮顶上。机制见 kb/static/js/scrollaway.js。
let favAway = null;
/** 这一处的滚动轴**跟着断点换**,所以 query 存在外面,断点一变就重绑(见下)。 */
const FAV_NARROW = matchMedia('(max-width: 760px)');

/**
 * 把收藏详情接上「读正文时让出位置」。
 *
 * **窄屏这块不自己滚**(`max-height:none; overflow:visible`,favorites.css),用户
 * 滚的是整页;还照桌面那条轴绑的话,它的 `scrollTop` 和可滚距离都是 0,一下都收
 * 不起来 —— 而那条 sticky 的头会一直占着屏幕上方一行。所以**轴要跟着当前布局
 * 选**,断点变了重绑一次。可见性参照一律是 `.fv-detail` 自己(window 永远「在」)。
 */
function bindFavScrollAway() {
  favAway?.release();
  favAway = null;
  const detail = $('fav-inspector').querySelector('.fv-detail');
  if (!detail) return;
  favAway = bindScrollAway(FAV_NARROW.matches ? window : detail,
    [detail.querySelector('.fv-aside-heading')],
    { visible: detail, host: $('fav-inspector') });
}
FAV_NARROW.addEventListener('change', () => { if (favAway) bindFavScrollAway(); });

function renderFavInspector() {
  const item = favItems.find(row => row.id === favActive);
  document.querySelector('.fv-workbench')?.classList.toggle('has-detail', !!item);
  favAway?.release();
  favAway = null;
  $('fav-inspector').innerHTML = item ? favDetailHtml(item) : favStartHtml();
  if (item) bindFavScrollAway();
}

function favDetailHtml(item) {
  const action = item.action || {};
  const originalUrl = safeUrl(item.url);
  const note = favDrafts.has(item.id) ? favDrafts.get(item.id) : item.note;
  const disabled = favBusy ? ' disabled' : '';
  const category = favCategory(item);
  const description = favDescription(item, true);
  return `<div class="fv-detail">
    <div class="fv-aside-heading scroll-away"><span>收藏详情</span><button class="fv-icon-button fv-detail-back" onclick="closeFavDetail()" aria-label="关闭收藏详情">${icon('close')}<span>返回收藏架</span></button></div>
    <button class="away-exit fv-detail-exit" onclick="closeFavDetail()" aria-label="关闭收藏详情">${icon('close')}</button>
    <div class="fv-detail-content"><span class="fv-detail-kind fv-${category}">${icon(FAV_FILTERS[category][1])}${FAV_FILTERS[category][0]}</span>
      <h2 class="fv-detail-title" tabindex="-1">${esc(action.name || item.title)}</h2>
      ${description !== item.quote ? `<p class="fv-detail-description">${esc(description)}</p>` : ''}
      ${item.quote ? `<blockquote>${esc(item.quote)}</blockquote>` : ''}
      <dl class="fv-detail-meta">${action.name && action.name !== item.title ? `<div><dt>原文</dt><dd>${esc(item.title)}</dd></div>` : ''}<div><dt>来源</dt><dd>${esc(favWhere(item))}</dd></div><div><dt>收藏于</dt><dd>${favDate(item.created_at)}</dd></div><div><dt>状态</dt><dd>${FAV_STATUSES[item.status]}</dd></div>${action.eta ? `<div><dt>预计用时</dt><dd>${esc(action.eta)}</dd></div>` : ''}</dl>
      <div class="fv-detail-links">${favDocA(item, `${esc(favOriginal(item).label)}${arrow('right')}`, 'fv-primary-link', true)}${originalUrl !== '#' ? `<a href="${esc(originalUrl)}" target="_blank" rel="noopener">原文${arrow('ext')}</a>` : ''}${favOriginal(item).indexed ? `<a href="${esc(obsLink(item))}">Obsidian${arrow('ext')}</a>` : ''}</div>
      ${action.cmd ? `<div class="fv-command"><div><span>${icon('code')}上手命令</span><button class="text-button" data-cmd="${esc(action.cmd)}" onclick="copyFavCmd(this)">复制</button></div><code>${esc(action.cmd)}</code><small>来自行动卡，请确认内容后再执行。</small></div>` : ''}
      ${!item.action && item.status === 'inbox' && !item.quote ? `<div class="fv-extract"><button class="text-button" data-fav-mutation onclick="extractFavAction(${item.id})"${disabled}>${icon('spark')}生成行动卡</button></div>` : ''}
      <section class="fv-note-editor"><div><label for="fav-note">我的随手记</label></div><textarea id="fav-note" rows="4" placeholder="想到的用法、想解决的问题，或试过后的心得…" oninput="draftFavNote(this.value)">${esc(note || '')}</textarea><div class="fv-note-footer"><span id="fav-note-state" role="status">${favDrafts.has(item.id) ? '有未保存的草稿' : ''}</span><button class="ghost" data-fav-mutation onclick="saveFavNote(${item.id})"${disabled}>保存笔记</button></div></section>
      <div class="fv-detail-actions">${item.status === 'inbox' ? `<button data-fav-mutation onclick="favMark(${item.id},'done')"${disabled}>✓ 标为完成</button><button class="ghost" data-fav-mutation onclick="favMark(${item.id},'archived')"${disabled}>先暂存</button>` : `<button data-fav-mutation onclick="favMark(${item.id},'inbox')"${disabled}>放回待探索${arrow('right')}</button>`}<button class="text-button fv-delete" data-fav-mutation onclick="favDel(${item.id})"${disabled}>删除收藏</button></div>
    </div></div>`;
}

function favRecommendations() {
  const candidates = favItems.filter(item => item.status === 'inbox' && favCategory(item) !== 'quote');
  const hero = favTri?.fresh ? favTri.data?.hero?.id : null;
  return candidates.sort((left, right) => Number(right.id === hero) - Number(left.id === hero)
    || Number(favCategory(right) === 'quick') - Number(favCategory(left) === 'quick'));
}

function swapHero() { favHeroIndex++; renderFavInspector(); }

function favStartHtml() {
  const pool = favRecommendations();
  const item = pool[favHeroIndex % (pool.length || 1)];
  const aiRecommended = item && favTri?.fresh && favTri.data?.hero?.id === item.id;
  const why = item ? (aiRecommended ? favTri.data.hero.why : '') || favDescription(item) : '';
  return `<div class="fv-start"><div class="fv-aside-heading"><span>${icon('spark')}灵感起步</span><span class="fv-tiny-dot" aria-hidden="true"></span></div>
    ${item ? `<div class="fv-recommendation"><div class="fv-rec-label"><span>${aiRecommended ? 'AI 推荐' : '从你的待探索中挑选'}</span>${item.action?.eta ? `<span>${esc(item.action.eta)}</span>` : ''}</div><h3>${esc(item.action?.name || item.title)}</h3><p>${esc(why)}</p><div class="fv-rec-actions"><button onclick="openFavDetail(${item.id})">就看这个${arrow('right')}</button>${pool.length > 1 ? '<button class="text-button" onclick="swapHero()">换一条</button>' : ''}</div></div>` : ''}
    ${favTriageHtml()}</div>`;
}

function favTriageHtml() {
  const triage = favTri;
  const inboxIds = new Set(favItems.filter(item => item.status === 'inbox').map(item => item.id));
  const duplicates = triage?.fresh ? (triage.data?.dup || [])
    .map(group => ({ ...group, ids: (group.ids || []).filter(id => inboxIds.has(id)) }))
    .filter(group => group.ids.length >= 2) : [];
  const cold = triage?.fresh ? (triage.data?.cold || []).filter(row => inboxIds.has(row.id)) : [];
  const stale = favItems.filter(item => favStaleDays > 0 && item.status === 'inbox' &&
    !cold.some(row => row.id === item.id) &&
    new Date(item.updated_at).getTime() < Date.now() - favStaleDays * 86400000).map(item => item.id);
  const status = triage?.running ? '正在后台整理，你可以继续浏览。'
    : triage?.error ? triage.error : !inboxIds.size ? '建议针对待探索收藏，可切换到「待探索」查看。'
    : triage?.data && !triage.fresh ? '收藏有变化，建议可重新生成。'
    : triage?.data ? '整理建议已就绪，不会自动移动收藏。' : '';
  return `<section class="fv-ai"><div class="fv-ai-heading"><h3>${icon('spark')}让 AI 搭把手</h3><button id="fav-triage-btn" class="text-button" onclick="runTriage()"${triage?.running ? ' disabled' : ''}>${triage?.running ? '<span class="spin"></span>整理中' : triage?.data ? '重新整理' : '生成建议'}</button></div><p role="status">${esc(status)}</p>
    ${duplicates.map(group => `<details class="fv-suggestion"><summary>${group.ids.length} 条收藏可能重复${arrow('caretDown')}</summary><p>${esc(group.verdict || '内容方向相近，先对比再决定留哪条。')}</p>${group.ids.map(id => { const row = favItems.find(item => item.id === id); return `<button class="text-button" onclick="openFavDetail(${id})">${esc(row.action?.name || row.title)}${id === group.keep ? ' · 建议保留' : ''}${arrow('right')}</button>`; }).join('')}${group.ids.includes(group.keep) ? `<button class="text-button" data-fav-mutation onclick="archiveFavDuplicates([${group.ids.join(',')}],${group.keep})"${favBusy ? ' disabled' : ''}>保留推荐，其余暂存${arrow('right')}</button>` : ''}</details>`).join('')}
    ${cold.length ? `<details class="fv-suggestion"><summary>${cold.length} 条收藏可以重新考虑${arrow('caretDown')}</summary><p>AI 的取舍建议，仅供参考。</p>${cold.map(suggestion => { const row = favItems.find(item => item.id === suggestion.id); return `<button class="text-button" onclick="openFavDetail(${row.id})">${esc(row.action?.name || row.title)}${arrow('right')}</button><p>${esc(suggestion.why || '可以暂存，等需要的时候再找回来。')}</p>`; }).join('')}</details>` : ''}
    ${stale.length ? `<details class="fv-suggestion"><summary>${stale.length} 条收藏很久没动过${arrow('caretDown')}</summary><p>超过 ${favStaleDays} 天未更新。不再需要的话可以暂存，之后随时找回。</p><button class="text-button" data-fav-mutation onclick="archiveFavSuggestions([${stale.join(',')}])"${favBusy ? ' disabled' : ''}>暂存这 ${stale.length} 条${arrow('right')}</button></details>` : ''}</section>`;
}

async function archiveFavSuggestions(ids) {
  if (!await confirmBox({ title: `暂存这 ${ids.length} 条收藏？`, body: `只整理超过 ${favStaleDays} 天未更新的待探索收藏，不会删除原文、摘录或笔记。之后可以撤销，或从「暂存」里找回。`, ok: '移至暂存' })) return;
  await favMarkMany(ids, 'archived');
}

async function archiveFavDuplicates(ids, keep) {
  if (!ids.includes(keep)) return;
  if (!await confirmBox({ title: '保留推荐，其余暂存？', body: `保留标注「建议保留」的那条收藏，其余 ${ids.length - 1} 条移至暂存。不会删除文章或笔记，操作后可以撤销。`, ok: '确认整理' })) return;
  await favMarkMany(ids.filter(id => id !== keep), 'archived');
}
