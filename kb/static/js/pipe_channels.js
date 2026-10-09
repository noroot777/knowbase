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

// 一次运行抓了哪几个来源。多来源记在 channels 里，只有一个来源时仍是老样子的
// channel + accounts —— 存量状态文件和旧的运行记录都还是后者，两种都要认。
function pipeRunScopes(filter) {
  // retry:「重试失败项」那一轮这个来源的范围是怎么来的（'targets' 收窄到了失败的
  // 那几个 / 'all' 这个来源收窄不了、跑的是原范围），面板那行「本次范围」要照它说话。
  if (filter?.channels?.length) return filter.channels.map(s => ({ channel: s.channel, accounts: s.accounts || [], retry: s.retry || '' }));
  return filter?.channel ? [{ channel: filter.channel, accounts: filter.accounts || [], retry: filter.retry || '' }] : [];
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
