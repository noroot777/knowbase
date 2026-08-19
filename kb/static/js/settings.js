// ── MCP 管理 ──────────────────────────────────────────────────────────────
let mcpCmd = '', mcpConfig = '';

async function refreshMcp() {
  try {
    const s = await fetch('/api/mcp/status').then(r => r.json());
    mcpCmd = s.command || '';
    mcpConfig = s.config || '';
    $('mcp-config').textContent = mcpConfig;
    $('mcp-cmd').textContent = mcpCmd;
    // 圆点/文案跟随 claude CLI 注册态;CLI 不可用不算错误(通用配置照样能用)
    $('mcp-dot').className = s.registered ? 'on' : 'off';
    $('mcp-status').textContent = !s.cli_available ? '通用配置就绪(未检测到 claude CLI)'
      : s.registered ? '已注册 ✓ (Claude Code 对话中可直接用)' : '未注册(可用上方通用配置)';
    $('mcp-reg').style.display = (s.cli_available && !s.registered) ? '' : 'none';
    $('mcp-unreg').style.display = (s.cli_available && s.registered) ? '' : 'none';
    return s;
  } catch {
    $('mcp-status').textContent = '状态获取失败';
  }
}

function openMcp() { $('mcp-modal').style.display = ''; $('mcp-msg').textContent = ''; refreshMcp(); }
function closeMcp() { $('mcp-modal').style.display = 'none'; }

async function mcpRegister() {
  $('mcp-msg').textContent = '注册中…';
  try {
    const r = await fetch('/api/mcp/register', { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.status);
    $('mcp-msg').textContent = '✓ ' + (d.message || '注册成功');
  } catch (e) {
    $('mcp-msg').textContent = '注册失败: ' + e.message;
  }
  refreshMcp();
}

async function mcpUnregister() {
  $('mcp-msg').textContent = '取消注册中…';
  try {
    const r = await fetch('/api/mcp/unregister', { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.status);
    $('mcp-msg').textContent = '✓ ' + (d.message || '已取消注册');
  } catch (e) {
    $('mcp-msg').textContent = '失败: ' + e.message;
  }
  refreshMcp();
}

function copyCmd() {
  navigator.clipboard.writeText(mcpCmd).then(() => {
    $('mcp-msg').textContent = '✓ 命令已复制';
  });
}

function copyMcpConfig() {
  navigator.clipboard.writeText(mcpConfig).then(() => {
    $('mcp-msg').textContent = '✓ 配置已复制';
  });
}

// ── 问答模型下拉 ──────────────────────────────────────────────────────────
async function loadModels() {
  try {
    const d = await fetch('/api/models').then(r => r.json());
    const sel = $('ask-model');
    sel.innerHTML = '';
    for (const m of d.models) {
      const o = document.createElement('option');
      o.value = m.key;
      o.textContent = m.label + (m.enabled ? '' : '(未配 key)');
      o.disabled = !m.enabled;
      if (m.key === d.default) o.selected = true;
      sel.appendChild(o);
    }
    // 默认项若未配 key,退到第一个可用的
    if (sel.selectedOptions[0]?.disabled) {
      const first = [...sel.options].find(o => !o.disabled);
      if (first) first.selected = true;
    }
  } catch { /* 保底:留空下拉,发送时后端用默认模型 */ }
}

// ── API Key 设置 ──────────────────────────────────────────────────────────
function fmtKeyState(k) {
  const s = !k.set ? '未设置'
    : (k.source === 'env' ? `key 由环境变量提供(${k.masked})` : `key 已设置(${k.masked})`);
  return k.base_url_source === 'env' ? s + ' · base_url 由环境变量提供' : s;
}

async function refreshKeys() {
  try {
    const d = await fetch('/api/keys').then(r => r.json());
    const k = d.keys;
    for (const p of ['anthropic', 'openai']) {
      $('key-' + p + '-st').textContent = fmtKeyState(k[p]);
      const keyEnv = k[p].source === 'env';         // key 来自环境变量→锁 key 框
      const urlEnv = k[p].base_url_source === 'env'; // base_url 来自环境变量→锁 url 框
      $('key-' + p).disabled = keyEnv;
      const url = $('url-' + p);
      url.disabled = urlEnv;
      url.value = k[p].base_url || '';               // 回填当前中转站地址,便于查看/改
    }
    $('keys-dot').className = (k.anthropic.set || k.openai.set) ? 'on' : 'off';
    return k;
  } catch { $('keys-msg').textContent = '状态获取失败'; }
}

function openKeys() {
  $('keys-modal').style.display = '';
  $('keys-msg').textContent = '';
  $('key-anthropic').value = '';   // key 框永远空着(不回显密文),url 框由 refreshKeys 回填
  $('key-openai').value = '';
  refreshKeys();
}
function closeKeys() { $('keys-modal').style.display = 'none'; }

async function saveKeys() {
  const body = {};
  for (const p of ['anthropic', 'openai']) {
    const kf = $('key-' + p), uf = $('url-' + p);
    if (!kf.disabled && kf.value !== '') body[p + '_key'] = kf.value;  // 空=不改,空格=清除
    if (!uf.disabled) body[p + '_base_url'] = uf.value;               // url 已回填,原样回传(改了才变)
  }
  if (Object.keys(body).length === 0) { $('keys-msg').textContent = '没有可改的项'; return; }
  $('keys-msg').textContent = '保存中…';
  try {
    const r = await fetch('/api/keys', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
    $('keys-msg').textContent = '✓ 已保存,即时生效';
    $('key-anthropic').value = ''; $('key-openai').value = '';
    refreshKeys();
    loadModels();   // 刷新下拉:刚配好 key 的模型解除置灰
  } catch (e) {
    $('keys-msg').textContent = '保存失败: ' + e.message;
  }
}

// 从中转站的 /v1/models 拉取可选模型,勾选后写入下拉
async function fetchModels(p) {
  const msg = $('models-' + p + '-msg');
  // 先把当前 key/base_url 存一下,拉取才有凭据
  const kf = $('key-' + p), uf = $('url-' + p), save = {};
  if (!kf.disabled && kf.value !== '') save[p + '_key'] = kf.value;
  if (!uf.disabled) save[p + '_base_url'] = uf.value;
  msg.textContent = '拉取中…';
  try {
    if (Object.keys(save).length) {
      await fetch('/api/keys', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(save) });
      kf.value = '';
    }
    const d = await fetch('/api/models/remote?provider=' + p).then(async r => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
      return r.json();
    });
    const cur = await fetch('/api/keys').then(r => r.json()).then(d => d.keys[p].models || []);
    const box = $('models-' + p + '-box');
    box.innerHTML = d.models.map(id =>
      `<label style="display:block;font-size:13px;padding:2px 0">`
      + `<input type="checkbox" value="${id}"${cur.includes(id) ? ' checked' : ''}> ${id}</label>`
    ).join('') || '<span class="hint">中转站没返回模型</span>';
    box.style.display = '';
    $('models-' + p + '-save').style.display = d.models.length ? '' : 'none';
    msg.textContent = `共 ${d.models.length} 个,勾选后保存`;
  } catch (e) { msg.textContent = '拉取失败: ' + e.message; }
}

async function saveModels(p) {
  const ids = [...$('models-' + p + '-box').querySelectorAll('input:checked')].map(x => x.value);
  $('models-' + p + '-msg').textContent = '保存中…';
  try {
    const r = await fetch('/api/models/select', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: p, models: ids }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
    $('models-' + p + '-msg').textContent = `✓ 已保存 ${ids.length} 个`;
    loadModels();   // 刷新主下拉
  } catch (e) { $('models-' + p + '-msg').textContent = '保存失败: ' + e.message; }
}
