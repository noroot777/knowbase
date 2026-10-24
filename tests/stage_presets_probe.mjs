import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { attachCdp } from "./pipe_history_cdp.mjs";

const base = process.argv[2];
assert.ok(base && new URL(base).hostname === "127.0.0.1", "需要隔离夹具地址");
const { proc, ws, send, evalJs, until, untilHere, clickAt, pageErrors } = await attachCdp();
const checks = [];
const card = key => `.stage-preset[data-preset="${key}"]`;
const editor = key => `.stage-preset-editor[data-preset="${key}"]`;
const busy = `document.querySelector('#stages-content').getAttribute('aria-busy') === 'true'`;
const message = `document.querySelector('#stages-msg').textContent`;
const settings = async () => (await fetch(base + "/api/stages")).json();
const check = (label, passed) => { assert.ok(passed, label); checks.push(label); };
const wait = async (label, expression) => check(label, await until(expression));
const click = async selector => check(`可点击 ${selector}`, await clickAt(selector));
const press = async (key, text = key === "Enter" ? "\r" : "") => {
  for (const type of ["keyDown", "keyUp"]) await send("Input.dispatchKeyEvent", {
    type, key, code: key, windowsVirtualKeyCode: key === "Enter" ? 13 : 27,
    ...(text && type === "keyDown" ? {text} : {}),
  });
};
const text = selector => `document.querySelector(${JSON.stringify(selector)})?.textContent`;
const setInput = async (selector, value) => evalJs(`(() => {
  const input = document.querySelector(${JSON.stringify(selector)});
  input.value = ${JSON.stringify(value)};
  input.dispatchEvent(new Event('input', {bubbles: true}));
})()`);
const settle = () => wait("保存完成后恢复控件", `!(${busy})`);
const screenshot = async name => {
  const result = await send("Page.captureScreenshot", { format: "png" });
  mkdirSync("output/playwright", { recursive: true });
  writeFileSync(`output/playwright/${name}.png`, Buffer.from(result.result.data, "base64"));
};
const clearToasts = () => evalJs("document.querySelector('#toast-host')?.replaceChildren()");
const reopen = async () => {
  await click('#stages-modal button[onclick="closeStages()"]');
  await evalJs("window.presetCardBeforeReopen = document.querySelector('.stage-preset'); openStages()");
  await wait("重开读取配置", `document.querySelectorAll('.stage-preset').length === 2 && document.querySelector('.stage-preset') !== window.presetCardBeforeReopen && !(${busy})`);
};
const rename = async (key, name, keyboard = false) => {
  await click(`${editor(key)} button`);
  await setInput(`${editor(key)} input`, name);
  if (keyboard) await press("Enter");
  else await click(`${editor(key)} button[type="submit"]`);
  await settle();
  await wait("组名已保存", `${text(editor(key) + ' .stage-preset-label')} === ${JSON.stringify(name.trim())}`);
};

try {
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url: base });
  await wait("页面初始化", "typeof openStages === 'function' && document.querySelector('#stages-btn')");
  await click('summary[aria-label="工作区设置"]');
  await click("#stages-btn");
  await wait("两套预设已显示", "document.querySelectorAll('.stage-preset').length === 2");
  check("Claude 默认已激活", await evalJs(`document.querySelector('${card('claude')}').getAttribute('aria-pressed') === 'true'`));
  const initial = await settings();
  check("铅笔紧随组名且不另占一行", await evalJs(`(() => {
    const name = document.querySelector('${editor('claude')} .stage-preset-label');
    const pencil = name.nextElementSibling;
    const heading = name.closest('.stage-preset-head');
    return pencil.getAttribute('aria-label') === '修改组名' && pencil.querySelector('svg path')
      && heading && pencil.getBoundingClientRect().left >= name.getBoundingClientRect().right
      && heading.closest('.stage-preset-item') && !document.querySelector('.stage-preset button');
  })()`));
  await screenshot("model-presets-pencil-desktop");
  for (const width of [390, 375]) {
    await send("Emulation.setDeviceMetricsOverride", {width, height: 667, deviceScaleFactor: 1, mobile: false});
    await click(`${editor("gpt")} button`);
    check("原地编辑自动聚焦并选中名称", await evalJs(`(() => {
      const input = document.querySelector('${editor('gpt')} input');
      return document.activeElement === input && input.selectionStart === 0 && input.selectionEnd === input.value.length;
    })()`));
    await setInput(`${editor("gpt")} input`, "不保存的草稿");
    check(`手机 ${width} 标题编辑不溢出`, await evalJs(`document.querySelector('${editor('gpt')} form').scrollWidth <= document.querySelector('${editor('gpt')} form').clientWidth && document.querySelector('#stages-modal .modal').scrollWidth <= document.querySelector('#stages-modal .modal').clientWidth`));
    if (width === 375) await screenshot("model-presets-pencil-edit-mobile");
    await press("Escape");
    await wait("Esc 只退出改名,不关闭面板", `!document.querySelector('${editor('gpt')} form') && document.querySelector('#stages-modal').style.display !== 'none'`);
    check("取消还原名称并返回铅笔焦点", await evalJs(`${text(editor('gpt') + ' .stage-preset-label')} === 'GPT 默认' && document.activeElement === document.querySelector('${editor('gpt')} button')`));
  }
  await screenshot("model-presets-pencil-mobile");
  await send("Emulation.clearDeviceMetricsOverride");
  check("编辑及取消没有切换模型或保存名称", JSON.stringify(await settings()) === JSON.stringify(initial));
  await evalJs(`document.querySelector('${editor('gpt')} button').focus()`);
  await press("Enter");
  await wait("键盘也能打开原地编辑", `document.querySelector('${editor('gpt')} input') !== null`);
  await click(`${editor("gpt")} button[type="button"]`);
  const beforeAgent = initial.stages.find(stage => stage.key === "agent").configured;
  const preview = await evalJs(`(() => {
    const models = document.querySelector('${card('gpt')}').closest('.stage-preset-item').querySelector('.stage-preset-models');
    const bounds = models.getBoundingClientRect();
    const point = {x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2};
    return {...point, preset: document.elementFromPoint(point.x, point.y)?.closest('.stage-preset')?.dataset.preset};
  })()`);
  check("模型预览区仍命中整组应用按钮", preview.preset === "gpt");
  for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", {
    type, x: preview.x, y: preview.y, button: "left", clickCount: 1,
  });
  await wait("点击卡片正文应用整组", `document.querySelector('${card('gpt')}').getAttribute('aria-pressed') === 'true'`);
  await settle();
  const applied = await settings();
  check("研究阶段配置完整保留", JSON.stringify(applied.stages.find(stage => stage.key === "agent").configured) === JSON.stringify(beforeAgent));
  check("其余阶段整组切换 GPT", applied.stages.filter(stage => stage.key !== "agent").every(stage => stage.provider === "openai"));
  check("轻量阶段使用轻量模型", applied.stages.find(stage => stage.key === "gate").model === "gpt-5.3-codex-spark");
  check("主力阶段使用主力模型", applied.stages.find(stage => stage.key === "ask").model === "gpt-5.5");
  await wait("GPT 当前方案", `document.querySelector('${card('gpt')}').getAttribute('aria-pressed') === 'true'`);
  await wait("问答和收藏同步模型", "document.querySelector('#ask-model').value === 'openai:gpt-5.5' && document.querySelector('#fav-model').value === 'openai:gpt-5.5'");
  check("说明研究阶段例外", await evalJs(`${message}.includes('研究阶段保持原配置')`));
  await evalJs(`(() => {
    const select = document.querySelector('#stages-list select');
    select.value = 'anthropic:claude-haiku-4-5';
    select.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await settle();
  check("逐项微调不误标整组激活", await evalJs("document.querySelectorAll('.stage-preset[aria-pressed=true]').length === 0"));
  await click(card("claude"));
  await settle();
  check("可切回 Claude", (await settings()).presets.find(preset => preset.key === "claude").active);

  const beforeNames = JSON.stringify((await settings()).stages.map(stage => stage.configured));
  await rename("claude", "Claude 研究专用", true);
  await rename("gpt", "  GPT 日常工作  ");
  check("改名不改变模型分配", JSON.stringify((await settings()).stages.map(stage => stage.configured)) === beforeNames);
  check("两个组名独立保存", (await settings()).presets.every(preset => preset.label === (preset.key === "claude" ? "Claude 研究专用" : "GPT 日常工作")));
  await send("Page.reload");
  await wait("刷新完成", "document.readyState === 'complete' && document.querySelector('#stages-modal')?.style.display === 'none' && typeof openStages === 'function'");
  await evalJs("openStages()");
  await wait("刷新保留组名", `${text(editor('gpt') + ' .stage-preset-label')} === 'GPT 日常工作'`);
  await click(`${editor("claude")} button`);
  await setInput(`${editor("claude")} input`, "未保存的名字");
  await click(`${editor("claude")} button[type="button"]`);
  check("取消不保存名称", (await settings()).presets.find(preset => preset.key === "claude").label === "Claude 研究专用");
  await click(`${editor("claude")} button`);
  check("不使用 UTF-16 长度截断组名", await evalJs(`!document.querySelector('${editor('claude')} input').hasAttribute('maxlength')`));
  await setInput(`${editor("claude")} input`, "   ");
  await clearToasts();
  await click(`${editor("claude")} button[type="submit"]`);
  await settle();
  await wait("空白名就地显示校验", `${text(editor('claude') + ' .field-err')}.includes('组名需为')`);
  check("字段错误不弹 toast", await evalJs("document.querySelectorAll('#toast-host > *').length === 0"));
  check("无效输入保留并聚焦", await evalJs(`(() => {
    const input = document.querySelector('${editor('claude')} input');
    return input.value === '   ' && !input.disabled && input.getAttribute('aria-invalid') === 'true' && document.activeElement === input;
  })()`));
  await screenshot("model-presets-inline-error-desktop");
  await send("Emulation.setDeviceMetricsOverride", { width: 375, height: 667, deviceScaleFactor: 1, mobile: false });
  await evalJs("document.querySelector('#stages-content').scrollTop = 0");
  check("手机字段错误不溢出", await evalJs("document.querySelector('#stages-modal .modal').scrollWidth <= document.querySelector('#stages-modal .modal').clientWidth"));
  await screenshot("model-presets-inline-error-mobile");
  await send("Emulation.clearDeviceMetricsOverride");
  await setInput(`${editor("claude")} input`, '方案 "A" <轻量>');
  check("输入后清理旧错误", await evalJs(`${text(editor('claude') + ' .field-err')} === '' && document.querySelector('${editor('claude')} input').getAttribute('aria-invalid') === 'false'`));
  await click(`${editor("claude")} button[type="submit"]`);
  await settle();
  check("特殊字符按文字显示", await evalJs(`${text(editor('claude') + ' .stage-preset-label')} === '方案 "A" <轻量>' && !document.querySelector('${editor('claude')} img, ${editor('claude')} script')`));
  await click(card("gpt"));
  await settle();
  check("切换提示回显自定义组名", await evalJs(`${message}.includes('GPT 日常工作')`));

  await evalJs(`window.presetRealTimeout = window.setTimeout;
    window.setTimeout = (callback, delay, ...args) => window.presetRealTimeout(callback, delay === STAGE_SAVE_TIMEOUT_MS ? 1000 : delay, ...args);`);
  let heldPath;
  let heldRequests = [];
  ws.addEventListener("message", event => {
    const notification = JSON.parse(event.data);
    if (notification.method !== "Fetch.requestPaused") return;
    const paused = notification.params;
    if (paused.request.method === "POST" && new URL(paused.request.url).pathname === heldPath) heldRequests.push(paused.requestId);
    else void send("Fetch.continueRequest", { requestId: paused.requestId });
  });
  const startSave = async route => {
    if (route === "/api/stages/preset/name") {
      await click(`${editor("gpt")} button`);
      await setInput(`${editor("gpt")} input`, "网络挂起的组名");
      await click(`${editor("gpt")} button[type="submit"]`);
    } else if (route === "/api/stages/preset") await click(card("claude"));
    else await evalJs(`(() => {
      const select = document.querySelector('#stages-list select');
      select.value = 'anthropic:claude-haiku-4-5';
      select.dispatchEvent(new Event('change', {bubbles: true}));
    })()`);
  };
  for (const route of ["/api/stages/preset", "/api/stages/preset/name", "/api/stages"]) {
    await clearToasts();
    const beforeHang = JSON.stringify(await settings());
    heldPath = route;
    heldRequests = [];
    await send("Fetch.enable", { patterns: [{ urlPattern: base + "/api/stages*" }] });
    await startSave(route);
    check(`${route} 真实请求挂起`, await untilHere(() => heldRequests.length === 1));
    check("正常保存期间禁用所有写入口", await evalJs(`(${busy}) && [...document.querySelectorAll('#stages-content button, #stages-content input, #stages-content select')].every(control => control.disabled)`));
    check("保存期间仍可关闭", await evalJs("!document.querySelector('#stages-modal button[onclick=\"closeStages()\"]').disabled"));
    await settle();
    check(`${route} 超时明确结果未确认`, await evalJs(`${message}.includes('保存超时') && ${message}.includes('尚未确认') && document.querySelector('#toast-host').textContent.includes('保存超时')`));
    check("超时后预设与下拉恢复", await evalJs("!document.querySelector('.stage-preset').disabled && !document.querySelector('#stages-list select').disabled"));
    check("挂起写入不自动重试", heldRequests.length === 1 && JSON.stringify(await settings()) === beforeHang);
    if (route === "/api/stages/preset") await screenshot("model-presets-save-timeout");
    if (route.endsWith("/name")) check("超时保留组名输入供修改", await evalJs(`document.querySelector('${editor('gpt')} input').value === '网络挂起的组名' && !document.querySelector('${editor('gpt')} input').disabled`));
    await send("Fetch.disable");
    await reopen();
  }
  heldPath = null;
  await screenshot("model-presets-timeout-recovered");

  await clearToasts();
  await evalJs(`window.presetRealFetch = window.fetch;
    window.fetch = async (url, options) => {
      if (options?.method !== 'POST' || !String(url).startsWith('/api/stages')) return window.presetRealFetch(url, options);
      return new Response(new ReadableStream({start(stream) {
        options.signal.addEventListener('abort', () => stream.error(new DOMException('aborted', 'AbortError')));
      }}), {status: 503});
    };`);
  await click(card("claude"));
  await settle();
  check("读取错误响应体超时也如实提示", await evalJs(`${message}.includes('保存超时')`));
  await evalJs("window.fetch = window.presetRealFetch; window.setTimeout = window.presetRealTimeout");
  await clearToasts();

  for (const outcome of ["success", "error", "reject"]) {
    await evalJs(`window.presetGates = [];
      window.fetch = async (url, options) => {
        if (options?.method !== 'POST' || !String(url).startsWith('/api/stages')) return window.presetRealFetch(url, options);
        const snapshot = await window.presetRealFetch('/api/stages').then(response => response.json());
        return new Promise((resolve, reject) => window.presetGates.push({snapshot, resolve, reject, signal: options.signal}));
      };
      window.releasePresetGate = (index, outcome) => {
        const gate = window.presetGates[index];
        if (outcome === 'reject') return gate.reject(new Error('过期网络错误'));
        const data = outcome === 'error' ? {detail: '过期保存错误'} : {...gate.snapshot, applied_preset: index === 0 ? '过期方案' : '新会话方案', preserved: []};
        if (index === 0 && outcome === 'success') data.presets[0].label = '过期回包名称';
        gate.resolve(new Response(JSON.stringify(data), {status: outcome === 'error' ? 503 : 200}));
      };`);
    await click(card("claude"));
    await wait("旧会话保存已挂起", "window.presetGates.length === 1");
    await evalJs("window.presetOldSettled = false; void stageSaveChain.then(() => { window.presetOldSettled = true; })");
    await reopen();
    check("重开已取消旧请求且解锁", await evalJs(`window.presetGates[0].signal.aborted && !(${busy})`));
    await click(card("claude"));
    await wait("新会话保存已开始", "window.presetGates.length === 2");
    await evalJs(`releasePresetGate(0, ${JSON.stringify(outcome)})`);
    await wait("旧请求收尾完成", "window.presetOldSettled");
    check(`${outcome} 旧回调不能解锁新保存`, await evalJs(`(${busy}) && stageSavePending === 1 && !window.presetGates[1].signal.aborted && document.querySelector('.stage-preset').disabled`));
    check(`${outcome} 旧回调不能污染状态或错误`, await evalJs(`${message} === '保存中…' && !${text('#stages-presets')}.includes('过期回包名称') && document.querySelectorAll('#toast-host > *').length === 0`));
    await evalJs("releasePresetGate(1, 'success')");
    await settle();
    check("新会话仍可正常完成", await evalJs(`${message}.includes('新会话方案') && stageSavePending === 0`));
    await evalJs("window.fetch = window.presetRealFetch");
  }

  for (const staleRead of ["success", "reject"]) {
    await evalJs(`window.presetReadGate = null;
      window.fetch = async (url, options) => {
        if (url !== '/api/stages' || options?.method === 'POST' || window.presetReadGate) return window.presetRealFetch(url, options);
        const snapshot = await window.presetRealFetch(url, options).then(response => response.json());
        return new Promise((resolve, reject) => { window.presetReadGate = {snapshot, resolve, reject}; });
      };
      openStages();`);
    await wait("初次读取延迟回包", "window.presetReadGate !== null");
    await click(card("claude"));
    await settle();
    const savedMessage = await evalJs(message);
    await evalJs(`window.presetReadGate.snapshot.presets[0].label = '旧读取的名称';
      if (${JSON.stringify(staleRead)} === 'reject') window.presetReadGate.reject(new Error('过期读取失败'));
      else window.presetReadGate.resolve(new Response(JSON.stringify(window.presetReadGate.snapshot)));`);
    await evalJs("new Promise(resolve => queueMicrotask(resolve))");
    check(`${staleRead} 旧读取不能覆盖新保存`, await evalJs(`document.querySelectorAll('.stage-preset').length === 2 && !${text('#stages-presets')}.includes('旧读取的名称') && ${message} === ${JSON.stringify(savedMessage)}`));
    await evalJs("window.fetch = window.presetRealFetch");
  }

  await evalJs(`document.querySelectorAll('#stages-presets form').forEach(form => cancelStagePresetName(form));
    window.fetch = async (url, options) => options?.method === 'POST' && String(url).startsWith('/api/stages')
    ? new Response('upstream unavailable', {status: 503}) : window.presetRealFetch(url, options);`);
  const beforeFailure = JSON.stringify(await settings());
  await click(card("gpt"));
  await settle();
  check("服务错误仍用 toast", await evalJs("document.querySelector('#toast-host').textContent.includes('upstream unavailable')"));
  check("服务错误不改变模型", JSON.stringify(await settings()) === beforeFailure);
  await evalJs("window.fetch = window.presetRealFetch");
  await clearToasts();
  await click(card("gpt"));
  await settle();
  check("失败后重试可应用", (await settings()).presets.find(preset => preset.key === "gpt").active);

  const keys = await fetch(base + "/api/keys", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({openai_key: " "})});
  check("隔离夹具可清除虚构 key", keys.ok);
  await reopen();
  check("缺 key 不可应用但仍能改名", await evalJs(`document.querySelector('${card('gpt')}').disabled && !document.querySelector('${editor('gpt')} button').disabled`));
  await rename("gpt", "GPT 待配置");
  await rename("gpt", "长".repeat(initial.preset_name_max_length));
  await send("Emulation.setDeviceMetricsOverride", {width: 375, height: 667, deviceScaleFactor: 1, mobile: false});
  await evalJs("document.querySelector('#stages-content').scrollTop = 0");
  check("最长名称和铅笔在手机卡片内可见", await evalJs(`(() => {
    const pencil = document.querySelector('${editor('gpt')} button');
    const bounds = pencil.getBoundingClientRect();
    const panel = document.querySelector('#stages-modal .modal');
    return bounds.left >= 0 && bounds.right <= innerWidth && bounds.width >= 28
      && panel.scrollWidth <= panel.clientWidth && !pencil.disabled;
  })()`));
  await screenshot("model-presets-pencil-long-name-mobile");
  await click(`${editor("gpt")} button`);
  await setInput(`${editor("gpt")} input`, "缺 key 也能取消");
  await press("Escape");
  check("缺 key 的组仍可用 Esc 取消", await evalJs(`!document.querySelector('${editor('gpt')} form') && document.querySelector('#stages-modal').style.display !== 'none'`));
  check("浏览器无脚本错误", pageErrors.length === 0);
  console.log(JSON.stringify({total: checks.length, checks, pageErrors}, null, 2));
} catch (error) {
  console.error(JSON.stringify({passed: checks.length, panel: await evalJs("document.querySelector('#stages-presets')?.innerText"), status: await evalJs("document.querySelector('#stages-msg')?.textContent"), pageErrors}));
  throw error;
} finally {
  ws.close();
  proc.kill();
}
