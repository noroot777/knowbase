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
  const read = beginModelRead('ask');
  try {
    const d = await fetch('/api/models', { cache: 'no-store' }).then(r => r.json());
    if (!ownsModelRead('ask', read, d)) return;
    recordModelSettings(d);
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
    if (modelAskSelection !== null) {
      if ([...sel.options].some(option => option.value === modelAskSelection && !option.disabled)) sel.value = modelAskSelection;
      else modelAskSelection = null;
    }
    appliedModelSettings('ask', d);
  } catch { /* 保底:留空下拉,发送时后端用默认模型 */ }
  finally { finishModelRead('ask', read); }
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

// 选中的那个模型 id 选择框自己就写着(或者写着「跟随默认」),所以这一行只说它读不
// 出来的东西:「跟随」时真正落到哪个模型、环境变量压过来的那个变量名、没配 key 那条
// 红字,以及**只在真有歧义时**才说的供应商 —— 同一个 id 两家中转站都可能有(这正是
// 下拉的 value 要带 provider 的原因,见 stageOptions),不重名时念一句「openai」只是
// 在选择框旁边再摆一个词。全都没有可说的就整行不画(`.stage-now:empty`)。
function stageNow(st, providers) {
  if (!st.model) {
    return `<div class="stage-now warn">没有可用模型:这步只能用 ${esc(st.providers.join('/'))},那家还没配 key</div>`;
  }
  const allow = st.providers.length ? st.providers : providers.map(p => p.key);
  const sameId = providers.filter(p => allow.includes(p.key) && p.models.includes(st.model));
  const parts = [];
  if (st.source !== 'config') parts.push(`<b>${esc(st.model)}</b>`);
  if (st.source !== 'config' || sameId.length > 1) parts.push(esc(st.provider));
  if (st.source === 'env') parts.push(esc(st.env));
  else if (st.source !== 'config') parts.push(esc(STAGE_SRC_NOTE[st.source] || st.source));
  if (!st.ready) parts.push('该供应商还没配 API key,这步会报错');
  return `<div class="stage-now${st.ready ? '' : ' warn'}">${parts.join(' · ')}</div>`;
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
    ${stageNow(st, providers)}
    ${stageRec(st)}
  </div>`;
}

function stagePresetName(label) {
  return `<b class="stage-preset-label">${esc(label)}</b>
    <button type="button" class="stage-preset-icon" aria-label="修改组名" title="修改组名"
            onclick="editStagePresetName(this)" onkeydown="editStagePresetName(this, event)">${icon('pencil')}</button>`;
}

function stagePresetCard(preset) {
  const models = preset.main_model
    ? `主力 · ${preset.main_model}\n轻量 · ${preset.cheap_model}` : '按已勾选的模型分配';
  return `<div class="stage-preset-item" role="group" aria-label="${esc(preset.label)}预设">
    <div class="stage-preset-head">
    <div class="stage-preset-editor" data-preset="${esc(preset.key)}" data-name="${esc(preset.label)}"
         data-name-max-length="${esc(preset.name_max_length)}">
      ${stagePresetName(preset.label)}
    </div>
    <button class="stage-preset${preset.active ? ' active' : ''}"
    data-preset="${esc(preset.key)}" data-unavailable="${!preset.ready}"
    aria-label="应用${esc(preset.label)}" aria-pressed="${preset.active}"
    ${preset.ready ? '' : 'disabled'} onclick="saveStagePreset(this.dataset.preset)">
      ${preset.active ? '当前方案' : '一键应用'}
    </button></div>
    <span class="stage-preset-models">${esc(models)}</span>
    ${preset.reason ? `<span class="stage-preset-warning">${esc(preset.reason)}</span>` : ''}
    </div>`;
}

function editStagePresetName(button, keyEvent) {
  if (keyEvent) {
    if (keyEvent.key !== 'Enter' || keyEvent.isComposing) return;
    keyEvent.preventDefault();
  }
  const editor = button.closest('.stage-preset-editor');
  const errorId = 'stage-preset-name-error-' + editor.dataset.preset;
  editor.innerHTML = `<form onsubmit="saveStagePresetName(event, this)">
    <input name="name" aria-label="组名" value="${esc(editor.dataset.name)}"
           aria-describedby="${esc(errorId)}"
           aria-invalid="false" title="最多 ${esc(editor.dataset.nameMaxLength)} 个字符；回车保存,Esc 取消" required oninput="clearStagePresetNameError(this.form)">
    <button type="submit" class="stage-preset-icon" aria-label="保存组名" title="保存组名">${icon('check')}</button>
    <button type="button" class="stage-preset-icon" aria-label="取消" title="取消"
            onclick="cancelStagePresetName(this)">${icon('close')}</button>
    <div id="${esc(errorId)}" class="field-err" role="alert"></div>
    </form>`;
  const input = editor.querySelector('input');
  input.focus();
  input.select();
}

function cancelStagePresetName(button) {
  const editor = button.closest('.stage-preset-editor');
  editor.innerHTML = stagePresetName(editor.dataset.name);
  editor.querySelector('button').focus();
}

document.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || event.isComposing) return;
  const form = event.target.closest?.('#stages-presets .stage-preset-editor form');
  if (!form || stageSavePending > 0) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  cancelStagePresetName(form);
}, true);

function saveStagePresetName(event, form) {
  event.preventDefault();
  clearStagePresetNameError(form);
  const generation = stageSaveGeneration;
  const preset = form.closest('.stage-preset-editor').dataset.preset;
  const input = form.elements.namedItem('name');
  const name = input.value;
  return queueStageSave(() => persistStages('/api/stages/preset/name', { preset, name }))
    .then(() => {
      if (generation === stageSaveGeneration && form.isConnected
          && input.getAttribute('aria-invalid') === 'true') input.focus();
    });
}

function clearStagePresetNameError(form) {
  form.querySelector('.field-err').textContent = '';
  form.elements.namedItem('name').setAttribute('aria-invalid', 'false');
}

function renderStages(data, savedPreset) {
  const editors = [...$('stages-presets').querySelectorAll('.stage-preset-editor:has(form)')]
    .filter(editor => editor.dataset.preset !== savedPreset);
  const focused = document.activeElement;
  stageConfigRevision = data.revision;
  recordModelSettings(data);
  $('stages-presets').innerHTML = (data.presets || []).map(preset => stagePresetCard({
    ...preset, name_max_length: data.preset_name_max_length,
  })).join('');
  for (const editor of editors) {
    const replacement = [...$('stages-presets').querySelectorAll('.stage-preset-editor')]
      .find(element => element.dataset.preset === editor.dataset.preset);
    if (!replacement) continue;
    editor.dataset.name = replacement.dataset.name;
    replacement.replaceWith(editor);
    if (editor.contains(focused)) focused.focus();
  }
  $('stages-list').innerHTML = data.stages.map(stage => stageRow(stage, data.providers)).join('');
  setStageBusy();
}

async function refreshStages(retry = true, signal) {
  const generation = stageSaveGeneration;
  const readVersion = ++stageReadVersion;
  try {
    const operation = stageUnconfirmedOperation;
    const response = await fetch('/api/stages' + (operation ? '?operation_id=' + encodeURIComponent(operation.id) : ''), { cache: 'no-store', signal });
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    signal?.throwIfAborted();
    if (generation !== stageSaveGeneration || readVersion !== stageReadVersion) return;
    if (Number.isSafeInteger(data.revision) && data.revision < modelSettingsRevision) {
      if (retry) return refreshStages(false, signal);
      throw new Error('配置读取已过期');
    }
    renderStages(data);
    refreshModelConsumers(data, true);
    if (operation && stageUnconfirmedOperation?.id === operation.id) {
      const committed = data.operation?.status === 'committed';
      const superseded = data.revision > operation.revision;
      $('stages-msg').textContent = committed ? '上次保存已提交,当前配置已读取。'
        : superseded ? '上次保存已被后续设置取代,当前配置已读取。'
        : '当前配置已读取,上次保存仍未确认；可重新选择,旧请求不能覆盖新的成功设置。';
      if (committed || superseded) rememberStageOperation(null);
    } else if ($('stages-msg').textContent.includes('正在重新读取')) $('stages-msg').textContent = '';
    return true;
  } catch (e) {
    if (signal?.aborted) throw e;
    if (generation !== stageSaveGeneration || readVersion !== stageReadVersion) return;
    $('stages-presets').innerHTML = '';
    $('stages-list').innerHTML = '<div class="empty">状态获取失败</div>';
    $('stages-msg').textContent = '读取配置失败,请重新打开面板核对。';
    return false;
  }
}

function openStages() {
  const interrupted = stageSavePending > 0;
  stageSaveGeneration++;
  stageSaveController?.abort();
  stageSaveController = null;
  stageSavePending = 0;
  stageSaveChain = Promise.resolve();
  $('stages-modal').style.display = '';
  $('stages-msg').textContent = interrupted ? '上次保存结果尚未确认,正在重新读取当前配置…' : '';
  setStageBusy();
  refreshStages();
}
function closeStages() { $('stages-modal').style.display = 'none'; }

// 连着改两个下拉时,两次保存排队走,不并发:后端已经有锁保证不会互相覆盖,
// 这里只是免得两个请求的响应交错回来、把列表渲染成旧的那一份。
let stageSaveChain = Promise.resolve();
let stageSavePending = 0;
let stageSaveGeneration = 0;
let stageReadVersion = 0;
let stageSaveController = null;
let stageConfigRevision;
let stageUnconfirmedOperation = readStageOperation();
const STAGE_SAVE_TIMEOUT_MS = 20000;

function readStageOperation() {
  try { return JSON.parse(sessionStorage.getItem('stage-save-operation') || 'null'); }
  catch { return null; }
}

function rememberStageOperation(operation) {
  stageUnconfirmedOperation = operation;
  try {
    if (operation) sessionStorage.setItem('stage-save-operation', JSON.stringify(operation));
    else sessionStorage.removeItem('stage-save-operation');
  } catch {}
}

function setStageBusy() {
  $('stages-content').setAttribute('aria-busy', String(stageSavePending > 0));
  $('stages-content').querySelectorAll('select, button, input').forEach(control => {
    control.disabled = stageSavePending > 0 || control.dataset.unavailable === 'true';
  });
}

function queueStageSave(action) {
  const generation = stageSaveGeneration;
  stageSavePending++;
  setStageBusy();
  stageSaveChain = stageSaveChain.then(() => {
    if (generation === stageSaveGeneration) return action();
  }).catch(() => {}).finally(() => {
    if (generation !== stageSaveGeneration) return;
    stageSavePending--;
    setStageBusy();
  });
  return stageSaveChain;
}

function saveStage(key, value) {
  return queueStageSave(() => _saveStage(key, value));
}

function saveStagePreset(preset) {
  return queueStageSave(() => persistStages('/api/stages/preset', { preset }));
}

async function _saveStage(key, value) {
  // value 空 = 清除该阶段配置,回落内置默认/自动挑选
  const i = value.indexOf(':');
  const body = i < 0 ? { key, provider: '', model: '' }
    : { key, provider: value.slice(0, i), model: value.slice(i + 1) };
  return persistStages('/api/stages', body);
}

async function persistStages(url, body) {
  const generation = stageSaveGeneration;
  const controller = new AbortController();
  stageSaveController = controller;
  stageReadVersion++;
  invalidateModelSettings();
  const operation = { id: modelOperationId(), revision: stageConfigRevision };
  rememberStageOperation(operation);
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, STAGE_SAVE_TIMEOUT_MS);
  $('stages-msg').textContent = '保存中…';
  try {
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, revision: stageConfigRevision, operation_id: operation.id }),
      signal: controller.signal,
    });
    if (!r.ok) {
      const detail = await apiFail(r, '保存失败');
      if (timedOut) throw new Error('保存超时');
      if (generation !== stageSaveGeneration) return;
      if (r.status < 500) rememberStageOperation(null);
      $('stages-msg').textContent = r.status >= 500 ? '保存结果尚未确认,请重开面板核对。' : '';
      const editor = [...$('stages-presets').querySelectorAll('.stage-preset-editor')]
        .find(element => element.dataset.preset === body.preset);
      const fieldError = editor?.querySelector('.field-err');
      if (r.status === 400 && body.name !== undefined && fieldError) {
        fieldError.textContent = detail;
        const input = editor.querySelector('input');
        input.setAttribute('aria-invalid', 'true');
        return;
      }
      toast('保存失败', { detail });
      if (r.status === 409) {
        await refreshStages(true, controller.signal);
      }
      return;
    }
    const d = await r.json();
    if (timedOut) throw new Error('保存超时');
    if (generation !== stageSaveGeneration) return;
    rememberStageOperation(null);
    if (Number.isSafeInteger(d.revision) && d.revision < modelSettingsRevision) {
      const refreshed = await refreshStages(true, controller.signal);
      if (refreshed) $('stages-msg').textContent = '配置已有后续更新,已读取当前配置。';
      return;
    }
    renderStages(d, d.renamed_preset ? body.preset : null);
    if (d.operation_revision !== undefined && d.operation_revision !== d.revision) {
      refreshModelConsumers(d);
      $('stages-msg').textContent = '这次保存后配置又有更新,已显示当前配置。';
      return;
    }
    if (d.renamed_preset) {
      refreshModelConsumers(d, true);
      $('stages-msg').textContent = '✓ 组名已保存：' + d.renamed_preset;
      return;
    }
    refreshModelConsumers(d);
    $('stages-msg').textContent = d.applied_preset
      ? `✓ 已切换为${d.applied_preset},即时生效${d.preserved.length ? '(研究阶段保持原配置)' : ''}`
      : '✓ 已保存,即时生效';
    refreshWechatSetup();
  } catch (e) {
    if (generation !== stageSaveGeneration) return;
    const detail = timedOut ? '保存超时,结果尚未确认。请重新打开面板核对后再试。' : e.message;
    $('stages-msg').textContent = timedOut ? detail : '保存结果尚未确认,请重开面板核对。';
    toast('保存失败', { detail });
  } finally {
    clearTimeout(timer);
    if (stageSaveController === controller) stageSaveController = null;
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
  invalidateModelSettings();
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
    refreshModelConsumers();
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
  invalidateModelSettings();
  const ids = [...$('models-' + p + '-box').querySelectorAll('input:checked')].map(x => x.value);
  $('models-' + p + '-msg').textContent = '保存中…';
  try {
    const r = await fetch('/api/models/select', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: p, models: ids }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
    $('models-' + p + '-msg').textContent = `✓ 已保存 ${ids.length} 个`;
    refreshModelConsumers(await r.json());
  } catch (e) { $('models-' + p + '-msg').textContent = '保存失败: ' + e.message; }
}
