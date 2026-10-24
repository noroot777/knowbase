import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { attachCdp } from './pipe_history_cdp.mjs';

const base = process.argv[2];
assert.equal(new URL(base).hostname, '127.0.0.1');
const { proc, ws, send, evalJs, until, clickAt, pageErrors } = await attachCdp();
const checks = [];
const check = (label, value) => { assert.ok(value, label); checks.push(label); };
const wait = async (label, expression) => check(label, await until(expression));
const click = async selector => check('点击 ' + selector, await clickAt(selector));
const api = async (path, body) => (await fetch(base + path, body ? {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
} : {})).json();
const idle = () => wait('保存恢复可操作', "document.querySelector('#stages-content').getAttribute('aria-busy') === 'false'");
const current = async key => {
  await click(`.stage-preset[data-preset="${key}"]`);
  await idle();
  await wait('服务器与面板一致 ' + key, `document.querySelector('.stage-preset[data-preset="${key}"]').getAttribute('aria-pressed') === 'true'`);
};
const gates = async (field, expected) => {
  const expression = `fetch('/__fixture/status').then(response => response.json()).then(data =>
    Object.entries(${JSON.stringify(expected)}).every(([key, count]) => data.gates[key]?.${field} === count))`;
  await wait('闸门 ' + field, expression);
};
const paint = () => evalJs('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
const screenshot = async name => {
  const response = await send('Page.captureScreenshot', { format: 'png' });
  mkdirSync('output/playwright', { recursive: true });
  writeFileSync(`output/playwright/${name}.png`, Buffer.from(response.result.data, 'base64'));
};

try {
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: base });
  await wait('页面初始化', "typeof openStages === 'function' && typeof initFavTools === 'function'");
  check('操作号无需安全上下文限定的 randomUUID', await evalJs(`(() => {
    const first = modelOperationId();
    return /^[0-9a-f]{32}$/.test(first) && first !== modelOperationId();
  })()`));
  if (process.argv.includes('--unguarded-reads')) await evalJs('ownsModelRead = () => true');
  await evalJs('Promise.all([loadModels(), initFavTools(true)])');
  await evalJs('openStages()');
  await wait('面板初始化', "document.querySelectorAll('.stage-preset').length === 2");
  await api('/__fixture/arm', { write: 1 });
  await click('.stage-preset[data-preset="gpt"]');
  await gates('waiting', { write: 1 });
  await click('#stages-modal button[onclick="closeStages()"]');
  await evalJs('openStages()');
  await idle();
  await wait('显示旧写未确认而非读取中', "document.querySelector('#stages-msg').textContent.includes('仍未确认')");
  await current('claude');
  const latest = await api('/api/stages');
  await api('/__fixture/release', {});
  await gates('finished', { write: 1 });
  const afterOld = await api('/api/stages');
  check('真实迟到事务没有覆盖新的 Claude', afterOld.revision === latest.revision && afterOld.presets.find(preset => preset.key === 'claude').active);
  await screenshot('model-settings-late-write-rejected');

  await evalJs('Promise.all([loadModels(), initFavTools(true)])');
  const held = { '/api/models': 1, '/api/favorites/settings': 1 };
  await api('/__fixture/arm', held);
  await current('gpt');
  await gates('waiting', held);
  await current('claude');
  await wait('新读取已选回 Claude', "document.querySelector('#ask-model').value === 'anthropic:claude-sonnet-4-6' && document.querySelector('#fav-model').value === 'anthropic:claude-sonnet-4-6'");
  await api('/__fixture/release', {});
  await gates('finished', held);
  await paint();
  check('旧读取不能覆盖问答和收藏下拉', await evalJs("document.querySelector('#ask-model').value === 'anthropic:claude-sonnet-4-6' && document.querySelector('#fav-model').value === 'anthropic:claude-sonnet-4-6'"));
  await evalJs("doSendChat('验证实际模型', false)");
  check('实际提问也携带最新 Claude', (await api('/__fixture/status')).last_ask.model === 'anthropic:claude-sonnet-4-6');
  await screenshot('model-settings-current-consumers');

  await api('/__fixture/arm', { '/api/models': 1 });
  await evalJs('void loadModels()');
  await gates('waiting', { '/api/models': 1 });
  await evalJs(`(() => {
    const select = document.querySelector('#ask-model');
    select.value = 'anthropic:claude-haiku-4-5';
    select.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await api('/__fixture/release', {});
  await gates('finished', { '/api/models': 1 });
  await paint();
  check('较早读取不能覆盖用户手选问答模型', await evalJs("document.querySelector('#ask-model').value === 'anthropic:claude-haiku-4-5'"));

  await click('.stage-preset-editor[data-preset="claude"] button');
  await evalJs("document.querySelector('.stage-preset-editor[data-preset=claude] input').value = '保留中的草稿'");
  await current('gpt');
  check('切组重绘仍保留组名草稿', await evalJs("document.querySelector('.stage-preset-editor[data-preset=claude] input').value === '保留中的草稿'"));
  await click('.stage-preset-editor[data-preset="gpt"] button');
  await evalJs(`(() => {
    const select = document.querySelector('#ask-model');
    select.value = 'anthropic:claude-haiku-4-5';
    select.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await evalJs("document.querySelector('.stage-preset-editor[data-preset=gpt] input').value = '😀'.repeat(40)");
  await click('.stage-preset-editor[data-preset="gpt"] button[type="submit"]');
  await idle();
  await evalJs('loadModels(true)');
  check('只改组名不重置手选问答模型', await evalJs("document.querySelector('#ask-model').value === 'anthropic:claude-haiku-4-5'"));
  check('40 个表情组名完整保存', (await api('/api/stages')).presets.find(preset => preset.key === 'gpt').label === '😀'.repeat(40));
  check('保存另一组也保留草稿', await evalJs("document.querySelector('.stage-preset-editor[data-preset=claude] input').value === '保留中的草稿'"));
  await evalJs('initFavTools(true)');
  await api('/__fixture/arm', { '/api/stages': 1 });
  await evalJs('void refreshStages()');
  await gates('waiting', { '/api/stages': 1 });
  await evalJs("document.querySelector('#fav-model').value = 'anthropic:claude-haiku-4-5'; saveFavModel()");
  const favoriteWrite = await api('/api/stages');
  check('收藏独立写入已提交', favoriteWrite.stages.find(stage => stage.key === 'fav').model === 'claude-haiku-4-5');
  await api('/__fixture/release', {});
  await gates('finished', { '/api/stages': 1 });
  await wait('阶段读取也拒绝收藏写入前的旧快照', `stageConfigRevision === ${favoriteWrite.revision}`);
  check('收藏设置不重置手选问答模型', await evalJs("document.querySelector('#ask-model').value === 'anthropic:claude-haiku-4-5'"));
  check('没有页面脚本异常', pageErrors.length === 0);
  console.log(JSON.stringify({ total: checks.length, checks }, null, 2));
} catch (error) {
  console.error(JSON.stringify({ passed: checks, status: await api('/__fixture/status'), pageErrors }));
  throw error;
} finally {
  await api('/__fixture/release', {}).catch(() => {});
  ws.close();
  proc.kill();
}
