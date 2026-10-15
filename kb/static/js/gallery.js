// ── 图鉴视图(默认视图):工具/项目条目的图鉴式浏览 + 详情抽屉 ────────────────
// 数据:进页面 fetch /api/projects 一次,之后全部内存过滤/排序。
// 渲染:48 张/批增量出卡，自动加载与显式加载按钮并存。

const KIND_LABEL = { project: '开源项目', product: '产品', model: '模型',
                     research: '研究', org: '公司', other: '其他' };
const KIND_ORDER = ['project', 'product', 'model', 'research', 'org', 'other'];
const G_BATCH = 48;
const G_SRC = { gzh: 'dr-gzh-projects', video: 'dr-video-projects' };

let gData = null;          // /api/projects 原始响应
let gFiltered = [];        // 当前筛选结果
let gRendered = 0;         // 已渲染张数
const gState = { q: '', kind: '', topic: '', src: '', sort: 'recent' };
let gTopicsExpanded = false;

function kindCls(k) { return KIND_ORDER.includes(k) ? 'k-' + k : 'k-other'; }
function kindLabel(k) { return KIND_LABEL[k] || k || '其他'; }

// ── 正字计数(签名元素):n 次提及 = 逐笔画 n 笔,每 5 笔一个正 ──
// 最多画 3 个正,超出后 3 个全画满并追加 ×N。出现时逐笔 draw-in,
// reduced-motion 下由 CSS 直接置 stroke-dashoffset:0(见 base.css/gallery.css)。
//
// 没画到的那几笔要留一道浅色底(.ghost),不能只画实笔:3 次提及那种半个正字
// 单看就是个「下」,读者看到的是一个错字而不是一个数到一半的计数。补上底之后
// 同一个字形立刻变成「5 格里填了 3 格」,而且每个正字宽度一致、能对齐。
const TALLY_STROKES = [
  [2.5, 4, 17.5, 4],      // s1 上横
  [10, 4, 10, 16.5],      // s2 中竖
  [10, 10, 16.5, 10],     // s3 右中横
  [4.5, 8.5, 4.5, 16.5],  // s4 左短竖
  [2.5, 17, 17.5, 17],    // s5 底横
];

function tallySvg(n, size = 15) {
  if (!n || n < 1) return '';
  const full = Math.floor(n / 5), rest = n % 5;
  const chars = Math.min(full + (rest ? 1 : 0), 3);
  const overflow = n > 15;
  let html = '', seq = 0;
  for (let c = 0; c < chars; c++) {
    const strokes = (overflow || c < full) ? 5 : rest;
    let lines = '';
    // 先铺满 5 笔浅色底,实笔再盖在上面 —— 顺序不能反,否则底会压住实笔
    for (let i = strokes; i < 5; i++) {
      const [x1, y1, x2, y2] = TALLY_STROKES[i];
      lines += `<line class="ghost" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
    }
    for (let i = 0; i < strokes; i++) {
      const [x1, y1, x2, y2] = TALLY_STROKES[i];
      const len = Math.hypot(x2 - x1, y2 - y1).toFixed(1);
      lines += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"` +
        ` stroke-dasharray="${len}" stroke-dashoffset="${len}" style="animation-delay:${seq * 70}ms"/>`;
      seq++;
    }
    html += `<svg class="tally anim" width="${size}" height="${size}" viewBox="0 0 20 20" aria-hidden="true">${lines}</svg>`;
  }
  if (overflow) html += `<span class="tally-x">×${n}</span>`;
  return html;
}

// ── 筛选行 ──
function renderKindSeg() {
  const counts = gData.kinds || {};
  // 已知 kind 按固定顺序;数据里冒出的新 kind 追加在后,别让任何一类筛不出来
  const kinds = KIND_ORDER.filter(k => counts[k])
    .concat(Object.keys(counts).filter(k => !KIND_ORDER.includes(k)));
  const seg = [['', `全部<b>${gData.tools.length}</b>`]].concat(
    kinds.map(k => [k, `${esc(kindLabel(k))}<b>${counts[k]}</b>`]));
  $('g-kinds').innerHTML = seg.map(([k, label]) =>
    `<button class="${gState.kind === k ? 'on' : ''}" aria-pressed="${gState.kind === k}" data-kind="${esc(k)}">${label}</button>`).join('');
}

function renderTopicChips() {
  const all = gData.topics || [];
  const shown = gTopicsExpanded ? all : all.filter((t, i) => i < 7 || t.value === gState.topic);
  $('g-topics').innerHTML = shown.map(t =>
    `<button class="gtopic ${gState.topic === t.value ? 'on' : ''}" aria-pressed="${gState.topic === t.value}" data-topic="${esc(t.value)}">${esc(t.value)}<i>${t.n}</i></button>`).join('');
  $('g-topics').classList.toggle('expanded', gTopicsExpanded);
  $('g-topic-toggle').hidden = all.length <= 7;
  $('g-topic-toggle').textContent = gTopicsExpanded ? '收起主题' : '展开全部';
  $('g-topic-toggle').setAttribute('aria-expanded', String(gTopicsExpanded));
}

function toggleTopics() { if (!gData) return; gTopicsExpanded = !gTopicsExpanded; renderTopicChips(); }

function resetGalleryFilters() {
  clearTimeout(gQTimer);
  Object.assign(gState, { q: '', kind: '', topic: '', src: '', sort: 'recent' });
  syncGalleryControls();
  applyGalleryFilter();
  $('g-q').focus();
}

function syncGalleryControls() {
  $('g-q').value = gState.q;
  $('g-src').value = gState.src;
  $('g-sort').value = gState.sort;
  if (gData) { renderKindSeg(); renderTopicChips(); }
}

function setGalleryView(view) {
  const list = view === 'list';
  $('g-grid').classList.toggle('list-view', list);
  for (const mode of ['grid', 'list']) {
    $('g-view-' + mode).classList.toggle('on', mode === view);
    $('g-view-' + mode).setAttribute('aria-pressed', String(mode === view));
  }
  try { localStorage.setItem('kb-gallery-view', list ? 'list' : 'grid'); } catch {}
}

function applyGalleryFilter() {
  if (!gData) return;
  const q = gState.q.trim().toLowerCase();
  let list = gData.tools.filter(t => {
    if (gState.kind && (t.kind || 'other') !== gState.kind) return false;
    if (gState.topic && !(t.all_topics || t.topics || []).includes(gState.topic)) return false;
    if (gState.src && !(t.sources || []).includes(G_SRC[gState.src])) return false;
    if (q) {
      const hay = (t.name + ' ' + (t.summary || '') + ' ' + (t.all_topics || t.topics || []).join(' ')).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  if (gState.sort === 'n') {
    list = [...list].sort((a, b) => (b.n - a.n) ||
      String(b.latest || '').localeCompare(String(a.latest || '')));
  } else if (gState.sort === 'first') {
    list = [...list].sort((a, b) => String(a.first || '').localeCompare(String(b.first || '')));
  }
  // 默认 recent:后端已按 (latest, n) 降序返回,保持原序
  gFiltered = list;
  gRendered = 0;
  $('g-grid').innerHTML = '';
  $('g-empty').style.display = list.length ? 'none' : '';
  $('g-count').textContent = `${list.length.toLocaleString()} 个结果`;
  $('g-section-title').textContent = gState.kind ? kindLabel(gState.kind) : '全部工具';
  $('g-reset').hidden = !['q', 'kind', 'topic', 'src'].some(k => gState[k]);
  if (typeof saveWorkspaceLocation === 'function') saveWorkspaceLocation();
  renderGalleryBatch();
  updateGalleryFooter();
}

function gcardHtml(t) {
  const kc = kindCls(t.kind);
  const tags = (t.topics || []).slice(0, 3).map(x => `<span class="gtag">${esc(x)}</span>`).join('');
  const glyph = { project: 'code', product: 'box', model: 'spark', research: 'orbit', org: 'network' }[t.kind] || 'layers';
  return `<button class="gcard" data-norm="${encodeURIComponent(t.norm)}" title="${esc(t.name)}">
    <span class="gcard-body">
      <span class="gcard-top"><span class="gmark ${kc}">${icon(glyph)}</span><span class="gcard-heading"><span class="gname">${esc(t.name)}</span><span class="gkind ${kc}">${esc(kindLabel(t.kind))}</span></span></span>
      ${t.promo_soft ? '<span class="gpromo" title="这个工具的每一条记录都出自疑似软广">全部出自软广?</span>' : ''}
      <span class="gsum">${esc(t.summary || '还没有简介，打开收录记录了解更多。')}</span>
      <span class="gtags">${tags}</span>
      <span class="gstat"><span class="gtally">${tallySvg(Math.min(t.n, 5), 13)}</span>
        <span class="gcount">${t.n} 次提及 · ${t.accounts} 个来源</span>
        <span class="gdate">${esc(String(t.latest || '').slice(0, 10))}</span></span>
    </span>
  </button>`;
}

function renderGalleryBatch() {
  if (!gFiltered.length || gRendered >= gFiltered.length) return;
  const next = gFiltered.slice(gRendered, gRendered + G_BATCH);
  gRendered += next.length;
  $('g-grid').insertAdjacentHTML('beforeend', next.map(gcardHtml).join(''));
  updateGalleryFooter();
}

function updateGalleryFooter() {
  $('g-loaded').textContent = gFiltered.length ? `已显示 ${gRendered} / ${gFiltered.length.toLocaleString()} 个工具` : '';
  $('g-load-more').hidden = gRendered >= gFiltered.length;
}

// 哨兵:滚到底继续出卡
const gObserver = new IntersectionObserver(es => {
  if (es.some(x => x.isIntersecting)) renderGalleryBatch();
}, { rootMargin: '200px' });

async function loadGallery() {
  $('g-grid').setAttribute('aria-busy', 'true');
  $('g-grid').innerHTML = Array.from({length: 6}, () => '<div class="gallery-skeleton" aria-hidden="true"></div>').join('');
  try {
    gData = await fetch('/api/projects').then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
  } catch (e) {
    $('g-grid').innerHTML = `<div class="empty-state"><h3>图鉴暂时没有加载成功</h3><p>请检查服务连接，然后重试。${esc(e.message)}</p><button onclick="loadGallery()">重新加载</button></div>`;
    $('g-grid').setAttribute('aria-busy', 'false');
    return;
  }
  $('g-grid').setAttribute('aria-busy', 'false');
  $('g-total').textContent = gData.tools.length.toLocaleString();
  $('nav-tool-count').textContent = gData.tools.length.toLocaleString();
  $('g-records').textContent = (gData.total_entries || 0).toLocaleString();
  const latest = gData.tools.reduce((day, t) => t.latest > day ? t.latest : day, '');
  $('g-updated').textContent = latest ? `最近收录 ${latest.slice(0, 10)}` : '等待第一条收录';
  renderKindSeg();
  renderTopicChips();
  applyGalleryFilter();
  gObserver.observe($('g-sentinel'));
}

// 筛选交互(脚本在 body 末尾加载,元素已存在,直接绑)
let gQTimer = null;
$('g-q').addEventListener('input', e => {
  clearTimeout(gQTimer);
  if (e.isComposing) return;
  gQTimer = setTimeout(() => { gState.q = $('g-q').value; applyGalleryFilter(); }, 120);
});
$('g-q').addEventListener('compositionend', () => {
  clearTimeout(gQTimer);
  gState.q = $('g-q').value; applyGalleryFilter();
});
$('g-kinds').addEventListener('click', e => {
  const b = e.target.closest('button[data-kind]');
  if (!b) return;
  gState.kind = b.dataset.kind;
  renderKindSeg(); applyGalleryFilter();
});
$('g-topics').addEventListener('click', e => {
  const c = e.target.closest('.gtopic');
  if (!c) return;
  gState.topic = gState.topic === c.dataset.topic ? '' : c.dataset.topic;
  renderTopicChips(); applyGalleryFilter();
});
$('g-src').addEventListener('change', () => { gState.src = $('g-src').value; applyGalleryFilter(); });
$('g-sort').addEventListener('change', () => { gState.sort = $('g-sort').value; applyGalleryFilter(); });
$('g-grid').addEventListener('click', e => {
  const card = e.target.closest('.gcard');
  if (card) openToolDrawer(decodeURIComponent(card.dataset.norm));
});

// ── 详情抽屉(z 30/31,低于阅读器 40/41:条目原文/引用能盖在上面打开) ──────
let toolNorm = null;       // 当前抽屉的工具 norm
let toolMeta = null;       // /api/projects/{norm} 响应缓存
let toolConvId = null;     // 抽屉 chatbox 会话 id;换工具时清空
let toolChatBusy = false;
let toolAbort = null;
let toolRequestVersion = 0;

function srcGlyph(source) {
  return source === 'dr-video-projects'
    ? '<span class="src-glyph vid" title="视频">视</span>'
    : '<span class="src-glyph gzh" title="公众号">公</span>';
}

async function openToolDrawer(norm) {
  const changed = norm !== toolNorm;
  $('tool-mask').style.display = '';
  $('tool-drawer').style.display = '';
  if (!changed && toolMeta) return;      // 同一工具重开:直接用现成视图
  toolNorm = norm;
  toolMeta = null;
  toolConvId = null;
  if (toolAbort) toolAbort.abort();
  toolRequestVersion++;
  toolAbort = null;
  toolChatBusy = false;
  $('td-chat-send').textContent = '发送';
  $('td-chat-send').classList.remove('stop');
  $('td-chat-send').onclick = sendToolChat;
  $('td-chat-input').value = '';
  $('td-chat-send').disabled = true;
  $('td-chat-msgs').innerHTML = '';
  $('td-name').textContent = '…';
  $('td-hero').innerHTML = '<div class="empty"><span class="spin"></span>加载中…</div>';
  $('td-entries').innerHTML = '';
  let d;
  try {
    d = await fetch('/api/projects/' + encodeURIComponent(norm)).then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
  } catch (e) {
    if (toolNorm === norm) $('td-hero').innerHTML = `<div class="empty-state"><h3>暂时无法读取工具</h3><p>${esc(e.message)}</p><button onclick="openToolDrawer(toolNorm)">重新加载</button></div>`;
    return;
  }
  if (toolNorm !== norm) return;         // 加载期间切开了别的工具
  toolMeta = d;
  $('td-chat-send').disabled = false;
  renderToolHero(d);
  renderToolEntries(d);
  $('td-scroll').scrollTop = 0;
}

function closeToolDrawer() {
  $('tool-mask').style.display = 'none';
  $('tool-drawer').style.display = 'none';
}

// 分类图谱(/graph/projects、/graph/skills)里点「在图鉴里看全部记录」会带
// ?tool=<norm> 跳回首页:直接把详情抽屉拉开。开完就把参数从地址栏抹掉,
// 否则关掉抽屉后刷新一次它又自己弹出来。
function openToolFromUrl() {
  const norm = new URLSearchParams(location.search).get('tool');
  if (!norm) return;
  history.replaceState(null, '', location.pathname + location.hash);
  openToolDrawer(norm);
}

// 外链箭头走 icons.js 的统一那份(字符 ↗ 各字体渲染不一,对不齐)
const EXT_ARROW = arrow('ext');

function renderToolHero(d) {
  $('td-name').textContent = d.name;
  // URL 去重:GitHub 不区分大小写 → 统一小写归并只留一个;其他域名路径可能区分,原样保留
  const seen = new Set(), urls = [];
  for (const u of [d.url, ...(d.urls || [])]) {
    if (!u) continue;
    let v = u;
    try {
      if (new URL(u).host.replace(/^www\./, '').toLowerCase() === 'github.com') v = u.toLowerCase();
    } catch {}
    if (seen.has(v)) continue;
    seen.add(v);
    urls.push(v);
  }
  const links = urls.map(u => {
    let label = u;
    try { const x = new URL(u); label = x.host + x.pathname.replace(/\/$/, ''); } catch {}
    if (label.length > 46) label = label.slice(0, 46) + '…';
    return `<a href="${esc(safeUrl(u))}" target="_blank" rel="noopener">${esc(label)}${EXT_ARROW}</a>`;
  }).join('');
  const kc = kindCls(d.kind);
  $('td-hero').innerHTML = `
    <div class="td-hero-top"><span class="gkind ${kc}">${esc(kindLabel(d.kind))}</span>
      <span class="lbl">首次 ${esc(String(d.first || '').slice(0, 10))} · 最近 ${esc(String(d.latest || '').slice(0, 10))}</span></div>
    <div class="name">${esc(d.name)}</div>
    <div class="td-stat"><span class="gtally">${tallySvg(d.n, 20)}</span>
      <span class="lbl">${d.n} 次提及 · ${d.accounts} 个来源</span></div>
    ${d.summary ? `<div class="sum">${esc(d.summary)}</div>` : ''}
    ${links ? `<div class="td-links">${links}</div>` : ''}
    ${(d.topics || []).length ? `<div class="gtags">${d.topics.map(x =>
      `<span class="gtag">${esc(x)}</span>`).join('')}</div>` : ''}`;
}

function renderToolEntries(d) {
  const rows = (d.entries || []).map((e, i) => {
    // docA/obsLink 需要 {vault,path,title,url}:条目 md 可直接在阅读器里打开
    const h = { vault: d.vault, path: e.path, title: e.article_title || e.name || d.name,
                url: e.url, account: e.account };
    // data-snip-*:这一条记录的出处,给「收藏此段」用(见本文件末尾)。
    // 一个容器里并排着好几条各有出处的正文,所以出处挂在每一条自己身上。
    // `data-snip-version` 是**下面这段正文是哪一版**(后端读这个文件时现算的,
    // `kb/projects.py` 的 `get_tool`):收藏时原样回传,后端落库前按盘上此刻那
    // 一份再算一次,不等就拒 —— 这个抽屉可以开着好几分钟,正文在这期间被重译
    // 写回、被补抓换掉都不会通知这一页。
    return `<details class="td-entry"${i === 0 ? ' open' : ''}
      data-snip-path="${encodeURIComponent(e.path || '')}"
      data-snip-version="${esc(e.version || '')}"
      data-snip-title="${esc(h.title || '')}" data-snip-url="${esc(safeUrl(e.url || ''))}">
      <summary>${srcGlyph(e.source)}<span class="acct">${esc(e.account || '')}</span>
        <span class="date">${esc(String(e.published || '').slice(0, 10))}</span>
        <span class="atitle">${esc(e.article_title || '')}</span>${promoBadge(e)}</summary>
      <div class="body md">${mdHtml(e.body)}</div>
      <div class="elinks">
        ${e.url ? `<a href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener">${
          e.source === 'dr-video-projects' ? '视频' : '原文'}${EXT_ARROW}</a>` : ''}
        <a href="${esc(obsLink(h))}">Obsidian</a>
        ${docA(h, '在阅读器打开')}
      </div>
    </details>`;
  }).join('');
  $('td-entries').innerHTML =
    `<div class="td-sec-title">记录时间线 · ${(d.entries || []).length} 条</div>` + rows +
    `<div class="td-sec-title">就这个工具提问</div>`;
}

// ── 抽屉 chatbox:走 /api/ask/stream 流式,气泡复用 renderMsg(引用/存为笔记都白得)。
// 首条消息带工具名做上下文,后续靠 toolConvId 续多轮;会话正常落库,问答 tab 里可见。
function sendToolChat() {
  const inp = $('td-chat-input');
  const q = inp.value.trim();
  if (!q || toolChatBusy || !toolMeta) return;
  inp.value = ''; inp.style.height = 'auto';
  doSendToolChat(q);
}

async function doSendToolChat(q) {
  const version = ++toolRequestVersion;
  const current = () => version === toolRequestVersion;
  toolChatBusy = true;
  const btn = $('td-chat-send');
  btn.textContent = '■ 停止'; btn.classList.add('stop');
  btn.onclick = () => { if (toolAbort) toolAbort.abort(); };
  const model = $('ask-model').value;
  const query = toolConvId === null ? `关于工具「${toolMeta.name}」:${q}` : q;
  const box = $('td-chat-msgs');
  // 提问气泡还没落库(这一轮要等 NDJSON 收完),所以**不给落库身份** ——
  // 收到 done 帧那一刻由下面 `chatMarkSaved` 补上。口径同问答面板(`SNIP_CID`)。
  box.insertAdjacentHTML('beforeend', renderMsg({ role: 'user', content: q }));
  box.insertAdjacentHTML('beforeend',
    `<div class="msg assistant" id="td-pending"><div class="gen-head">模型 · ${esc(model || '默认')}</div>` +
    `<div class="bubble md" id="td-pending-body"><span class="spin"></span></div></div>`);
  $('td-scroll').scrollTop = $('td-scroll').scrollHeight;

  toolAbort = new AbortController();
  let partial = '', refs = [], doneCid = null, doneUid = 0, doneAid = 0,
    errMsg = null, lastPaint = 0;
  try {
    const r = await fetch('/api/ask/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, model, conversation_id: toolConvId }),
      signal: toolAbort.signal,
    });
    if (!current()) return;
    if (!r.ok) {
      let msg = r.status;
      try { const j = await r.json(); msg = j.detail || msg; } catch {}
      throw new Error(msg);
    }
    // NDJSON 读流循环与 doSendChat 同款:meta → delta× → done | error
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let lineBuf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (!current()) { await reader.cancel(); return; }
      if (done) break;
      lineBuf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = lineBuf.indexOf('\n')) >= 0) {
        const line = lineBuf.slice(0, nl); lineBuf = lineBuf.slice(nl + 1);
        if (!line.trim()) continue;
        const f = JSON.parse(line);
        if (f.type === 'meta') refs = f.refs || [];
        else if (f.type === 'delta') {
          partial += f.text;
          const now = Date.now();
          if (now - lastPaint > 40) {
            const b = document.getElementById('td-pending-body');
            if (b) b.innerHTML = mdHtml(partial);
            lastPaint = now;
          }
        } else if (f.type === 'done') {
          doneCid = f.conversation_id; doneUid = f.user_message_id; doneAid = f.message_id;
          if (f.refs) refs = f.refs;
        } else if (f.type === 'error') errMsg = f.message;
      }
    }
    if (errMsg) throw new Error(errMsg);
    const pend = document.getElementById('td-pending');
    // **正常 EOF 却没有 done 帧 = 这一轮一个字都没进库**(后端是 `_save_qa` 之后
    // 才发 done):半截答案照旧铺出来,但不给落库身份,所以收不了片段。口径与事故
    // 形态同问答面板,见 `chat.js` 的 `SNIP_CID`。
    if (!(Number(doneCid) > 0)) {
      if (pend) pend.outerHTML = renderMsg({ role: 'assistant', content: partial, refs, model });
      toast('这一轮没有收到完成确认', {
        type: 'info', detail: '屏幕上这段回答没有落库,刷新就没了;重问一次才收得了片段',
      });
      loadConvs();
      return;
    }
    toolConvId = doneCid;
    if (pend) {
      chatMarkSaved(pend, doneCid, doneUid);   // done 帧到手 = 这一轮两条消息已落库
      pend.outerHTML = renderMsg({ role: 'assistant', content: partial, refs, model },
        { cid: doneCid, mid: doneAid });
    }
    loadConvs();     // 会话已落库,问答 tab 侧栏同步
  } catch (e) {
    if (!current()) return;
    const pend = document.getElementById('td-pending');
    if (e.name === 'AbortError') {
      if (pend) pend.outerHTML =
        `<div class="msg assistant"><div class="msg-meta">已停止(未保存)</div></div>`;
    } else if (pend) {
      pend.outerHTML = `<div class="msg assistant"><div class="bubble md err">出错了: ${esc(e.message)}</div></div>`;
    }
  } finally {
    if (current()) {
      toolChatBusy = false;
      toolAbort = null;
      btn.textContent = '发送'; btn.classList.remove('stop'); btn.onclick = sendToolChat;
      $('td-scroll').scrollTop = $('td-scroll').scrollHeight;
    }
  }
}

{
  const t = $('td-chat-input');
  t.addEventListener('input', () => {
    t.style.height = 'auto';
    t.style.height = Math.min(t.scrollHeight, 120) + 'px';
  });
  t.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendToolChat(); }
  });
}

// ── 抽屉里两处正文的「收藏此段」 ──────────────────────────────────────────────
// 条目正文是一篇真文档(`source/dr-gzh-projects/…` 一类条目化产物,索引里有行),
// 所以按 origin=doc 绑 path,回跳走阅读页的锚点定位。出处挂在每个 `.td-entry`
// 自己的 data 属性上:一个容器里并排着这个工具的全部记录,各有各的出处。
attachSnip($('td-entries'), range => {
  // 选到小节标题(「记录时间线 · N 条」「就这个工具提问」)时找不到宿主,**这一步
  // 自己判空**:靠后面访问 dataset 抛异常、再被 `snipSourceOf` 的 catch 吞掉,
  // 行为上也对,可判据就藏进了一个 catch 里 —— 读代码看不出这儿有个判断,
  // 反向用例也分不出两者(实测:定点去掉下面那句判空,回归红 0 条)。
  const host = snipHostOf(range, '.td-entry');
  if (!host) return null;
  const path = decodeURIComponent(host.dataset.snipPath || '');
  if (!path) return null;        // 这条记录没有对应文档(正常导出的条目都有)
  // 版本号缺了 = 后端渲染这一条时那个文件读不出来(`get_tool` 里 body 和
  // version 同出一次读,读失败的那一条正文也是空的,本来就没什么可选)。
  // 绑一篇文档却说不出绑的是哪一版的话,后端会按 400 拒 —— 与其让用户点一颗
  // 注定失败的按钮,不如这一步就不认。
  const version = host.dataset.snipVersion || '';
  if (!version) return null;
  // **可收的范围、采锚点的范围,都收到这一条记录的正文上。** 这个 `<details>`
  // 里住着的不只正文:摘要行那几样(账号、日期、文章标题)和下面「原文 /
  // Obsidian / 在阅读器打开」三颗按钮。它们在**目标文档里压根不存在** —— 采进
  // 后文锚点之后那条收藏带着一段谁也找不到的上下文(审查实测的 suffix 是
  // `原文 Obsidian 在阅读器打开 就这个工具提问`),选中摘要行还能存下一条
  // 「某账号 某日期 某标题」冒充正文的收藏。口径同问答那一侧(`chatSnipSource`
  // 把范围收到 `.bubble` 之内、`.refs` 之外)。
  const body = host.querySelector('.body');
  if (!body || !body.contains(range.commonAncestorContainer)) return null;
  return {
    origin: 'doc', path, doc_version: version, root: body,
    origin_title: host.dataset.snipTitle || (toolMeta && toolMeta.name) || '',
    origin_url: host.dataset.snipUrl || '',
    // **落库之前按阅读页那一头的文本核一遍。** 「文件版本号一样」只证明盘上那
    // 一篇没换过,不证明用户选到的这句话在**那一页上**也连着:这儿是
    // `mdHtml` 渲染的(正文里的 `<strong>` 成了一个粗体元素、标签本身看不见),
    // 阅读页把原始 HTML 当字面文字显示 —— 跨过这么一段的选区在那一头中间插着
    // 四个尖括号字,核不过。这一类没有退路可退(原文在 vault 里就是这一篇),
    // 所以核不过照旧绑它,但会当场说一句「点回去可能定位不到这一处」
    // (见 snip.js 的 `snipVerified` / `STAY_NOTE`)。
    docText: () => snipDocText(path),
  };
});

// 抽屉里的问答和问答面板是同一条路(会话照样落库,问答 tab 侧栏里看得见),
// 所以来源也记成 chat、ref 用这边的会话 id。判据与文案共用 `chatSnipSource`。
attachSnip($('td-chat-msgs'), range => chatSnipSource(range));
