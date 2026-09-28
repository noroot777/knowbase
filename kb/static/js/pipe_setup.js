// A cancellable configuration dialog: drafts are separate for each source channel.
let pipeAcctList = [], pipeSel = new Set(), pipeAccountsReady = false;
let pipeRange = { label: '不限时间', from: '', to: '' }, pipeRangeHint = '';
let pipeAccountVersion = 0, pipeAccountAbort = null, pipeAccountsChannel = '';
const pipeDrafts = new Map();
function localDay(d) {
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function isoDay(offset) { const d = new Date(); d.setDate(d.getDate() + offset); return localDay(d); }
function pipePresets() {
  const today = isoDay(0), m3 = new Date(); m3.setMonth(m3.getMonth() - 3);
  return [{ label: '不限时间', from: '', to: '' }, { label: '今天', from: today, to: today },
    { label: '近 7 天', from: isoDay(-6), to: today }, { label: '近 30 天', from: isoDay(-29), to: today },
    { label: '近 3 个月', from: localDay(m3), to: today }, { label: '今年', from: today.slice(0, 4) + '-01-01', to: today }];
}
function pipeModalOpen() { return $('pipe-modal').style.display !== 'none'; }
function savePipeDraft() {
  if (pipeAccountsReady && pipeAccountsChannel) pipeDrafts.set(pipeAccountsChannel, { selected: [...pipeSel], range: { ...pipeRange } });
}
function pipeSetupError(message = '') {
  $('pipe-setup-error').hidden = !message; $('pipe-setup-error').textContent = message;
}
async function openPipeModal() {
  if (pipeAction || pipeSnapshot?.running) return;
  dismissToastGroup('pipeline');
  $('pipe-modal').style.display = ''; $('pipe-fields').disabled = false;
  pipeSetupError(); $('pipe-acct-search').value = ''; pipeAccountsReady = false;
  $('pipe-acct-list').innerHTML = '<p class="pipe-acct-empty">正在读取同步通道与订阅…</p>';
  renderPipeEst();
  if (!pipeConnected) refreshPipe();
  try {
    await loadPipeChannels();
    if (!pipeModalOpen()) return;
    renderPipeChannel(); await loadPipeAccounts();
  } catch (e) {
    if (!pipeModalOpen()) return;
    $('pipe-acct-list').innerHTML = '<div class="pipe-acct-empty">同步通道暂不可用。<br><button class="ghost" onclick="openPipeModal()">重新读取同步通道</button></div>';
    pipeSetupError(e.message);
  }
}
function closePipeModal() {
  savePipeDraft(); pipeAccountVersion++; pipeAccountAbort?.abort();
  $('pipe-modal').style.display = 'none'; closePipeRange();
}
async function loadPipeAccounts() {
  savePipeDraft();
  const channel = pipeChannel, version = ++pipeAccountVersion;
  pipeAccountAbort?.abort(); pipeAccountAbort = new AbortController();
  pipeAccountsReady = false; pipeAcctList = []; pipeSel = new Set();
  $('pipe-acct-list').setAttribute('aria-busy', 'true');
  $('pipe-acct-list').innerHTML = '<p class="pipe-acct-empty">正在读取订阅列表…</p>';
  $('pipe-acct-count').textContent = ''; renderPipeEst(); pipeSetupError();
  const draft = pipeDrafts.get(channel);
  pipeRange = draft?.range ? { ...draft.range } : { label: '不限时间', from: '', to: '' };
  renderPipeRange();
  try {
    const data = await pipeJson(channelUrl('/api/pipeline/accounts', channel), { signal: pipeAccountAbort.signal }, 60000);
    if (version !== pipeAccountVersion || channel !== pipeChannel || !pipeModalOpen()) return;
    pipeAcctList = [...new Set((data.accounts || []).filter(n => typeof n === 'string'))];
    pipeSel = new Set(draft ? draft.selected.filter(n => pipeAcctList.includes(n)) : pipeAcctList);
    pipeAccountsReady = true; pipeAccountsChannel = channel;
    renderPipeAcctList(); savePipeDraft();
  } catch (e) {
    if (version !== pipeAccountVersion || !pipeModalOpen()) return;
    $('pipe-acct-list').innerHTML = '<div class="pipe-acct-empty">列表暂不可用。<br><button class="ghost" onclick="loadPipeAccounts()">重新读取订阅</button></div>';
    pipeSetupError(e.message);
  } finally {
    if (version === pipeAccountVersion) { $('pipe-acct-list').setAttribute('aria-busy', 'false'); renderPipeEst(); }
  }
}
function closePipeRange() {
  $('pipe-range-pop').style.display = 'none'; $('pipe-range-btn').setAttribute('aria-expanded', 'false');
  renderPipeEst();
}
function togglePipeRangePop(e) {
  e.stopPropagation();
  if ($('pipe-range-pop').style.display !== 'none') { closePipeRange(); return; }
  renderPipePresets(); $('pipe-range-pop').style.display = '';
  $('pipe-range-btn').setAttribute('aria-expanded', 'true');
  renderPipeEst();
}
function renderPipePresets() {
  $('pipe-range-presets').innerHTML = pipePresets().map((p, i) => `<button onclick="applyPipePreset(${i})" aria-pressed="${p.label === pipeRange.label}">${p.label}</button>`).join('');
  $('pipe-custom-from').value = pipeRange.from; $('pipe-custom-to').value = pipeRange.to;
  $('pipe-custom-err').textContent = '';
}
function applyPipePreset(i) {
  pipeRange = { ...pipePresets()[i] }; closePipeRange(); renderPipeRange(); savePipeDraft(); $('pipe-range-btn').focus();
}
function applyPipeCustomRange() {
  const from = $('pipe-custom-from').value, to = $('pipe-custom-to').value;
  if (!from || !to || from > to) {
    $('pipe-custom-err').textContent = !from || !to ? '请填写开始和结束日期' : '开始日期不能晚于结束日期'; return;
  }
  pipeRange = { label: `${from} 至 ${to}`, from, to };
  closePipeRange(); renderPipeRange(); savePipeDraft(); $('pipe-range-btn').focus();
}
function renderPipeRange() {
  $('pipe-range-label').textContent = pipeRange.label;
  $('pipe-range-hint').textContent = pipeRange.from ? pipeRangeHint || '包含开始与结束当天。日期只影响抓取范围，后续步骤仍处理知识库中的待处理内容。' : '扫描所选来源可提供的文章；已有内容按身份去重。后续步骤会处理知识库中的待处理内容。';
  renderPipeEst();
}
function filteredPipeAccounts() {
  const query = $('pipe-acct-search').value.trim().toLowerCase();
  return pipeAcctList.filter(n => n.toLowerCase().includes(query));
}
function renderPipeAcctList() {
  if (!pipeAccountsReady) return;
  const items = filteredPipeAccounts(), box = $('pipe-acct-list'), scroll = box.scrollTop;
  box.innerHTML = items.length ? items.map(n => `<label class="pipe-acct"><input type="checkbox" data-acct="${esc(n)}" ${pipeSel.has(n) ? 'checked' : ''}><span>${esc(n)}</span></label>`).join('') : `<div class="pipe-acct-empty">${pipeAcctList.length ? '没有匹配的订阅，试试其他关键词。' : '当前通道暂无订阅，请先管理订阅或检查采集器。'}</div>`;
  box.scrollTop = scroll;
  updatePipeSelection();
}
function updatePipeSelection() {
  const filtered = !!$('pipe-acct-search').value.trim();
  $('pipe-acct-count').textContent = `已选 ${pipeSel.size} / ${pipeAcctList.length}`;
  $('pipe-select-all').textContent = filtered ? '全选搜索结果' : '全选';
  $('pipe-select-none').textContent = filtered ? '取消搜索结果' : '清空选择';
  $('pipe-select-all').disabled = !pipeAccountsReady || !filteredPipeAccounts().length;
  $('pipe-select-none').disabled = !pipeAccountsReady || !pipeSel.size;
  renderPipeEst(); savePipeDraft();
}
$('pipe-acct-list').addEventListener('change', e => {
  if (!e.target.matches('input[data-acct]')) return;
  const n = e.target.dataset.acct;
  if (e.target.checked) pipeSel.add(n); else pipeSel.delete(n);
  updatePipeSelection();
});
function pipeSelectAll() { for (const n of filteredPipeAccounts()) pipeSel.add(n); renderPipeAcctList(); }
function pipeSelectNone() { for (const n of filteredPipeAccounts()) pipeSel.delete(n); renderPipeAcctList(); }
function renderPipeEst() {
  const channel = pipeChannels.find(c => c.id === pipeChannel), n = pipeSel.size;
  const editingRange = $('pipe-range-pop').style.display !== 'none';
  const connection = $('pipe-setup-connection'), reconnect = connection.querySelector('button');
  connection.hidden = pipeConnected && !pipeSnapshot?.running;
  connection.querySelector('span').textContent = !pipeConnected
    ? '尚未确认后台运行状态。可先配置，连接恢复且没有运行中的任务时才能开始同步。'
    : '已有同步正在后台执行。配置已保留，结束后可启动下一次同步。';
  reconnect.hidden = pipeConnected;
  reconnect.disabled = !!pipeRequest;
  reconnect.textContent = pipeRequest ? '正在连接…' : '重新连接';
  $('pipe-est').textContent = !pipeAccountsReady ? '列表读取完成后即可开始。' : n ? `${channel?.label || '所选通道'} · ${n} 个订阅 · ${pipeRange.label}。执行中可离开本页，稍后回来查看。` : '请至少选择一个订阅。';
  if (!pipeConnected) $('pipe-est').textContent = '请先恢复状态连接，再开始同步。';
  else if (pipeSnapshot?.running) $('pipe-est').textContent = '等待当前同步结束，配置会保留。';
  if (editingRange) $('pipe-est').textContent = '请先应用日期范围，或收起日期选择以使用当前已选范围。';
  $('pipe-start').disabled = editingRange || !pipeConnected || !pipeAccountsReady || !n || !!pipeAction || !!pipeSnapshot?.running;
  $('pipe-start').textContent = pipeAction === 'start' ? '正在启动…' : n ? `开始同步 · ${n} 个` : '开始同步';
}
async function startPipeFromModal() {
  if ($('pipe-range-pop').style.display !== 'none' || !pipeConnected || pipeAction || !pipeAccountsReady || !pipeSel.size || pipeSnapshot?.running) return;
  const body = { channel: pipeChannel, accounts: [...pipeSel] };
  if (pipeRange.from) Object.assign(body, { since_date: pipeRange.from, until_date: pipeRange.to });
  pipeAction = 'start'; pipeSetupError(); invalidatePipeStatus();
  let failure = '';
  $('pipe-fields').disabled = true; renderPipeEst(); renderPipeButtons();
  try {
    await pipePost('/api/pipeline/run', body);
    closePipeModal();
    toast('同步已启动', { key: 'pipeline', type: 'ok', detail: '任务在后台执行，可随时回来查看进度。' });
  } catch (e) {
    failure = e.message;
    pipeSetupError('启动未确认：' + e.message + '。正在核对运行状态，请勿重复提交。');
    if (!pipeModalOpen()) toast('启动未确认', { key: 'pipeline', detail: e.message + '，请查看当前运行状态。' });
  } finally {
    await refreshPipe(); pipeAction = ''; $('pipe-fields').disabled = false;
    if (pipeSnapshot?.running) {
      if (pipeModalOpen()) closePipeModal();
      if (activeTab === 'pipe') $('pipe-state-title').focus({ preventScroll: true });
    }
    else if (failure) pipeSetupError(pipeConnected ? '同步未启动：' + failure + '。配置已保留，可以重试。' : '暂时无法确认任务是否启动，请先等待状态连接恢复。配置已保留。');
    renderPipeButtons(); renderPipeEst();
  }
}
