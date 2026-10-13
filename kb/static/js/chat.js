// ── 问答 chatbox ──────────────────────────────────────────────────────────
// 面板高度/留白/文本域增长那几条硬约束在 docs/decisions/frontend-feedback.md 第一节,先读再改。
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

// 空态只留三条能直接点的例子。原来顶上压着「大图标 + 一句标语 + 一段说明」,
// 那是给第一次访问的访客写的;这是自己每天开的工具,说明文案第二次看就是噪音。
const CHAT_EMPTY = `
  <div class="chat-empty">
    <div class="chat-empty-sub">问点什么，回答会带上出处。</div>
    <button class="chat-suggest" onclick="suggestChat(this)">最近一周公众号都提过哪些开源项目？</button>
    <button class="chat-suggest" onclick="suggestChat(this)">知识库里有哪些可以生成视频的开源模型？</button>
    <button class="chat-suggest" onclick="suggestChat(this)">Claude Code 有哪些实用技巧？</button>
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
  // 可用高度一变就重算上限。两个都要观察,各挡一类:
  //   .chat-main —— 卡片自己变矮(窗口缩放、展开历史栏、关掉首次配置横幅)。
  //     后两件事压根不触发 window resize,所以不能只监听窗口。
  //   .chat-inputbar —— **卡片一动不动,输入框自己变高**:模型下拉的选项是异步
  //     拿回来的,长模型名把它从 40px 撑到 240px、发送按钮被 flex-wrap 挤到新行,
  //     输入框高 167 → 201,而卡片还是 226.9(2026-09-30 第 3 轮审查实测
  //     375×667 + 横幅 + 历史栏 + 先粘 12 行再放行 /api/models:发送按钮下沿越过
  //     裁剪线约 10px,点下沿打到 #main-content)。只观察卡片的话这一类全漏。
  //     发送按钮在「发送 / ■ 停止」之间换字、字体迟到同样会改这一行的排布。
  // 卡片高度是父级 flex 给的、跟文本域无关;输入框这条回路则在下一趟算出同一个
  // 高度、赋回同一个值,尺寸没变就不会再有第三次通知,自己收敛。
  const card = t.closest('.chat-main'), bar = t.closest('.chat-inputbar');
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => autoGrowChat());
    for (const el of [card, bar]) if (el) ro.observe(el);
  }
  t.focus();
}

// 文本域随内容长高,但**不许长到把模型下拉和发送按钮顶出卡片**:.chat-main 是
// overflow: hidden 的裁剪边界,顶出去不是「挤下去还能滚到」,而是当场看不见、
// 点不到(2026-09-30 审查实测:375×667 + 首次配置横幅 + 展开历史栏 + 12 行输入,
// 发送按钮整个落在卡片底边之外,elementFromPoint 打到的是 #main-content)。
// 上限只能在 JS 里算:这里写的是**行内 height**,而行内高度会成为 flex 的
// 「specified size suggestion」——.chat-inputbar 再怎么 flex-shrink / min-height: 0
// 都压不到它以下,CSS 侧无解。也不能反过来把 .chat-main 的地板调大,那只会把
// 整个输入区重新顶出首屏,正是上一轮修掉的形态。
const CHAT_INPUT_MAX = 150;   // 舒适上限,跟 #chat-input 的 max-height 是同一个数
const CHAT_INPUT_MIN = 32;    // 一行(14.5px × 1.5 + 上下内边距);再挤也留一行

function autoGrowChat() {
  const t = $('chat-input');
  fitChatTight(t);
  t.style.height = 'auto';
  const want = Math.min(t.scrollHeight, CHAT_INPUT_MAX);
  const h = Math.max(CHAT_INPUT_MIN, Math.min(want, chatInputRoom(t))) + 'px';
  // 算出来一样就原样赋回去:尺寸不变 → ResizeObserver 不再发通知,上面那条
  // 「观察输入框」的回路到此为止。
  t.style.height = h;
}

// 放不下就让别的东西退让(`body.chat-tight` / `-2` / `-3`,值在 workspace.css)。
// **逐级试,每加一级重新量一次**:第一级收的是卡片**里面**那点空隙(消息区内边距、
// 输入框内外边距和行间 gap、横幅留白),它给不出卡片本身的高度;卡片仍装不下一行
// 输入区时才上第二级,让页头那两处留白退让(主内容上下内边距、标题行下边距);
// 还不够就上第三级,让首次配置横幅按回一行(≤600 时它默认折行,一个人吃掉 114.59px);
// 再不够就上第四级,把横幅那句说明文字收起(标题和两个按钮都留着);最后一级把
// 模型下拉压窄,让它和发送按钮回到同一行(长模型名把下拉撑到 240px 时它们会分两行,
// 输入区从 82 涨到 112px)。
// 不逐级的话就是 600×360 那个形态:卡片 57.95px、输入区一行要 82px,差的 24px
// 卡片里根本没有 —— 文本域被 `overflow: hidden` 从**上边**裁掉(只剩下缘 9px),
// 聚焦还把卡片内部滚到底(`scrollTop=36`),点写字的地方打到的是 `#main-content`,
// 字进不去输入框,滚页面也没用(2026-09-30 复审第 3 轮实测)。
// **判定永远从「一级都不加」重新开始**,而且每一级都在文本域压到一行下限时量,
// 所以收完不会反过来判成「放得下」再松开,不会来回抖;`card.clientHeight` 每一趟
// 现读(竖排那档的 `.chat-layout` gap、第二级的页头留白都跟着 class 变),两边口径
// 一致。同步改完就恢复原值,中间不会有一帧被画出来,ResizeObserver 也不会为
// 「改了又改回去」的同一个尺寸发通知。
const CHAT_TIGHT = ['chat-tight', 'chat-tight-2', 'chat-tight-3', 'chat-tight-4',
                    'chat-tight-5'];

function fitChatTight(t) {
  const bar = t.closest('.chat-inputbar');
  const card = t.closest('.chat-main');
  const cls = document.body.classList;
  // 先无条件摘干净再判:离开问答时这个函数照样会被 ResizeObserver 叫到,
  // 提前 return 而不摘的话紧凑样式会跟着走到别的页面上去。
  cls.remove(...CHAT_TIGHT);
  if (!bar || !card) return;
  // **「面板没显示」和「可见但被挤到只剩那两条边框」是两回事**,不能拿
  // `clientHeight` 当隐藏判据 —— 后者恰恰是最需要收缩的形态,而原来那句
  // `!card.clientHeight` 让它一级都不试:568×320 / 600×320 带首次配置横幅时
  // 卡片被挤到 2px(内容高 0),写问题的地方、模型下拉、发送按钮**全部消失**,
  // 点原位置打不进字、滚页面也没用(2026-09-30 复审第 4 轮实测)。
  // 越需要释放空间反而越不释放,是这条判据写反了。
  if (!card.getClientRects().length) return;
  const prev = t.style.height;
  t.style.height = CHAT_INPUT_MIN + 'px';
  for (const lvl of CHAT_TIGHT) {
    const gap = chatBarNeed(bar) - card.clientHeight;
    if (gap <= 0) break;
    cls.add(lvl);
    // **这一级白加就当场退回去**:同一条规则在不同窗口上效果是反的 —— 横幅按回
    // 一行在 568 宽上省掉整整 46px,在 320 那种真窄屏上两个 `flex-shrink: 0` 的
    // 按钮会把文字压成一条细柱,横幅反而从 136.7 长到 230.1px(复审第 2 轮实测)。
    // 「哪一级对哪个窗口有用」不是一个宽度断点能回答的,所以每一级都按**实际
    // 占位**验一次,没换来余量就摘掉,再试下一级。
    // **「有没有用」要比缺口,不能只比卡片高度**:第一级收的全是卡片**里面**那点
    // 空隙,它按定义不会让卡片外高变化 —— 只比卡片高度就把它一律判成无效。关掉
    // 横幅的 812×200 上实测:第一级把最小需求从 144 压到 92px,却被摘掉,于是二级
    // 把余量抬到 103px 之后需求又回到 144,四级走完还差 29px,输入框下沿和发送
    // 按钮重新被裁(2026-09-30 复审第 5 轮发现)。缺口 = 需求 - 余量,两头哪边
    // 变好都算数。
    if (chatBarNeed(bar) - card.clientHeight >= gap) cls.remove(lvl);
  }
  t.style.height = prev;
}

// 输入区在卡片里的最小占地 = 消息区压不下去的那点内边距 + 输入框上下外边距
//   + 输入框自身高度(此时文本域正被压在一行下限上)。
function chatBarNeed(bar) {
  const bs = getComputedStyle(bar);
  const msgs = $('chat-msgs');
  const ms = msgs ? getComputedStyle(msgs) : null;
  const pad = ms ? parseFloat(ms.paddingTop) + parseFloat(ms.paddingBottom) : 0;
  return pad + parseFloat(bs.marginTop) + parseFloat(bs.marginBottom)
       + bar.getBoundingClientRect().height;
}

// 卡片里留给文本域的最大高度 = 卡片内容高 - 消息区压不下去的那点内边距
//   - 输入框上下外边距 - 输入框里除文本域之外的部分(边框、内边距、下拉与按钮那一行)。
// 调用时文本域正处在 height:auto,所以直接拿两个实测高度相减就是「除它之外」。
// 量不出来(面板没显示、拿不到卡片)时返回 Infinity,退回原来的纯内容增长。
function chatInputRoom(t) {
  const bar = t.closest('.chat-inputbar');
  const card = t.closest('.chat-main');
  // 同 fitChatTight:只有**真没显示**才退回纯内容增长。可见卡片就算内容高是 0
  // 也得按它算,否则文本域会照着 150px 长起来、把整块输入区顶得更远。
  if (!bar || !card || !card.getClientRects().length) return Infinity;
  const bs = getComputedStyle(bar);
  const msgs = $('chat-msgs');           // flex: 1 + 内部滚动,能缩到只剩内边距
  const ms = msgs ? getComputedStyle(msgs) : null;
  const pad = ms ? parseFloat(ms.paddingTop) + parseFloat(ms.paddingBottom) : 0;
  const other = bar.getBoundingClientRect().height - t.getBoundingClientRect().height;
  return card.clientHeight - pad - parseFloat(bs.marginTop) - parseFloat(bs.marginBottom) - other;
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
    <div role="button" tabindex="0" class="conv-item ${chatConvId === null ? 'on' : ''}" onclick="newChat()">
      <div class="conv-title" title="${esc(inflight.q)}">${inflight.error ? '' : spin}${esc(inflight.q)}</div>
      <div class="conv-meta">${inflight.error ? '回答失败 · 点击查看' : '回答生成中…'}</div>
    </div>` : '';
  $('chat-convs').innerHTML = newItem + (chatConvs.length ? chatConvs.map(c => `
    <div role="button" tabindex="0" class="conv-item ${c.id === chatConvId ? 'on' : ''}" onclick="openConv(${c.id})">
      <div class="conv-title" title="${esc(c.title)}">${
        (inflight && !inflight.error && inflight.convId === c.id) ? spin : ''
      }${esc(c.title || '(无标题)')}</div>
      <div class="conv-meta">${timeAgo(c.updated_at)} · ${Math.max(1, Math.floor(c.n_messages / 2))} 轮</div>
      <button class="conv-del" aria-label="删除对话" title="删除对话" onclick="event.stopPropagation();delConv(${c.id})">✕</button>
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
      `[${r.n}] ${docA(r, r.title)} — ${esc(r.account || '')}${promoBadge(r)}` +
      ` · <a href="${esc(obsLink(r))}" style="color:var(--muted)" title="在 Obsidian 中打开">Obsidian</a>` +
      (r.url ? ` · <a href="${esc(safeUrl(r.url))}" target="_blank" rel="noopener" style="color:var(--muted)">原文${arrow('ext')}</a>` : '')
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

// Conversation rows are keyboard-operable; nested delete buttons keep their own action.
$('chat-convs').addEventListener('keydown', e => {
  if (e.target.matches('.conv-item') && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault(); e.target.click();
  }
});
