// Local onboarding stays independent of cloud models and of the active sync channel.
let wechatSetup = null, wechatPoll = null, wechatRequest = false;
let wechatStatusVersion = 0;
const WECHAT_WINDOWS_AGENT_PROMPT = `请在本项目目录中协助接入这台 Windows x64 上我自己的绿泡泡 4.x，只采集我选择的公众号。
先检查本人账号登录、公众号推送同步与数据库目录；自定义目录使用 KB_WECHAT_DATABASE_ROOT。已有可用 Reader 或材料优先复用，不覆盖、不重复获取。
优先使用「设置 → 绿泡泡接入」的检查与接入流程，由项目准备固定版本的 Windows Reader、SQLCipher 与只读获取工具；不安装 Mac 的 wxkey、Go 或 LLDB。
只有明确缺少材料时，说明将只读访问本机绿泡泡进程，经我明确确认后再获取；不要注入、重启、修改绿泡泡、自动提权或关闭安全软件。权限不一致或版本不兼容时停下来说明，不无限重试。
我负责本人账号登录与确认；不要要求我提供密码或 key，不打印、上传或提交密钥、聊天内容或私有配置。
获取后必须验证 live_database_read_ok=true，再选择一个公众号小范围采集；不能把拿到 key 当作接入成功，不读取私聊或群聊作为文章。
完成后说明验证结果、版本边界与未解决问题，提醒我回接入页面「刷新状态」，验证后再同步公众号。`;
const WECHAT_AGENT_PROMPT = `请在本项目目录中，根据 README 协助接入这台 Mac 上我自己的绿泡泡，目标是采集我选择的公众号文章。

1. 先检查 macOS、uv、Go、Apple Command Line Tools，以及本人账号是否登录、目标公众号推送是否已同步。Docker 或远程服务器不能获取这台 Mac 的 key，请在 Mac 本机运行本项目。
2. 检查已有 Reader、配置和数据库读取能力；有可用配置或密钥就复用，不覆盖、不重复获取。已有接入任务运行时先等待，不要并行获取。缺少工具时使用本项目固定版本的 Reader、SQLCipher 和 wxkey，无需额外安装整个 wechat-intelligence-hub 或其他 Skill。
3. 优先使用本项目「设置 → 绿泡泡接入」的检查与接入流程，不绕过确认校验，不用通知预览代替数据库读取。没有浏览器操作能力时，引导我完成必要的页面点击。
4. 只有确实缺少可用 key 时，说明管理员授权、访问绿泡泡进程、可能退出或重启绿泡泡并创建重签名副本等影响，经我明确确认后，使用项目的备份和获取流程。不要擅自关闭 SIP 或修改绿泡泡原程序。
5. 我负责登录绿泡泡和完成系统授权。不要让我在对话中提供密码或 key，不要打印、上传或提交密钥、原始聊天及私有配置到 Git。
6. 获取后验证 live_database_read_ok=true，再让我选择一个公众号做小范围采集，确认正文成功保存。验证仅限公众号，不读取私聊或群聊；不能把拿到 key 当作接入成功。
7. 失败时先定位原因，不反复扫描、重启或无限重试。完成后报告验证结果、实际支持范围、系统或绿泡泡改动和未解决的问题，并提醒我回接入页面「刷新状态」，已验证后再去同步公众号。

仅处理本机已同步推送中的文章，正文需仍可访问；不承诺获取公众号全部历史。`;

function openWechatSetup() {
  $('wechat-modal').style.display = '';
  $('wechat-error').textContent = '';
  $('wechat-agent-prompt').value = WECHAT_AGENT_PROMPT;
  $('wechat-agent-copy-status').textContent = '';
  refreshWechatSetup();
}

async function copyWechatAgentPrompt() {
  const button = $('wechat-agent-copy'), field = $('wechat-agent-prompt');
  const status = $('wechat-agent-copy-status');
  if (button.disabled) return;
  button.disabled = true;
  status.textContent = '';
  try {
    await navigator.clipboard.writeText(field.value);
    status.textContent = '已复制，粘贴给你的本机智能体即可。';
  } catch {
    field.focus();
    field.select();
    status.textContent = '无法自动复制，已选中提示词，请按 ⌘C 或 Ctrl+C 复制。';
  } finally {
    button.disabled = false;
  }
}

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
  const windows = s.platform === 'windows';
  $('wechat-agent-prompt').value = windows ? WECHAT_WINDOWS_AGENT_PROMPT : WECHAT_AGENT_PROMPT;
  $('wechat-authorization-hint').textContent = windows ?
    '确认后，固定版本工具仅只读访问本人绿泡泡进程中的数据库访问材料，最多 3 分钟；不会注入、退出、重启或修改客户端，不会自动提权。若权限不足或版本不兼容会停止。页面不接收密码或明文 key，失败不会自动再次获取。' :
    '固定版本 wxkey 会请求 macOS 管理员授权、访问绿泡泡进程，可能退出或重启绿泡泡并创建重签名副本。请先保存正在编辑的消息。密码仅在系统授权框输入，页面不接收密码或明文 key；失败不会自动再次获取。';
  $('wechat-method-title').textContent = docker ? '请在 Mac 本机完成绿泡泡接入' : '方式一：在此页面接入';
  $('wechat-title').textContent = docker ? '配置 Docker 知识库' : '接入你的绿泡泡';
  $('wechat-welcome-title').textContent = docker ? '让文章进入你的知识库' : '让知识库接上你的绿泡泡';
  $('wechat-intro').textContent = docker ? '在 Mac 采集文章，在容器中索引、检索和分析。' : '从本机已同步的公众号推送中发现文章，收进知识库。';
  $('wechat-source-title').textContent = docker ? '导入文章目录' : '接入本机绿泡泡';
  $('wechat-source-hint').textContent = docker ? '挂载 Mac 导出的 vault，再运行知识库索引' : windows ? 'Windows x64 · 绿泡泡 4.x；已有材料优先复用，获取前需本人确认' : '已有配置直接复用，缺 key 时自动准备获取工具';
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
    await loadPipeChannels();
    setPipeChannel('wechat_local');
    selectRunChannel('wechat_local');   // 下一次新建同步默认就抓公众号这条
    closeWechatSetup();
    subsPendingKind = 'wechat';         // 落到「我的订阅」的公众号视图,连接状态就地可见
    switchTab('subs');
  } catch (error) { $('wechat-error').textContent = error.message; }
}
