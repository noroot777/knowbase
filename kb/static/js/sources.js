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
let sourceItemsVersion = 0, sourceItemsFailed = false, sourceTranslating = 0;
let sourceBusy = false;
const sourceFilters = new Map();

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
      $('subs-error').hidden = false;
      $('subs-error-text').textContent = '来源目录暂时读不到，请确认服务后重试。';
      return;
    }
  }
  if (!subsCatalog.some(m => m.id === subsKind)) subsKind = subsCatalog[0].id;
  try { localStorage.setItem('kb-subs-kind', subsKind); } catch {}
  renderSubsSwitch();
  const feeds = sourceModules.has(subsKind);
  $('subs-wechat').hidden = feeds;
  $('subs-feeds').hidden = !feeds;
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

function renderSubsSwitch() {
  $('subs-switch').innerHTML = subsCatalog.map(m => `<button role="tab" id="subs-switch-${esc(m.id)}"
    class="${m.id === subsKind ? 'on' : ''}" aria-selected="${m.id === subsKind}"
    onclick="openSubsPanel('${esc(m.id)}')">${icon(m.icon)}<span>${esc(m.label)}</span></button>`).join('');
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
  const ours = running && !!mine && pipeRunScopes(pipeSnapshot?.crawl_filter).some(s => s.channel === mine);
  $('subs-wechat-sync').disabled = subsWechatBusy || running || !!pipeAction;
  $('subs-wechat-sync').textContent = ours ? '同步中…' : '同步全部';
  $('subs-wechat-progress').hidden = !running;
  $('subs-wechat-progress-text').textContent = ours
    ? (pipeSnapshot.stages?.[pipeSnapshot.current_stage]?.detail || '正在同步公众号')
    : '其他来源正在同步，新任务会顺延。';
}
async function syncWechatSubs() {
  if (subsWechatBusy || pipeSnapshot?.running) return;
  subsWechatBusy = true; renderWechatSubsStatus();
  try {
    await loadPipeChannels();
    // 提交的通道同样按来源目录现解析,不拿全局选择当然值
    const channel = wechatSubsChannel();
    if (!channel) throw new Error('这个来源的采集通道暂不可用，请确认采集器配置后重试');
    setPipeChannel(channel);
    // accounts 空 = 该来源的全部订阅(kb/web/pipeline_api.py 的 ChannelScope)
    await pipePost('/api/pipeline/run', { channels: [{ channel, accounts: [] }] });
    toast('同步已开始', { type: 'ok', detail: '进度在同步中心实时显示，离开页面不影响同步。' });
    await refreshPipe();
  } catch (e) { toast('未能开始同步', { detail: e.message }); }
  finally { subsWechatBusy = false; renderWechatSubsStatus(); }
}

async function openSourceModule(kind) {
  clearTimeout(sourcePoll);
  clearTimeout(sourceSearchTimer);
  sourceKind = kind; sourceVersion++; sourceItemsVersion++; sourceItemsFailed = false;
  sourceData = null; sourceRows = []; sourceOffset = 0;
  const spec = sourceModules.get(kind);
  const filter = sourceFilters.get(kind) || { view: 'items', q: '', id: '' };
  sourceView = filter.view;
  $('source-title').textContent = spec.label;
  $('source-description').textContent = spec.description;
  $('source-note').textContent = spec.note;
  $('source-q').value = filter.q;
  $('source-filter').innerHTML = '<option value="">全部订阅</option>';
  $('source-summary').textContent = '正在读取来源…';
  $('source-list').innerHTML = '<div class="source-empty">正在读取内容…</div>';
  $('source-settings-form').reset();
  $('source-settings-fields').disabled = true;
  await loadSourceModule(true, filter.id);
}

function sourceError(message = '') {
  $('source-error').hidden = !message;
  $('source-error-text').textContent = message;
}
function sourceTime(value) {
  if (!value) return '尚未同步';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' });
}
// feeds 视图还亮着才继续轮询/渲染:tab 在订阅页、页内选的就是这个来源
function sourceActive() { return activeTab === 'subs' && subsKind === sourceKind && sourceModules.has(sourceKind); }

async function loadSourceModule(reset = false, selectedId = null) {
  if (!sourceActive()) return;
  const kind = sourceKind, version = ++sourceVersion;
  try {
    const data = await pipeJson('/api/intake/' + kind);
    if (version !== sourceVersion || kind !== sourceKind || !sourceActive()) return;
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
    $('source-configuration').textContent = `${data.unconfigured} 个账号待配置：请填写可用的 X 订阅桥接，或为账号添加专用订阅链接。已配置的来源可照常同步。`;
    $('source-subscriptions-count').textContent = data.sources.length;
    if (reset) fillSourceSettings(data.settings);
    const run = data.last_run;
    $('source-last-run').textContent = run ? `上次检查 ${sourceTime(run.finished_at)} · 新增 ${run.summary.added} 条${run.summary.errors.length ? ' · ' + run.summary.errors.length + ' 个来源失败' : ''}${run.summary.message ? ' · ' + run.summary.message : ''}` : '尚未同步。先检查订阅，再开始收集。';
    if (reset || changed || sourceItemsFailed) await renderSourceView(reset);
    updateSourceRunStatus();
  } catch (e) {
    if (version === sourceVersion && kind === sourceKind && sourceActive()) sourceError(e.message);
  } finally {
    if (sourceActive()) queueSourcePoll();
  }
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
  const ours = running && pipeRunScopes(pipeSnapshot?.crawl_filter).some(scope => scope.channel === sourceKind);
  $('source-sync').disabled = sourceBusy || !!running || !sourceData?.ready;
  $('source-sync').textContent = ours ? '同步中…' : '同步全部';
  $('source-progress').hidden = !running;
  $('source-progress-text').textContent = ours
    ? (pipeSnapshot.stages[pipeSnapshot.current_stage]?.detail || '正在同步')
    : '其他来源正在同步，自动任务会顺延。';
  $('source-list').querySelectorAll('[data-source-action]').forEach(b => {
    const source = sourceData?.sources.find(s=>s.id===Number(b.dataset.sourceId));
    const paused = b.dataset.sourceAction === 'sync' && (!source?.enabled || source?.configuration_error);
    b.disabled = sourceBusy || !!running || paused;
  });
  $('source-add').disabled = sourceBusy || !!ours;
}
function switchSourceView(view) {
  sourceView = view; sourceOffset = 0;
  renderSourceView(true).catch(e => sourceError(e.message));
}
function sourceFilterChanged() {
  clearTimeout(sourceSearchTimer);
  sourceItemsVersion++; // Invalidate responses immediately, including during debounce.
  sourceSearchTimer = setTimeout(() => renderSourceView(true).catch(e => sourceError(e.message)), 220);
}
async function renderSourceView(reset = false) {
  if (!sourceData) return;
  const kind = sourceKind, version = ++sourceItemsVersion;
  $('source-view-items').classList.toggle('on', sourceView === 'items');
  $('source-view-manage').classList.toggle('on', sourceView === 'manage');
  $('source-view-items').setAttribute('aria-pressed', String(sourceView === 'items'));
  $('source-view-manage').setAttribute('aria-pressed', String(sourceView === 'manage'));
  $('source-q').placeholder = sourceView === 'items' ? '搜索已收录的内容…' : '搜索名称、链接或分组…';
  $('source-filter').hidden = sourceView === 'manage';
  $('source-more').hidden = true;
  $('source-more').disabled = false;
  const q = $('source-q').value.trim(), id = $('source-filter').value;
  sourceFilters.set(kind, { view: sourceView, q, id });
  if (sourceView === 'manage') {
    sourceItemsFailed = false;
    const rows = sourceData.sources.filter(s => (s.name+' '+s.url+' '+s.group_name).toLowerCase().includes(q.toLowerCase()));
    $('source-list').innerHTML = rows.map(renderSubscription).join('') || '<div class="source-empty"><h3>没有匹配的订阅</h3><p>换个关键词，或添加新的来源。</p></div>';
    updateSourceRunStatus();
    return;
  }
  const count = !reset && sourceOffset ? sourceOffset : 30;
  // Reload the visible window to avoid duplicates when a sync inserts new entries.
  if (reset) $('source-list').innerHTML = '<div class="source-empty">正在读取内容…</div>';
  try {
    const data = await pipeJson(`/api/intake/${kind}/items?limit=${Math.min(count,100)}&offset=0&q=${encodeURIComponent(q)}${id ? '&source_id='+id : ''}`);
    if (!currentSourceItems(kind, version, q, id)) return;
    sourceItemsFailed = false;
    sourceRows = data.items; sourceOffset = data.items.length; sourceTotal = data.total;
    sourceTranslating = data.translating || 0;
    renderSourceItems();
  } catch (e) {
    if (!currentSourceItems(kind, version, q, id)) return;
    sourceItemsFailed = true;
    $('source-list').innerHTML = `<div class="source-empty" role="alert"><h3>内容读取失败</h3><p>${esc(e.message)}</p><p>稍后会自动重试，也可以立即重新读取。</p><button class="ghost" data-retry-items>重新读取</button></div>`;
  }
}
function currentSourceItems(kind, version, q, id) {
  return kind === sourceKind && version === sourceItemsVersion && sourceActive() && sourceView === 'items'
    && q === $('source-q').value.trim() && id === $('source-filter').value;
}
function renderSubscription(s) {
  const status = !s.enabled ? '已停用' : s.configuration_error ? '待配置' : s.last_error ? '连接失败' : s.last_success ? '已同步' : '待首次同步';
  return `<article class="source-subscription ${!s.enabled ? 'is-paused' : ''}">
    <div class="source-avatar" aria-hidden="true">${esc(s.name.slice(0,1))}</div>
    <div class="source-subscription-body"><div class="source-row-title"><h3>${esc(s.name)}</h3><span class="source-state ${s.enabled && s.last_error ? 'failed' : ''}">${status}</span></div>
    <a class="source-url" href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener noreferrer">${esc(s.url)}</a>
    <div class="source-row-meta">${esc(s.group_name || '未分组')} · ${s.item_count} 条 · ${s.last_success ? '最近成功 '+sourceTime(s.last_success) : '尚未成功同步'}</div>
    ${s.configuration_error ? `<p class="source-row-meta">${esc(s.configuration_error)}</p>` : ''}
    ${s.last_error ? `<details class="source-failure"><summary>查看失败原因与重试时间</summary><p>${esc(s.last_error)}</p><p>自动重试：${sourceTime(s.retry_at*1000)}；也可点击“同步”立即重试。</p></details>` : ''}</div>
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
           retranslating: !!item.retranslating, partial: !!item.partial };
}
function renderSourceItems() {
  const rows = sourceRows.map(item => { const t = sourceText(item); return `<article class="source-entry">
    <div class="source-entry-meta"><span>${esc(item.author)}</span><time>${item.published ? sourceTime(item.published) : '发布时间未知'}</time></div>
    ${t.translating
      // 等翻译的条目不给“展开阅读”:知识库里这一条还不存在,展开只能展开英文
      // 原文。标题和正文都换成中文状态文案,原文入口留在右下角那个链接上。
      ? `<h3 class="source-entry-name">外文内容，正在等翻译</h3>
    <p class="source-excerpt">译成中文之后才写进知识库；这一条现在只有原文，点右下角“原文”可以直接看。</p>`
      : `<h3><button class="source-entry-title" data-read-item="${item.key}">${esc(t.title)}</button></h3>
    <p class="source-excerpt">${esc(t.body.slice(0,380))}${t.body.length>380?'…':''}</p>`}
    <div class="source-entry-footer"><span>${item.content_kind === 'summary' ? '订阅摘要' : sourceKind === 'social' ? '社交观点' : '订阅正文'}${t.translating ? ' · 外文原文，正在等翻译' : t.translated ? ' · 已译为中文' : t.retranslating ? ' · 外文原文已入库，正在等中文译文覆盖' : t.partial ? ' · 正文已译为中文，标题还是原文（下次同步补译）' : ''}</span>${t.translating ? '' : `<button class="text-button" data-read-item="${item.key}">展开阅读</button>`}<a href="${esc(safeUrl(item.url))}" target="_blank" rel="noopener noreferrer">原文${arrow('ext')}</a></div></article>`; }).join('');
  const note = sourceTranslating
    ? `<p class="source-note" role="status">${sourceTranslating} 条外文内容还在等翻译：译成中文之后才写进知识库。</p>` : '';
  $('source-list').innerHTML = rows ? note + rows :
    `<div class="source-empty">${icon(sourceModules.get(sourceKind).icon)}<h3>${$('source-q').value ? '没有匹配的内容' : '关注已就位，等待第一条内容'}</h3><p>${$('source-q').value ? '换个关键词或切换订阅试试。' : sourceData?.unconfigured && !sourceData?.ready ? '账号待配置。先填写 X 订阅桥接或账号的专用订阅链接，再开始同步。' : '点击“同步全部”开始收集，也可以先到订阅管理调整关注列表。'}</p></div>`;
  $('source-more').hidden = sourceOffset >= sourceTotal;
  $('source-more').textContent = `加载更多（已显示 ${sourceRows.length} / ${sourceTotal}）`;
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
    sourceTranslating = data.translating || 0; renderSourceItems();
  } catch(e) { if (currentSourceItems(kind, version, q, id)) sourceError(e.message); }
  finally { if (version === sourceItemsVersion) $('source-more').disabled = false; }
}
async function syncSource(ids = []) {
  if (sourceBusy) return;
  sourceBusy = true; updateSourceRunStatus();
  try {
    await pipePost(`/api/intake/${sourceKind}/sync`, { source_ids:ids });
    await refreshPipe(); updateSourceRunStatus();
    toast('同步已开始', { type:'ok', detail:'内容会先保存，再建立检索索引。离开页面不影响同步。' });
    queueSourcePoll();
  } catch(e) { toast('未能开始同步', { detail:e.message }); }
  finally { sourceBusy=false; updateSourceRunStatus(); }
}
let sourceReaderItem = null, sourceReaderOriginal = false;
function readSourceItem(key) {
  const item = sourceRows.find(r => r.key === key);
  if (!item) return;
  sourceReaderItem = item; sourceReaderOriginal = false;
  renderSourceReader();
  $('source-reader-link').href = safeUrl(item.url);
  $('source-reader').style.display = '';
}
function renderSourceReader() {
  const item = sourceReaderItem, t = sourceText(item);
  const original = sourceReaderOriginal;
  $('source-reader-title').textContent = original ? item.title : t.title;
  $('source-reader-meta').textContent = `${item.author} · ${item.published ? sourceTime(item.published) : '发布时间未知'}${item.content_kind==='summary'?' · 订阅摘要，全文请打开原文':''}${t.translated ? (original ? ' · 英文原文' : ' · 自动翻译') : ''}`;
  $('source-reader-body').textContent = original ? item.body : t.body;
  $('source-reader-original').hidden = !t.translated;
  $('source-reader-original').textContent = original ? '看中文译文' : '看英文原文';
}
function toggleSourceOriginal() {
  sourceReaderOriginal = !sourceReaderOriginal;
  renderSourceReader();
}
$('source-list').addEventListener('click', e => {
  if (e.target.closest('[data-retry-items]')) renderSourceView(true);
  const read = e.target.closest('[data-read-item]');
  if (read) readSourceItem(read.dataset.readItem);
  const action = e.target.closest('[data-source-action]');
  if (action && !action.disabled) actOnSource(action.dataset.sourceAction, Number(action.dataset.sourceId));
});
