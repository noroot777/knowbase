// 来源由采集器注册表提供。两件事分开记：页面顶部那个选择只决定「看哪个来源的
// 连接状态、管哪个来源的订阅」，而一次同步可以同时抓好几个来源（pipeRunSel）。
let pipeChannel = '';
let pipeChannels = [];
let pipeChannelsLoading = null;
let pipeRunSel = [];
// 通道 id → 人话名字，**含已经退役的那些**。上次运行记录里写着的可能正是一条退役
// 通道（公众号早先那条凭证通道）——只查 pipeChannels 的话那行会显示成一个裸 id。
let pipeChannelLabels = {};
// 退役通道名 → 接替它的那条（服务端 /api/pipeline/channels 的 aliases，和后端
// resolve_channel 同一份表）。前端不自己写死任何一个旧名字。
let pipeChannelAliases = {};
try { pipeChannel = localStorage.getItem('kb-sync-channel') || ''; } catch {}
try { pipeRunSel = JSON.parse(localStorage.getItem('kb-sync-channels') || '[]'); } catch {}

// 浏览器里存着的选择 → 现在认得的通道名。公众号曾经有两条通道二选一，所以
// localStorage 里写着的可能是已经退役的那一条，也可能两条都在（更早的版本两条都能勾）。
// 当成"不认识的来源"直接过滤掉的话，只要还勾着 RSS 之类，下面那句「一个都没剩才补默认」
// 的兜底就不触发 —— 升级后打开新建同步只剩 RSS，公众号被静默地漏掉，一句提示都没有。
// 去重保序：两条旧名字会映射到同一条。
function adoptChannels(list) {
  return [...new Set((list || []).filter(Boolean)
    .map(id => pipeChannelAliases[id] || id))];
}

// 一份新的来源清单落进内存：存下来的选择跟着别名表走，各处面板重画。
function applyChannelData(data) {
  pipeChannels = data.channels || [];
  pipeChannelLabels = data.labels || {};
  pipeChannelAliases = data.aliases || {};
  pipeChannel = adoptChannels([pipeChannel])[0] || '';
  if (!pipeChannels.some(c => c.id === pipeChannel)) pipeChannel = pipeChannels.some(c => c.id === data.default) ? data.default : pipeChannels[0].id;
  try { localStorage.setItem('kb-sync-channel', pipeChannel); } catch {}
  // 记下来的来源可能已经不在注册表里（改过名、拿掉了一个采集器）
  pipeRunSel = adoptChannels(pipeRunSel).filter(id => pipeChannels.some(c => c.id === id));
  if (!pipeRunSel.length) pipeRunSel = [pipeChannel];
  saveRunChannels();
  renderPipeChannel();
}

// 可勾选的来源就是采集器注册表，只随服务端的代码变 —— 一个进程里读一次就够。
// 并发请求合并成同一个 promise。
async function loadPipeChannels() {
  if (pipeChannels.length) return true;
  if (pipeChannelsLoading) return pipeChannelsLoading;
  const request = (async () => {
    const data = await pipeJson('/api/pipeline/channels');
    if (!data.channels?.length) throw new Error('暂无可用的同步来源，请检查采集器配置后重试');
    applyChannelData(data);
    return true;
  })();
  pipeChannelsLoading = request;
  try { return await request; } finally { if (pipeChannelsLoading === request) pipeChannelsLoading = null; }
}

function channelUrl(path, channel = pipeChannel) {
  return path + (path.includes('?') ? '&' : '?') + 'channel=' + encodeURIComponent(channel);
}
function channelLabel(id) { return pipeChannels.find(c => c.id === id)?.label || pipeChannelLabels[id] || id; }

// 「这一轮实际作用在哪几个来源上」的**唯一口径**。同步中心的来源行、公众号页和
// 订阅页的「正在同步」判断、结束后要不要重读条目、以及范围文案全都过它 —— 分别各判
// 一遍的话,同一轮运行在几个页面上会给出几种说法。
//
// **这是「按来源执行的那几步各自范围的并集」,不是某一步范围的别名。** 抓取和导出
// 各有自己的范围(`crawl_filter` / `export_filter`:抓取收窄到失败的那几条、导出照旧
// 走整条来源),拿抓取那一份当整轮事实会**整个漏掉只导出的那一轮** —— 实测「只补
// 导出」那一轮的抓取范围是 `{force: true}`(一个来源名都没有),于是开着的 RSS 页在
// 重试成功之后**一直显示翻译失败和等待翻译**:它靠「这一轮带不带本来源」决定重读
// 条目(`sources.js` 的 `sourceRunFinished`),而仅翻译/导出既不改订阅行也不改收录
// 总数,另外两个判据对它都是瞎的 —— 下一次轮询照旧什么都不重读,只刷新整页才看得到
// 已经译好的正文。同一条还让同步中心写「选择来源后开始一次同步」、历史那行写
// 「未记录来源」、订阅的增/改/删写保护整条失效(第 1 轮自由审查 R1 实页实测)。
// **也不许写成 `crawl_filter || export_filter`**:只导出那一轮 `{force: true}` 已经
// 为真,而抓取 A / 导出 A+B 那一轮又会漏掉 B。
//
// **判据是这一轮计划里有没有那一步**,不是 filter 里写着谁:`start_pipeline` **一律**
// 往 filter 里盖一个默认采集器名(实测 `wechat_local`),所以一轮只跑 index/extract 的
// 运行照着 filter 念,公众号页会显示「同步中…」、而真正参与这一轮的 RSS 反倒被说成
// 「其他来源正在同步」。**纯全库那几步一个来源都不凭空附加。**
//
// 同一个来源两步里都出现时,那一格的 accounts / retry 取**抓取**那一份(它是收窄过的
// 那一份,而下面那行「本次范围」念的正是它);导出只补上抓取没碰到的来源。
//
// 后端那一份同口径是 `kb/pipeline_stages.py` 的 `run_scopes`,**输出形状也一样**。
// 硬约束在 `docs/decisions/retry-scope.md` 第一节。
// 多来源记在 channels 里,只有一个来源时仍是老样子的 channel + accounts —— 存量状态
// 文件和旧的运行记录都还是后者,两种都要认。
function pipeRunScopes(result) {
  // **落盘记录自己写着答案就念它**:给人翻的那份历史记录**故意不存导出范围**
  // (那一份不许喂给重试派生),所以后端收尾时把这一轮的来源集合直接存成结论
  // (`pipeline_persist.run_record` 的 `scopes`)。老记录没有这一格,照旧往下派生。
  if (Array.isArray(result?.scopes))
    return result.scopes.filter(s => s && typeof s === 'object')
      .map(s => ({ channel: s.channel || '', accounts: s.accounts || [], retry: s.retry || '' }));
  // 一份范围里那几段来源。retry:「重试失败项」那一轮这个来源的范围是怎么来的
  // ('targets' 收窄到了失败的那几个 / 'all' 这个来源收窄不了、跑的是原范围),
  // 面板那行「本次范围」要照它说话。
  const of = filter => {
    const f = filter || {};
    if (f.channels?.length) return f.channels.map(s => ({ channel: s.channel, accounts: s.accounts || [], retry: s.retry || '' }));
    return f.channel ? [{ channel: f.channel, accounts: f.accounts || [], retry: f.retry || '' }] : [];
  };
  const planned = Array.isArray(result?.planned_stages) ? result.planned_stages : null;
  // 老调用方可能直接递一份 crawl_filter 进来;没有 planned_stages 的(存量落盘记录、
  // 裸 filter)按老样子只摊平抓取范围,别把它们一律判成「不按来源」。
  const run = result && 'crawl_filter' in result ? result : { crawl_filter: result };
  if (!planned) return of(run.crawl_filter);
  const merged = [];
  for (const [stage, key] of [['crawl', 'crawl_filter'], ['export', 'export_filter']]) {
    if (!planned.includes(stage)) continue;
    // 回落到抓取范围是给升级前那些没有导出范围的旧记录用的,口径同后端。
    for (const scope of of(run[key] || run.crawl_filter))
      if (!merged.some(s => s.channel === scope.channel)) merged.push(scope);
  }
  return merged;
}
// 「这一轮现在在做什么」的一句话。**全库那一档要单独有话说** —— 它不归任何来源,
// 但那不等于「别人在跑」:照「没有来源范围 = 其他来源正在同步」念,用户在 RSS 页和
// 公众号页会同时看到「其他来源正在同步」,而那一轮做的正是他自己这些内容的索引与
// 提取。三个页面共用这一份,别各写各的。
function pipeRunNote(snapshot, fallback) {
  const detail = snapshot?.stages?.[snapshot.current_stage]?.detail;
  if (pipeLibraryOnly(snapshot)) {
    const planned = (snapshot.planned_stages || []).map(k => STAGE_META[k]?.name).filter(Boolean);
    return detail || `正在为全部来源${planned.join('、') || '处理入库'}`;
  }
  return detail || fallback;
}
// 这一轮是不是「不按来源」的全库处理（既没抓取也没导出）。文案要照它说话。
function pipeLibraryOnly(result) {
  const planned = Array.isArray(result?.planned_stages) ? result.planned_stages : [];
  return planned.length > 0 && !planned.includes('crawl') && !planned.includes('export');
}

function renderPipeChannel() {
  renderPipeChannelList();
  renderPipeRange();
}

function saveRunChannels() {
  try { localStorage.setItem('kb-sync-channels', JSON.stringify(pipeRunSel)); } catch {}
}

// 新建同步弹窗里的来源多选。列表顺序即抓取顺序，勾选先后不影响。
function renderPipeChannelList() {
  const box = $('pipe-channel-list');
  if (!box) return;
  box.innerHTML = pipeChannels.map(c => `<label class="pipe-channel">
    <input type="checkbox" data-run-channel="${esc(c.id)}"${pipeRunSel.includes(c.id) ? ' checked' : ''}>
    <span><b>${esc(c.label)}</b>${c.hint ? `<small>${esc(c.hint)}</small>` : ''}</span></label>`).join('');
  $('pipe-channel-count').textContent = `已选 ${pipeRunSel.length} / ${pipeChannels.length}`;
  // 每张卡片上已经写着自己的说明,底下再复述一遍勾中那个就是噪音;这行只用来说「一个都没勾」
  const hint = $('pipe-channel-hint');
  hint.textContent = pipeRunSel.length ? '' : '请至少选择一个来源。';
  hint.hidden = !hint.textContent;
}

$('pipe-channel-list').addEventListener('change', e => {
  const input = e.target.closest('input[data-run-channel]');
  if (!input) return;
  const id = input.dataset.runChannel;
  savePipeDraft();
  if (input.checked) pipeRunSel = [...pipeChannels.map(c => c.id)].filter(c => c === id || pipeRunSel.includes(c));
  else pipeRunSel = pipeRunSel.filter(c => c !== id);
  saveRunChannels();
  renderPipeChannelList();
  renderPipeRange();
  loadPipeAccounts();
});

// 「查看/管理哪个来源的连接与订阅」的当前选择(channelUrl 据此拼参数)。
// 不动这次同步要抓哪几个来源(pipeRunSel)。
function setPipeChannel(value) {
  if (!pipeChannels.some(c => c.id === value)) return;
  pipeChannel = value;
  try { localStorage.setItem('kb-sync-channel', value); } catch {}
}

// 接入向导走完之后直接把公众号那条选上，用户回到同步中心就能开跑。
function selectRunChannel(id) {
  if (!pipeChannels.some(c => c.id === id)) return;
  if (!pipeRunSel.includes(id)) { pipeRunSel = [...pipeRunSel, id]; saveRunChannels(); }
  renderPipeChannelList();
}
