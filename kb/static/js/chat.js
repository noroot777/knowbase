// ── 问答 chatbox ──────────────────────────────────────────────────────────
let chatConvId = null;      // 当前会话 id;null = 新对话(首次提问时由后端创建)
let chatConvs = [];         // 会话列表缓存
let chatBusy = false;       // 生成中(只禁并发发送,浏览/切换会话不受限)
let chatInited = false;
// 进行中(或失败还没展示)的请求:{ convId, q, error? }。convId=null 表示新对话。
// 请求与视图解耦:生成中可以随意切换会话,切回时由 appendInflight 恢复气泡。
let inflight = null;

let chatAbort = null;       // 进行中请求的 AbortController;点停止即 abort

// 生成中的气泡:顶部标模型 + 阶段(agent 研究步骤/生成中),body 承载流式渲染的 markdown。
// partial 有内容(切回来恢复时)则直接渲染,否则先转圈。
function pendingHtml(model, partial, phase) {
  const body = partial ? mdHtml(partial) : '<span class="spin"></span>';
  const ph = partial ? '生成中…' : (phase || '检索中…');
  return `<div class="msg assistant" id="chat-pending">` +
    `<div class="gen-head">模型 · ${esc(modelLabel(model) || '默认')} · <span id="chat-gen-phase">${esc(ph)}</span></div>` +
    `<div class="bubble md" id="chat-pending-body">${body}</div></div>`;
}

// 把已累计的 inflight.partial 渲染进当前视图的 pending body(切走了则找不到,自动跳过)
function paintPending() {
  const b = document.getElementById('chat-pending-body');
  if (b && inflight) b.innerHTML = mdHtml(inflight.partial || '');
  const ph = document.getElementById('chat-gen-phase');
  if (ph) ph.textContent = '生成中…';
}

// 发送按钮在「发送 / ■ 停止」两态间切换;生成中任何时候都能点停止(含追问)
function setSendMode(busy) {
  const b = $('chat-send');
  b.disabled = false;
  if (busy) { b.textContent = '■ 停止'; b.classList.add('stop'); b.onclick = stopChat; }
  else { b.textContent = '发送'; b.classList.remove('stop'); b.onclick = sendChat; }
}

function stopChat() { if (chatAbort) chatAbort.abort(); }

const CHAT_EMPTY = `
  <div class="chat-empty">
    <div class="chat-empty-title">知识库问答</div>
    <div class="chat-empty-sub">
      问题交给模型自主研究:自己选择检索/枚举工具、结果太少会换近义词、换来源再查
      (气泡上方能看到每一步),查够了再综合成带引用的回答。穷举类("都提过哪些")
      走全量聚合不漏项;追问可直接基于上文。支持多轮对话、随时停止,记录自动保存。
    </div>
    <span class="chat-suggest" onclick="suggestChat(this)">最近一周公众号都提过哪些开源项目?</span>
    <span class="chat-suggest" onclick="suggestChat(this)">知识库里有哪些可以生成视频的开源模型?</span>
    <span class="chat-suggest" onclick="suggestChat(this)">Claude Code 有哪些实用技巧?</span>
  </div>`;

function initChat() {
  if (chatInited) { $('chat-input').focus(); return; }
  chatInited = true;
  $('chat-msgs').innerHTML = CHAT_EMPTY;
  loadConvs();
  const t = $('chat-input');
  t.addEventListener('input', autoGrowChat);
  t.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
  });
  t.focus();
}

function autoGrowChat() {
  const t = $('chat-input');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 150) + 'px';
}

function timeAgo(iso) {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return '刚刚';
  if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
  if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
  if (s < 7 * 86400) return Math.floor(s / 86400) + ' 天前';
  return iso.slice(5, 10).replace('-', '/');
}

async function loadConvs() {
  const d = await fetch('/api/conversations').then(r => r.json()).catch(() => null);
  chatConvs = (d && d.conversations) || [];
  renderConvList();
}

function renderConvList() {
  const spin = '<span class="spin" style="width:10px;height:10px;border-width:1.5px;margin-right:5px"></span>';
  // 进行中/失败的"新对话"还没落库,列表顶部给个占位项,点击可切回去看进度
  const newItem = (inflight && inflight.convId === null) ? `
    <div class="conv-item ${chatConvId === null ? 'on' : ''}" onclick="newChat()">
      <div class="conv-title" title="${esc(inflight.q)}">${inflight.error ? '' : spin}${esc(inflight.q)}</div>
      <div class="conv-meta">${inflight.error ? '回答失败 · 点击查看' : '回答生成中…'}</div>
    </div>` : '';
  $('chat-convs').innerHTML = newItem + (chatConvs.length ? chatConvs.map(c => `
    <div class="conv-item ${c.id === chatConvId ? 'on' : ''}" onclick="openConv(${c.id})">
      <div class="conv-title" title="${esc(c.title)}">${
        (inflight && !inflight.error && inflight.convId === c.id) ? spin : ''
      }${esc(c.title || '(无标题)')}</div>
      <div class="conv-meta">${timeAgo(c.updated_at)} · ${Math.max(1, Math.floor(c.n_messages / 2))} 轮</div>
      <span class="conv-del" title="删除对话" onclick="event.stopPropagation();delConv(${c.id})">✕</span>
    </div>`).join('')
    : (newItem ? '' : '<div class="chat-side-empty">还没有历史对话</div>'));
}

// 切到某个会话视图时,若该会话有进行中/失败未展示的请求,把气泡补回来
function appendInflight(convId) {
  if (!inflight || inflight.convId !== convId) return;
  const box = $('chat-msgs');
  box.querySelector('.chat-empty')?.remove();
  box.insertAdjacentHTML('beforeend', renderMsg({ role: 'user', content: inflight.q }));
  box.insertAdjacentHTML('beforeend',
    inflight.error ? renderChatError(inflight.error, inflight.q)
                   : pendingHtml(inflight.model, inflight.partial, inflight.phase));
  scrollChat(true);
}

function renderChatError(msg, q) {
  return `<div class="msg assistant"><div class="bubble md err">出错了: ${esc(msg)} ` +
    `<button class="ghost" style="padding:3px 12px;font-size:12.5px" data-q="${esc(q)}" ` +
    `onclick="retryChat(this)">重试</button></div></div>`;
}

async function openConv(id, force) {
  if (id === chatConvId && !force) return;
  chatConvId = id;
  renderConvList();
  $('chat-msgs').innerHTML = '<div class="chat-empty"><span class="spin"></span>加载对话…</div>';
  const d = await fetch('/api/conversations/' + id).then(r => r.json()).catch(() => null);
  if (chatConvId !== id) return;   // 加载期间又切走了,别覆盖别人的视图
  if (!d) { $('chat-msgs').innerHTML = '<div class="chat-empty">对话加载失败</div>'; return; }
  $('chat-msgs').innerHTML = d.messages.map(renderMsg).join('');
  appendInflight(id);
  scrollChat(true);
}

function newChat() {
  chatConvId = null;
  renderConvList();
  $('chat-msgs').innerHTML = CHAT_EMPTY;
  appendInflight(null);   // 进行中的新对话切回来,恢复气泡
  $('chat-input').focus();
}

async function delConv(id) {
  if (inflight && inflight.convId === id) {
    toast('这个对话正在生成回答', { type: 'info', detail: '等它结束再删' });
    return;
  }
  const yes = await confirmBox({
    title: '删除这个对话?', body: '记录不可恢复。', ok: '删除', danger: true,
  });
  if (!yes) return;
  await fetch('/api/conversations/' + id, { method: 'DELETE' }).catch(() => {});
  if (id === chatConvId) { chatConvId = null; $('chat-msgs').innerHTML = CHAT_EMPTY; }
  loadConvs();
}

function renderMsg(m) {
  if (m.role === 'user') {
    return `<div class="msg user"><div class="bubble">${esc(m.content)}</div></div>`;
  }
  const refMap = {};
  (m.refs || []).forEach(r => { refMap[r.n] = r; });
  // 先净化模型输出,再把 [n] 换成我们自己拼的引用链接(顺序反了会被 sanitizer 洗掉 data-doc)
  const html = mdHtml(m.content).replace(/\[(\d+)\]/g, (s, n) =>
    refMap[n] ? docA(refMap[n], `[${n}]`, 'cite') : s);
  const refsHtml = (m.refs || []).length ? `
    <details class="refs"><summary>来源 ${m.refs.length} 篇</summary>${m.refs.map(r =>
      `[${r.n}] ${docA(r, r.title)} — ${esc(r.account || '')}` +
      ` · <a href="${esc(obsLink(r))}" style="color:var(--muted)" title="在 Obsidian 中打开">Obsidian</a>` +
      (r.url ? ` · <a href="${esc(safeUrl(r.url))}" target="_blank" rel="noopener" style="color:var(--muted)">原文↗</a>` : '')
    ).join('<br>')}</details>` : '';
  const meta = m.model ? `<div class="msg-meta">${esc(modelLabel(m.model))}${m.created_at ? ' · ' + timeAgo(m.created_at) : ''}` +
    ` · <button class="ghost note-btn" style="padding:2px 10px;font-size:12px" ` +
    `data-payload="${encodeURIComponent(JSON.stringify({ content: m.content, refs: m.refs || [] }))}" ` +
    `onclick="saveNote(this)" title="把这条回答回填到 vault 的 wiki/notes/(知识复利)">存为笔记</button></div>` : '';
  return `<div class="msg assistant"><div class="bubble md">${html}${refsHtml}</div>${meta}</div>`;
}

async function saveNote(btn) {
  const d = JSON.parse(decodeURIComponent(btn.dataset.payload));
  let q = '', el = btn.closest('.msg');
  while (el && (el = el.previousElementSibling)) {
    if (el.classList.contains('user')) { q = el.querySelector('.bubble').textContent; break; }
  }
  btn.disabled = true; btn.textContent = '保存中…';
  try {
    const r = await fetch('/api/notes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, answer: d.content, refs: d.refs }),
    });
    if (!r.ok) throw new Error(await apiFail(r));
    const j = await r.json();
    btn.outerHTML = `<a href="${j.obsidian}" style="color:var(--ok,#2E7D57);font-size:12px">已存入 notes ✓ 打开</a>`;
  } catch (e) {
    btn.disabled = false; btn.textContent = '存为笔记';
    toast('存为笔记失败', { detail: e.message });
  }
}

function scrollChat(instant) {
  const box = $('chat-msgs');
  box.scrollTo({ top: box.scrollHeight, behavior: instant ? 'instant' : 'smooth' });
}

function suggestChat(el) {
  $('chat-input').value = el.textContent;
  autoGrowChat();
  sendChat();
}

function sendChat() {
  const q = $('chat-input').value.trim();
  if (!q || chatBusy) return;
  $('chat-input').value = '';
  autoGrowChat();
  doSendChat(q, false);
}

async function doSendChat(q, isRetry) {
  chatBusy = true;
  const reqConvId = chatConvId;          // 请求归属的会话,发出后不随视图切换变
  const model = $('ask-model').value;
  inflight = { convId: reqConvId, q, model, partial: '' };
  setSendMode(true);                     // 发送按钮变「■ 停止」
  const box = $('chat-msgs');
  box.querySelector('.chat-empty')?.remove();
  if (!isRetry) box.insertAdjacentHTML('beforeend', renderMsg({ role: 'user', content: q }));
  box.insertAdjacentHTML('beforeend', pendingHtml(model, ''));
  renderConvList();                      // 侧栏标上生成中的 spinner
  scrollChat();

  chatAbort = new AbortController();
  let lastPaint = 0;
  try {
    const r = await fetch('/api/ask/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q, model, conversation_id: reqConvId }),
      signal: chatAbort.signal,
    });
    if (!r.ok) {   // 开流前的错误(400/404/502 等)是普通 JSON
      let msg = r.status;
      try { const j = await r.json(); msg = j.detail || msg; } catch {}
      throw new Error(msg);
    }
    // 逐行读 NDJSON 帧:meta(refs/plan/model)→ 若干 delta(text)→ done / error
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let lineBuf = '', refs = [], doneCid = null, errMsg = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      lineBuf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = lineBuf.indexOf('\n')) >= 0) {
        const line = lineBuf.slice(0, nl); lineBuf = lineBuf.slice(nl + 1);
        if (!line.trim()) continue;
        const f = JSON.parse(line);
        if (f.type === 'meta') { refs = f.refs || []; }
        else if (f.type === 'progress') {
          // agent 研究步骤:显示在气泡阶段行(已开始出正文就不再覆盖)
          inflight.phase = f.text;
          const el = document.getElementById('chat-gen-phase');
          if (el && !inflight.partial) el.textContent = f.text;
        }
        else if (f.type === 'delta') {
          inflight.partial += f.text;
          const now = Date.now();
          if (now - lastPaint > 40) { paintPending(); lastPaint = now; }
        } else if (f.type === 'done') { doneCid = f.conversation_id; if (f.refs) refs = f.refs; }
        else if (f.type === 'error') { errMsg = f.message; }
      }
    }
    if (errMsg) throw new Error(errMsg);
    // 正常收完:用最终答案 + refs + 模型定稿(renderMsg 下方带模型标注 + 存为笔记)
    const answer = inflight.partial;
    inflight = null;
    const pend = $('chat-pending');
    if (pend) {
      chatConvId = doneCid;
      pend.outerHTML = renderMsg({ role: 'assistant', content: answer, refs, model });
      scrollChat();
    } else if (chatConvId === doneCid) {
      openConv(doneCid, true);           // 竞态兜底:切回加载中 → 强刷拿落库版
    }
    loadConvs();
  } catch (e) {
    if (e.name === 'AbortError') {
      // 用户主动停止:丢弃半截、不入库、不重试(刷新即消失,同失败处理)
      inflight = null;
      const pend = $('chat-pending');
      if (pend) pend.outerHTML =
        `<div class="msg assistant"><div class="msg-meta">⏹ 已停止(未保存)</div></div>`;
      renderConvList();
    } else {
      // 错误留 inflight(没落库):当前视图直接展示,切走再切回由 appendInflight 恢复
      inflight = { convId: reqConvId, q, error: e.message };
      const pend = $('chat-pending');
      if (pend) pend.outerHTML = renderChatError(e.message, q);
      renderConvList();
    }
  } finally {
    chatBusy = false;
    chatAbort = null;
    setSendMode(false);                  // 停止按钮变回「发送」
  }
}

function retryChat(btn) {
  if (chatBusy) return;
  const q = btn.dataset.q;
  btn.closest('.msg').remove();    // 移除错误气泡,保留 user 气泡
  if (inflight && inflight.error) inflight = null;   // 这条错误已被消费
  doSendChat(q, true);
}
