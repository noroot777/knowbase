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
async function openPipeModal() {
  if (pipeAction || pipeSnapshot?.running) return;
  dismissToastGroup('pipeline');
  $('pipe-modal').style.display = ''; $('pipe-fields').disabled = false;
  pipeSetupError(); $('pipe-acct-search').value = '';
  $('pipe-acct-list').innerHTML = '<p class="pipe-acct-empty">正在读取同步来源与订阅…</p>';
  renderPipeStages(); renderPipeEst();
  // **每次打开都重读一遍定时事实**:弹窗里「出完报接着整理图鉴和 Wiki」那句话是
  // 替后端许诺,而那一段跟着定时设置里的勾走(`pipeDailyLibrary`)。同步中心平时
  // 不读定时(读它的是「我的订阅」和日报页),不读就只能照"读不到"那一档说话 ——
  // 而这恰恰是最常见的入口。回包落地时 `watchSchedules` 会把这几句就地换掉。
  loadSchedules();
  if (!pipeConnected) refreshPipe();
  try {
    await loadPipeChannels();
    if (!pipeModalOpen()) return;
    // 每次打开都重读一遍订阅:上次看到的那份可能已经过期(期间添加/停用/移除过号)
    renderPipeChannel(); await loadPipeAccounts(true);
  } catch (e) {
    if (!pipeModalOpen()) return;
    $('pipe-acct-list').innerHTML = '<div class="pipe-acct-empty">同步来源暂不可用。<br><button class="ghost" onclick="openPipeModal()">重新读取来源</button></div>';
    pipeSetupError(e.message);
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
// 汇总那一句。**第一件事是念这次跑哪几段** —— 原来它只说来源和订阅,而步骤
// 是固定的六段,现在步骤可勾,「这次到底会发生什么」就得从这里读出来。
function pipeEstText() {
  if (!pipeStageList().length) return '一步都没勾，这次什么也不会发生。';
  const plan = pipeStagePlan(), tail = pipeStageTail();
  if (!pipeNeedsSource())
    return `${plan}。这几步都是全库的，上面的来源、时间范围和订阅这次都不作数。` + tail;
  const n = pipeSelCount(), failed = pipeFailedChannels().length;
  if (!pipeRunSel.length) return '请至少选择一个来源。';
  if (pipeLoadingChannels().length) return '订阅读取完成后即可开始。';
  if (!pipeReadyChannels().length) return '所选来源的订阅都没能读出来，请重新读取后再开始。';
  // 日报那条通道只认来源,所以这一档念的是来源名,不念订阅数和日期段(念了就是
  // 一句假话 —— 那两样它根本不收)。
  if (pipeDailyOn())
    return `${plan}。来源 ${pipeReadyChannels().map(channelLabel).join(' · ')}。` + tail;
  // 不抓取、只导出:**念来源,不念订阅数和日期段** —— 导出是按来源扫的(它那一步
  // 压根不收订阅和日期),念了就是一句假话。
  if (!pipeNeedsTargets())
    return `${plan}。来源 ${pipeReadyChannels().map(channelLabel).join(' · ')}`
      + '，时间范围和订阅这次不作数。' + tail;
  if (!n) return '请至少选择一个订阅。';
  const sources = pipeReadyChannels().filter(id => pipeSelected(id).size)
    .map(id => `${channelLabel(id)} ${pipeSelected(id).size} 个`).join(' · ');
  return `${plan}。${sources} · ${pipeRange.label}。执行中可离开本页，稍后回来查看。`
    + (failed ? `另有 ${failed} 个来源读取失败，本次不会同步它们。` : '') + tail;
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
  if (editingRange) $('pipe-est').textContent = '请先应用日期范围，或收起日期选择以使用当前已选范围。';
  // 能不能起跑由 `pipeStartBlocked()` 一处判(不勾抓取时订阅数本来就该不参与)
  $('pipe-start').disabled = editingRange || !pipeConnected || !!pipeStartBlocked()
    || !!pipeAction || !!pipeSnapshot?.running;
  const steps = pipeStageList().length;
  $('pipe-start').textContent = pipeAction === 'start' ? '正在启动…'
    : steps ? `开始同步 · ${steps} 步` : '开始同步';
}
function pipeRunBody() {
  const body = { stages: pipeStageList() };
  // **按步骤的实际依赖决定带不带来源**(`pipeNeedsSource` / `pipeNeedsTargets`,
  // 口径和后端 `run_scopes` 同一份):
  // - 全库那几步(索引/提取/整理/Wiki)一个来源字段都不带 —— 带上去只会让留档上
  //   那一轮看着像是按来源跑过;
  // - 抓取**或**导出只要有一个在跑就必须带来源:导出也是按来源跑的,不带的话后端
  //   回落到默认采集器,用户选的订阅源被静默换成公众号(第 3 轮审查 R1);
  // - 订阅名单和日期段只有抓取收,所以只导出那一轮**把来源整条带上、accounts 留空**
  //   (那就是「这个来源整条导出一遍」,也正是 `materialize()` 实际覆盖的范围)。
  if (!pipeNeedsSource()) return body;
  const targets = pipeNeedsTargets();
  body.channels = pipeReadyChannels()
    .filter(id => !targets || pipeSelected(id).size)
    .map(id => ({ channel: id, accounts: targets ? [...pipeSelected(id)] : [] }));
  if (targets && pipeRange.from)
    Object.assign(body, { since_date: pipeRange.from, until_date: pipeRange.to });
  return body;
}
async function startPipeFromModal() {
  if ($('pipe-range-pop').style.display !== 'none' || !pipeConnected || pipeAction
      || pipeStartBlocked() || pipeSnapshot?.running) return;
  // 勾了「跑完出日报」走的是日报那条通道(它自己排步骤、出完报接着整理图鉴与
  // Wiki);没勾才是单纯跑这几段管道。两条路的请求体不一样,别拼到一起去。
  const daily = pipeDailyOn();
  const path = daily ? '/api/daily/run' : '/api/pipeline/run';
  const body = daily ? { sync: true, channels: pipeReadyChannels() } : pipeRunBody();
  let approval = null;
  // 出报会盖掉当天那份（`daily.generate` 最后是整份替换文件，不可恢复）。
  // **走的是和日报页那颗按钮同一份确认**（`confirmDailyOverwrite`）；这条路不带
  // 区间，所以目标就是今天，而同步中心平时没读过日报清单 —— 交给它自己问一次。
  // 后端在落盘之前还会按这份批准核对一次（`kb/daily_overwrite.py`）：那一道管的是
  // 「问的是不是真正要写的那一份」，这一道管的是「问没问过」，两道都要。
  if (daily) {
    approval = await confirmDailyOverwrite(dailyToday());
    if (!approval) return;
    // 批准跟着请求下去:这条路要先跑几小时同步,目标日期是同步跑完才定下来的,
    // 浏览器这一刻的日期不是写文件时的目标身份(审查 R4)。
    Object.assign(body, { overwrite_day: approval.day,
                          overwrite_version: approval.version });
  }
  pipeAction = 'start'; pipeSetupError(); invalidatePipeStatus();
  let failure = '';
  $('pipe-fields').disabled = true; renderPipeEst(); renderPipeButtons();
  try {
    await pipePost(path, body);
    closePipeModal();
    toast(daily ? '同步已启动，跑完出日报' : '同步已启动',
          { key: 'pipeline', type: 'ok', detail: '任务在后台执行，可随时回来查看进度。' });
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
