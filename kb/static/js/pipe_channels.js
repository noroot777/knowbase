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

// 存下来的通道名 → 现在生效的那条。两条绿泡泡通道抓的是同一批公众号、只能走一条，
// 所以存量里写着哪一条都按设置里选中的那条算（后端 known_channels 是同一个口径）。
// 换通道是「改道」不是「减来源」：当成"不认识的来源"丢掉的话，只要还勾着 RSS 之类，
// 下面那句「一个都没剩才补默认」的兜底就不触发 —— 上一版同时勾了公众号和 RSS 的用户，
// 升级后打开新建同步只剩 RSS，公众号被静默地漏掉，界面上一点提示都没有。
// 去重保序：上一版两条都能勾，存量里可能同时写着它们。
function adoptWechatChannel(list) {
  const wechat = new Set((pipeWechat.options || []).map(c => c.id));
  return [...new Set((list || []).filter(Boolean)
    .map(id => (wechat.has(id) && pipeWechat.selected ? pipeWechat.selected : id)))];
}

// 一份新的来源清单落进内存：存下来的选择跟着当前设置走，各处面板重画。首次加载、
// 设置窗口重新核对、别的页面改过设置之后走的都是这一段。
function applyChannelData(data) {
  const before = pipeWechat.selected;
  pipeChannels = data.channels || [];
  pipeChannelLabels = data.labels || {};
  pipeWechat = data.wechat || { selected: '', options: [] };
  pipeChannel = adoptWechatChannel([pipeChannel])[0] || '';
  if (!pipeChannels.some(c => c.id === pipeChannel)) pipeChannel = pipeChannels.some(c => c.id === data.default) ? data.default : pipeChannels[0].id;
  try { localStorage.setItem('kb-sync-channel', pipeChannel); } catch {}
  // 记下来的来源可能已经不在注册表里（改过名、拿掉了一个采集器）
  pipeRunSel = adoptWechatChannel(pipeRunSel).filter(id => pipeChannels.some(c => c.id === id));
  if (!pipeRunSel.length) pipeRunSel = [pipeChannel];
  saveRunChannels();
  $('pipe-channel-current').innerHTML = pipeChannels.map(c => `<option value="${esc(c.id)}">${esc(c.label)}</option>`).join('');
  renderPipeChannel();
  // 定时范围、日报范围各自在加载时渲染一次、之后不跟着切页面重读：不在这儿通知它们
  // 就地换掉的话，界面上会一直列着一条已经不存在的来源，勾了它保存下去也只是被
  // 后端改写成新的那条 —— 显示的和存下来的对不上。
  if (before && before !== pipeWechat.selected)
    document.dispatchEvent(new CustomEvent('kb:channels-changed',
      { detail: { before, after: pipeWechat.selected } }));
}

// 可勾选的来源 = 注册表减去没被选中的那条绿泡泡通道，所以这份清单取决于一个随时改得动
// 的设置（别的页面、CLI、API 都改得动）。force=true 重新核对服务端：设置窗口和新建同步
// 打开时各来一次 —— 否则这份缓存会把一个和落盘值相反的选择一直显示下去，并按它派发
// 同步。状态轮询那类高频路径照旧吃缓存。
async function loadPipeChannels(force = false) {
  if (pipeChannels.length && !force) return;
  if (pipeChannelsLoading) return pipeChannelsLoading;
  pipeChannelsLoading = (async () => {
    const data = await pipeJson('/api/pipeline/channels');
    if (!data.channels?.length) throw new Error('暂无可用的同步来源，请检查采集器配置后重试');
    applyChannelData(data);
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

// 换绿泡泡的采集通道（设置项，不是这次同步的范围）。存下来的那些选择（顶部下拉、
// 这次同步勾了哪几个、定时范围）都按通道名记，POST 回来的清单交给 applyChannelData
// 带着它们一起改道：另一条通道从此不在列表里。
async function saveWechatChannel(id) {
  if (!id || id === pipeWechat.selected) return pipeWechat.selected;
  applyChannelData(await pipePost('/api/pipeline/channels/wechat', { channel: id }));
  // 同源的其他页面靠 storage 事件收到这件事：CustomEvent 不跨文档，而这个设置是全局
  // 单值 —— 不广播的话另一个页面会一直按一条已经不生效的通道显示、并按它派发同步。
  // 带时间戳是因为 storage 只在值真变了时才触发（换回原来那条也得通知）。
  try { localStorage.setItem('kb-wechat-channel', `${pipeWechat.selected}@${Date.now()}`); } catch {}
  return pipeWechat.selected;
}

// 别的页面（或 CLI/API）改过采集通道：重新核对一次服务端，把范围选择和各处面板带到
// 新通道上。storage 事件只在其他同源文档里触发，自己那次写不会绕回自己身上。
window.addEventListener('storage', e => {
  if (e.key !== 'kb-wechat-channel' || !pipeChannels.length) return;
  loadPipeChannels(true).catch(() => {});
});
