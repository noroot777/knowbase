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
  // **锁定那一次一个字都不往同步中心那份记忆里写**(理由同 `applyPipeLock`)。
  // 不挡的话,点一次某条订阅的「同步」,这个来源记着的"上次勾了哪几个"就被改写成
  // 那一条 —— 下次从同步中心进来,这个来源只剩那一条还勾着,而没人说一声。
  if (pipeLock) return;
  for (const id of pipeReadyChannels()) pipeDrafts.set(id, [...pipeSelected(id)]);
}
function pipeSetupError(message = '') {
  $('pipe-setup-error').hidden = !message; $('pipe-setup-error').textContent = message;
}
// ── 锁定入口 ────────────────────────────────────────────────────────────────
// 「我的订阅」里点同步,弹的是同一个弹窗,但**这一次只跑你正在看的那一页**:
// 来源定死成它、其余灰掉点不动。两条理由:
// - 那一页的按钮本来就只管那一类来源,弹窗里却能勾别的,等于给了一个它从来没有
//   过的能力,而用户按"我点的是公众号那一页"理解这一轮;
// - 起跑请求里那个来源不是从弹窗勾选里来的,而是入口定的 —— 两边各判一遍就会岔。
// `{ channel, accounts }`:accounts 非空 = 连订阅也定死(每行那颗「同步」)。
// null = 同步中心的「新建同步」,六个来源随便勾。
let pipeLock = null;
// 同步中心那一份自己的配置,被锁定入口顶掉时**整份**存这儿,关窗时整份还回去。
// **必须整份,不许逐字段挑**:只还原「来源 + 步骤」的那一版漏掉了日报开关和
// `pipeSel`,而 `pipeSel` 的残留会在**下一次打开普通弹窗**时被 `loadPipeAccounts`
// 开头那句 `savePipeDraft()` 存进 `pipeDrafts`(那时锁已经解了,挡不住) ——
// 实测:同步中心勾好「博客甲、乙」,去订阅页点一下「博客丙」的同步再取消,
// 回同步中心只剩博客丙,点开始发出去的也只有博客丙(第 1 轮审查 R1)。
// **不能靠"下次打开再读一遍草稿"**:那份草稿的口径在 `applyChannelData` 里
// (退役名映射 + 认不出来的过滤 + 一个都没剩时补默认),在这儿再写一遍就是两份。
let pipeOwn = null;
function pipeLocked() { return pipeLock?.channel || ''; }
// 整份快照。`runSel` 这一格留到 `applyPipeLock()` 再填 —— 它要等通道目录落地、
// `applyChannelData` 按草稿重建完才算「同步中心那一份」(见下面那段)。
function stashPipeOwn() {
  if (pipeOwn) return;
  pipeOwn = {
    runSel: null,
    stageSel: new Set(pipeStageSel),
    stageKept: pipeStageKept ? new Set(pipeStageKept) : null,
    daily: $('pipe-daily').checked,
    sel: new Map([...pipeSel].map(([k, v]) => [k, new Set(v)])),
  };
}
// **上锁要等通道目录落地之后**:`loadPipeChannels()` 头一回会跑 `applyChannelData`,
// 那一步按 localStorage 里的草稿重建 `pipeRunSel` —— 在它之前上锁会被它盖掉,
// 实测点公众号那一页的「同步全部」提交的是「公众号 + RSS」两个来源
// (`tests/retired_channel_probe.mjs` 那三条)。
// **而且一个字都不许往草稿里写**:`kb-sync-channels` 是同步中心那一份多来源选择,
// 从「我的订阅」点一次就把它改写成单来源的话,下次新建同步少了几个来源,没人说。
function applyPipeLock() {
  if (!pipeLock) return;
  if (pipeOwn && pipeOwn.runSel === null) pipeOwn.runSel = [...pipeRunSel];
  pipeRunSel = [pipeLock.channel];
}
// 整份还回去。**日报开关也要还**:原来这里无条件置 false,于是「同步中心勾好
// 跑完出日报 → 去订阅页打开看一眼 → 关掉」就把那个勾取消了,连 `pipeStageKept`
// 里用户自选的那几段也一起清掉(第 1 轮审查 R1 实测 B)。
function releasePipeLock() {
  pipeLock = null;
  if (!pipeOwn) return;
  if (pipeOwn.runSel) pipeRunSel = pipeOwn.runSel;
  pipeStageSel = pipeOwn.stageSel;
  pipeStageKept = pipeOwn.stageKept;
  $('pipe-daily').checked = pipeOwn.daily;
  // `pipeSel` 是 const Map:清掉再填,不能整个换掉
  pipeSel.clear();
  for (const [k, v] of pipeOwn.sel) pipeSel.set(k, v);
  pipeOwn = null;
}
async function openPipeModal(lock = null) {
  // **不开也要说一声。** 原来这里是静默 return:同步中心那颗按钮在跑着时是禁用的,
  // 所以这条路走不到;而「我的订阅」那几颗现在也开这个弹窗,它们要等下一次轮询
  // 才知道"正在同步" —— 中间那段时间按下去什么也不发生,看着就像坏了。
  if (pipeAction || pipeSnapshot?.running) {
    toast('已有同步在跑', { key: 'pipeline', detail: '等它结束再开始下一次，进度在同步中心。' });
    return;
  }
  // 入口给的订阅归一成 `{id, name}`:**id 决定抓谁、name 只负责显示**(理由在
  // `loadChannelAccounts` 那段)。写字符串的老形态按「两者相同」收下。
  pipeLock = lock && lock.channel
    ? { channel: lock.channel, stages: [...(lock.stages || [])],
        accounts: (lock.accounts || []).map(a => typeof a === 'string'
          ? { id: a, name: a } : { id: String(a.id), name: a.name || String(a.id) }) }
    : null;
  if (pipeLock) {
    // **整份存下来再动**:下面要改的不只是步骤(还有日报开关、订阅勾选),
    // 关窗时整份还回去(`releasePipeLock`)。
    stashPipeOwn();
    // **入口定默认步骤**:从「我的订阅」进来默认跑入库那四段,不带整理工具和
    // 更新 Wiki —— 那两段是全库的、一跑几十分钟,而这个入口要的是「把我这一类
    // 的新内容收进来」。改之前公众号那颗按钮不带步骤清单,后端兜底成六段全跑,
    // 顺手跑掉几十分钟而页面上一个字都不说。默认归默认,**弹窗里照旧改得动**。
    if (pipeLock.stages?.length) {
      // **入口的默认只管入口自己**:同步中心那一份已经在 `pipeOwn` 里了,关窗
      // 原样还回去 —— 不存的话从「我的订阅」点一次,同步中心的「新建同步」就
      // 永远变成四段了,而那是用户自己挑的一份(探针反过来钉着这一条)。
      $('pipe-daily').checked = false;   // 日报是同步中心那一档的事
      pipeStageKept = null;
      pipeStageSel = new Set(pipeLock.stages);
    }
  }
  // 锁到具体几条订阅时名单必须摊开 —— 折着的那一行写的是「全部」,而这一次不是
  pipeAcctOpen = !!pipeLock?.accounts.length;
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
    applyPipeLock();
    // 每次打开都重读一遍订阅:上次看到的那份可能已经过期(期间添加/停用/移除过号)
    renderPipeChannel(); await loadPipeAccounts(true);
  } catch (e) {
    if (!pipeModalOpen()) return;
    $('pipe-acct-list').innerHTML = '<div class="pipe-acct-empty">同步来源暂不可用。<br><button class="ghost" onclick="openPipeModal(pipeLock)">重新读取来源</button></div>';
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
  releasePipeLock();
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
    const lockAccts = pipeLock?.channel === channel ? pipeLock.accounts : null;
    // 锁到具体几条订阅那一档(每行那颗「同步」):选择照入口摆,**不沿用上次的
    // 草稿** —— 沿用的话点「同步这一条」跑的是上次勾的那一批。名字还在这份名单
    // 里 = 这条订阅还在(都没了就一个都不勾,起跑那一关会拦住,不会静默跑成别的)。
    // 锁定「整条来源」那一档(accounts 空)**也不许看草稿**:它说的就是全部,
    // 读草稿会让「同步全部」实际只跑同步中心上次收窄过的那几个(第 1 轮审查 R1)。
    const want = lockAccts
      ? (lockAccts.length ? lockAccts.filter(a => list.includes(a.name)).map(a => a.name) : list)
      : (draft ? draft.filter(n => list.includes(n)) : list);
    pipeAccounts.set(channel, { state: 'ready', list, error: '' });
    pipeSel.set(channel, new Set(want));
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
    : '<div class="pipe-acct-empty">请先勾选至少一个来源。</div>';
  box.scrollTop = scroll;
  updatePipeSelection();
}
// ── 订阅名单折叠 ────────────────────────────────────────────────────────────
// 摊开的搜索框 + 列表占 280px,而九成情况这里就是「全部」。所以默认折成一行,
// **但收窄过的选择不许折** —— 折起来等于把「这次只跑其中几个」藏了,用户会按
// 「全部」理解这一轮,而面板上一个字都不说。用户自己点开的也记着(`pipeAcctOpen`)。
let pipeAcctOpen = false;
function togglePipeAcctFold() {
  pipeAcctOpen = !pipeAcctOpen;
  renderPipeAcctFold();
  if (pipeAcctOpen) $('pipe-acct-search').focus();
}
// 这一刻折不折得起来。**三种情况一律摊开**,各有各的理由:
// - **收窄过**(有来源没整条全选):折起来等于把「这次只跑其中几个」藏了,用户
//   会按「全部」理解这一轮,而面板上一个字都不说;
// - **有来源读失败**:「重新读取订阅」那颗按钮在列表里面,折着就没有出路;
// - **用户自己点开过**(`pipeAcctOpen`)。
// 正在读取**不算**摊开的理由 —— 那一下折着的行里就写着「正在读取订阅」,
// 摊开只会让弹窗先高一截、读完再塌回去。
function pipeAcctFoldable() {
  if (pipeFailedChannels().length) return false;
  return pipeReadyChannels().every(id =>
    pipeSelected(id).size === (pipeAccounts.get(id)?.list.length || 0));
}
function renderPipeAcctFold() {
  const block = $('pipe-acct-block'), canFold = pipeAcctFoldable();
  block.classList.toggle('open', pipeAcctOpen || !canFold);
  // 折不起来的那几档(收窄过、有来源读失败)连按钮都不摆:摆一颗按下去没反应的
  // 按钮,比没有更糟
  block.classList.toggle('stuck-open', !canFold);
  $('pipe-acct-fold').textContent = pipeAcctOpen ? '收起' : '挑几个';
  const ready = pipeReadyChannels(), loading = pipeLoadingChannels().length;
  $('pipe-acct-folded').innerHTML =
    !pipeRunSel.length ? '<span>请先勾选至少一个来源。</span>'
    : loading ? '<span>正在读取订阅…</span>'
    : ready.length ? `<span>${ready.map(id => `${esc(channelLabel(id))} 全部`).join(' · ')}</span>`
        + `<span class="n">${pipeSelCount()}</span>`
    : '<span>订阅还没读出来。</span>';
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
  renderPipeAcctFold();
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
// 汇总那一句。**这里只念这一次的事实,不再复述跑哪几段** —— 步骤就在右栏勾着,
// 同一份信息在一个弹窗里念两遍正是操作条占掉三行的由来(改之前整条 160px)。
// 两处例外,都不是复述:
// - **日报那一档照旧整句念**(`pipeStagePlan`):它的顺序是「出报插在中间」,而
//   右栏那一组是按 `PIPE_STAGE_DEFS` 平铺的,表达不了插在哪儿;少念了,用户就会
//   被出报之后那两段几十分钟的慢活偷袭。
// - **「含 N 段全库的慢活」那句(`pipeStageTail`)一档都不许省**:慢活标记在右栏
//   每个格子上,但"这一次一共有几段"只有这里数得出来。
function pipeEstText() {
  if (!pipeStageList().length) return '一步都没勾，这次什么也不会发生。';
  const tail = pipeStageTail();
  if (!pipeNeedsSource())
    return '这几步都是全库的，来源、时间范围和订阅这次都不作数。' + tail;
  const n = pipeSelCount(), failed = pipeFailedChannels().length;
  if (!pipeRunSel.length) return '请至少选择一个来源。';
  if (pipeLoadingChannels().length) return '订阅读取完成后即可开始。';
  if (!pipeReadyChannels().length) return '所选来源的订阅都没能读出来，请重新读取后再开始。';
  // 日报那条通道只认来源,所以这一档念的是来源名,不念订阅数和日期段(念了就是
  // 一句假话 —— 那两样它根本不收)。
  if (pipeDailyOn())
    return `${pipeStagePlan()}。来源 ${pipeReadyChannels().map(channelLabel).join(' · ')}。` + tail;
  // 不抓取、只导出:**念来源,不念订阅数和日期段** —— 导出是按来源扫的(它那一步
  // 压根不收订阅和日期),念了就是一句假话。
  if (!pipeNeedsTargets())
    return `来源 ${pipeReadyChannels().map(channelLabel).join(' · ')}`
      + '，时间范围和订阅这次不作数。' + tail;
  if (!n) return '请至少选择一个订阅。';
  const sources = pipeReadyChannels().filter(id => pipeSelected(id).size)
    .map(id => `${channelLabel(id)} ${pipeSelected(id).size} 个`).join(' · ');
  return `${sources} · ${pipeRange.label}。执行中可离开本页，稍后回来查看。`
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
// **锁到具体几条订阅时,请求里发的是稳定 id,不是名字。** 名字不唯一:两条地址
// 不同、名字相同的订阅,后端那道范围匹配(`reg.in_scope`「id 相等**或** name
// 相等」)会把同名的全部带上 —— 实测点第一条的「同步」,两条都被抓了
// (第 1 轮审查 R2)。显示仍用名字(勾选框、折起来那一行都按名字渲染),只有发
// 出去的那一份换成 id。
// **只换入口点名的那几条,别的照旧发名字,这不是将就** —— 那份名单是按名字去重
// 过的(`/api/pipeline/accounts` 只给名字),列表里「同名博客」那一行**代表的就是
// 两条**,勾它的意思就是两条都跑;换成某一条的 id 反而会漏掉另一条。所以:入口
// 点名「这一条」⇒ 发 id;在名单里勾一个名字 ⇒ 发名字。
function pipeStableTargets(channel, names) {
  const accts = pipeLock?.channel === channel ? pipeLock.accounts : null;
  if (!accts?.length) return names;
  const byName = new Map(accts.map(a => [a.name, a.id]));
  return names.map(n => byName.get(n) ?? n);
}
function pipeRunBody() {
  // **force:按下这颗按钮的是人,退避是给自动轮次用的。** 不带的后果是退避中的
  // 那几条被静默跳过(只记一笔 `deferred`),而面板上说「同步已开始」——
  // 「失败之后手动点一次同步」这条唯一出路要绕开的正是那道闸。
  const body = { stages: pipeStageList(), force: true };
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
    .map(id => ({ channel: id, accounts: targets ? pipeStableTargets(id, [...pipeSelected(id)]) : [] }));
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
    // 「我的订阅」那几颗按钮是从这个弹窗起跑的,起完要让那一页就地改口(否则它
    // 要等下一次轮询才显示「同步中…」,中间按钮看着还能点)。没在那一页时是空操作。
    if (typeof onPipeStarted === 'function') onPipeStarted();
    if (pipeSnapshot?.running) {
      if (pipeModalOpen()) closePipeModal();
      if (activeTab === 'pipe') $('pipe-state-title').focus({ preventScroll: true });
    }
    else if (failure) pipeSetupError(pipeConnected ? '同步未启动：' + failure + '。配置已保留，可以重试。' : '暂时无法确认任务是否启动，请先等待状态连接恢复。配置已保留。');
    renderPipeButtons(); renderPipeEst();
  }
}
