import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../kb/static/js/wechat_setup.js', import.meta.url), 'utf8');
const markup = readFileSync(new URL('../kb/static/index.html', import.meta.url), 'utf8');
const readme = readFileSync(new URL('../README.md', import.meta.url), 'utf8');

test('unavailable status exposes only explicit backup/reset, never automatic acquisition', async () => {
  const {context, element, requests} = fixture(undefined);
  vm.runInContext('wechatSetup = {state:"status_unavailable",running:false,can_connect:false,can_reset_status:true}; renderWechatSetup()', context);
  assert.equal(element('wechat-check').disabled, true);
  assert.equal(element('wechat-reset-status').hidden, false);
  assert.equal(element('wechat-reset-status').disabled, false);
  await vm.runInContext('startWechatSetup("connect"); startWechatSetup("acquire")', context);
  assert.equal(requests.length, 0);
  await vm.runInContext('startWechatSetup("reset_status")', context);
  const writes = requests.filter(request => request.options?.method === 'POST');
  assert.deepEqual(writes.map(request => JSON.parse(request.options.body)), [{action:'reset_status'}]);
  assert.ok(markup.includes('备份并仅重置状态'));
});

for (const state of [
  {state:'status_unavailable', running:true, can_connect:false, can_reset_status:false},
  {state:'status_unavailable', running:null, can_connect:false, can_reset_status:false},
  {state:'unchecked', running:false},
  {state:'unchecked', deployment:'docker', running:false, can_connect:false},
]) {
  test('reset cannot bypass busy, unknown-lock, normal or container states: ' + JSON.stringify(state), async () => {
    const {context, element, requests} = fixture(undefined);
    vm.runInContext('wechatSetup = ' + JSON.stringify(state) + '; renderWechatSetup()', context);
    assert.equal(element('wechat-reset-status').disabled, true);
    await vm.runInContext('startWechatSetup("reset_status")', context);
    assert.equal(requests.length, 0);
  });
}

function fixture(clipboard) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '', disabled: false, checked: false, style: {},
      dataset: {}, setAttribute() {}, focus() { this.focused = true; },
      select() { this.selected = true; },
    });
    return elements.get(id);
  };
  const requests = [];
  const context = vm.createContext({
    $: element, navigator: {clipboard}, localStorage: {getItem() { return null; }},
    clearTimeout() {}, setTimeout() {}, esc: text => text,
    fetch: async (url, options) => {
      requests.push({url, options});
      return {ok: true, json: async () => ({state: 'unchecked'})};
    },
  });
  vm.runInContext(source, context);
  return {context, element, requests};
}

test('malformed and irrelevant accounts cannot throw or wedge the request gate', async () => {
  for (const state of ['failed', 'unchecked', 'authorization_required', 'account_selection_required']) {
    for (const accounts of [{id: 'old'}, 'old', [null], [{id: []}]]) {
      const {context, requests} = fixture(undefined);
      vm.runInContext('wechatSetup = ' + JSON.stringify({state, running: false, accounts}) + '; renderWechatSetup()', context);
      await vm.runInContext('startWechatSetup()', context);
      assert.equal(vm.runInContext('wechatRequest', context), false);
      if (state !== 'account_selection_required') {
        assert.equal(requests.filter(request => request.options?.method === 'POST').length, 1);
      }
      await vm.runInContext('refreshWechatSetup()', context);
      assert.ok(requests.some(request => !request.options));
    }
  }
});

test('an unexpected render failure releases the request gate and leaves refresh usable', async () => {
  const {context, requests} = fixture(undefined);
  const render = context.renderWechatSetup;
  let attempts = 0;
  context.renderWechatSetup = () => {
    if (attempts++ === 0) throw new Error('synthetic render failure');
    return render();
  };
  await vm.runInContext('startWechatSetup()', context);
  assert.equal(vm.runInContext('wechatRequest', context), false);
  await vm.runInContext('startWechatSetup()', context);
  assert.equal(requests.filter(request => request.options?.method === 'POST').length, 1);
});

test('reset response replaces stale capabilities even if the next GET fails', async () => {
  const {context, element} = fixture(undefined);
  let posts = 0;
  context.fetch = async (url, options) => {
    if (!options) throw new Error('synthetic refresh failure');
    posts++;
    return {ok: true, json: async () => ({state: 'unchecked', running: false})};
  };
  vm.runInContext('wechatSetup = {state:"status_unavailable",can_connect:false,can_reset_status:true}; renderWechatSetup()', context);
  await vm.runInContext('startWechatSetup("reset_status")', context);
  assert.equal(element('wechat-reset-status').hidden, true);
  assert.equal(element('wechat-check').disabled, false);
  assert.equal(vm.runInContext('wechatSetup.can_connect', context), undefined);
  assert.equal(vm.runInContext('wechatRequest', context), false);
  await vm.runInContext('startWechatSetup()', context);
  assert.equal(posts, 2);
});

test('README names the actual buttons for first connection and an existing connection', () => {
  const {context, element} = fixture(undefined);
  const onboarding = readme.split('#### 绿泡泡接入（首次）')[1].split('</details>')[0];
  const instruction = onboarding.match(/^2\..*$/m)?.[0];
  assert.ok(instruction, 'README must describe the first connection action');
  for (const scenario of [
    {state: 'unchecked', ready: false, label: '检查并接入绿泡泡'},
    {state: 'ready', ready: true, label: '重新检查连接'},
  ]) {
    vm.runInContext(`wechatSetup = ${JSON.stringify(scenario)}; renderWechatSetup()`, context);
    const label = element('wechat-check').textContent;
    assert.equal(label, scenario.label);
    assert.ok(instruction.includes(`「${label}」`), `README must name the ${scenario.state} action: ${label}`);
  }
});

test('alternate method exposes a labelled, read-only prompt without starting acquisition', async () => {
  const {context, element, requests} = fixture({writeText: async () => {}});
  vm.runInContext('openWechatSetup()', context);
  await new Promise(resolve => setImmediate(resolve));
  const prompt = element('wechat-agent-prompt').value;
  for (const required of ['Mac 本机', '不覆盖、不重复获取', '经我明确确认后', '不要擅自关闭 SIP',
    '不要打印、上传或提交密钥', 'live_database_read_ok=true', '不读取私聊或群聊', '刷新状态']) {
    assert.ok(prompt.includes(required), required);
  }
  assert.ok(!prompt.includes('微信'));
  assert.ok(!/\/Users\/|confirmation_token|[a-f0-9]{64}/.test(prompt));
  assert.ok(markup.includes('方式二：让你的智能体协助接入'));
  assert.match(markup, /<label for="wechat-agent-prompt">/);
  assert.match(markup, /<textarea[^>]+id="wechat-agent-prompt"[^>]+readonly/);
  assert.ok(requests.every(request => !request.options?.method));
});

test('copy writes the complete visible prompt and confirms success without a network call', async () => {
  const copied = [];
  const {context, element, requests} = fixture({writeText: async text => copied.push(text)});
  element('wechat-agent-prompt').value = vm.runInContext('WECHAT_AGENT_PROMPT', context);
  await vm.runInContext('copyWechatAgentPrompt()', context);
  assert.deepEqual(copied, [element('wechat-agent-prompt').value]);
  assert.match(element('wechat-agent-copy-status').textContent, /已复制/);
  assert.equal(element('wechat-agent-copy').disabled, false);
  assert.deepEqual(requests, []);
});

for (const scenario of ['denied', 'missing']) {
  test(`clipboard ${scenario} selects the prompt and gives manual-copy instructions`, async () => {
    const clipboard = scenario === 'missing' ? undefined : {writeText: async () => {
      throw new Error('Permission denied');
    }};
    const {context, element} = fixture(clipboard);
    element('wechat-agent-prompt').value = '完整提示词';
    await vm.runInContext('copyWechatAgentPrompt()', context);
    assert.equal(element('wechat-agent-prompt').focused, true);
    assert.equal(element('wechat-agent-prompt').selected, true);
    assert.equal(element('wechat-agent-prompt').value, '完整提示词');
    assert.match(element('wechat-agent-copy-status').textContent, /无法自动复制.*Ctrl\+C/);
    assert.ok(!element('wechat-agent-copy-status').textContent.includes('已复制'));
    assert.equal(element('wechat-agent-copy').disabled, false);
  });
}

test('pending copy blocks duplicate clicks and re-enables the action when complete', async () => {
  let complete;
  let copies = 0;
  const {context, element} = fixture({writeText: () => {
    copies++;
    return new Promise(resolve => { complete = resolve; });
  }});
  const pending = vm.runInContext('copyWechatAgentPrompt()', context);
  assert.equal(element('wechat-agent-copy').disabled, true);
  await vm.runInContext('copyWechatAgentPrompt()', context);
  assert.equal(copies, 1);
  complete();
  await pending;
  assert.equal(element('wechat-agent-copy').disabled, false);
});

test('Docker keeps the prompt available without suggesting acquisition inside the container', () => {
  const {context, element} = fixture(undefined);
  vm.runInContext('wechatSetup = {deployment: "docker", can_connect: false}; renderWechatSetup()', context);
  assert.equal(element('wechat-check').disabled, true);
  assert.equal(element('wechat-agent-copy').disabled, false);
  assert.match(element('wechat-method-title').textContent, /Mac 本机/);
  assert.match(vm.runInContext('WECHAT_AGENT_PROMPT', context), /Docker 或远程服务器不能获取/);
});

test('Windows shows platform-specific consent and a safe native onboarding prompt', () => {
  const {context, element} = fixture(undefined);
  vm.runInContext('wechatSetup = {platform: "windows", state: "authorization_required", running: false}; renderWechatSetup()', context);
  const consent = element('wechat-authorization-hint').textContent;
  assert.match(consent, /只读.*最多 3 分钟/);
  assert.ok(!consent.includes('重签名副本'));
  assert.match(consent, /不会自动提权/);
  assert.equal(element('wechat-acquire').disabled, true);
  assert.equal(element('wechat-authorization').hidden, false);
  assert.match(element('wechat-source-hint').textContent, /Windows x64/);
  const prompt = element('wechat-agent-prompt').value;
  assert.match(prompt, /Windows x64/);
  assert.match(prompt, /不安装 Mac 的 wxkey/);
  assert.match(prompt, /经我明确确认后/);
  assert.match(prompt, /live_database_read_ok=true/);
});

test('unreadable status is explicit, disables acquisition and continues recovery polling', async () => {
  const {context, element} = fixture(undefined);
  const timers = [];
  context.setTimeout = (callback, delay) => timers.push({callback, delay});
  context.fetch = async () => ({ok: true, json: async () => ({
    platform: 'windows', state: 'status_unavailable', running: null,
    can_connect: false, retryable: true, message: '无法读取绿泡泡接入状态',
  })});
  await vm.runInContext('refreshWechatSetup()', context);
  assert.match(element('wechat-status').textContent, /无法读取/);
  assert.equal(element('wechat-database-state').textContent, '状态未知');
  assert.equal(element('wechat-check').textContent, '状态暂不可用');
  assert.equal(element('wechat-check').disabled, true);
  assert.equal(element('wechat-authorization').hidden, true);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 4000);
  context.fetch = async () => ({ok: true, json: async () => ({
    platform: 'windows', state: 'ready', ready: true, running: false,
  })});
  await timers[0].callback();
  assert.equal(element('wechat-database-state').textContent, '已验证');
  assert.equal(element('wechat-check').disabled, false);
  assert.equal(timers.length, 1);
});

test('a lost response while status is unknown keeps recovery polling', async () => {
  const {context} = fixture(undefined);
  const timers = [];
  context.setTimeout = (callback, delay) => timers.push({callback, delay});
  context.fetch = async () => { throw new Error('状态读取失败'); };
  vm.runInContext('wechatSetup = {state: "status_unavailable", retryable: true}', context);
  await vm.runInContext('refreshWechatSetup()', context);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 4000);
});
