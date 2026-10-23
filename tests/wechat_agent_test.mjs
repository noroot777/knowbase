import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../kb/static/js/wechat_setup.js', import.meta.url), 'utf8');
const markup = readFileSync(new URL('../kb/static/index.html', import.meta.url), 'utf8');
const readme = readFileSync(new URL('../README.md', import.meta.url), 'utf8');

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
