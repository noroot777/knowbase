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
  $('wechat-status').textContent = s.message || '正在读取微信接入状态…';
  $('wechat-status').setAttribute('aria-busy', String(busy));
  $('wechat-provider-state').textContent = s.provider_ready ? '已配置' : '待配置';
  $('wechat-model-state').textContent = s.models_ready ? '已就绪' : '待配置';
  $('wechat-database-state').textContent = s.ready ? '已验证' : s.running ? '进行中' : '待接入';
  $('wechat-dot').className = s.ready ? 'on' : 'off';
  $('wechat-dot').textContent = s.running ? '…' : '●';
  $('wechat-check').disabled = busy;
  $('wechat-check').textContent = busy ? '正在处理…' : s.ready ? '重新检查连接' : '检查并接入微信';
  const authorization = s.state === 'authorization_required' && !busy;
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
  $('wechat-welcome-status').textContent = s.running ? s.message :
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
  if (wechatRequest || wechatSetup?.running) return;
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
