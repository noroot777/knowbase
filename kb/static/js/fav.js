// ── 收藏(备忘待试清单:整篇 + 片段,inbox/done/archived 流转) ──────────────
let favSet = new Set();     // 有整篇收藏条目的 path(不分状态),搜索卡片/reader 的 ★ 用
let favView = 'inbox';      // 收藏面板当前视图
let favListMode = false;    // inbox 视图:false=智能分诊,true=完整列表
let favToolsInited = false; // 模型下拉只初始化一次
let triagePoll = null;      // 分诊轮询定时器
let lastFavD = null, lastFavTri = null;  // swapHero 就地重渲染用
let heroPool = [], heroIdx = 0;

async function loadFavSet() {
  try {
    const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
    favSet = new Set(d.doc_paths);
  } catch {}
}

function setFavBtn(el, on) {
  el.classList.toggle('on', on);
  el.textContent = on ? '★ 已收藏' : '☆ 收藏';
}

async function toggleFav(el) {
  const path = decodeURIComponent(el.dataset.path);
  try {
    const r = await fetch('/api/favorites/toggle', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    if (!r.ok) throw new Error(await apiFail(r));
    const d = await r.json();
    d.favorited ? favSet.add(path) : favSet.delete(path);
    document.querySelectorAll(`.favbtn[data-path="${CSS.escape(el.dataset.path)}"]`)
      .forEach(b => setFavBtn(b, d.favorited));
    // reader 里操作时收藏面板可能正开着,同步一下
    if ($('favpanel').style.display !== 'none') loadFavPanel();
  } catch (e) {
    toast('收藏操作失败', { detail: e.message });
  }
}

function switchFavView(v) { favView = v; loadFavPanel(); }
function toggleFavMode() { favListMode = !favListMode; loadFavPanel(); }

// 模型下拉(分诊/行动卡用):候选来自 /api/models,当前值来自收藏设置
async function initFavTools() {
  if (favToolsInited) return;
  favToolsInited = true;
  try {
    const [ms, st] = await Promise.all([
      fetch('/api/models').then(r => r.json()),
      fetch('/api/favorites/settings').then(r => r.json()),
    ]);
    const sel = $('fav-model');
    // option 的 value 是带家的 "provider:model_id"(同名 id 两家都有时才分得开),
    // 显示只留模型名。这个下拉和设置面板的「收藏 · 分诊与行动卡」是同一处配置。
    sel.innerHTML = ms.models.map(m =>
      `<option value="${esc(m.key)}"${m.enabled ? '' : ' disabled'}>${esc(m.label)}</option>`).join('');
    if (![...sel.options].some(o => o.value === st.model)) {
      sel.insertAdjacentHTML('afterbegin',
        `<option value="${esc(st.model)}">${esc(modelLabel(st.model))}</option>`);
    }
    sel.value = st.model;
  } catch {}
}

async function saveFavModel() {
  try {
    await fetch('/api/favorites/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: $('fav-model').value }),
    });
  } catch (e) { toast('保存模型失败', { detail: e.message }); }
}

function renderFavEmpty() {
  const msg = {
    inbox: '待试清单是空的,轻松 — 搜索结果点 ☆ 收藏整篇,阅读时选中文字可收藏片段',
    done: '还没有标过「试过了」的收藏',
    archived: '没有沉底的收藏',
  }[favView];
  $('fav-list').innerHTML = `<div class="empty">${msg}</div>`;
}

// docA 的收藏版:片段自动带 #kbq= 定位锚。label 当纯文本(与 docA 一致),
// 要塞 HTML 片段用 favDocAHtml
function favDocA(f, label, cls = '') {
  return favDocAHtml(f, esc(label), cls);
}
function favDocAHtml(f, labelHtml, cls = '') {
  const hash = f.quote ? '#kbq=' + encodeURIComponent(
    JSON.stringify({ q: f.quote, p: f.prefix || '', s: f.suffix || '' })) : '';
  return docAHtml(f, labelHtml, cls, hash);
}

function favOps(h) {
  const b = (label, fn) => `<button class="ghost" onclick="${fn}">${label}</button>`;
  if (h.status === 'inbox')
    return `<span class="fav-ops">` +
           b('✓ 试过了', `favMark(${h.id},'done')`) +
           b('沉底', `favMark(${h.id},'archived')`) +
           b('删除', `favDel(${h.id})`) + `</span>`;
  return `<span class="fav-ops">` +
         b('↩ 待试', `favMark(${h.id},'inbox')`) + b('删除', `favDel(${h.id})`) + `</span>`;
}

// ── 完整列表模式:一篇文章一张卡,整篇是文章行、片段是子行 ──
function favGroupCard(g) {
  const h = g.doc;
  const meta =
    `<span class="fav-row-meta">${esc(h.account || '')} · ${(h.published || '').slice(5, 10)}` +
    ` · 收藏于 ${(g.latest || '').slice(5, 10)}` +
    ` · <a href="${esc(obsLink(h))}" title="在 Obsidian 中打开">Obsidian</a>` +
    (h.url ? ` · <a href="${esc(safeUrl(h.url))}" target="_blank" rel="noopener">原文${arrow('ext')}</a>` : '') + `</span>`;
  const snipRows = g.snips.map(s => `
      <div class="fav-row fav-snip-row" title="${esc(s.quote.slice(0, 200))}">
        ${favDocAHtml(s, `<span class="fav-snip-q">❝ ${esc(s.quote)}</span>`, 'fav-snip-link')}
        ${favOps(s)}
      </div>
      ${s.note ? `<div class="fav-note">✎ ${esc(s.note)}</div>` : ''}`).join('');
  return `
    <div class="hit fav-group" data-fav-card="1">
      <div class="fav-row">
        <span class="fav-title">${docA(h, h.title)}</span>
        ${meta}
        ${g.whole ? favOps(g.whole) : ''}
      </div>
      ${g.whole && h.summary ? `<div class="fav-sum">${esc(h.summary)}</div>` : ''}
      ${snipRows}
    </div>`;
}

function favListHtml(d) {
  // 按文章聚合;favorites 已按收藏时间倒序,首次出现定组序
  const groups = new Map();
  for (const f of d.favorites) {
    if (!groups.has(f.path)) groups.set(f.path, { doc: f, latest: f.created_at, whole: null, snips: [] });
    const g = groups.get(f.path);
    f.quote ? g.snips.push(f) : (g.whole = f);
  }
  return [...groups.values()].map(favGroupCard).join('');
}

// ── 分诊模式:置顶推荐 + 快试/整块/重复/凉了/摘抄 ──
function favCmdHtml(cmd) {
  return `<code class="fav-cmd" data-cmd="${esc(cmd)}" onclick="copyFavCmd(this)" title="点击复制">${esc(cmd)}</code>`;
}
function copyFavCmd(el) {
  navigator.clipboard.writeText(el.dataset.cmd).then(() => {
    const t = el.textContent;
    el.textContent = '✓ 已复制';
    setTimeout(() => { el.textContent = t; }, 1100);
  }).catch(() => {});
}
function swapHero() {
  heroIdx++;
  if (lastFavD && lastFavTri) renderTriage(lastFavD, lastFavTri);
}

function favHeroHtml(f, why) {
  const a = f.action || {};
  return `<div class="fav-hero">
    <div class="k">⚡ 现在有一会儿?先试这个</div>
    <b>${favDocA(f, a.name || f.title)}</b>
    <div class="why">${esc(why || a.what || f.summary || '')}</div>
    <div class="cmdline">
      ${a.cmd ? favCmdHtml(a.cmd) : ''}
      <button class="ghost" onclick="favMark(${f.id},'done')">✓ 试过了</button>
      ${heroPool.length > 1 ? `<button class="ghost" onclick="swapHero()">换一个</button>` : ''}
    </div></div>`;
}

function favAcardHtml(f) {
  const a = f.action || {};
  const etaLong = /小时|半天|天|周|研究|阅读/.test(a.eta || '');
  return `<div class="fav-acard">
    <div class="top"><span class="name">${favDocA(f, a.name || f.title)}</span>
      ${a.eta ? `<span class="eta${etaLong ? ' long' : ''}">${esc(a.eta)}</span>` : ''}</div>
    <div class="what">${esc(a.what || f.summary || '')}</div>
    ${a.cmd ? `<div class="cmdline">${favCmdHtml(a.cmd)}</div>` : ''}
    <div class="src">来源:${favDocA(f, f.title)} · ${esc(f.account || '')} · 收藏于 ${(f.created_at || '').slice(5, 10)}</div>
    <div class="ops"><button class="ghost" onclick="favMark(${f.id},'done')">✓ 试过了</button>
      <button class="ghost" onclick="favMark(${f.id},'archived')">沉底</button></div>
  </div>`;
}

function favDupHtml(g, byId) {
  const opts = g.ids.map(id => {
    const f = byId.get(id);
    const win = id === g.keep;
    const others = g.ids.filter(i => i !== id);
    return `<div class="opt${win ? ' win' : ''}">
      <b>${favDocA(f, (f.action && f.action.name) || f.title)}</b>
      <div class="why">${esc(f.account || '')} · ${(f.published || '').slice(5, 10)}</div>
      ${win ? `<button class="ghost" onclick="favMarkMany([${others}],'archived')">留这个,其余沉底</button>`
            : `<button class="ghost" onclick="favMark(${id},'archived')">沉底</button>`}
    </div>`;
  }).join('');
  return `<div class="fav-dup">
    <div class="verdict">${esc(g.verdict || '方向重合,试一个就够')}</div>
    <div class="pair">${opts}</div></div>`;
}

function favColdRow(f, why) {
  return `<div class="fav-rowline"><span class="t">${favDocA(f, f.title)}</span>
    <span class="why">${esc(why || '')}</span>
    <span class="rops"><button class="ghost" onclick="favMark(${f.id},'archived')">沉底</button></span></div>`;
}
function favQuoteRow(f) {
  return `<div class="fav-rowline q" title="${esc((f.quote || '').slice(0, 200))}">
    <span class="t">${favDocA(f, '❝ ' + (f.quote || ''))}</span>
    <span class="why">${esc((f.title || '').slice(0, 18))} · ${(f.created_at || '').slice(5, 10)}</span>
    <span class="rops"><button class="ghost" onclick="favDel(${f.id})">删除</button></span></div>`;
}
function favFreshRow(f) {
  return `<div class="fav-rowline"><span class="t">${favDocA(f, f.quote ? '❝ ' + f.quote : f.title)}</span>
    <span class="why">收藏于 ${(f.created_at || '').slice(5, 10)}</span>
    <span class="rops"><button class="ghost" onclick="favMark(${f.id},'done')">✓ 试过了</button>
      <button class="ghost" onclick="favMark(${f.id},'archived')">沉底</button></span></div>`;
}

function favDrawer(head, n, hint, inner, act = '') {
  if (!inner) return '';
  return `<div class="fav-drawer"><div class="fav-drawer-head"><b>${head}</b>
    <span class="n">${n} 个</span>${hint ? `<span class="hint">${hint}</span>` : ''}${act}</div>${inner}</div>`;
}

function renderTriage(d, tri) {
  lastFavD = d; lastFavTri = tri;
  const byId = new Map(d.favorites.map(f => [f.id, f]));
  const t = (tri && tri.data) || null;
  if (tri && tri.running) {
    $('fav-list').innerHTML = '<div class="empty"><span class="spin"></span>LLM 分诊中,通常十几秒…</div>';
    pollTriage();
    return;
  }
  if (!t) {
    $('fav-list').innerHTML = `
      <div class="fav-triage-note">还没跑过智能分诊 — 让 LLM 把待试清单分成「快试 / 整块时间 / 重复 / 凉了 / 摘抄」
        <button onclick="runTriage()">✨ 开始分诊</button></div>` + favListHtml(d);
    return;
  }
  const has = id => byId.has(id);
  const quick = (t.quick || []).map(x => x.id).filter(has);
  const deep = (t.deep || []).map(x => x.id).filter(has);
  const dups = (t.dup || []).map(g => ({ ...g, ids: (g.ids || []).filter(has) }))
    .filter(g => g.ids.length >= 2);
  const colds = (t.cold || []).filter(x => has(x.id));
  const quotes = (t.quotes || []).filter(has);
  const covered = new Set([...quick, ...deep, ...colds.map(x => x.id), ...quotes,
    ...dups.flatMap(g => g.ids)]);
  if (t.hero && has(t.hero.id)) covered.add(t.hero.id);
  const freshIds = [...byId.keys()].filter(id => !covered.has(id));

  heroPool = [...new Set([...(t.hero && has(t.hero.id) ? [t.hero.id] : []), ...quick, ...deep])];
  const heroId = heroPool.length ? heroPool[heroIdx % heroPool.length] : null;
  const inCards = ids => ids.filter(id => id !== heroId);

  let html = '';
  if (tri.error) {
    html += `<div class="fav-triage-note">上次分诊失败:${esc(tri.error)}
      <button onclick="runTriage()">重试</button></div>`;
  } else if (!tri.fresh) {
    html += `<div class="fav-triage-note">收藏有变动,以下结果生成于 ${esc((tri.generated_at || '').slice(5, 16))}(${esc(modelLabel(tri.model))}),可能过时
      <button onclick="runTriage()">✨ 重新分诊</button></div>`;
  }
  if (heroId) html += favHeroHtml(byId.get(heroId),
    t.hero && t.hero.id === heroId ? t.hero.why : '');
  if (freshIds.length) html += favDrawer('🆕 新收藏,还没分诊', freshIds.length, '',
    freshIds.map(id => favFreshRow(byId.get(id))).join(''));
  const quickCards = inCards(quick);
  if (quickCards.length) html += favDrawer('⚡ 十分钟能试', quickCards.length, '一条命令的事',
    `<div class="fav-cards">${quickCards.map(id => favAcardHtml(byId.get(id))).join('')}</div>`);
  const deepCards = inCards(deep);
  if (deepCards.length) html += favDrawer('🕐 要留整块时间', deepCards.length, '周末或空闲晚上',
    `<div class="fav-cards">${deepCards.map(id => favAcardHtml(byId.get(id))).join('')}</div>`);
  if (dups.length) html += favDrawer('♻ 疑似重复,试一个就够',
    dups.reduce((s, g) => s + g.ids.length, 0), '',
    dups.map(g => favDupHtml(g, byId)).join(''));
  if (colds.length) html += favDrawer('🍂 可能凉了', colds.length, '',
    colds.map(x => favColdRow(byId.get(x.id), x.why)).join(''),
    `<span class="act" onclick="favMarkMany([${colds.map(x => x.id)}],'archived')">全部沉底</span>`);
  if (quotes.length) html += favDrawer('📌 摘抄', quotes.length, '不用试,留着引用,不算欠债',
    quotes.map(id => favQuoteRow(byId.get(id))).join(''));
  $('fav-list').innerHTML = html || '<div class="empty">分诊结果为空</div>';
}

async function runTriage() {
  try { await fetch('/api/favorites/triage/run', { method: 'POST' }); } catch {}
  $('fav-list').innerHTML = '<div class="empty"><span class="spin"></span>LLM 分诊中,通常十几秒…</div>';
  pollTriage();
}

function pollTriage() {
  const btn = $('fav-triage-btn');
  btn.disabled = true;
  clearInterval(triagePoll);
  triagePoll = setInterval(async () => {
    const s = await fetch('/api/favorites/triage').then(r => r.json()).catch(() => null);
    if (s && !s.running) {
      clearInterval(triagePoll); triagePoll = null;
      btn.disabled = false;
      loadFavPanel();
    }
  }, 2000);
}

async function loadFavPanel() {
  clearInterval(triagePoll); triagePoll = null;
  const triageMode = favView === 'inbox' && !favListMode;
  $('fav-tools').style.display = favView === 'inbox' ? '' : 'none';
  $('fav-mode-toggle').innerHTML = favListMode ? `智能分诊${arrow('caretUp')}` : `以列表查看${arrow('caretDown')}`;
  $('fav-triage-btn').style.display = triageMode ? '' : 'none';
  $('fav-list').innerHTML = '<div class="empty"><span class="spin"></span>加载中…</div>';
  if (favView === 'inbox') initFavTools();

  let d = null, tri = null;
  try {
    [d, tri] = await Promise.all([
      fetch('/api/favorites?status=' + favView).then(r => r.json()),
      triageMode ? fetch('/api/favorites/triage').then(r => r.json()).catch(() => null)
                 : Promise.resolve(null),
    ]);
  } catch {}
  if (!d) { $('fav-list').innerHTML = '<div class="empty">收藏加载失败</div>'; return; }
  favSet = new Set(d.doc_paths);
  const labels = { inbox: '待试', done: '已处理', archived: '沉底' };
  for (const v of Object.keys(labels)) {
    const btn = $('fav-t-' + v);
    btn.textContent = labels[v] + (d.counts[v] ? ` ${d.counts[v]}` : '');
    btn.classList.toggle('on', v === favView);
  }
  const bar = $('fav-stale');
  // 陈旧提示只在列表模式给(分诊模式里「凉了」抽屉承担同一职责)
  if (favView === 'inbox' && !triageMode && d.stale > 0) {
    bar.innerHTML = `⏳ ${d.stale} 条待试超过 ${d.stale_days} 天没动过,多半不会再试了` +
      `<button onclick="archiveStale()">一键沉底</button>`;
    bar.style.display = '';
  } else {
    bar.style.display = 'none';
  }
  if (!d.favorites.length) { renderFavEmpty(); return; }
  if (triageMode) renderTriage(d, tri);
  else $('fav-list').innerHTML = favListHtml(d);
}

async function favMark(id, status) {
  try {
    const r = await fetch('/api/favorites/' + id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    if (!r.ok) throw new Error(await apiFail(r));
    loadFavPanel();
  } catch (e) { toast('标记失败', { detail: e.message }); }
}

async function favMarkMany(ids, status) {
  try {
    await Promise.all(ids.map(id => fetch('/api/favorites/' + id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    })));
    loadFavPanel();
  } catch (e) { toast('批量标记失败', { detail: e.message }); }
}

async function favDel(id) {
  try {
    const r = await fetch('/api/favorites/' + id, { method: 'DELETE' });
    if (!r.ok && r.status !== 204) throw new Error(await apiFail(r));
    loadFavPanel();
  } catch (e) { toast('删除失败', { detail: e.message }); }
}

async function archiveStale() {
  try {
    const r = await fetch('/api/favorites/archive-stale', { method: 'POST' });
    if (!r.ok) throw new Error(await apiFail(r));
    loadFavPanel();
  } catch (e) { toast('沉底失败', { detail: e.message }); }
}
