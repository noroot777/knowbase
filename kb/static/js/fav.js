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
  if (!await confirmBox({ title: '删除这条收藏？', body: '只删除这条收藏及其笔记，不会删除原文。也可以先移至暂存。', ok: '删除收藏', danger: true })) return;
  setFavBusy(true);
  try {
    await favJson('/api/favorites/' + id, { method: 'DELETE' });
    favDrafts.delete(id);
    favUndo = favUndo.filter(item => item.id !== id);
    favActive = null;
    favFeedback('收藏已删除，原文仍在知识库中。', false);
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
