// Local onboarding stays independent of cloud models and of the active sync channel.
let wechatSetup = null, wechatPoll = null, wechatRequest = false;
let wechatStatusVersion = 0;
let wechatChannelBusy = false;
function openWechatSetup() {
  $('wechat-modal').style.display = '';
  $('wechat-error').textContent = '';
  refreshWechatSetup();
  loadWechatChannels();
}

// ── 采集通道（设置项）─────────────────────────────────────────────────────────
// 绿泡泡两条通道抓的是同一批公众号，只能走一条，所以它不在「这次同步抓哪几个来源」
// 里勾，而是在这儿选一次：选中的那条才会出现在同步范围和日报范围里。

function renderWechatChannels() {
  const options = pipeWechat?.options || [];
  $('wechat-channels').innerHTML = options.map(c => `<label class="pipe-channel">
    <input type="radio" name="wechat-channel" value="${esc(c.id)}"
      ${c.id === pipeWechat.selected ? 'checked' : ''} ${wechatChannelBusy ? 'disabled' : ''}>
    <span><b>${esc(c.label)}</b>${c.hint ? `<small>${esc(c.hint)}</small>` : ''}</span></label>`).join('');
}

async function loadWechatChannels() {
  const msg = $('wechat-channel-msg');
  try {
    // 每次打开都重新核对一次服务端：这是个全局单值，另一个页面、CLI、API 都改得动，
    // 吃缓存的话单选按钮会指着一个和落盘值相反的通道。
    const applied = await loadPipeChannels(true);
    // 这次核对被更新的通道状态作废了（最常见的是核对还没回来、用户已经在这个弹窗里存过
    // 一次）：那句「已切换到…」才是最新的事实，别拿一次过期的核对把它清掉。
    if (applied !== false) msg.textContent = '';
    renderWechatChannels();
  } catch (error) {
    $('wechat-channels').innerHTML = '';
    msg.textContent = '读取采集通道失败：' + error.message;
  }
}

async function changeWechatChannel(id) {
  if (wechatChannelBusy || !id || id === pipeWechat.selected) return;
  wechatChannelBusy = true;
  $('wechat-channel-msg').textContent = '正在保存…';
  renderWechatChannels();
  try {
    await saveWechatChannel(id);
    $('wechat-channel-msg').textContent = `已切换到「${channelLabel(pipeWechat.selected)}」，下次同步按它采集。`;
    refreshPipeSource();          // 顶部那块「来源状态」问的是另一条通道了
    initTargets();
  } catch (error) {
    $('wechat-channel-msg').textContent = '未能切换通道：' + error.message;
  } finally {
    wechatChannelBusy = false;
    // 按 pipeWechat.selected 重画：保存失败时它没变，单选按钮也就拨回原来那条
    // —— 留在用户点的那一项上等于谎报保存成功。
    renderWechatChannels();
  }
}

$('wechat-channels').addEventListener('change', e => {
  const input = e.target.closest('input[name=wechat-channel]');
  if (input) changeWechatChannel(input.value);
});

// 通道换过了（可能是别的页面换的，storage 事件那条路）：弹窗正开着时把单选按钮也
// 拨过去，别让它停在一条已经不生效的通道上。
document.addEventListener('kb:channels-changed', () => {
  if ($('wechat-modal').style.display !== 'none') renderWechatChannels();
});

function closeWechatSetup() { $('wechat-modal').style.display = 'none'; }
function dismissWechatWelcome() {
  $('wechat-welcome').hidden = true;
  try { localStorage.setItem('kb-wechat-welcome-dismissed', '1'); } catch {}
}
function wechatConfigured(open, id) {
  // 接入向导里点「API Key」「模型配置」:向导留在底下不关,新弹窗叠在它上面。
  // 原来是先 closeWechatSetup() 再 open(),于是关掉上面那层之后人落回空白页面,
  // 还得重新点开向导才能接着走完剩下的步骤。
  const modal = $(id);
  modal.classList.add('stacked');   // 先加类再打开:syncOverlays 按 z-index 认最上层
  open();
  syncOverlays();
  const done = new MutationObserver(() => {
    if (modal.style.display !== 'none') return;
    done.disconnect();
    modal.classList.remove('stacked');
    refreshWechatSetup();           // 刚配完的 key / 模型,向导那两步的状态跟着更新
  });
  done.observe(modal, { attributes: true, attributeFilter: ['style'] });
}

function renderWechatSetup() {
  const s = wechatSetup || {}, busy = !!s.running || wechatRequest;
  const docker = s.deployment === 'docker';
  $('wechat-title').textContent = docker ? '配置 Docker 知识库' : '接入你的绿泡泡';
  $('wechat-welcome-title').textContent = docker ? '让文章进入你的知识库' : '让知识库接上你的绿泡泡';
  $('wechat-intro').textContent = docker ? '在 Mac 采集文章，在容器中索引、检索和分析。' : '从本机已同步的公众号推送中发现文章，收进知识库。';
  $('wechat-source-title').textContent = docker ? '导入文章目录' : '接入本机绿泡泡';
  $('wechat-source-hint').textContent = docker ? '挂载 Mac 导出的 vault，再运行知识库索引' : '已有配置直接复用，缺 key 时自动准备获取工具';
  $('wechat-runtime-hint').textContent = docker ? '挂载与索引步骤见项目的 Docker 部署说明。' : '可以关闭窗口，任务继续在后台执行。获取和验证不需要大模型 API。';
  $('wechat-status').textContent = s.message || '正在读取绿泡泡接入状态…';
  $('wechat-status').setAttribute('aria-busy', String(busy));
  $('wechat-provider-state').textContent = s.provider_ready ? '已配置' : '待配置';
  $('wechat-model-state').textContent = s.models_ready ? '已就绪' : '待配置';
  $('wechat-database-state').textContent = docker ? '目录挂载' : s.ready ? '已验证' : s.running ? '进行中' : '待接入';
  $('wechat-dot').className = s.ready ? 'on' : 'off';
  $('wechat-dot').textContent = s.running ? '…' : '●';
  $('wechat-check').disabled = busy || s.can_connect === false;
  $('wechat-check').textContent = s.can_connect === false ? '请在 Mac 本机接入' : busy ? '正在处理…' : s.ready ? '重新检查连接' : '检查并接入绿泡泡';
  const authorization = s.state === 'authorization_required' && !busy && s.can_connect !== false;
  $('wechat-authorization').hidden = !authorization;
  $('wechat-acquire').disabled = busy || !$('wechat-consent').checked;
  $('wechat-next').hidden = !s.ready;
  $('wechat-account-row').hidden = s.state !== 'account_selection_required';
  const select = $('wechat-account');
  const accounts = s.accounts || [];
  const signature = JSON.stringify(accounts);
  if (select.dataset.accounts !== signature) {
    select.innerHTML = '<option value="">请选择本人账号</option>' + accounts.map(a =>
      `<option value="${esc(a.id)}">${esc(a.label)}</option>`).join('');
    select.dataset.accounts = signature;
  }
  if (s.state === 'account_selection_required' && !select.value) $('wechat-check').disabled = true;
  let dismissed = false;
  try { dismissed = localStorage.getItem('kb-wechat-welcome-dismissed') === '1'; } catch {}
  $('wechat-welcome').hidden = s.ready || dismissed;
  $('wechat-welcome-status').textContent = s.can_connect === false ? 'Docker 已就绪。配置模型、挂载文章目录后即可建立知识索引；绿泡泡 key 在 Mac 本机获取。' : s.running ? s.message :
    '配置供应商和模型后，接入本机绿泡泡，开始收集公众号文章。';
}

async function refreshWechatSetup() {
  if (wechatRequest) return;
  const version = ++wechatStatusVersion;
  clearTimeout(wechatPoll);
  try {
    const response = await fetch('/api/wechat/setup');
    if (!response.ok) throw new Error(await apiFail(response, '状态读取失败'));
    const s = await response.json();
    if (version !== wechatStatusVersion) return;
    if (s.confirmation_token !== wechatSetup?.confirmation_token) $('wechat-consent').checked = false;
    wechatSetup = s;
    renderWechatSetup();
    if (s.running) wechatPoll = setTimeout(refreshWechatSetup, 1500);
  } catch (error) {
    if (version !== wechatStatusVersion) return;
    $('wechat-error').textContent = error.message;
    // A lost response must not make a running acquisition appear finished.
    if (wechatSetup?.running) wechatPoll = setTimeout(refreshWechatSetup, 4000);
  }
}

async function startWechatSetup(action = 'connect') {
  if (wechatRequest || wechatSetup?.running || wechatSetup?.can_connect === false) return;
  const body = { action };
  if (action === 'acquire') {
    if (!$('wechat-consent').checked || wechatSetup?.state !== 'authorization_required') return;
    Object.assign(body, { confirmed: true, confirmation_token: wechatSetup.confirmation_token });
  } else if (wechatSetup?.state === 'account_selection_required') {
    if (!$('wechat-account').value) return;
    body.account_id = $('wechat-account').value;
  }
  wechatRequest = true; wechatStatusVersion++; clearTimeout(wechatPoll);
  $('wechat-error').textContent = ''; renderWechatSetup();
  try {
    const response = await fetch('/api/wechat/setup', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-KB-Wechat-Action': '1' },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(await apiFail(response, '接入未启动'));
    wechatSetup = { ...wechatSetup, ...await response.json() };
    $('wechat-consent').checked = false;
  } catch (error) { $('wechat-error').textContent = error.message; }
  finally { wechatRequest = false; renderWechatSetup(); await refreshWechatSetup(); }
}

async function useLocalWechat() {
  try {
    // 重新核对：缓存里那份可能已经被别处改过，陈旧的 selected 会让下面那句直接短路。
    await loadPipeChannels(true);
    await saveWechatChannel('wechat_local');   // 接入完成了，采集通道就定在本机这条
    renderWechatChannels();
    await changePipeChannel('wechat_local');
    selectRunChannel('wechat_local');   // 下一次新建同步默认就抓本机这条通道
    closeWechatSetup(); switchTab('pipe');
    await refreshPipeSource();
  } catch (error) { $('wechat-error').textContent = error.message; }
}
