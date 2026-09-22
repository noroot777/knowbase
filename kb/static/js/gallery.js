// ── 图鉴视图(默认视图):工具/项目条目的图鉴式浏览 + 详情抽屉 ────────────────
// 数据:进页面 fetch /api/projects 一次,之后全部内存过滤/排序。
// 渲染:96 张/批增量出卡 + IntersectionObserver 哨兵(1445 个工具一次全渲染会卡)。

const KIND_LABEL = { project: '开源项目', product: '产品', model: '模型',
                     research: '研究', org: '公司', other: '其他' };
const KIND_ORDER = ['project', 'product', 'model', 'research', 'org', 'other'];
const G_BATCH = 96;
const G_SRC = { gzh: 'dr-gzh-projects', video: 'dr-video-projects' };

let gData = null;          // /api/projects 原始响应
let gFiltered = [];        // 当前筛选结果
let gRendered = 0;         // 已渲染张数
const gState = { q: '', kind: '', topic: '', src: '', sort: 'recent' };

function kindCls(k) { return KIND_ORDER.includes(k) ? 'k-' + k : 'k-other'; }
function kindLabel(k) { return KIND_LABEL[k] || k || '其他'; }

// ── 正字计数(签名元素):n 次提及 = 逐笔画 n 笔,每 5 笔一个正 ──
// 最多画 3 个正,超出后 3 个全画满并追加 ×N。出现时逐笔 draw-in,
// reduced-motion 下由 CSS 直接置 stroke-dashoffset:0(见 base.css/gallery.css)。
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
    kinds.map(k => [k, `${kindLabel(k)}<b>${counts[k]}</b>`]));
  $('g-kinds').innerHTML = seg.map(([k, label]) =>
    `<button class="${gState.kind === k ? 'on' : ''}" data-kind="${k}">${label}</button>`).join('');
}

function renderTopicChips() {
  $('g-topics').innerHTML = (gData.topics || []).map(t =>
    `<span class="gtopic ${gState.topic === t.value ? 'on' : ''}" data-topic="${esc(t.value)}">${esc(t.value)}<i>${t.n}</i></span>`).join('');
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
  $('g-count').textContent =
    `${list.length} / ${gData.tools.length} 个工具 · 共 ${gData.total_entries} 条记录`;
  renderGalleryBatch();
}

function gcardHtml(t) {
  const kc = kindCls(t.kind);
  const tags = (t.topics || []).slice(0, 3).map(x => `<span class="gtag">${esc(x)}</span>`).join('');
  return `<button class="gcard" data-norm="${encodeURIComponent(t.norm)}" title="${esc(t.name)}">
    <span class="gspine ${kc}"></span>
    <span class="gcard-body">
      <span class="gcard-top"><span class="gname">${esc(t.name)}</span><span class="gkind ${kc}">${kindLabel(t.kind)}</span></span>
      ${t.promo_soft ? '<span class="gpromo" title="这个工具的每一条记录都出自疑似软广">全部出自软广?</span>' : ''}
      <span class="gsum">${esc(t.summary || '')}</span>
      <span class="gstat"><span class="gtally">${tallySvg(t.n, 15)}</span>
        <span class="gcount">${t.n} 次 · ${t.accounts} 个号</span>
        <span class="gdate">${esc(String(t.latest || '').slice(0, 10))}</span></span>
      ${tags ? `<span class="gtags">${tags}</span>` : ''}
    </span>
  </button>`;
}

function renderGalleryBatch() {
  if (!gFiltered.length || gRendered >= gFiltered.length) return;
  const next = gFiltered.slice(gRendered, gRendered + G_BATCH);
  gRendered += next.length;
  $('g-grid').insertAdjacentHTML('beforeend', next.map(gcardHtml).join(''));
}

// 哨兵:滚到底继续出卡
const gObserver = new IntersectionObserver(es => {
  if (es.some(x => x.isIntersecting)) renderGalleryBatch();
}, { rootMargin: '800px' });

async function loadGallery() {
  $('g-grid').innerHTML = '<div class="empty"><span class="spin"></span>加载图鉴…</div>';
  try {
    gData = await fetch('/api/projects').then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
  } catch (e) {
    $('g-grid').innerHTML = `<div class="empty">图鉴加载失败: ${esc(e.message)}</div>`;
    return;
  }
  renderKindSeg();
  renderTopicChips();
  applyGalleryFilter();
  gObserver.observe($('g-sentinel'));
}

// 筛选交互(脚本在 body 末尾加载,元素已存在,直接绑)
let gQTimer = null;
$('g-q').addEventListener('input', () => {
  clearTimeout(gQTimer);
  gQTimer = setTimeout(() => { gState.q = $('g-q').value; applyGalleryFilter(); }, 120);
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
    if (toolNorm === norm) $('td-hero').innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`;
    return;
  }
  if (toolNorm !== norm) return;         // 加载期间切开了别的工具
  toolMeta = d;
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

// 外链箭头:内联 SVG(字符 ↗ 各字体渲染不一,对不齐);hover 随链接微移
const EXT_ARROW = '<svg class="ext-arrow" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">' +
  '<path d="M2.1 7.9 7.5 2.5M3.1 2.2h4.7v4.7" fill="none" stroke="currentColor"' +
  ' stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>';

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
    return `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(label)}${EXT_ARROW}</a>`;
  }).join('');
  const kc = kindCls(d.kind);
  $('td-hero').innerHTML = `
    <div class="td-hero-top"><span class="gkind ${kc}">${kindLabel(d.kind)}</span>
      <span class="lbl">首次 ${esc(String(d.first || '').slice(0, 10))} · 最近 ${esc(String(d.latest || '').slice(0, 10))}</span></div>
    <div class="name">${esc(d.name)}</div>
    <div class="td-stat"><span class="gtally">${tallySvg(d.n, 20)}</span>
      <span class="lbl">${d.n} 次提及 · ${d.accounts} 个号</span></div>
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
    return `<details class="td-entry"${i === 0 ? ' open' : ''}>
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
  toolChatBusy = true;
  const btn = $('td-chat-send');
  btn.textContent = '■ 停止'; btn.classList.add('stop');
  btn.onclick = () => { if (toolAbort) toolAbort.abort(); };
  const model = $('ask-model').value;
  const query = toolConvId === null ? `关于工具「${toolMeta.name}」:${q}` : q;
  const box = $('td-chat-msgs');
  box.insertAdjacentHTML('beforeend', renderMsg({ role: 'user', content: q }));
  box.insertAdjacentHTML('beforeend',
    `<div class="msg assistant" id="td-pending"><div class="gen-head">模型 · ${esc(model || '默认')}</div>` +
    `<div class="bubble md" id="td-pending-body"><span class="spin"></span></div></div>`);
  $('td-scroll').scrollTop = $('td-scroll').scrollHeight;

  toolAbort = new AbortController();
  let partial = '', refs = [], doneCid = null, errMsg = null, lastPaint = 0;
  try {
    const r = await fetch('/api/ask/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, model, conversation_id: toolConvId }),
      signal: toolAbort.signal,
    });
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
        } else if (f.type === 'done') { doneCid = f.conversation_id; if (f.refs) refs = f.refs; }
        else if (f.type === 'error') errMsg = f.message;
      }
    }
    if (errMsg) throw new Error(errMsg);
    toolConvId = doneCid;
    const pend = document.getElementById('td-pending');
    if (pend) pend.outerHTML = renderMsg({ role: 'assistant', content: partial, refs, model });
    loadConvs();     // 会话已落库,问答 tab 侧栏同步
  } catch (e) {
    const pend = document.getElementById('td-pending');
    if (e.name === 'AbortError') {
      if (pend) pend.outerHTML =
        `<div class="msg assistant"><div class="msg-meta">已停止(未保存)</div></div>`;
    } else if (pend) {
      pend.outerHTML = `<div class="msg assistant"><div class="bubble md err">出错了: ${esc(e.message)}</div></div>`;
    }
  } finally {
    toolChatBusy = false;
    toolAbort = null;
    btn.textContent = '发送'; btn.classList.remove('stop'); btn.onclick = sendToolChat;
    $('td-scroll').scrollTop = $('td-scroll').scrollHeight;
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
