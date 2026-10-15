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
    inbox: '待试清单是空的,轻松 — 搜索结果点 ☆ 收藏整篇;日报、问答、订阅、图鉴和阅读页里选中文字就能收藏片段',
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
// **按「有没有 path」分流,不按 origin 分。** 有 path 的照旧走阅读页(那套
// quote+前后文定位在 kb/web/pages.py 里,最可靠);没有 path 的(日报正文、问答
// 回答、没导出的订阅条目)在 vault 里没有对应文件 —— 照旧拼 `/doc/` 会得到一个
// 空路径,点下去是 iframe 里一张 404,所以换成回各自面板的按钮。
function favDocAHtml(f, labelHtml, cls = '') {
  if (!f.path) return favJumpHtml(f, labelHtml, cls);
  // **`u` 要一起带过去**:阅读页那一头的裸引文退路按它决定能不能采信(采集那一刻
  // 只有一处 ≠ 现在只剩一处,见 `kb/web/pages.py` 的 `seek`)。老收藏没有这个数
  // (NULL),那就一个字都不写 —— 那一头把"没有"当"不知道",不给用那一档。
  const hash = f.quote ? '#kbq=' + encodeURIComponent(
    JSON.stringify({ q: f.quote, p: f.prefix || '', s: f.suffix || '',
                     ...(f.quote_unique === 1 || f.quote_unique === 0
                       ? { u: f.quote_unique } : {}) })) : '';
  return docAHtml(f, labelHtml, cls, hash);
}

// 回跳目标整个编进 data 属性,不去全局列表缓存里按 id 捞:这几颗按钮的寿命
// 比那份缓存长(分诊结果渲染完,下一次 loadFavPanel 之前它一直在屏幕上)。
function favJumpHtml(f, labelHtml, cls = '') {
  const payload = encodeURIComponent(JSON.stringify({
    origin: f.origin || '', ref: f.origin_ref || '', url: f.url || '',
    q: f.quote || '', p: f.prefix || '', s: f.suffix || '',
    // 同 `favDocAHtml` 的 hash:裸引文那一档要靠它(见 snip_text.js 的 `snipSeek`)
    ...(f.quote_unique === 1 || f.quote_unique === 0 ? { u: f.quote_unique } : {}),
  }));
  const label = SNIP_LABEL[f.origin] || '这段话';
  return `<button class="fav-jump${cls ? ' ' + cls : ''}" data-jump="${payload}"` +
    ` onclick="jumpFavOrigin(this)" title="回到${esc(label)}里这一段">${labelHtml}</button>`;
}

// 回跳:把目标存成一个"待定位",再切面板 —— 面板自己加载完会消费它。
// **不在这里 await 面板的加载**:switchTab 会顺手起一轮 loadDailyPanel /
// initChat,那一轮自己也要决定"打开哪一份",两边各发一次请求就是先发后到的
// 旧回包盖掉新的(日报面板的 dailyReadSeq 为同一类竞态而存在)。把目标交给它、
// 由它一处决定,就没有两条路要对齐。
function jumpFavOrigin(el) {
  let j;
  try { j = JSON.parse(decodeURIComponent(el.dataset.jump)); } catch { return; }
  // `u` 原样传下去(没有就不传):面板那几头拿它去 `snipSeek`,少这一个字段的话
  // 连「采集时本来就唯一」的那些收藏也退不了裸引文那一档
  const spec = { q: j.q, p: j.p, s: j.s,
                 ...(j.u === 1 || j.u === 0 ? { u: j.u } : {}) };
  if (j.origin === 'daily') {
    if (!j.ref) return toast('这条收藏没有记下是哪一天的日报');
    dailyPendingSnip = { ref: j.ref, spec };
    switchTab('daily');
    return;
  }
  if (j.origin === 'chat') {
    const cid = Number(j.ref);
    if (!cid) return toast('这条收藏没有记下是哪一个对话');
    chatPendingSnip = { ref: cid, spec };
    switchTab('ask');
    openConv(cid, true);
    return;
  }
  if (j.origin === 'feed') {
    // 订阅片段落到这一类,说明屏幕上那份正文在 vault 里没有同一份(外文原文,
    // 或者还没导出)。**站外原文才是那段话的出处**,优先开它;没有链接时退回
    // 它所在的订阅来源,并说清为什么没直接定位 —— 那条内容可能早翻过页了。
    // **最后这一道导航也过 `safeUrl`。** 这个字段是内容自带的(RSS 的 link),
    // 后端只管长度;同一份数据渲染成链接时各处都过 `safeUrl`(本文件下面那条
    // 「原文」、`source_reader.js` 的条目页脚),只有这一句直接把它交给
    // `window.open` —— 口径两样,那就迟早有一天不一样。认不出协议时退回下面
    // 「没有链接」那条路:切回它所在的来源,并说清为什么没直接定位。
    const url = typeof safeUrl === 'function' ? safeUrl(j.url) : j.url;
    if (j.url && url !== '#') { window.open(url, '_blank', 'noopener'); return; }
    // 来源目录里认得的 id(rss/wechat/social…)`switchTab` 自己会落到订阅面板的
    // 对应视图。**认不得的一律退回 'subs'**:`switchTab` 对不认识的名字是直接
    // return,页面一动不动,而下面那句提示写着"已切到它所在的来源" —— 一句假话。
    const kind = String(j.ref || '').split(':')[0];
    const known = typeof subsCatalog !== 'undefined'
      && subsCatalog.some(m => m.id === kind);
    switchTab(known ? kind : 'subs');
    toast('这条订阅内容没有站外链接', { type: 'info',
      detail: '已切到它所在的来源,可用搜索框找这段话' });
    return;
  }
  toast('这条收藏不知道该回到哪里', { detail: '来源:' + (j.origin || '(空)') });
}

/** 面板加载完自己调一次:定位成功就清掉待定位,失败如实说。
 *  `pending.ref` 对不上的不要动 —— 那是另一次回跳的目标。 */
function consumeSnipJump(pending, ref, el, skip) {
  if (!pending || String(pending.ref) !== String(ref)) return pending;
  // `skip` 要和采集那一头传的是同一条(问答那边是 `CHAT_SNIP_SKIP`):一头跳过
  // 界面文字、一头不跳,采到的"唯一"在这一头就可能不唯一了,见 snipTextIndex。
  if (!locateSnip(el, pending.spec, skip)) {
    toast('没找到这段收藏', { type: 'info',
      detail: '这段正文可能已经变过;收藏里那段原文还在' });
  }
  return null;
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
// 来源角标:只给**不是知识库文章**的那几类。doc 不标 —— 它占绝大多数,标了
// 就是每张卡上一枚什么也没区分的徽章(同「图鉴类型色别撞重点色」那条理由)。
function favOriginBadge(f) {
  return f.origin && f.origin !== 'doc'
    ? `<span class="badge fav-origin">${esc(SNIP_LABEL[f.origin] || f.origin)}</span>` : '';
}

function favGroupCard(g) {
  const h = g.doc;
  // Obsidian 链接只在有 vault 文档时给:没有 path 时 obsLink 会拼出一个
  // `&file=` 空着的地址,点下去 Obsidian 打开的是"新建笔记"。
  const meta =
    `<span class="fav-row-meta">${esc(h.account || '')}${h.account ? ' · ' : ''}` +
    `${(h.published || '').slice(5, 10)}${h.published ? ' · ' : ''}` +
    `收藏于 ${(g.latest || '').slice(5, 10)}` +
    (h.path ? ` · <a href="${esc(obsLink(h))}" title="在 Obsidian 中打开">Obsidian</a>` : '') +
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
        <span class="fav-title">${h.path ? docA(h, h.title) : favJumpHtml(h, esc(h.title))}</span>
        ${favOriginBadge(h)}
        ${meta}
        ${g.whole ? favOps(g.whole) : ''}
      </div>
      ${g.whole && h.summary ? `<div class="fav-sum">${esc(h.summary)}</div>` : ''}
      ${snipRows}
    </div>`;
}

// 一张卡片 = 一处出处。有 path 的按 path 聚,**没有 path 的按 origin+ref 聚** ——
// 拿空 path 当键的话,日报片段、问答片段、订阅片段会全挤进同一张卡,卡上那个
// 标题还是其中第一条的(同一天的两条日报摘录归一张卡是对的,一条日报摘录和
// 一句模型回答归一张卡不是)。
function favGroupKey(f) {
  return f.path || (f.origin || '') + '\u0000' + (f.origin_ref || '');
}

function favListHtml(d) {
  // 按出处聚合;favorites 已按收藏时间倒序,首次出现定组序
  const groups = new Map();
  for (const f of d.favorites) {
    const key = favGroupKey(f);
    if (!groups.has(key)) groups.set(key, { doc: f, latest: f.created_at, whole: null, snips: [] });
    const g = groups.get(key);
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
    <span class="why">${favOriginBadge(f)}${esc((f.title || '').slice(0, 18))} · ${(f.created_at || '').slice(5, 10)}</span>
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
