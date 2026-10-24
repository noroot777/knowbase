import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { attachCdp } from './pipe_history_cdp.mjs';

const base = process.argv[2];
assert.equal(new URL(base).hostname, '127.0.0.1');
const { proc, ws, send, evalJs, until, untilHere, clickAt, pageErrors } = await attachCdp();
const checks = [];
const check = (label, value) => { assert.ok(value, label); checks.push(label); };
const wait = async (label, expression, timeout) => check(label, await until(expression, timeout));
const click = async selector => check('点击 ' + selector, await clickAt(selector));
const api = async (path, body) => {
  const response = await fetch(base + path, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  } : {});
  assert.ok(response.ok, path + ': ' + response.status);
  return response.json();
};
const idle = () => wait('保存恢复可操作', 'stageSavePending === 0');
const modelState = () => evalJs(`({ask: $('ask-model').value, fav: $('fav-model').value,
  known: modelSettingsRevision, applied: {...modelSettingsApplied}, stage: stageConfigRevision})`);
const screenshot = async name => {
  const image = await send('Page.captureScreenshot', { format: 'png' });
  mkdirSync('output/playwright', { recursive: true });
  writeFileSync(`output/playwright/model-reconciliation-${name}.png`, Buffer.from(image.result.data, 'base64'));
};
const open = async () => {
  await evalJs("window.previousStageCard = document.querySelector('.stage-preset'); openStages()");
  await wait('模型面板读取当前版本', "document.querySelectorAll('.stage-preset').length === 2 && document.querySelector('.stage-preset') !== window.previousStageCard && stageConfigRevision === modelSettingsRevision");
};
const paint = () => evalJs('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
const deadline = process.argv.includes('--real-deadline') ? 20000 : 1000;
const paused = new Map();
let holdReads = false;
let holdPost = false;
let heldPostStatus;
ws.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (message.method !== 'Fetch.requestPaused') return;
  const request = message.params;
  if ((holdReads && request.request.method === 'GET') || (holdPost && request.request.method === 'POST')) {
    paused.set(request.requestId, request.request.method);
    if (request.request.method === 'POST') heldPostStatus = request.responseStatusCode;
  } else void send('Fetch.continueResponse', { requestId: request.requestId });
});
const release = async method => {
  for (const [requestId, requestMethod] of paused) {
    if (method && requestMethod !== method) continue;
    paused.delete(requestId);
    await send('Fetch.continueResponse', { requestId });
  }
};
const savedPreset = key => wait('新保存生效 ' + key,
  `document.querySelector('.stage-preset[data-preset="${key}"]').getAttribute('aria-pressed') === 'true' && stageSavePending === 0`);

try {
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: base });
  await wait('页面初始化', "typeof openStages === 'function' && $('ask-model').options.length > 1");
  await evalJs('Promise.all([loadModels(), initFavTools(true)])');
  if (process.argv.includes('--without-reconciliation')) await evalJs('reconcileModelConsumers = () => {}');
  const gpt = await api('/api/stages/preset', { preset: 'gpt' });
  await evalJs('initFavTools(true)');
  await wait('收藏先读新版本也会补齐落后的问答', `$('ask-model').value === 'openai:gpt-5.5'
    && modelSettingsApplied.ask === ${gpt.revision} && modelSettingsApplied.fav === ${gpt.revision}`);
  await open();
  await screenshot('current-gpt');
  await click('#stages-modal button[onclick="closeStages()"]');
  await click('#tab-ask');
  await click('#chat-input');
  await send('Input.insertText', { text: '跨消费者同步验证' });
  await click('#chat-send');
  await wait('真实提问携带 GPT', "fetch('/__fixture/status').then(response => response.json()).then(data => data.last_ask.model === 'openai:gpt-5.5')");
  const claude = await api('/api/stages/preset', { preset: 'claude' });
  await evalJs('loadModels()');
  await wait('问答先读新版本也会补齐落后的收藏', `$('fav-model').value === 'anthropic:claude-sonnet-4-6'
    && modelSettingsApplied.fav === ${claude.revision}`);

  await api('/__fixture/arm', { '/api/models': 1 });
  await evalJs('void loadModels()');
  await wait('暂缓旧问答快照', "fetch('/__fixture/status').then(response => response.json()).then(data => data.gates['/api/models']?.waiting === 1)");
  const next = await api('/api/stages/preset', { preset: 'gpt' });
  await evalJs('initFavTools(true)');
  check('全局已知版本不能冒充问答已应用版本', await evalJs(`modelSettingsRevision === ${next.revision} && modelSettingsApplied.ask < modelSettingsRevision`));
  await api('/__fixture/release', {});
  await wait('丢弃旧问答响应后自动补读而非留在旧值', `$('ask-model').value === 'openai:gpt-5.5' && modelSettingsApplied.ask === ${next.revision}`);
  await api('/__fixture/arm', { '/api/models': 1 });
  await evalJs('void loadModels()');
  await wait('再次暂缓旧问答快照', "fetch('/__fixture/status').then(response => response.json()).then(data => data.gates['/api/models']?.waiting === 1)");
  const panel = await api('/api/stages/preset', { preset: 'claude' });
  await evalJs('initFavTools(true)');
  check('面板前已有已知但未应用的版本', await evalJs(`modelSettingsRevision === ${panel.revision} && modelSettingsApplied.ask < modelSettingsRevision`));
  await open();
  check('相同已知版本的面板仍补齐问答', await evalJs(`$('ask-model').value === 'anthropic:claude-sonnet-4-6' && modelSettingsApplied.ask === ${panel.revision}`));
  await api('/__fixture/release', {});
  await paint();
  check('释放旧读取不会覆盖新面板', (await modelState()).ask === 'anthropic:claude-sonnet-4-6');

  await evalJs(`(() => {
    const select = $('ask-model');
    select.value = 'anthropic:claude-haiku-4-5';
    select.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  const manual = await api('/api/stages/preset', { preset: 'claude' });
  await evalJs('initFavTools(true)');
  await wait('同步配置但保留明确手选的临时模型', `$('ask-model').value === 'anthropic:claude-haiku-4-5' && modelSettingsApplied.ask === ${manual.revision}`);
  await open();
  check('打开面板也不重置手选模型', (await modelState()).ask === 'anthropic:claude-haiku-4-5');
  await evalJs("doSendChat('验证临时手选', false)");
  check('真实提问仍尊重临时手选模型', (await api('/__fixture/status')).last_ask.model === 'anthropic:claude-haiku-4-5');
  await click('.stage-preset[data-preset="claude"]');
  await idle();
  await wait('明确切换整组才重置临时选择', "$('ask-model').value === 'anthropic:claude-sonnet-4-6' && modelAskSelection === null");
  console.log('consumer-state', JSON.stringify(await modelState()));

  if (deadline !== 20000) await evalJs(`(() => {
    const original = window.setTimeout;
    window.setTimeout = (callback, delay, ...args) => original(callback, delay === STAGE_SAVE_TIMEOUT_MS ? ${deadline} : delay, ...args);
  })()`);
  if (process.argv.includes('--without-recovery-signal')) await evalJs(`(() => {
    const original = refreshStages;
    refreshStages = retry => original(retry);
  })()`);
  await send('Fetch.enable', { patterns: [{ urlPattern: base + '/api/stages*', requestStage: 'Response' }] });
  for (const branch of ['conflict', 'stale-success']) {
    await open();
    if (branch === 'conflict') await api('/api/stages/preset/name', { preset: 'claude', name: '冲突恢复验证' });
    holdReads = true;
    holdPost = branch === 'stale-success';
    const started = Date.now();
    await click('.stage-preset[data-preset="gpt"]');
    if (holdPost) {
      check('成功提交的真实响应暂缓', await untilHere(() => [...paused.values()].includes('POST')));
      check('过期成功分支确实收到成功而非冲突', heldPostStatus === 200);
      await api('/api/stages/preset', { preset: 'claude' });
      await evalJs('initFavTools(true)');
      holdPost = false;
      await release('POST');
    }
    check('恢复读取的真实响应暂缓 ' + branch, await untilHere(() => [...paused.values()].includes('GET')));
    check('恢复读取仍属同一次保存', await evalJs('stageSavePending === 1'));
    await wait('恢复 GET 也受总截止时间约束 ' + branch, 'stageSavePending === 0', deadline + 3000);
    const state = await evalJs(`({busy: stageSavePending, message: $('stages-msg').textContent,
      disabled: [...$('stages-content').querySelectorAll('select, button, input')].some(control => control.disabled && control.dataset.unavailable !== 'true')})`);
    check('超时解锁并保留未确认提示 ' + branch, !state.disabled && state.message.includes('超时') && state.message.includes('尚未确认'));
    console.log('recovery-deadline', JSON.stringify({ branch, deadline, elapsed: Date.now() - started, ...state }));
    await screenshot(`${branch}-${deadline}`);
    holdReads = false;
    await open();
    const before = (await api('/api/stages')).revision;
    await click('.stage-preset[data-preset="claude"]');
    await savedPreset('claude');
    const revision = (await api('/api/stages')).revision;
    check('超时后新的写入确实成功提交 ' + branch, revision > before);
    await release();
    await paint();
    check('迟到恢复响应不能覆盖新保存 ' + branch, await evalJs(`stageConfigRevision === ${revision} && $('ask-model').value === 'anthropic:claude-sonnet-4-6' && stageSavePending === 0`));
  }
  check('没有页面脚本异常', pageErrors.length === 0);
  console.log(JSON.stringify({ total: checks.length, checks }, null, 2));
} catch (error) {
  console.error(JSON.stringify({ checks, state: await modelState(), pageErrors }));
  throw error;
} finally {
  holdReads = false;
  holdPost = false;
  await release();
  await send('Fetch.disable');
  await api('/__fixture/release', {}).catch(() => {});
  ws.close();
  proc.kill();
}
