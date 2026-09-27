// 同步通道由采集器注册表提供；手动选择记在本浏览器，定时选择单独保存。
let pipeChannel = '';
let pipeChannels = [];
let pipeChannelsLoading = null;
try { pipeChannel = localStorage.getItem('kb-sync-channel') || ''; } catch {}

async function loadPipeChannels() {
  if (pipeChannels.length) return;
  if (pipeChannelsLoading) return pipeChannelsLoading;
  pipeChannelsLoading = (async () => {
    const data = await pipeJson('/api/pipeline/channels');
    if (!data.channels?.length) throw new Error('暂无可用的同步通道，请检查采集器配置后重试');
    pipeChannels = data.channels || [];
    if (!pipeChannels.some(c => c.id === pipeChannel)) pipeChannel = pipeChannels.some(c => c.id === data.default) ? data.default : pipeChannels[0].id;
    for (const id of ['pipe-channel', 'pipe-channel-current', 'sched-channel']) {
      $(id).innerHTML = pipeChannels.map(c => `<option value="${esc(c.id)}">${esc(c.label)}</option>`).join('');
      $(id).value = id === 'sched-channel' ? data.default : pipeChannel;
    }
    renderPipeChannel();
  })();
  try { await pipeChannelsLoading; } finally { pipeChannelsLoading = null; }
}

function channelUrl(path, channel = pipeChannel) {
  return path + (path.includes('?') ? '&' : '?') + 'channel=' + encodeURIComponent(channel);
}

function renderPipeChannel() {
  const channel = pipeChannels.find(c => c.id === pipeChannel);
  $('pipe-channel').value = pipeChannel;
  $('pipe-channel-current').value = pipeChannel;
  $('pipe-channel-hint').textContent = channel?.hint || '';
  pipeRangeHint = channel?.range_hint || '';
  renderPipeRange();
}

async function changePipeChannel(value) {
  if (pipeAction || pipeSnapshot?.running || !pipeChannels.some(c => c.id === value)) { renderPipeChannel(); return; }
  savePipeDraft();
  pipeChannel = value;
  try { localStorage.setItem('kb-sync-channel', value); } catch {}
  renderPipeChannel();
  $('src-detail').textContent = '正在检查所选通道…';
  refreshPipeSource(); initTargets();
  if (pipeModalOpen()) { $('pipe-acct-search').value = ''; closePipeRange(); await loadPipeAccounts(); }
}
