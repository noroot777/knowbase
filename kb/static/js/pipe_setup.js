// A cancellable configuration dialog: one subscription draft per source, one shared range.
// 一次同步可以勾好几个来源，所以选择是「来源 → 订阅集合」两层，不是一个扁平列表。
const pipeAccounts = new Map();   // channel -> {state:'loading'|'ready'|'error', list, error}
const pipeSel = new Map();        // channel -> Set(订阅名)
const pipeDrafts = new Map();     // channel -> [订阅名]，关掉弹窗再打开时接着用
const pipeAcctAborts = new Map();
let pipeRange = { label: '不限时间', from: '', to: '' };
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
function pipeReadyChannels() { return pipeRunSel.filter(id => pipeAccounts.get(id)?.state === 'ready'); }
function pipeLoadingChannels() { return pipeRunSel.filter(id => pipeAccounts.get(id)?.state === 'loading'); }
function pipeFailedChannels() { return pipeRunSel.filter(id => pipeAccounts.get(id)?.state === 'error'); }
function pipeSelected(channel) { return pipeSel.get(channel) || new Set(); }
function pipeSelCount() { return pipeReadyChannels().reduce((n, id) => n + pipeSelected(id).size, 0); }
function savePipeDraft() {
  for (const id of pipeReadyChannels()) pipeDrafts.set(id, [...pipeSelected(id)]);
}
function pipeSetupError(message = '') {
  $('pipe-setup-error').hidden = !message; $('pipe-setup-error').textContent = message;
}
// 采集通道在设置里换过了（可能是别的页面换的）。两件事:订阅选择跟着改道、开着的弹窗
// 重读订阅。
// **改道跟弹窗开没开无关,目标通道有没有旧草稿也无关**:两条绿泡泡通道订阅的是同一批
// 号,换通道是「改道」不是「换来源」,采哪几个号只由用户最后一次选择决定。少一半就出事,
// 两种形态都实测过(2026-09-30 第 3 轮审查):
//  · **关着的时候换**——迁移写在 `pipeModalOpen()` 后面的话这儿直接 return,新通道没有
//    草稿,下次打开 `loadChannelAccounts` 的 `draft ? … : list` 分支按「整份订阅全选」
//    初始化:上次只勾了一个号,换个采集方式回来变成全选,点开始把没勾的那些一起采了;
//  · **换回曾用过的那条**——`!pipeDrafts.has(after)` 那个条件会把它排除掉,于是目标通道
//    那份**更早**的草稿盖掉用户刚改过的这份:只选乙,来回换一次变回只选甲,开始后采的
//    也是甲。所以一律用当前选择覆盖目标通道的草稿。
// 取值优先 `pipeSel`(屏幕上此刻的选择),它没有才退回草稿;空集合也要照搬 —— 「一个都
// 不采」是用户的选择,当成"没有草稿"就又变成全选了。
// **重读订阅只在弹窗开着时做**:那份 `pipeAccounts` 是按旧通道读的,`pipeRunSel` 已经
// 改道、里面却没有新通道那一条 —— 它既不算 ready 也不算 loading,于是 `pipeRunBody()`
// 直接把它漏掉、开始按钮还照样能点,用户明明勾着公众号、点下去只同步了 RSS,一句提示都
// 没有(2026-09-30 复审实测)。读取期间新通道是 loading,开始按钮自动禁用,读完才恢复。
document.addEventListener('kb:channels-changed', e => {
  const { before, after } = e.detail;
  if (before && after && before !== after) {
    const latest = pipeSel.get(before) || pipeDrafts.get(before);
    if (latest) pipeDrafts.set(after, [...latest]);
  }
  if (pipeModalOpen()) loadPipeAccounts(true).catch(() => {});
});
async function openPipeModal() {
  if (pipeAction || pipeSnapshot?.running) return;
  dismissToastGroup('pipeline');
  $('pipe-modal').style.display = ''; $('pipe-fields').disabled = false;
  pipeSetupError(); $('pipe-acct-search').value = '';
  $('pipe-acct-list').innerHTML = '<p class="pipe-acct-empty">正在读取同步来源与订阅…</p>';
  renderPipeEst();
  if (!pipeConnected) refreshPipe();
  try {
    // 每次打开都重新核对来源清单:绿泡泡那条互斥通道是个全局设置,别的页面/CLI/API
    // 都改得动 —— 吃缓存的话这儿会按一条已经不生效的通道显示并派发同步。
    await loadPipeChannels(true);
    if (!pipeModalOpen()) return;
    // 每次打开都重读一遍订阅:上次看到的那份可能已经过期(期间添加/停用/移除过号)
    renderPipeChannel(); await loadPipeAccounts(true);
  } catch (e) {
    if (!pipeModalOpen()) return;
    $('pipe-acct-list').innerHTML = '<div class="pipe-acct-empty">同步来源暂不可用。<br><button class="ghost" onclick="openPipeModal()">重新读取来源</button></div>';
    pipeSetupError(e.message);
    // 核对没成功,`pipeChannelsFresh` 仍是假:这一句把它落到开始按钮上 —— 上次那份
    // ready 订阅是按一条可能已经不生效的通道读的,页面都写着「暂不可用」了,按钮还能点
    // 下去拿旧通道开跑(2026-09-30 复审实测:GET 503 之后照样派发 wechat_local)。
    renderPipeEst();
  }
}
function closePipeModal() {
  savePipeDraft();
  for (const [id, controller] of pipeAcctAborts) {
    controller.abort();
    // 中止掉的那次读取不会再回来了。状态留成 loading 的话,下次打开会被
    // loadChannelAccounts 的短路当成"正在读",既不发新请求也永远结束不了,
    // 开始按钮就一直禁用着。
    pipeAccounts.delete(id);
  }
  pipeAcctAborts.clear();
  $('pipe-modal').style.display = 'none'; closePipeRange();
}
// 勾掉的来源不再占列表，但它的选择留在草稿里：再勾回来时还是上次那几个订阅。
// force=true 用在打开弹窗时(重读全部),勾选变化时不带,只读新勾上的那个。
async function loadPipeAccounts(force = false) {
  savePipeDraft();
  for (const id of [...pipeAccounts.keys()]) {
    if (pipeRunSel.includes(id)) continue;
    pipeAcctAborts.get(id)?.abort(); pipeAcctAborts.delete(id);
    pipeAccounts.delete(id); pipeSel.delete(id);
  }
  renderPipeAcctList();
  await Promise.all(pipeRunSel.map(id => loadChannelAccounts(id, force)));
}
async function loadChannelAccounts(channel, force = false) {
  if (!force && ['ready', 'loading'].includes(pipeAccounts.get(channel)?.state)) return;
  pipeAcctAborts.get(channel)?.abort();
  const controller = new AbortController();
  pipeAcctAborts.set(channel, controller);
  pipeAccounts.set(channel, { state: 'loading', list: [], error: '' });
  renderPipeAcctList();
  try {
    const data = await pipeJson(channelUrl('/api/pipeline/accounts', channel), { signal: controller.signal }, 60000);
    // 这一轮还算不算数：来源被勾掉了，或者又发起了一次更新的读取
    if (!pipeRunSel.includes(channel) || pipeAcctAborts.get(channel) !== controller) return;
    const list = [...new Set((data.accounts || []).filter(n => typeof n === 'string'))];
    const draft = pipeDrafts.get(channel);
    pipeAccounts.set(channel, { state: 'ready', list, error: '' });
    pipeSel.set(channel, new Set(draft ? draft.filter(n => list.includes(n)) : list));
    savePipeDraft();
  } catch (e) {
    if (!pipeRunSel.includes(channel) || pipeAcctAborts.get(channel) !== controller) return;
    pipeAccounts.set(channel, { state: 'error', list: [], error: e.message });
  } finally {
    if (pipeAcctAborts.get(channel) === controller) pipeAcctAborts.delete(channel);
    if (pipeModalOpen()) renderPipeAcctList();
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
  pipeRange = { ...pipePresets()[i] }; closePipeRange(); renderPipeRange(); $('pipe-range-btn').focus();
}
function applyPipeCustomRange() {
  const from = $('pipe-custom-from').value, to = $('pipe-custom-to').value;
  if (!from || !to || from > to) {
    $('pipe-custom-err').textContent = !from || !to ? '请填写开始和结束日期' : '开始日期不能晚于结束日期'; return;
  }
  pipeRange = { label: `${from} 至 ${to}`, from, to };
  closePipeRange(); renderPipeRange(); $('pipe-range-btn').focus();
}
// 区间补抓的注意事项每个来源各有各的（公众号翻不到 30 天前、RSS 补不齐历史），
// 勾了几个就把几条都说出来，只显示当前查看那一个会漏掉别人的限制。
function pipeRangeHints() {
  return [...new Set(pipeRunSel.map(id => pipeChannels.find(c => c.id === id)?.range_hint).filter(Boolean))].join(' ');
}
function renderPipeRange() {
  $('pipe-range-label').textContent = pipeRange.label;
  $('pipe-range-hint').textContent = pipeRange.from ? pipeRangeHints() || '包含开始与结束当天。日期只影响抓取范围，后续步骤仍处理知识库中的待处理内容。' : '扫描所选来源可提供的文章；已有内容按身份去重。后续步骤会处理知识库中的待处理内容。';
  renderPipeEst();
}
function pipeQuery() { return $('pipe-acct-search').value.trim().toLowerCase(); }
function filteredPipeAccounts(channel) {
  const query = pipeQuery();
  return (pipeAccounts.get(channel)?.list || []).filter(n => n.toLowerCase().includes(query));
}
function pipeChannelBlock(id) {
  const info = pipeAccounts.get(id) || { state: 'loading' }, label = esc(channelLabel(id));
  if (info.state === 'loading') return `<div class="pipe-acct-group"><div class="pipe-acct-group-head"><b>${label}</b><span>正在读取订阅…</span></div></div>`;
  if (info.state === 'error') {
    return `<div class="pipe-acct-group"><div class="pipe-acct-group-head"><b>${label}</b><span>读取失败</span></div>
      <div class="pipe-acct-empty">${esc(info.error)}<br><button class="ghost" onclick="loadChannelAccounts('${esc(id)}', true)">重新读取订阅</button></div></div>`;
  }
  const items = filteredPipeAccounts(id);
  const rows = items.map(n => `<label class="pipe-acct"><input type="checkbox" data-channel="${esc(id)}" data-acct="${esc(n)}"${pipeSelected(id).has(n) ? ' checked' : ''}><span>${esc(n)}</span></label>`).join('')
    || `<div class="pipe-acct-empty">${info.list.length ? '没有匹配的订阅，试试其他关键词。' : '这个来源暂无订阅，请先管理订阅或检查采集器。'}</div>`;
  return `<div class="pipe-acct-group"><div class="pipe-acct-group-head"><b>${label}</b><span data-group-count="${esc(id)}">已选 ${pipeSelected(id).size} / ${info.list.length}</span></div>${rows}</div>`;
}
// 勾一个订阅只改分组标题上那个计数。整块重渲染会把键盘焦点从刚勾中的复选框上弄掉,
// 连着勾第二个就得重新 Tab 过去。
function updateGroupCounts() {
  for (const el of $('pipe-acct-list').querySelectorAll('[data-group-count]')) {
    const info = pipeAccounts.get(el.dataset.groupCount);
    if (info?.state === 'ready') el.textContent = `已选 ${pipeSelected(el.dataset.groupCount).size} / ${info.list.length}`;
  }
}
function renderPipeAcctList() {
  const box = $('pipe-acct-list'), scroll = box.scrollTop;
  box.setAttribute('aria-busy', String(!!pipeLoadingChannels().length));
  box.innerHTML = pipeRunSel.length ? pipeRunSel.map(pipeChannelBlock).join('')
    : '<div class="pipe-acct-empty">先在上面勾选至少一个来源。</div>';
  box.scrollTop = scroll;
  updatePipeSelection();
}
function updatePipeSelection() {
  const total = pipeReadyChannels().reduce((n, id) => n + (pipeAccounts.get(id)?.list.length || 0), 0);
  const filtered = !!pipeQuery();
  $('pipe-acct-count').textContent = `已选 ${pipeSelCount()} / ${total}`;
  $('pipe-select-all').textContent = filtered ? '全选搜索结果' : '全选';
  $('pipe-select-none').textContent = filtered ? '取消搜索结果' : '清空选择';
  const visible = pipeReadyChannels().reduce((n, id) => n + filteredPipeAccounts(id).length, 0);
  $('pipe-select-all').disabled = !visible;
  $('pipe-select-none').disabled = !pipeSelCount();
  renderPipeEst(); savePipeDraft();
}
$('pipe-acct-list').addEventListener('change', e => {
  if (!e.target.matches('input[data-acct]')) return;
  const { channel, acct } = e.target.dataset;
  const selected = pipeSel.get(channel) || new Set();
  if (e.target.checked) selected.add(acct); else selected.delete(acct);
  pipeSel.set(channel, selected);
  updateGroupCounts(); updatePipeSelection();
});
function pipeSelectAll() {
  for (const id of pipeReadyChannels()) {
    const selected = pipeSel.get(id) || new Set();
    for (const n of filteredPipeAccounts(id)) selected.add(n);
    pipeSel.set(id, selected);
  }
  renderPipeAcctList();
}
function pipeSelectNone() {
  for (const id of pipeReadyChannels()) {
    const selected = pipeSel.get(id) || new Set();
    for (const n of filteredPipeAccounts(id)) selected.delete(n);
    pipeSel.set(id, selected);
  }
  renderPipeAcctList();
}
function pipeEstText() {
  const n = pipeSelCount(), failed = pipeFailedChannels().length;
  if (!pipeRunSel.length) return '请至少选择一个来源。';
  if (pipeLoadingChannels().length) return '订阅读取完成后即可开始。';
  if (!pipeReadyChannels().length) return '所选来源的订阅都没能读出来，请重新读取后再开始。';
  if (!n) return '请至少选择一个订阅。';
  const sources = pipeReadyChannels().filter(id => pipeSelected(id).size)
    .map(id => `${channelLabel(id)} ${pipeSelected(id).size} 个`).join(' · ');
  return `${sources} · ${pipeRange.label}。执行中可离开本页，稍后回来查看。`
    + (failed ? `另有 ${failed} 个来源读取失败，本次不会同步它们。` : '');
}
function renderPipeEst() {
  const editingRange = $('pipe-range-pop').style.display !== 'none';
  const connection = $('pipe-setup-connection'), reconnect = connection.querySelector('button');
  connection.hidden = pipeConnected && !pipeSnapshot?.running;
  connection.querySelector('span').textContent = !pipeConnected
    ? '尚未确认后台运行状态。可先配置，连接恢复且没有运行中的任务时才能开始同步。'
    : '已有同步正在后台执行。配置已保留，结束后可启动下一次同步。';
  reconnect.hidden = pipeConnected;
  reconnect.disabled = !!pipeRequest;
  reconnect.textContent = pipeRequest ? '正在连接…' : '重新连接';
  const n = pipeSelCount();
  $('pipe-est').textContent = pipeEstText();
  if (!pipeConnected) $('pipe-est').textContent = '请先恢复状态连接，再开始同步。';
  else if (pipeSnapshot?.running) $('pipe-est').textContent = '等待当前同步结束，配置会保留。';
  else if (!pipeChannelsFresh) $('pipe-est').textContent = '同步来源尚未确认，请重新读取来源后再开始。';
  if (editingRange) $('pipe-est').textContent = '请先应用日期范围，或收起日期选择以使用当前已选范围。';
  $('pipe-start').disabled = editingRange || !pipeConnected || !n || !!pipeAction || !!pipeSnapshot?.running
    || !!pipeLoadingChannels().length || !pipeChannelsFresh;
  $('pipe-start').textContent = pipeAction === 'start' ? '正在启动…' : n ? `开始同步 · ${n} 个` : '开始同步';
}
function pipeRunBody() {
  const channels = pipeReadyChannels()
    .filter(id => pipeSelected(id).size)
    .map(id => ({ channel: id, accounts: [...pipeSelected(id)] }));
  const body = { channels };
  if (pipeRange.from) Object.assign(body, { since_date: pipeRange.from, until_date: pipeRange.to });
  return body;
}
async function startPipeFromModal() {
  if ($('pipe-range-pop').style.display !== 'none' || !pipeConnected || pipeAction
      || !pipeChannelsFresh || !pipeSelCount() || pipeSnapshot?.running) return;
  const body = pipeRunBody();
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
