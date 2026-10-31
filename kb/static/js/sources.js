// The catalog supplies module identity; this shared workspace handles feed-backed sources.
const sourceModules = new Map();
// 「我的订阅」页:三类来源(公众号 / 大V观点 / RSS)并列,页内切换。
// subsCatalog 是完整目录(含非 feeds 的公众号),sourceModules 仍只收 feeds 那一半
// ——/api/intake/{kind} 那套接口只对 feeds 来源成立。
let subsCatalog = [];
let subsKind = '';
let subsPendingKind = '';   // switchTab 接到老深链(#social 等)时暂存的目标视图
try { subsKind = localStorage.getItem('kb-subs-kind') || ''; } catch {}
let sourceKind = '', sourceView = 'items', sourceData = null, sourceRows = [];
let sourceOffset = 0, sourceTotal = 0, sourceVersion = 0, sourcePoll = null, sourceSearchTimer;
let sourceItemsVersion = 0, sourceItemsFailed = false, sourceTranslating = 0, sourceFetching = 0;
// 后端给的「译不动」那一档(kb/feeds/translate.py 的 stuck):failed 条试过至少一次
// 还没译出来、其中 shipped 条已经试满额度按原文入库了,reason 是最近一次的原因。
// 它**不跟搜索词走**(见后端注释):这行提示底下那颗按钮的射程得可预测。
let sourceBusy = false;
let sourceNoticesAutoOpened = false;
let wechatNoticesAutoOpened = false;
let sourceNoticeGesture = null;
const sourceFilters = new Map();
const sourceViewState = new Map();

async function initSourceModules() {
  try {
    const data = await pipeJson('/api/intake/modules');
    const modules = Array.isArray(data.modules) ? data.modules : [];
    subsCatalog = modules;
    for (const spec of modules.filter(m => m.manager === 'feeds')) sourceModules.set(spec.id, spec);
  } catch {
    // 目录读不到不拦整个页面:打开「我的订阅」时 openSubsPanel 会重试一次,
    // 失败原因显示在页内横幅上,其他面板照常可用。
  }
}

async function openSubsPanel(kind = '') {
  if (kind) subsKind = kind;
  if (!subsCatalog.length) {
    $('subs-error').hidden = true;
    await initSourceModules();
    if (!subsCatalog.length) {
      $('subs-switch').innerHTML = '';
      $('subs-wechat').hidden = true;
      $('subs-feeds').hidden = true;
      applySubsReadingLayout();
      $('subs-error').hidden = false;
      $('subs-error-text').textContent = '来源目录暂时读不到，请确认服务后重试。';
      return;
    }
  }
  if (!subsCatalog.some(m => m.id === subsKind)) subsKind = subsCatalog[0].id;
  $('subs-error').hidden = true;
  try { localStorage.setItem('kb-subs-kind', subsKind); } catch {}
  renderSubsSwitch();
  const feeds = sourceModules.has(subsKind);
  $('subs-wechat').hidden = feeds;
  $('subs-feeds').hidden = !feeds;
  applySubsReadingLayout();
  if (feeds) {
    openSourceModule(subsKind);
  } else {
    // 离开 feeds 视图就停掉它的轮询;sourceKind 清空让 sourceActive() 收假
    clearTimeout(sourcePoll);
    clearTimeout(sourceSearchTimer);
    sourceKind = '';
    openWechatSubs();
  }
}

function applySubsReadingLayout() {
  const active = typeof activeTab !== 'undefined' && activeTab === 'subs';
  const on = active
    && sourceModules.has(subsKind) && !$('subs-feeds').hidden && sourceView === 'items';
  document.body.classList.toggle('subs-mode', active);
  document.body.classList.toggle('subs-reading', on);
  // 阅读区收起的那条工具栏跟着对一遍账。**是对账不是复位**:这个函数后台同步完
  // 一轮也会走到,一刀切复位就成了「读着读着工具栏自己弹回来」(实测收起约 4.6s
  // 后恢复,正文从 886 缩回 826)。判据在 scrollaway.js 的 syncScrollAway()。
  if (typeof syncScrollAway === 'function') syncScrollAway();
}

function renderSubsSwitch() {
  const focused = $('subs-switch').contains(document.activeElement);
  $('subs-switch').innerHTML = subsCatalog.map(m => `<button role="tab" id="subs-switch-${esc(m.id)}"
    class="${m.id === subsKind ? 'on' : ''}" aria-selected="${m.id === subsKind}"
    tabindex="${m.id === subsKind ? '0' : '-1'}" aria-controls="${m.manager === 'feeds' ? 'subs-feeds' : 'subs-wechat'}"
    onclick="openSubsPanel('${esc(m.id)}')">${icon(m.icon)}<span>${esc(m.label)}</span></button>`).join('');
  const selected = $('subs-switch-' + subsKind);
  if (focused) selected?.focus({ preventScroll:true });
  if (selected) {
    const box = selected.getBoundingClientRect(), parent = $('subs-switch').getBoundingClientRect();
    if (box.left < parent.left) $('subs-switch').scrollLeft -= parent.left - box.left;
    if (box.right > parent.right) $('subs-switch').scrollLeft += box.right - parent.right;
  }
}

$('subs-switch').addEventListener('keydown', event => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  const current = subsCatalog.findIndex(module => 'subs-switch-' + module.id === event.target.id);
  if (current < 0) return;
  event.preventDefault();
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? subsCatalog.length - 1
    : (current + (event.key === 'ArrowRight' ? 1 : -1) + subsCatalog.length) % subsCatalog.length;
  openSubsPanel(subsCatalog[next].id);
});

function closeWechatNotices(restoreFocus = false) {
  const notices = $('subs-wechat-notices');
  const focused = restoreFocus && notices.contains(document.activeElement);
  notices.open = false;
  notices.classList.remove('is-auto-open');
  wechatNoticesAutoOpened = false;
  if (focused) notices.querySelector('summary').focus({ preventScroll:true });
}
function updateWechatNotices(detailChanged = false) {
  const detail = !$('src-detail').hidden, running = !$('subs-wechat-progress').hidden;
  const notices = $('subs-wechat-notices'), summary = $('subs-wechat-notices-summary');
  notices.hidden = !detail && !running;
  if (notices.hidden || (!detail && wechatNoticesAutoOpened)) closeWechatNotices();
  notices.classList.toggle('has-error', detail && $('src-detail').classList.contains('error'));
  summary.textContent = detail ? $('src-detail').textContent : $('subs-wechat-progress-text').textContent;
  summary.title = summary.textContent;
  if (detail && detailChanged) {
    if (!notices.open) wechatNoticesAutoOpened = true;
    notices.open = true;
  }
  notices.classList.toggle('is-auto-open', wechatNoticesAutoOpened && notices.open);
}

// ── 公众号视图:连接卡 + 订阅列表 + 同步全部 ────────────────────────────────
let subsWechatBusy = false;
// 这个视图管的通道由**来源目录**决定(模块声明的 channels,退役名过别名表),
// 不信 `kb-sync-channel` 里那份全局选择 —— 旧版同步中心用它记「上次看哪个来源的
// 连接状态」,存量值可能是 rss/social(都是合法通道,别名迁移不会动它们)。
// 沿用的话公众号视图的连接卡、订阅列表、「同步全部」全跟着串到别的来源上。
function wechatSubsChannel() {
  const spec = subsCatalog.find(m => m.id === subsKind && m.manager !== 'feeds');
  return adoptChannels(spec?.channels || []).find(id => pipeChannels.some(c => c.id === id)) || '';
}
async function openWechatSubs() {
  renderWechatSubsStatus();
  // 失败要留重试入口:初始化失败后通道还没绑上,这时唯一能把视图带回正轨的
  // 就是重新跑一遍本函数(连接卡那颗「检查连接」按的也是它)。
  const retry = '<button class="text-button" onclick="openWechatSubs()">重新读取</button>';
  try { await loadPipeChannels(); } catch (e) {
    refreshPipeSource();   // 连接卡如实落「连接未确认」,不许停在「检查中…」
    $('tgt-list').innerHTML = `<div class="empty">${esc(e.message)}。${retry}</div>`;
    return;
  }
  const channel = wechatSubsChannel();
  if (!channel) {
    refreshPipeSource();
    $('tgt-list').innerHTML = `<div class="empty">这个来源的采集通道暂不可用，请确认采集器配置后重试。${retry}</div>`;
    return;
  }
  // 绑到全局选择上:channelUrl(连接卡、订阅列表的请求)和运行态判断都读它
  setPipeChannel(channel);
  renderWechatSubsStatus();
  refreshPipeSource();
  loadTargets();
}
// 同步状态由 renderPipeButtons 顺手驱动(pipe.js 的轮询回路),这里只管画。
function renderWechatSubsStatus() {
  const section = $('subs-wechat');
  if (!section || section.hidden) return;
  const running = !!pipeSnapshot?.running;
  const mine = wechatSubsChannel();
  // 整份快照递进去，不是只递 crawl_filter：一轮只跑 index/extract 的运行里，
  // filter 上那个来源名是 `start_pipeline` 自动盖的，照着念会把公众号说成正在同步。
  const ours = running && !!mine && pipeRunScopes(pipeSnapshot).some(s => s.channel === mine);
  $('subs-wechat-sync').disabled = subsWechatBusy || running || !!pipeAction;
  $('subs-wechat-sync').textContent = ours ? '同步中…' : '同步全部';
  $('subs-wechat-progress').hidden = !running;
  // 全库那一轮(索引/提取)不归任何来源,但它做的正是这些内容 —— 不能跟
  // 「别的来源在抓取」共用一句话。判据和文案都在 pipe_channels.js。
  $('subs-wechat-progress-text').textContent = ours
    ? pipeRunNote(pipeSnapshot, '正在同步公众号')
    : pipeLibraryOnly(pipeSnapshot)
    ? pipeRunNote(pipeSnapshot) + '，新任务会顺延。'
    : '其他来源正在同步，新任务会顺延。';
  updateWechatNotices();
}
// 公众号那一页的「同步全部」:**也走起跑弹窗**,锁定在本机绿泡泡那条通道上。
// 改之前它直接起跑而**不带步骤清单**,被后端兜底成六段全跑 —— 顺手跑掉几十分钟的
// 全库整理和 Wiki,页面上一个字都不说(`docs/decisions/sync-entry.md`)。
async function syncWechatSubs() {
  if (subsWechatBusy || pipeSnapshot?.running) return;
  subsWechatBusy = true; renderWechatSubsStatus();
  try {
    await loadPipeChannels();
    // 提交的通道同样按来源目录现解析,不拿全局选择当然值
    const channel = wechatSubsChannel();
    if (!channel) throw new Error('这个来源的采集通道暂不可用，请确认采集器配置后重试');
    setPipeChannel(channel);
    await openPipeModal({ channel, accounts: [], stages: PIPE_INTAKE_STAGES });
  } catch (e) { toast('未能打开同步', { detail: e.message }); }
  finally { subsWechatBusy = false; renderWechatSubsStatus(); }
}

async function openSourceModule(kind) {
  rememberSourceView();
  clearTimeout(sourcePoll);
  clearTimeout(sourceSearchTimer);
  sourceKind = kind; sourceVersion++; sourceItemsVersion++; sourceItemsFailed = false;
  sourceData = null; sourceRows = []; sourceOffset = 0;
  resetSourceReader('正在读取内容…');
  closeSourceNotices();
  sourceError();
  const spec = sourceModules.get(kind);
  const filter = sourceFilters.get(kind) || { view: 'items', q: '', id: '' };
  sourceView = filter.view;
  updateSourceReadingView();
  $('source-title').textContent = spec.label;
  $('source-note').textContent = spec.note;
  // 不可轮询的那一类(手动收录)把「添加订阅」换成投递栏,见 grab.js
  applyGrabBar(spec);
  $('source-q').value = filter.q;
  $('source-filter').innerHTML = '<option value="">全部订阅</option>';
  $('source-summary').textContent = '正在读取来源…';
  $('source-list').innerHTML = '<div class="source-empty">正在读取内容…</div>';
  $('source-settings-form').reset();
  $('source-settings-fields').disabled = true;
  await loadSourceModule(true, filter.id);
}

function sourceError(message = '') {
  const changed = $('source-error-text').textContent !== message;
  $('source-error').hidden = !message;
  $('source-error-text').textContent = message;
  updateSourceNotices();
  if (message && changed) {
    if (!$('source-notices').open) sourceNoticesAutoOpened = true;
    $('source-notices').open = true;
  }
  $('source-notices').classList.toggle('is-auto-open', sourceNoticesAutoOpened && $('source-notices').open);
}
function closeSourceNotices(restoreFocus = false) {
  const notices = $('source-notices');
  const focused = restoreFocus && notices.contains(document.activeElement);
  notices.open = false;
  notices.classList.remove('is-auto-open');
  sourceNoticesAutoOpened = false;
  if (focused) notices.querySelector('summary').focus({ preventScroll:true });
}
function dismissSourceNotice(noticesId, event) {
  const notices = $(noticesId), gesture = sourceNoticeGesture;
  sourceNoticeGesture = null;
  const control = event.target.closest('button, a, input, select, textarea');
  if (!control && event.detail > 0 && gesture?.notices === notices && gesture.textSelected) return;
  if (noticesId === 'source-notices') closeSourceNotices(true);
  else closeWechatNotices(true);
  if (control) return;
  const field = document.elementFromPoint(event.clientX, event.clientY);
  if (field?.matches('input, select, textarea') && !field.disabled) field.focus({ preventScroll:true });
}
function trackSourceNoticeGesture(event) {
  const gesture = sourceNoticeGesture;
  if (!gesture || gesture.pointerId !== event.pointerId) return;
  if (event.type === 'pointercancel') { sourceNoticeGesture = null; return; }
  if (event.clientX !== gesture.x || event.clientY !== gesture.y) gesture.moved = true;
  if (Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y) >= 4) gesture.dragged = true;
  if (event.type !== 'pointerup') return;
  const selection = window.getSelection();
  gesture.textSelected = gesture.moved && (gesture.dragged || gesture.startsOnText) && selection && !selection.isCollapsed
    && gesture.notices.contains(selection.anchorNode) && gesture.notices.contains(selection.focusNode);
}
function sourceNoticeStartsOnText(body, event) {
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT), range = document.createRange();
  for (let text = walker.nextNode(); text; text = walker.nextNode()) {
    if (!text.textContent.trim()) continue;
    range.selectNodeContents(text);
    if ([...range.getClientRects()].some(box => event.clientX >= box.left && event.clientX <= box.right
      && event.clientY >= box.top && event.clientY <= box.bottom)) return true;
  }
  return false;
}
function updateSourceNotices() {
  const error = !$('source-error').hidden, running = !$('source-progress').hidden;
  const issues = !$('source-issues').hidden, configuration = !$('source-configuration').hidden;
  $('source-notices').hidden = !error && !running && !issues && !configuration;
  if ($('source-notices').hidden || (!error && sourceNoticesAutoOpened)) closeSourceNotices();
  $('source-notices').classList.toggle('has-error', error || issues);
  $('source-notices-summary').textContent = error ? '读取失败，点击查看原因'
    : running ? $('source-progress-text').textContent : issues ? '来源连接需处理' : '部分订阅待配置';
  // 这块浮层挂在会收起的那条工具栏里。**报错出现时得先把工具栏放出来** ——
  // 收起态是 height:0 + inert,「读取失败」和它那颗「重新读取」就都走不到人跟前
  // 了。所有改提示状态的路都汇到这个函数,所以闸只在这里拨一次(口径只有一份)。
  if (typeof syncScrollAway === 'function') syncScrollAway();
}
function sourceTime(value) {
  if (!value) return '尚未同步';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' });
}
// feeds 视图还亮着才继续轮询/渲染:tab 在订阅页、页内选的就是这个来源
function sourceActive() { return activeTab === 'subs' && subsKind === sourceKind && sourceModules.has(sourceKind); }

// 「出现了一轮新的已结束运行,而且它带着本来源」—— 口径同 `pipe.js` 那个
// 待处理计数:**不能只认 running→idle 那个跳变**,这条路一次模型都不调的情况下
// 整轮可能几百毫秒,面板一次都没见过 `running=true`。认 `run_id` 而不是
// `started_at`(后者只精确到秒,治不了这一档)。
let sourceSeenRun = '';
function sourceRunFinished(reset) {
  const snap = pipeSnapshot;
  // **来源要从同一份终态上问**(`pipeResult`,同步中心和历史清单读的就是它)。
  // 顶层那几格只是「这个进程此刻的状态」:恢复回来的那一轮里没有来源结论(历史
  // 故意不存导出范围),整批汇总那一轮里顶层又只剩最后那一小轮的范围 —— 两档都
  // 让这一页认不出「刚跑完那一轮跟我有关」(形态在
  // `docs/decisions/retry-scope.md` 的「消费方一律从同一份终态上问」)。
  const run = snap ? pipeResult(snap) : null;
  const id = (snap && !snap.running && run && run.run_id) || '';
  // **头一次进来(或换来源)看到的那一轮是历史记录,不是"刚刚跑完"** —— 那时
  // 本来就会整份重渲染。**吸收它的时点是 `reset` 自己,不是「至今见过 run_id
  // 没有」**:面板打开时管道多半正空转,状态里压根没有 run_id,那个问法会让守卫
  // 一直武装着、直到第一次真见到一个 —— 而那一次恰恰就是要认的那一轮(实测
  // 这一档整组红:提示不消失、条目也不重读)。
  if (reset) { sourceSeenRun = id; return false; }
  if (!id || id === sourceSeenRun) return false;
  sourceSeenRun = id;
  return pipeRunScopes(run).some(s => s.channel === sourceKind);
}

async function loadSourceModule(reset = false, selectedId = null) {
  if (!sourceActive()) return;
  const kind = sourceKind, version = ++sourceVersion;
  try {
    const data = await pipeJson('/api/intake/' + kind);
    if (version !== sourceVersion || kind !== sourceKind || !sourceActive()) return;
    // **「这一轮跑完了」要自己认一道,不能只比 `sources` 和 `total`** ——
    // 重试那条路不跑采集:条目一条没新增(`total` 不变)、订阅行的
    // `last_success` 也不动(那是采集阶段写的),于是两个判据对它都是瞎的,
    // 而真正变了的恰恰是条目上那几档翻译状态和下面那行计数。
    const finished = sourceRunFinished(reset);
    const changed = !sourceData || JSON.stringify(data.sources) !== JSON.stringify(sourceData.sources) || data.total !== sourceData.total;
    sourceData = data;
    sourceError();
    $('source-summary').textContent = `${data.enabled} / ${data.sources.length} 个订阅启用 · 已收录 ${data.total} 条`;
    const selected = selectedId ?? $('source-filter').value;
    $('source-filter').innerHTML = '<option value="">全部订阅</option>' + data.sources.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
    $('source-filter').value = data.sources.some(s => String(s.id) === selected) ? selected : '';
    $('source-issues').hidden = !data.errors;
    $('source-issues').innerHTML = `${esc(String(data.errors))} 个来源上次连接失败，已安排退避重试。查看订阅管理${arrow('right')}`;
    $('source-configuration').hidden = !data.unconfigured;
    // 理由由后端按**当前通道**算好(feedgrab 要登录态、浏览器要在那个独立
    // profile 里登录过、attach 要那个调试开关),这里不自己拼某一档的前提 ——
    // 选了别的通道的人
    // 看到的会是一个跟他这一轮采集毫无关系的设置。
    const why = (data.sources.find(s => s.configuration_error)?.configuration_error || '')
      .replace(/^待配置[：:]\s*/, '');
    $('source-configuration').textContent = `${data.unconfigured} 个账号待配置：`
      + (why ? why + '。' : '') + '已配置的来源可照常同步。';
    $('source-subscriptions-count').textContent = data.sources.length;
    if (reset) fillSourceSettings(data.settings);
    const run = data.last_run;
    $('source-last-run').textContent = run ? `上次检查 ${sourceTime(run.finished_at)} · 新增 ${run.summary.added} 条${run.summary.errors.length ? ' · ' + run.summary.errors.length + ' 个来源失败' : ''}${run.summary.message ? ' · ' + run.summary.message : ''}` : '尚未同步。';
    // 显式把已加载条数传过去。**这一处今天是冗余带,如实记着**:
    // `renderSourceView` 不给 `visibleCount` 时自己会回落到 `sourceOffset`,而
    // 它和 `sourceRows.length` 在这条路上恒等(两者一起累加)—— 定点注入去掉这
    // 个实参,探针红 0 条。留着是为了把"重读要保留已加载的那些"写在看得见的
    // 地方:那个回落值换个人改一下,这条路就静默缩回默认一屏了。
    if (reset || changed || sourceItemsFailed || finished)
      await renderSourceView(reset, reset ? 0 : sourceRows.length);
    updateSourceRunStatus();
  } catch (e) {
    if (version === sourceVersion && kind === sourceKind && sourceActive()) sourceError(e.message);
  } finally {
    if (sourceActive()) queueSourcePoll();
  }
}
// 起跑弹窗起完一次跑会叫它(`pipe_setup.js` 的 `startPipeFromModal`)。
// 不在「我的订阅」那几页时是空操作。
function onPipeStarted() {
  if (typeof renderWechatSubsStatus === 'function') renderWechatSubsStatus();
  if (!sourceActive()) return;
  updateSourceRunStatus(); queueSourcePoll();
}
function queueSourcePoll() {
  clearTimeout(sourcePoll);
  sourcePoll = setTimeout(() => {
    if (!sourceActive()) return;
    updateSourceRunStatus();
    loadSourceModule(false);
  }, pipeSnapshot?.running ? 4000 : 15000);
}
function updateSourceRunStatus() {
  const running = pipeSnapshot?.running;
  // 一次同步可能带上好几个来源，本来源在不在这一批里决定了显示「同步中」还是「顺延」
  const ours = running && pipeRunScopes(pipeSnapshot).some(scope => scope.channel === sourceKind);
  $('source-sync').disabled = sourceBusy || !!running || !sourceData?.ready;
  // 手动收录没有可轮询的源:这颗按钮在那一类里跑的是后半条链(把已经收下、
  // 还没落 vault 的那几条写进知识库),所以文案跟着换 —— 写「同步全部」会让人
  // 以为它会去哪儿再抓一遍。
  const manual = sourceModules.get(sourceKind)?.pollable === false;
  $('source-sync').textContent = manual
    ? (ours ? '写入中…' : '写入知识库')
    : (ours ? '同步中…' : '同步全部');
  $('source-progress').hidden = !running;
  $('source-progress-text').textContent = ours
    ? pipeRunNote(pipeSnapshot, '正在同步')
    : pipeLibraryOnly(pipeSnapshot)
    ? pipeRunNote(pipeSnapshot) + '，自动任务会顺延。'
    : '其他来源正在同步，自动任务会顺延。';
  $('source-list').querySelectorAll('[data-source-action]').forEach(b => {
    const source = sourceData?.sources.find(s=>s.id===Number(b.dataset.sourceId));
    const paused = b.dataset.sourceAction === 'sync' && (!source?.enabled || source?.configuration_error);
    b.disabled = sourceBusy || !!running || paused;
  });
  $('source-add').disabled = sourceBusy || !!ours;
  updateGrabControls();
  updateSourceNotices();
}
function rememberSourceView() {
  if (!sourceKind) return;
  sourceViewState.set(sourceKind + ':' + sourceView, {
    q:$('source-q').value, id:$('source-filter').value,
    listScroll:$('source-list-pane').scrollTop, readerScroll:$('source-reader-scroll').scrollTop,
    loaded:sourceOffset,
  });
}
async function switchSourceView(view) {
  if (!['items', 'manage'].includes(view) || sourceView === view) return;
  clearTimeout(sourceSearchTimer);
  rememberSourceView();
  const kind = sourceKind, saved = sourceViewState.get(kind + ':' + view);
  sourceView = view; sourceOffset = 0;
  closeSourceNotices();
  $('source-q').value = saved?.q || '';
  $('source-filter').value = saved?.id || '';
  updateSourceReadingView();
  try {
    const request = renderSourceView(true, saved?.loaded || 0), version = sourceItemsVersion;
    await request;
    if (saved && sourceKind === kind && sourceView === view && sourceItemsVersion === version) {
      $('source-list-pane').scrollTop = saved.listScroll;
      $('source-reader-scroll').scrollTop = saved.readerScroll;
    }
  } catch (e) { if (sourceKind === kind && sourceView === view) sourceError(e.message); }
}
function updateSourceFilterControls() {
  const manage = sourceView === 'manage';
  $('source-view-items').classList.toggle('on', !manage);
  $('source-view-manage').classList.toggle('on', manage);
  $('source-view-items').setAttribute('aria-pressed', String(!manage));
  $('source-view-manage').setAttribute('aria-pressed', String(manage));
  $('source-q').placeholder = manage ? '搜索名称、链接或分组…' : '搜索已收录的内容…';
  $('source-q').setAttribute('aria-label', manage ? '搜索订阅名称、链接或分组' : '搜索来源内容');
  $('source-filter').hidden = manage;
  $('source-list-expand').hidden = manage;
  $('source-filter-reset').hidden = !$('source-q').value.trim() && (manage || !$('source-filter').value);
}
function clearSourceFilters() {
  $('source-q').value = '';
  $('source-filter').value = '';
  sourceFilterChanged();
  $('source-q').focus({ preventScroll:true });
}
function sourceFilterChanged() {
  clearTimeout(sourceSearchTimer);
  updateSourceFilterControls();
  sourceItemsVersion++; // Invalidate responses immediately, including during debounce.
  if (sourceView === 'items') resetSourceReader('正在筛选内容…');
  sourceSearchTimer = setTimeout(() => renderSourceView(true).catch(e => sourceError(e.message)), 220);
}
async function renderSourceView(reset = false, visibleCount = 0) {
  if (typeof renderYouTubeRanking === 'function') renderYouTubeRanking();
  if (!sourceData) return;
  const kind = sourceKind, version = ++sourceItemsVersion;
  $('source-more').hidden = true;
  $('source-more').disabled = false;
  updateSourceReadingView();
  const q = $('source-q').value.trim(), id = $('source-filter').value;
  sourceFilters.set(kind, { view: sourceView, q, id });
  if (sourceView === 'manage') {
    sourceItemsFailed = false;
    const rows = sourceData.sources.filter(s => (s.name+' '+s.url+' '+s.group_name).toLowerCase().includes(q.toLowerCase()));
    $('source-list').innerHTML = rows.map(renderSubscription).join('') || `<div class="source-empty"><h3>${q ? '没有匹配的订阅' : '还没有订阅'}</h3><button class="ghost" ${q ? 'data-clear-source-filters' : 'data-add-source'}>${q ? '清除筛选' : '添加订阅'}</button></div>`;
    $('source-items-count').textContent = `${rows.length} / ${sourceData.sources.length} 个`;
    if (reset) $('source-list-pane').scrollTop = 0;
    updateSourceRunStatus();
    return;
  }
  const count = visibleCount || (!reset && sourceOffset ? sourceOffset : 30);
  // Reload the visible window to avoid duplicates when a sync inserts new entries.
  if (reset) {
    $('source-list').innerHTML = '<div class="source-empty">正在读取内容…</div>';
    $('source-list-pane').scrollTop = 0;
  }
  try {
    const data = await pipeJson(`/api/intake/${kind}/items?limit=${Math.min(count,100)}&offset=0&q=${encodeURIComponent(q)}${id ? '&source_id='+id : ''}`);
    if (!currentSourceItems(kind, version, q, id)) return;
    sourceItemsFailed = false;
    sourceRows = data.items; sourceOffset = data.items.length; sourceTotal = data.total;
    sourceTranslating = data.translating || 0; sourceFetching = data.fetching || 0;
    sourceStuck = data.stuck || null; sourceTranslateReady = data.translate_ready !== false;
    renderSourceItems();
  } catch (e) {
    if (!currentSourceItems(kind, version, q, id)) return;
    sourceItemsFailed = true;
    resetSourceReader('内容读取失败，请重新读取。');
    $('source-list').innerHTML = `<div class="source-empty" role="alert"><h3>内容读取失败</h3><p>${esc(e.message)}</p><button class="ghost" data-retry-items>重新读取</button></div>`;
  }
}
function currentSourceItems(kind, version, q, id) {
  return kind === sourceKind && version === sourceItemsVersion && sourceActive() && sourceView === 'items'
    && q === $('source-q').value.trim() && id === $('source-filter').value;
}
// 空态那句「要先配置什么」按后端给的理由写,不写死某一档:选了浏览器
// 登录态通道的人看到另一档的前提,说的是跟他这一轮采集毫无关系的一个
// 设置(口径同 `registry.configuration_error`,它已经按当前通道分支了)。
function sourceConfigHint() {
  const why = (sourceData?.sources || []).map(s => s.configuration_error).find(Boolean) || '';
  const reason = why.replace(/^待配置[:：]\s*/, '').replace(/[。；;，,]$/, '');
  return reason ? `${reason}；配置好之后再开始同步。` : '这些账号还要先配置，配置好之后再开始同步。';
}
function renderSubscription(s) {
  // `last_note` = 上一轮同步成功但**没读完**:浏览器通道滚不到上次的位置、
  // feedgrab 翻页中途失败或者那一批里有几条没抓下来 —— 两条通道都会留下这一句。
  // 它排在 last_error 之后、已同步之前:抓到的条目确实入库了,但把它显示成
  // 「已同步」就等于宣布这一轮读完了 —— 而时间线下面那一段还没读到。
  const status = !s.enabled ? '已停用' : s.configuration_error ? '待配置' : s.last_error ? '连接失败' : s.last_note ? '部分同步' : s.last_notice ? '已同步（有跳过）' : s.last_success ? '已同步' : '待首次同步';
  return `<article class="source-subscription ${!s.enabled ? 'is-paused' : ''}">
    <div class="source-avatar" aria-hidden="true">${esc(s.name.slice(0,1))}</div>
    <div class="source-subscription-body"><div class="source-row-title"><h3>${esc(s.name)}</h3><span class="source-state ${s.enabled && s.last_error ? 'failed' : ''}">${status}</span></div>
    <a class="source-url" href="${esc(safeUrl(s.url))}" title="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.url)}</a>
    <div class="source-row-meta">${esc(s.group_name || '未分组')} · ${s.item_count} 条 · ${s.last_success ? '最近成功 '+sourceTime(s.last_success) : '尚未成功同步'}${s.fulltext && sourceModules.get(s.kind)?.body_from_link ? ' · 补抓原文正文' : ''}${s.extract ? ' · 提取知识标签' : ''}</div>
    ${s.configuration_error ? `<p class="source-row-meta">${esc(s.configuration_error)}</p>` : ''}
    ${!s.last_error && s.last_note ? `<p class="source-row-meta">${esc(s.last_note)}；下次同步从上次的位置接着读。</p>` : ''}
    ${!s.last_error && s.last_notice ? `<p class="source-row-meta source-skip-notice">${esc(s.last_notice)}</p>` : ''}
    ${s.last_error ? `<details class="source-failure"><summary>查看失败原因与重试时间</summary><p>${esc(s.last_error)}</p><p>${sourceTime(s.retry_at*1000)} 之后的同步才会再试它；也可点击“同步”立即重试。</p></details>` : ''}</div>
    <div class="source-row-actions"><button class="text-button" data-source-action="edit" data-source-id="${s.id}">编辑</button><button class="text-button" data-source-action="toggle" data-source-id="${s.id}">${s.enabled ? '停用' : '启用'}</button><button class="text-button" data-source-action="sync" data-source-id="${s.id}" ${!s.enabled?'disabled':''}>同步</button><button class="text-button source-delete" data-source-action="delete" data-source-id="${s.id}">删除</button></div></article>`;
}
// 外文条目在入库前已经译成中文(kb/feeds/translate.py),面板一律显示译文:
// vault 里存的就是这一份,面板显示英文的话用户根本不知道库里到底是什么。
// 原文没丢,阅读弹窗里可以切回去。
// `translating` / `retranslating` 是后端按导出闸门那一份口径给的,不是前端
// 自己猜的(kb/feeds/registry.py 的 items):
//   translating    还扣着没入库,在等译文 —— 一轮只译 40 条,第 41 条在译完
//                  之前既不在 vault 里、也不该在面板上以英文冒充库里那一份
//                  (2026-09-30 审查实测)。**连标题都不显示原文**:这是进
//                  RSS 页面就看得见的默认内容,露一行英文标题和"英文内容都
//                  译成中文"一样矛盾(复审实测)。想看原文点右边那个链接。
//   retranslating  原文已经按原样入库(没配 key 那阵子同步的),补上 key 之后
//                  重新排进队列等中文覆盖。这一态**照原文显示、可以展开**:
//                  vault 里此刻确实是这一份英文,说它"暂未写入知识库"和文件、
//                  索引的事实正相反(复审实测)。如实显示 + 写明在等覆盖。
//   partial        正文是中文、标题还是原文的半成品(老解析器留下的存量),
//                  下一次同步会把它清回队列补译标题。
// **「已译为中文」的判据和后端同一个**:正文有译文、而且标题(只要原标题不空)
// 也有译文。原来是"任一字段非空就算完成",于是半成品页脚写着已译为中文、标题
// 却是一行英文,连着三轮审查指的都是这处谎报(第 3 轮实测)。
function sourceText(item) {
  const title = item.title_zh || item.title, body = item.body_zh || item.body;
  const whole = !!item.body_zh && (!item.title || !!item.title_zh);
  return { title, body, translated: whole, translating: !!item.translating,
           retranslating: !!item.retranslating, partial: !!item.partial,
           // 正文补抓那两档(kb/feeds/fulltext.py)。它排在翻译之前,所以后端
           // 的 `translate.queued_sql` 已经把这批条目排除在翻译队列之外 ——
           // 这里不会和上面几档同时为真。`refetching` 是"壳已经入库、正在补抓
           // 正文覆盖它",和 `retranslating` 是同一个形状。
           fetching: !!item.fetching, refetching: !!item.refetching };
}
function renderSourceItems() {
  const rows = sourceRows.map(renderSourceEntry).join('');
  // 两句提示各说各的一档,都有就都说:它们是流水线上先后两步,数字也不是一回事
  // (补抓那一档还没轮到翻译,翻译那一档的正文早就到手了)。
  const notes = [
    sourceFetching ? `${sourceFetching} 条只拿到链接，正在补抓原文正文：抓到之后才翻译、写进知识库。` : '',
    sourceTranslating ? `${sourceTranslating} 条外文内容还在等翻译：译成中文之后才写进知识库。` : '',
  ].filter(Boolean);
  const note = notes.length || stuckNote() ? `<p class="source-note" role="status">${notes.join('')}${stuckNote()}</p>` : '';
  // **提示挂在列表外面**:它的射程只跟选中的订阅走,而搜索词只决定列出哪几条。
  // 写成"有内容才显示"的话,搜一个词恰好一条不中,那句"有 N 条翻译失败"和那颗
  // 按钮会一起消失 —— 而它们说的事实一个字都没变。
  $('source-list').innerHTML = note + (rows ||
    `<div class="source-empty"><h3>${$('source-q').value || $('source-filter').value ? '没有匹配的内容' : '还没有内容'}</h3>${$('source-q').value || $('source-filter').value ? '<button class="ghost" data-clear-source-filters>清除筛选</button>' : sourceData?.unconfigured && !sourceData?.ready ? `<p>${esc(sourceConfigHint())}</p>` : ''}</div>`);
  $('source-more').hidden = sourceOffset >= sourceTotal;
  $('source-more').textContent = `加载更多（已显示 ${sourceRows.length} / ${sourceTotal}）`;
  $('source-items-count').textContent = `${sourceTotal} 条`;
  syncSourceReader();
}
async function moreSourceItems() {
  const kind = sourceKind, version = ++sourceItemsVersion;
  const q = $('source-q').value.trim(), id = $('source-filter').value;
  $('source-more').disabled = true;
  try {
    const data = await pipeJson(`/api/intake/${kind}/items?offset=${sourceOffset}&q=${encodeURIComponent(q)}${id?'&source_id='+id:''}`);
    if (!currentSourceItems(kind, version, q, id)) return;
    const seen = new Set(sourceRows.map(r => r.key));
    sourceRows.push(...data.items.filter(r => !seen.has(r.key)));
    sourceOffset += data.items.length; sourceTotal = data.total;
    sourceTranslating = data.translating || 0; sourceFetching = data.fetching || 0;
    sourceStuck = data.stuck || null; sourceTranslateReady = data.translate_ready !== false;
    renderSourceItems();
  } catch(e) { if (currentSourceItems(kind, version, q, id)) sourceError(e.message); }
  finally { if (version === sourceItemsVersion) $('source-more').disabled = false; }
}
// 「我的订阅」里点同步 = 打开起跑弹窗,**锁定在你正在看的这一页**。硬约束(含
// 「手动收录为什么不走这条路」)在 `docs/decisions/sync-entry.md` 第一节。
const PIPE_INTAKE_STAGES = ['crawl', 'export', 'index', 'extract'];
async function syncSource(ids = []) {
  if (sourceBusy) return;
  // 手动收录没有可轮询的源,弹窗里来源/时间范围/订阅三块对它全是空的,照旧直接起跑
  if (sourceModules.get(sourceKind)?.pollable !== false) {
    // 订阅身份传 `{id, name}`:**id 是这一条的稳定身份,名字只给弹窗显示**。
    // 只传名字的后果是请求走到名称那个字段上去,而名称目标代表的是**同名的那几条**
    // —— 两条地址不同、名字相同的 RSS,点第一条的「同步」实测两条都被抓(第 1 轮
    // 审查 R2)。这颗按钮改走弹窗之前传的是 `source_ids`(精确),所以那是这次改动
    // 带进来的回归。两种写法怎么分字段发在 `pipe_setup.js` 的 `pipeTargetFields`。
    const picks = ids.map(id => sourceData?.sources.find(s => s.id === id))
      .filter(Boolean).map(s => ({ id: String(s.id), name: s.name }));
    return openPipeModal({ channel: sourceKind, accounts: picks, stages: PIPE_INTAKE_STAGES });
  }
  sourceBusy = true; updateSourceRunStatus();
  try {
    await pipePost(`/api/intake/${sourceKind}/sync`, { source_ids:ids });
    await refreshPipe(); updateSourceRunStatus();
    toast('已开始写入知识库', { type:'ok', detail:'内容会先保存，再建立检索索引。离开页面不影响同步。' });
    queueSourcePoll();
  } catch(e) { toast('未能开始', { detail:e.message }); }
  finally { sourceBusy=false; updateSourceRunStatus(); }
}
$('source-list').addEventListener('click', e => {
  if (e.target.closest('[data-clear-source-filters]')) clearSourceFilters();
  if (e.target.closest('[data-add-source]')) openSourceEditor();
  if (e.target.closest('[data-retry-items]')) renderSourceView(true);
  if (e.target.closest('[data-retry-translate]')) retryTranslate();
  const read = e.target.closest('[data-read-item]');
  if (read) readSourceItem(read.dataset.readItem);
  const capture = e.target.closest('[data-capture-item]');
  if (capture) openCaptureDrawer(capture.dataset.captureItem);
  const entry = e.target.closest('[data-source-key]');
  // **拖选不是点击。** 卡片整块可点(点开右侧阅读器),而卡片里那一截正文现在能
  // 选来收藏(`kb/static/js/snip_cards.js`)—— 一次拖选松手时浏览器照样派一个
  // click:不挡住的话这一下会打开阅读器,窄屏上还会把列表整块藏起来,「☆ 收藏
  // 此段」那颗按钮当场指着一个看不见的地方。真的单击时 mousedown 已经把选区折叠
  // 掉了,所以这道守卫不影响点开(判据在 `snip.js` 的 `snipSelectionIn`)。
  if (entry && !e.target.closest('a,button') && !snipSelectionIn(entry)) {
    readSourceItem(entry.dataset.sourceKey);
  }
  const action = e.target.closest('[data-source-action]');
  if (action && !action.disabled) actOnSource(action.dataset.sourceAction, Number(action.dataset.sourceId));
});
$('source-list').addEventListener('keydown', event => {
  const entry = event.target.closest('[data-source-key]');
  if (!entry || event.target !== entry) return;
  if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    const entries = [...$('source-list').querySelectorAll('[data-source-key]')];
    const current = entries.indexOf(entry);
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? entries.length - 1
      : Math.max(0, Math.min(entries.length - 1, current + (event.key === 'ArrowDown' ? 1 : -1)));
    entries[index].focus({ preventScroll:true });
    entries[index].scrollIntoView({ block:'nearest' });
    readSourceItem(entries[index].dataset.sourceKey, false);
    return;
  }
  if (!['Enter', ' '].includes(event.key)) return;
  event.preventDefault();
  readSourceItem(entry.dataset.sourceKey);
});

$('source-notices').querySelector('summary').addEventListener('click', () => {
  sourceNoticesAutoOpened = false;
  $('source-notices').classList.remove('is-auto-open');
});
$('subs-wechat-notices').querySelector('summary').addEventListener('click', () => {
  wechatNoticesAutoOpened = false;
  $('subs-wechat-notices').classList.remove('is-auto-open');
});
document.addEventListener('pointerdown', event => {
  const body = event.target.closest('#source-notices .source-notices-body, #subs-wechat-notices .source-notices-body');
  sourceNoticeGesture = body && event.isPrimary && event.button === 0
    ? { notices:body.closest('details'), pointerId:event.pointerId, x:event.clientX, y:event.clientY,
        moved:false, dragged:false, startsOnText:sourceNoticeStartsOnText(body, event), textSelected:false }
    : null;
  const notices = $('source-notices');
  if (notices.open && !notices.contains(event.target)) closeSourceNotices();
  const wechatNotices = $('subs-wechat-notices');
  if (wechatNotices.open && !wechatNotices.contains(event.target)) closeWechatNotices();
}, true);
for (const type of ['pointermove', 'pointerup', 'pointercancel']) document.addEventListener(type, trackSourceNoticeGesture, true);
document.addEventListener('click', event => {
  if (!$('source-notices').contains(event.target)) closeSourceNotices();
  else if ($('source-notices').open && $('source-notices').querySelector('.source-notices-body').contains(event.target)) dismissSourceNotice('source-notices', event);
  if (!$('subs-wechat-notices').contains(event.target)) closeWechatNotices();
  else if ($('subs-wechat-notices').open && $('subs-wechat-notices').querySelector('.source-notices-body').contains(event.target)) dismissSourceNotice('subs-wechat-notices', event);
  sourceNoticeGesture = null;
}, true);
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || event.defaultPrevented) return;
  for (const id of ['source-notices', 'subs-wechat-notices']) {
    if (!$(id).open || $(id).hidden) continue;
    if (id === 'source-notices') { closeSourceNotices(true); continue; }
    closeWechatNotices(true);
  }
});
