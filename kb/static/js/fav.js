let favSet = new Set();
let favView = 'all';
let favItems = [];
let favStaleDays = null;
let favTri = null;
let favFilter = 'all';
let favLayout = 'grid';
let favActive = null;
let favSelecting = false;
let favSelected = new Set();
let favUndo = [];
let favBusy = false;
let favRequest = 0;
let favToolsInited = false;
let triagePoll = null;
let favPollFailures = 0;
let favHeroIndex = 0;
const favDrafts = new Map();
const FAV_STATUSES = { all: '全部收藏', inbox: '待探索', done: '已完成', archived: '暂存' };
const FAV_FILTERS = {
  all: ['全部类型', 'layers'], quick: ['十分钟以内', 'code'],
  deep: ['留点时间', 'clock'], quote: ['灵感摘录', 'bookmark'], fresh: ['待整理', 'box'],
};
try { favLayout = localStorage.getItem('kb-fav-layout') === 'list' ? 'list' : 'grid'; } catch {}

async function favJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(await apiFail(response));
  return response.status === 204 ? null : response.json();
}

async function loadFavSet() {
  try { favSet = new Set((await favJson('/api/favorites')).doc_paths); } catch {}
}

function setFavBtn(el, on) {
  el.classList.toggle('on', on);
  el.textContent = on ? '★ 已收藏' : '☆ 收藏';
}

async function toggleFav(el) {
  try {
    const path = decodeURIComponent(el.dataset.path);
    const result = await favJson('/api/favorites/toggle', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }),
    });
    result.favorited ? favSet.add(path) : favSet.delete(path);
    document.querySelectorAll(`.favbtn[data-path="${CSS.escape(el.dataset.path)}"]`)
      .forEach(button => setFavBtn(button, result.favorited));
    if ($('favpanel').style.display !== 'none') loadFavPanel();
  } catch (error) { toast('收藏操作失败', { detail: error.message }); }
}

function switchFavView(view) {
  if (favBusy || !(view in FAV_STATUSES)) return;
  favView = view;
  favItems = [];
  favActive = null;
  favSelected.clear();
  for (const status of Object.keys(FAV_STATUSES)) {
    $('fav-t-' + status).classList.toggle('on', status === view);
    $('fav-t-' + status).setAttribute('aria-pressed', String(status === view));
  }
  renderFavInspector();
  loadFavPanel();
}

async function initFavTools(force = false) {
  if (favToolsInited && !force) return;
  favToolsInited = true;
  try {
    const [models, settings] = await Promise.all([
      favJson('/api/models'), favJson('/api/favorites/settings'),
    ]);
    const select = $('fav-model');
    select.innerHTML = models.models.map(model => `<option value="${esc(model.key)}"${model.enabled ? '' : ' disabled'}>${esc(model.label)}</option>`).join('');
    if (![...select.options].some(option => option.value === settings.model)) {
      select.insertAdjacentHTML('afterbegin', `<option value="${esc(settings.model)}">${esc(modelLabel(settings.model))}</option>`);
    }
    select.value = settings.model;
    $('fav-model-state').textContent = '';
  } catch (error) {
    favToolsInited = false;
    $('fav-model-state').textContent = '模型读取失败：' + error.message;
  }
}

async function saveFavModel() {
  try {
    await favJson('/api/favorites/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: $('fav-model').value }),
    });
    $('fav-model-state').textContent = '模型已保存';
  } catch (error) { $('fav-model-state').textContent = '保存失败：' + error.message; }
}

function favOriginal(item) {
  const indexed = item.document_available === true;
  const detached = item.origin === 'doc' && !indexed;
  const url = safeUrl(item.url);
  const label = indexed ? '打开阅读' : detached
    ? (url !== '#' ? '打开站外原文' : '原文已不在索引里')
    : '回到' + (SNIP_LABEL[item.origin] || '原处');
  return { indexed, detached, url, label };
}

function favDescription(item, detail = false) {
  return (!detail && item.quote) || item.action?.what || item.summary || item.quote
    || (favOriginal(item).detached ? '原文已不在索引里,可继续整理已保存的收藏内容。'
      : detail ? '这条收藏的更多内容，藏在原文里。' : '打开原文，重新发现这条收藏。');
}

function favDocAHtml(f, labelHtml, cls = '') {
  const original = favOriginal(f);
  if (original.detached) {
    return original.url !== '#'
      ? `<a href="${esc(original.url)}" target="_blank" rel="noopener" class="${esc(cls)}" title="原文已不在索引里，打开站外原文">${labelHtml}</a>`
      : `<span class="fv-original-missing" title="原文已不在索引里,收藏内容仍保留">${esc(original.label)}</span>`;
  }
  if (!original.indexed) return favJumpHtml(f, labelHtml, cls);
  // **`u`、`e`、`v` 三个都要一起带过去。**
  // `u` = 采集那一刻这段引文在那一篇里只有一处吗:阅读页的裸引文退路按它决定能
  // 不能采信(采集那一刻只有一处 ≠ 现在只剩一处,见 `kb/web/pages.py` 的 `seek`)。
  // `v` = 采集那一刻核对过的正文版本(`favorites.doc_version`):阅读页拿它和
  // `DOC_VERSION` 相比,**相等才算「要搜的这一篇就是采集时那一篇」**,于是锚点
  // 一侧失配可以只听另一侧(两头摊的文本本来就不是同一种渲染);不等就说明这一篇
  // 改过,那时一侧的「唯一命中」证明不了它是原处,如实说位置说不清。
  // `e` = 采集那一刻这条锚点定不定得住(`favorites.anchor_exact`):记着 0 的
  // (采集那一刻就有几处连前后文都一样,或者核对时在那一篇里压根对不上)阅读页
  // 一律不定位 —— 「现在正好剩一处」只说明别处被删掉了。
  // 三个数**都是老收藏可能没有的**(NULL),没有就一个字都不写 —— 那一头把
  // "没有"当"不知道":`u`/`v` 缺了弱档不给用,`e` 缺了按原样放行(拒掉它等于
  // 把存量收藏一次性废掉,而那些收藏的锚点并没有被证明定不住)。
  const hash = f.quote ? '#kbq=' + encodeURIComponent(
    JSON.stringify({ q: f.quote, p: f.prefix || '', s: f.suffix || '',
                     ...(f.quote_unique === 1 || f.quote_unique === 0
                       ? { u: f.quote_unique } : {}),
                     ...(f.anchor_exact === 1 || f.anchor_exact === 0
                       ? { e: f.anchor_exact } : {}),
                     ...(f.doc_version ? { v: f.doc_version } : {}) })) : '';
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
    // 同上:采集那一刻这条锚点定不定得住(`favorites.anchor_exact`)。**这一个
    // 和 `v` 不一样,要往这儿带** —— 日报正文、问答回答这几头压根没有文档版本
    // 可比,而「采集那一刻就定不住」是它们自己也有的事实。
    ...(f.anchor_exact === 1 || f.anchor_exact === 0 ? { e: f.anchor_exact } : {}),
    // **问答片段出自哪一条消息**(后端核验过的 `favorites.origin_msg`)。回跳要
    // 落到那一条消息上,不是在整个对话里搜那段引文:同一个问题问两遍得到一模
    // 一样的回答时,整对话里有两处,于是「位置说不清」、一个高亮也不给,而真正
    // 要找的那一条明明记着;两条回答只有一半相同时更坏 —— 锚点在另一条上先命中,
    // 高亮落在别人身上(见 `chat.js` 的 `chatSnipUnit`)。
    ...(Number(f.origin_msg) > 0 ? { m: Number(f.origin_msg) } : {}),
  }));
  // `v` 不往这儿带:这颗按钮只给**没有 path** 的收藏用(日报/问答/没导出的订阅
  // 条目),它们压根不绑文档,没有正文版本可比 —— 绑了文档的走上面那条阅读页。
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
                 ...(j.u === 1 || j.u === 0 ? { u: j.u } : {}),
                 ...(j.e === 1 || j.e === 0 ? { e: j.e } : {}) };
  // **`same` 由各面板自己决定,这儿一个字都不写**(默认 = 证明不了)。日报正文、
  // 订阅正文这两头:采集和回跳走的是同一个渲染器,一侧对不上就只能是正文自己变过
  // 了,没有「两种渲染」这个借口 —— 弱档于是一律不采信。问答那一头不一样,见
  // 下面 chat 分支和 `chat.js` 的 `chatSnipUnit`。
  if (j.origin === 'daily') {
    if (!j.ref) return toast('这条收藏没有记下是哪一天的日报');
    dailyPendingSnip = { ref: j.ref, spec };
    switchTab('daily');
    return;
  }
  if (j.origin === 'chat') {
    const cid = Number(j.ref);
    if (!cid) return toast('这条收藏没有记下是哪一个对话');
    // `msg` 交给 `openConv`:它负责把落点收到那一条消息上(没有这个数的老收藏
    // 才在整个对话里搜,而且不采信弱档)
    chatPendingSnip = { ref: cid, msg: Number(j.m) || 0, spec };
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

function favCategory(item) {
  if (item.quote || item.action?.kind === 'reference') return 'quote';
  const action = item.action || {};
  const eta = String(action.eta || '');
  if (/小时|半天|天|周/.test(eta)) return 'deep';
  const minutes = eta.match(/(\d+(?:\.\d+)?)\s*分钟/);
  if (minutes) return Number(minutes[1]) <= 10 ? 'quick' : 'deep';
  if (favTri?.fresh) {
    if ((favTri.data?.quick || []).some(row => row.id === item.id)) return 'quick';
    if ((favTri.data?.deep || []).some(row => row.id === item.id)) return 'deep';
  }
  return 'fresh';
}

function visibleFavs() {
  const query = $('fav-search').value.trim().toLocaleLowerCase();
  const source = $('fav-source').value;
  const items = favItems.filter(item => {
    if (favFilter !== 'all' && favCategory(item) !== favFilter) return false;
    if (source && item.source !== source) return false;
    return !query || [item.title, item.summary, item.account, item.quote, item.note,
      ...Object.values(item.action || {})].join(' ').toLocaleLowerCase().includes(query);
  });
  const sort = $('fav-sort').value;
  return items.sort((left, right) => sort === 'name'
    ? (left.action?.name || left.title).localeCompare(right.action?.name || right.title, 'zh-CN')
    : (sort === 'oldest' ? 1 : -1) * (left.created_at.localeCompare(right.created_at) || left.id - right.id));
}

function filterFavs(category) {
  if (category && category in FAV_FILTERS) favFilter = category;
  renderFavCollection();
}

function pruneFavSelection(items) {
  const visibleIds = new Set(items.map(item => item.id));
  favSelected = new Set([...favSelected].filter(id => visibleIds.has(id)));
}

function resetFavFilters() {
  favFilter = 'all';
  $('fav-search').value = '';
  $('fav-source').value = '';
  filterFavs();
}

function setFavLayout(layout) {
  favLayout = layout;
  try { localStorage.setItem('kb-fav-layout', layout); } catch {}
  renderFavCollection();
}

function toggleFavSelection(on = !favSelecting) {
  favSelecting = on;
  favSelected.clear();
  renderFavCollection();
}

function selectFav(id, selected) {
  selected ? favSelected.add(id) : favSelected.delete(id);
  $('fav-list').querySelector(`[data-fav-card="${id}"]`)?.classList.toggle('is-selected', selected);
  updateFavSelection();
}

function selectVisibleFavs(selected) {
  favSelected = new Set(selected ? visibleFavs().map(item => item.id) : []);
  renderFavCollection();
}

function updateFavSelection() {
  $('fav-selected-count').textContent = `已选 ${favSelected.size} 条`;
  const visible = visibleFavs();
  $('fav-select-all').checked = !!visible.length && visible.every(item => favSelected.has(item.id));
  $('fav-select-all').indeterminate = !!favSelected.size && !$('fav-select-all').checked;
  $('fav-batch').querySelectorAll('[data-fav-mutation]').forEach(button => {
    button.disabled = favBusy || !favSelected.size;
  });
}

async function loadFavPanel() {
  const request = ++favRequest;
  const view = favView;
  clearTimeout(triagePoll);
  $('fav-list').setAttribute('aria-busy', 'true');
  if (!favItems.length) $('fav-list').innerHTML = '<div class="fv-empty"><span class="spin"></span>正在打开收藏架…</div>';
  $('fav-error').hidden = true;
  try {
    const data = await favJson('/api/favorites?status=' + view);
    if (request !== favRequest) return;
    favItems = data.favorites;
    favStaleDays = Number.isFinite(data.stale_days) && data.stale_days > 0 ? data.stale_days : null;
    favSet = new Set(data.doc_paths);
    if (!favItems.some(item => item.id === favActive)) favActive = null;
    renderFavHeader(data);
    renderFavCollection();
    renderFavInspector();
    refreshFavTriage(request);
  } catch (error) {
    if (request !== favRequest) return;
    showFavError('收藏加载失败：' + error.message, 'loadFavPanel()');
    if (!favItems.length) $('fav-list').innerHTML = '';
  } finally {
    if (request === favRequest) $('fav-list').setAttribute('aria-busy', 'false');
  }
}

function showFavError(message, retry = '') {
  const box = $('fav-error');
  box.hidden = false;
  box.innerHTML = `<span>${esc(message)}</span>${retry ? `<button class="text-button" onclick="${retry}">重试</button>` : ''}`;
}

async function refreshFavTriage(request = favRequest) {
  try {
    const triage = await favJson('/api/favorites/triage');
    if (request !== favRequest) return;
    favTri = triage;
    favPollFailures = 0;
    renderFavCollection();
    if (!favActive) renderFavInspector();
    if (triage.running) pollTriage();
  } catch (error) {
    if (request !== favRequest) return;
    favTri = { error: '整理建议暂不可用：' + error.message, fresh: false };
    if (!favActive) renderFavInspector();
  }
}

async function runTriage() {
  try {
    await favJson('/api/favorites/triage/run', { method: 'POST' });
    favTri = { ...favTri, running: true, error: '' };
    if (!favActive) renderFavInspector();
    favPollFailures = 0;
    pollTriage();
  } catch (error) { showFavError('AI 整理未能启动：' + error.message, 'runTriage()'); }
}

function pollTriage() {
  clearTimeout(triagePoll);
  triagePoll = setTimeout(async () => {
    if ($('favpanel').style.display === 'none') return;
    const request = favRequest;
    try {
      const triage = await favJson('/api/favorites/triage');
      if (request !== favRequest) return;
      favTri = triage;
      favPollFailures = 0;
      if (!favActive) renderFavInspector();
      if (triage.running) pollTriage();
      else renderFavCollection();
    } catch (error) {
      if (request !== favRequest) return;
      if (++favPollFailures < 3) pollTriage();
      else {
        favTri = { ...favTri, running: false, error: '整理状态读取失败：' + error.message };
        if (!favActive) renderFavInspector();
        showFavError('无法继续读取整理进度。你的收藏仍可正常使用。', 'refreshFavTriage()');
      }
    }
  }, 2000);
}

function setFavBusy(busy) {
  favBusy = busy;
  $('favpanel').querySelectorAll('[data-fav-mutation], #fav-undo').forEach(button => { button.disabled = busy; });
  updateFavSelection();
}

async function favPatch(id, patch) {
  return favJson('/api/favorites/' + id, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
  });
}

async function favMark(id, status) { return favMarkMany([id], status); }

async function favMarkMany(ids, status) {
  if (favBusy || !ids.length) return;
  const targets = favItems.filter(item => ids.includes(item.id) && item.status !== status);
  if (!targets.length) return;
  setFavBusy(true);
  const changed = [];
  const failed = [];
  await Promise.all(targets.map(async item => {
    try { await favPatch(item.id, { status }); changed.push({ id: item.id, status: item.status }); }
    catch (error) { failed.push(error.message); }
  }));
  if (changed.length) {
    favUndo = changed;
    favFeedback(`${changed.length} 条收藏${status === 'done' ? '已标为完成' : status === 'archived' ? '已移至暂存，随时可以找回' : '已放回待探索'}`);
    changed.forEach(item => favSelected.delete(item.id));
    favTri = favTri ? { ...favTri, fresh: false } : null;
    await loadFavPanel();
  }
  setFavBusy(false);
  if (failed.length) showFavError(`${failed.length} 条未能更新${changed.length ? '，其余已保存' : '，状态未改变'}：${failed[0]}`);
}

function favFeedback(message, undo = true) {
  $('fav-feedback').hidden = false;
  $('fav-feedback-text').textContent = message;
  $('fav-undo').hidden = !undo;
}

async function undoFavChange() {
  if (favBusy || !favUndo.length) return;
  setFavBusy(true);
  const failed = [];
  await Promise.all(favUndo.map(async item => {
    try { await favPatch(item.id, { status: item.status }); }
    catch { failed.push(item); }
  }));
  favUndo = failed;
  if (!failed.length) favFeedback('已撤销，收藏回到了原来的位置。', false);
  await loadFavPanel();
  setFavBusy(false);
  if (failed.length) showFavError(`${failed.length} 条收藏撤销失败，请重试。`, 'undoFavChange()');
}

async function favDel(id) {
  if (favBusy) return;
  if (!await confirmBox({ title: '删除这条收藏？', body: '只删除这条收藏及其笔记。也可以先移至暂存。', ok: '删除收藏', danger: true })) return;
  setFavBusy(true);
  try {
    await favJson('/api/favorites/' + id, { method: 'DELETE' });
    favDrafts.delete(id);
    favUndo = favUndo.filter(item => item.id !== id);
    favActive = null;
    favFeedback('收藏及其笔记已删除。', false);
    await loadFavPanel();
  } catch (error) { showFavError('删除失败：' + error.message); }
  finally { setFavBusy(false); }
}

function draftFavNote(value) {
  if (value === favItems.find(item => item.id === favActive)?.note) favDrafts.delete(favActive);
  else favDrafts.set(favActive, value);
  $('fav-note-state').textContent = favDrafts.has(favActive) ? '未保存 · 切换收藏时保留草稿' : '给未来的自己留个线索';
}

async function saveFavNote(id) {
  if (favBusy) return;
  const note = favDrafts.get(id);
  if (note === undefined) return;
  setFavBusy(true);
  try {
    await favPatch(id, { note: note.trim() });
    const item = favItems.find(row => row.id === id);
    if (item) item.note = note.trim();
    if (favDrafts.get(id) === note) favDrafts.delete(id);
    if (favActive === id) $('fav-note-state').textContent = favDrafts.has(id) ? '还有未保存的修改' : '笔记已保存';
    renderFavCollection();
  } catch (error) {
    if (favActive === id) $('fav-note-state').textContent = '保存失败，草稿已保留。请重试。';
    toast('笔记保存失败', { detail: error.message });
  } finally { setFavBusy(false); }
}

async function copyFavCmd(button) {
  try {
    await navigator.clipboard.writeText(button.dataset.cmd);
    toast('命令已复制，不会自动执行', { type: 'ok' });
  } catch { toast('复制失败，请选中命令手动复制'); }
}

async function extractFavAction(id) {
  if (favBusy) return;
  setFavBusy(true);
  try {
    const result = await favJson(`/api/favorites/${id}/extract`, { method: 'POST' });
    const item = favItems.find(row => row.id === id);
    if (item) item.action = result.action;
    renderFavCollection();
    if (favActive === id) renderFavInspector();
  } catch (error) { showFavError('行动卡生成失败：' + error.message); }
  finally { setFavBusy(false); }
}

window.addEventListener('beforeunload', event => {
  if (favDrafts.size) { event.preventDefault(); event.returnValue = ''; }
});
