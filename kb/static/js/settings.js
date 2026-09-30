// ── MCP 管理 ──────────────────────────────────────────────────────────────
let mcpCmd = '', mcpConfig = '';

async function refreshMcp() {
  try {
    const s = await fetch('/api/mcp/status').then(r => r.json());
    mcpCmd = s.command || '';
    mcpConfig = s.config || '';
    $('mcp-config').textContent = mcpConfig;
    $('mcp-cmd').textContent = mcpCmd;
    // 工具清单由后端从 MCP server 现取,前端不写死(写死过一次,漏了 kb_enumerate)
    const tools = s.tools || [];
    $('mcp-tools').innerHTML = tools.length
      ? tools.map(t => `<b title="${esc(t.desc || '')}">${esc(t.name)}</b>`).join('、') + '。'
      : '(取不到工具清单)';
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
// option 的 value 是带家的 "provider:model_id":裸 id 传回后端只能靠前缀猜家,
// 同一个 id 两家中转站都可能有,猜错就是拿 A 家 key 打 B 家的站。
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
    // 后端已经把各阶段当前指向的模型并进候选了;真要是还对不上(接口版本不一致、
    // 刚被别处改掉),这里补一条并选中它。不补的话浏览器会默认选中第一个 option,
    // 而发送时总是带上下拉的值 —— 面板里配好的问答模型就被静默换掉了
    if (d.default && ![...sel.options].some(o => o.value === d.default)) {
      sel.insertAdjacentHTML('afterbegin',
        `<option value="${esc(d.default)}">${esc(modelLabel(d.default))}</option>`);
      sel.value = d.default;
    }
    // 默认项若未配 key,退到第一个可用的
    if (sel.selectedOptions[0]?.disabled) {
      const first = [...sel.options].find(o => !o.disabled);
      if (first) first.selected = true;
    }
  } catch { /* 保底:留空下拉,发送时后端用默认模型 */ }
}

// ── 模型分配:每个用到大模型的阶段各选一家一模型 ──────────────────────────
// 下拉的 value 是 "provider:model_id",供应商跟着选项一起存下去——只存模型名的话,
// 同一个 id 在两家中转站都可能有,后端只能按前缀猜,猜错就是拿 A 家 key 打 B 家的站。
const STAGE_SRC_NOTE = {
  default: '内置默认', env: '环境变量', legacy: '旧配置', auto: '自动挑选', config: '',
};

function stageOptions(st, providers) {
  const allow = st.providers.length ? st.providers : providers.map(p => p.key);
  const cur = st.configured;
  let seen = false, html = '';
  for (const p of providers) {
    if (!allow.includes(p.key)) continue;
    let ids = p.models.slice();
    // 当前配的 id 可能没在「API Key」面板里勾选(比如老配置、内置默认),补进来免得一存就丢
    if (cur && cur.provider === p.key && !ids.includes(cur.model)) ids.unshift(cur.model);
    if (!ids.length) continue;
    html += `<optgroup label="${esc(p.label)}${p.has_key ? '' : '(未配 key)'}">`;
    for (const id of ids) {
      const v = `${p.key}:${id}`;
      const on = cur && cur.provider === p.key && cur.model === id;
      if (on) seen = true;
      html += `<option value="${esc(v)}"${on ? ' selected' : ''}>${esc(id)}</option>`;
    }
    html += '</optgroup>';
  }
  const auto = `<option value=""${seen ? '' : ' selected'}>跟随默认</option>`;
  return auto + html;
}

function stageNow(st) {
  if (!st.model) {
    return `<div class="stage-now warn">没有可用模型:这步只能用 ${esc(st.providers.join('/'))},那家还没配 key</div>`;
  }
  const note = st.source === 'config' ? '已指定'
    : `跟随${STAGE_SRC_NOTE[st.source] || st.source}`
      + (st.source === 'env' ? `(${esc(st.env)})` : '');
  const warn = st.ready ? '' : ' warn';
  const tail = st.ready ? '' : ' · 该供应商还没配 API key,这步会报错';
  return `<div class="stage-now${warn}">当前:<b>${esc(st.model)}</b>`
    + ` · ${esc(st.provider)} · ${note}${tail}</div>`;
}

// 推荐那行:后端按「档位」在当前实际可选的模型里挑出来的具体 id + 一句为什么。
// 挑不出来(那家没配 key、中转站只开了别的型号)时退回档位说法,总得说点能照着
// 做的东西——只写「推荐主力档」而不指到一个选项,面板上就是一句废话。
function stageRec(st) {
  if (!st.rec_why) return '';
  const cur = st.provider && st.model ? `${st.provider}:${st.model}` : '';
  const pick = st.recommend || '';
  const on = pick && pick === cur;
  const what = pick ? `<b>${esc(modelLabel(pick))}</b>` : esc(st.rec_tier || '');
  // 模型 id 走 data-* 读到运行时,不拼进 onclick 的 JS 字面量里:esc() 是 HTML 属性
  // 转义,`&#39;` 在属性值里会被解回真正的单引号 —— 一个带引号的 id(中转站的型号
  // 名是用户那侧给的)就把 onclick 断成 `saveStage('wiki','a'break')`,点下去只抛
  // SyntaxError、什么也不存。阶段 key 是 STAGES 里的固定值,照旧内联。
  const act = pick && !on
    ? ` <button class="text-button" data-rec="${esc(pick)}"`
      + ` onclick="saveStage('${esc(st.key)}', this.dataset.rec)">改用推荐</button>`
    : (on ? ' · 已在用' : '');
  return `<div class="stage-rec">推荐:${what} · ${esc(st.rec_why)}${act}</div>`;
}

function stageRow(st, providers) {
  const locked = st.source === 'env' && !st.configured;
  return `<div class="stage-row">
    <div>
      <div class="stage-name">${esc(st.label)}</div>
      <div class="stage-hint">${esc(st.hint)}</div>
    </div>
    <select onchange="saveStage('${esc(st.key)}', this.value)"
            title="${locked ? '环境变量已指定,选了也会被它压过' : ''}">
      ${stageOptions(st, providers)}
    </select>
    ${stageNow(st)}
    ${stageRec(st)}
  </div>`;
}

async function refreshStages() {
  try {
    const d = await fetch('/api/stages').then(r => r.json());
    $('stages-list').innerHTML = d.stages.map(s => stageRow(s, d.providers)).join('');
  } catch (e) {
    $('stages-list').innerHTML = '<div class="empty">状态获取失败</div>';
  }
}

function openStages() {
  $('stages-modal').style.display = '';
  $('stages-msg').textContent = '';
  refreshStages();
}
function closeStages() { $('stages-modal').style.display = 'none'; }

// 连着改两个下拉时,两次保存排队走,不并发:后端已经有锁保证不会互相覆盖,
// 这里只是免得两个请求的响应交错回来、把列表渲染成旧的那一份。
let stageSaveChain = Promise.resolve();

function saveStage(key, value) {
  stageSaveChain = stageSaveChain.then(() => _saveStage(key, value)).catch(() => {});
  return stageSaveChain;
}

async function _saveStage(key, value) {
  // value 空 = 清除该阶段配置,回落内置默认/自动挑选
  const i = value.indexOf(':');
  const body = i < 0 ? { key, provider: '', model: '' }
    : { key, provider: value.slice(0, i), model: value.slice(i + 1) };
  $('stages-msg').textContent = '保存中…';
  try {
    const r = await fetch('/api/stages', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (await toastIfFail(r, '保存失败')) { $('stages-msg').textContent = ''; return; }
    const d = await r.json();
    $('stages-list').innerHTML = d.stages.map(s => stageRow(s, d.providers)).join('');
    $('stages-msg').textContent = '✓ 已保存,即时生效';
    loadModels();       // 问答下拉的默认项跟着「问答 · 回答」阶段走
    refreshWechatSetup();
    if (key === 'fav') { // 收藏页那个下拉和「收藏 · 分诊与行动卡」是同一处配置
      favToolsInited = false;
      initFavTools();
    }
  } catch (e) {
    $('stages-msg').textContent = '';
    toast('保存失败', { detail: e.message });
  }
}

// ── API Key 设置 ──────────────────────────────────────────────────────────
function fmtKeyState(k) {
  const s = !k.set ? '未设置'
    : (k.source === 'env' ? `key 由环境变量提供(${k.masked})` : `key 已设置(${k.masked})`);
  return k.base_url_source === 'env' ? s + ' · base_url 由环境变量提供' : s;
}

function updateOpenaiConversionState() {
  $('openai-conversion-state').textContent = $('openai-protocol-conversion').checked
    ? '协议转换已开启' : '使用原生 Responses';
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
    $('openai-protocol-conversion').checked = k.openai.protocol_conversion_enabled !== false;
    $('openai-protocol-conversion').disabled = false;
    updateOpenaiConversionState();
    $('keys-dot').className = (k.anthropic.set || k.openai.set) ? 'on' : 'off';
    return k;
  } catch { $('keys-msg').textContent = '状态获取失败'; }
}

function openKeys() {
  $('keys-modal').style.display = '';
  $('keys-msg').textContent = '';
  $('key-anthropic').value = '';   // key 框永远空着(不回显密文),url 框由 refreshKeys 回填
  $('key-openai').value = '';
  $('openai-protocol-conversion').disabled = true;
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
  if (!$('openai-protocol-conversion').disabled) {
    body.openai_protocol_conversion_enabled = $('openai-protocol-conversion').checked;
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
    refreshWechatSetup();
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
  if (p === 'openai' && !$('openai-protocol-conversion').disabled) {
    save.openai_protocol_conversion_enabled = $('openai-protocol-conversion').checked;
  }
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
      `<label><input type="checkbox" value="${esc(id)}"${cur.includes(id) ? ' checked' : ''}> ${esc(id)}</label>`
    ).join('') || '<span class="hint">中转站没返回模型</span>';
    box.hidden = false;
    $('models-' + p + '-save').hidden = !d.models.length;
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
