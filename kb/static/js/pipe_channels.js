// 同步通道由采集器注册表提供；手动选择记在本浏览器，定时选择单独保存。
let pipeChannel = '';
let pipeChannels = [];
let pipeChannelsLoading = null;
try { pipeChannel = localStorage.getItem('kb-sync-channel') || ''; } catch {}

async function loadPipeChannels() {
  if (pipeChannels.length) return;
  if (pipeChannelsLoading) return pipeChannelsLoading;
  pipeChannelsLoading = (async () => {
    const r = await fetch('/api/pipeline/channels');
    if (!r.ok) throw new Error(await apiFail(r, '读取同步通道失败'));
    const data = await r.json();
    pipeChannels = data.channels || [];
    if (!pipeChannels.some(c => c.id === pipeChannel)) pipeChannel = data.default;
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
  if (!pipeChannels.some(c => c.id === value)) return;
  pipeChannel = value;
  try { localStorage.setItem('kb-sync-channel', value); } catch {}
  renderPipeChannel();
  $('src-detail').textContent = '正在检查所选通道…';
  await Promise.all([refreshPipe(false), initTargets()]);
}
