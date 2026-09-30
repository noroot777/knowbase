// 来源由采集器注册表提供。两件事分开记：页面顶部那个选择只决定「看哪个来源的
// 连接状态、管哪个来源的订阅」，而一次同步可以同时抓好几个来源（pipeRunSel）。
let pipeChannel = '';
let pipeChannels = [];
let pipeChannelsLoading = null;
let pipeRunSel = [];
// 全部通道的人话名字（含没被选中的那条绿泡泡通道）。上次运行记录里写着的可能正是
// 它——只查 pipeChannels 的话那行会显示成一个裸 id。
let pipeChannelLabels = {};
// 绿泡泡两条通道互斥，选哪条是设置（见 kb.collectors.wechat_channel），不是这次
// 同步的范围；面板在「绿泡泡接入」里改它。
let pipeWechat = { selected: '', options: [] };
try { pipeChannel = localStorage.getItem('kb-sync-channel') || ''; } catch {}
try { pipeRunSel = JSON.parse(localStorage.getItem('kb-sync-channels') || '[]'); } catch {}

async function loadPipeChannels() {
  if (pipeChannels.length) return;
  if (pipeChannelsLoading) return pipeChannelsLoading;
  pipeChannelsLoading = (async () => {
    const data = await pipeJson('/api/pipeline/channels');
    if (!data.channels?.length) throw new Error('暂无可用的同步来源，请检查采集器配置后重试');
    pipeChannels = data.channels || [];
    pipeChannelLabels = data.labels || {};
    pipeWechat = data.wechat || { selected: '', options: [] };
    if (!pipeChannels.some(c => c.id === pipeChannel)) pipeChannel = pipeChannels.some(c => c.id === data.default) ? data.default : pipeChannels[0].id;
    // 记下来的来源可能已经不在注册表里（改过名、拿掉了一个采集器）
    pipeRunSel = pipeRunSel.filter(id => pipeChannels.some(c => c.id === id));
    if (!pipeRunSel.length) pipeRunSel = [pipeChannel];
    $('pipe-channel-current').innerHTML = pipeChannels.map(c => `<option value="${esc(c.id)}">${esc(c.label)}</option>`).join('');
    $('pipe-channel-current').value = pipeChannel;
    renderPipeChannel();
  })();
  try { await pipeChannelsLoading; } finally { pipeChannelsLoading = null; }
}

function channelUrl(path, channel = pipeChannel) {
  return path + (path.includes('?') ? '&' : '?') + 'channel=' + encodeURIComponent(channel);
}
function channelLabel(id) { return pipeChannels.find(c => c.id === id)?.label || pipeChannelLabels[id] || id; }

// 一次运行抓了哪几个来源。多来源记在 channels 里，只有一个来源时仍是老样子的
// channel + accounts —— 存量状态文件和旧的运行记录都还是后者，两种都要认。
function pipeRunScopes(filter) {
  if (filter?.channels?.length) return filter.channels.map(s => ({ channel: s.channel, accounts: s.accounts || [] }));
  return filter?.channel ? [{ channel: filter.channel, accounts: filter.accounts || [] }] : [];
}

function renderPipeChannel() {
  $('pipe-channel-current').value = pipeChannel;
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

// 顶部那个选择：换的是「查看/管理哪个来源」，不动这次同步要抓哪几个。
async function changePipeChannel(value) {
  if (!pipeChannels.some(c => c.id === value)) { renderPipeChannel(); return; }
  pipeChannel = value;
  try { localStorage.setItem('kb-sync-channel', value); } catch {}
  renderPipeChannel();
  $('src-detail').textContent = '正在检查所选来源…';
  refreshPipeSource(); initTargets();
}

// 接入向导走完之后直接把本机通道选上，用户回到同步中心就能开跑。
function selectRunChannel(id) {
  if (!pipeChannels.some(c => c.id === id)) return;
  if (!pipeRunSel.includes(id)) { pipeRunSel = [...pipeRunSel, id]; saveRunChannels(); }
  renderPipeChannelList();
}

// 换绿泡泡的采集通道（设置项）。存下来的那些选择（顶部下拉、这次同步勾了哪几个、
// 定时范围）都按通道名记，换完要重新拉一次清单让它们跟着走：另一条通道从此不在
// 列表里，loadPipeChannels 会把它过滤掉并回落到新的那条。
async function saveWechatChannel(id) {
  if (!id || id === pipeWechat.selected) return pipeWechat.selected;
  const before = pipeWechat.selected;
  const data = await pipePost('/api/pipeline/channels/wechat', { channel: id });
  pipeChannels = data.channels || [];
  pipeChannelLabels = data.labels || {};
  pipeWechat = data.wechat || pipeWechat;
  // 旧通道的每一处引用都换成新的：两条抓的是同一批公众号，这是"改道"不是"减来源"。
  const swap = list => list.map(c => (c === before ? id : c));
  pipeChannel = pipeChannels.some(c => c.id === pipeChannel) ? pipeChannel
    : pipeChannels.some(c => c.id === data.default) ? data.default : pipeChannels[0].id;
  try { localStorage.setItem('kb-sync-channel', pipeChannel); } catch {}
  pipeRunSel = [...new Set(swap(pipeRunSel))].filter(c => pipeChannels.some(x => x.id === c));
  if (!pipeRunSel.length) pipeRunSel = [pipeChannel];
  saveRunChannels();
  $('pipe-channel-current').innerHTML = pipeChannels.map(c => `<option value="${esc(c.id)}">${esc(c.label)}</option>`).join('');
  renderPipeChannel();
  // 定时范围、日报范围各自在加载时渲染一次、之后不跟着切页面重读：不在这儿通知它们
  // 就地换掉的话，界面上会一直列着一条已经不存在的来源，勾了它保存下去也只是被
  // 后端改写成新的那条 —— 显示的和存下来的对不上。
  document.dispatchEvent(new CustomEvent('kb:channels-changed', { detail: { before, after: id } }));
  return pipeWechat.selected;
}
