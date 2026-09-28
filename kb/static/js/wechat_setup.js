// Local onboarding stays independent of cloud models and of the active sync channel.
let wechatSetup = null, wechatPoll = null, wechatRequest = false;
let wechatStatusVersion = 0;
function openWechatSetup() {
  $('wechat-modal').style.display = '';
  $('wechat-error').textContent = '';
  refreshWechatSetup();
}
function closeWechatSetup() { $('wechat-modal').style.display = 'none'; }
function dismissWechatWelcome() {
  $('wechat-welcome').hidden = true;
  try { localStorage.setItem('kb-wechat-welcome-dismissed', '1'); } catch {}
}
function wechatConfigured(open) { closeWechatSetup(); open(); }

function renderWechatSetup() {
  const s = wechatSetup || {}, busy = !!s.running || wechatRequest;
  const docker = s.deployment === 'docker';
  $('wechat-title').textContent = docker ? '配置 Docker 知识库' : '接入你的微信';
  $('wechat-welcome-title').textContent = docker ? '让文章进入你的知识库' : '让知识库接上你的微信';
  $('wechat-intro').textContent = docker ? '在 Mac 采集文章，在容器中索引、检索和分析。' : '从本机已同步的公众号推送中发现文章，收进知识库。';
  $('wechat-source-title').textContent = docker ? '导入文章目录' : '接入本机微信';
  $('wechat-source-hint').textContent = docker ? '挂载 Mac 导出的 vault，再运行知识库索引' : '已有配置直接复用，缺 key 时自动准备获取工具';
  $('wechat-runtime-hint').textContent = docker ? '挂载与索引步骤见项目的 Docker 部署说明。' : '可以关闭窗口，任务继续在后台执行。获取和验证不需要大模型 API。';
  $('wechat-status').textContent = s.message || '正在读取微信接入状态…';
  $('wechat-status').setAttribute('aria-busy', String(busy));
  $('wechat-provider-state').textContent = s.provider_ready ? '已配置' : '待配置';
  $('wechat-model-state').textContent = s.models_ready ? '已就绪' : '待配置';
  $('wechat-database-state').textContent = docker ? '目录挂载' : s.ready ? '已验证' : s.running ? '进行中' : '待接入';
  $('wechat-dot').className = s.ready ? 'on' : 'off';
  $('wechat-dot').textContent = s.running ? '…' : '●';
  $('wechat-check').disabled = busy || s.can_connect === false;
  $('wechat-check').textContent = s.can_connect === false ? '请在 Mac 本机接入' : busy ? '正在处理…' : s.ready ? '重新检查连接' : '检查并接入微信';
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
  $('wechat-welcome-status').textContent = s.can_connect === false ? 'Docker 已就绪。配置模型、挂载文章目录后即可建立知识索引；微信 key 在 Mac 本机获取。' : s.running ? s.message :
    '配置供应商和模型后，接入本机微信，开始收集公众号文章。';
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
    await loadPipeChannels();
    await changePipeChannel('wechat_local');
    closeWechatSetup(); switchTab('pipe');
    await refreshPipeSource();
  } catch (error) { $('wechat-error').textContent = error.message; }
}
