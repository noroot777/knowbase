import { writeFileSync } from 'node:fs';
import { launchShell } from './probe_shell.mjs';

const base = process.argv[2];
const { proc, port } = await launchShell(['--window-size=1440,1080']);
const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }));
let sequence = 0;
const pending = new Map();
const errors = [];
const checks = [];
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (pending.has(message.id)) {
    pending.get(message.id)(message);
    pending.delete(message.id);
  }
  if (message.method === 'Runtime.exceptionThrown') {
    errors.push(message.params.exceptionDetails.exception?.description
      || message.params.exceptionDetails.text);
  }
});
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++sequence;
  pending.set(id, message => message.error ? reject(new Error(JSON.stringify(message.error)))
    : resolve(message.result));
  socket.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expression => {
  const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result?.value;
};
const waitFor = async expression => {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`等待页面超时: ${expression}`);
};
const check = (name, condition) => checks.push({ name, ok: !!condition });
const click = async selector => {
  const point = await evaluate(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    element.scrollIntoView({block: 'center'});
    const rect = element.getBoundingClientRect();
    return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
  })()`);
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', ...point });
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', button: 'left', clickCount: 1, ...point });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', button: 'left', clickCount: 1, ...point });
};

try {
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: `${base}/#fav` });
  await waitFor('typeof favItems !== "undefined" && favItems.length === 10');
  check('all ten favorites remain visible', await evaluate(
    'document.querySelectorAll("[data-fav-card]").length === 10'));
  check('detached favorites retain their titles and snapshots', await evaluate(
    'favItems.filter(item => !item.path).length === 3'
    + ' && favItems.filter(item => !item.path).every(item => item.title)'
    + ' && favItems.find(item => item.id === 1).url === "https://example.org/1"'));
  await evaluate('document.querySelector(`[data-fav-preview="1"]`).click()');
  await waitFor('document.querySelector("#fav-inspector .fv-detail-title")');
  check('notes and action card remain in the real detail view', await evaluate(
    'document.querySelector("#fav-note").value === "试着接到每周的信息整理流程里。"'
    + ' && document.querySelector(".fv-command code").textContent.includes("npx defuddle")'));
  check('primary link opens the saved external original, never a dead local document', await evaluate(
    'document.querySelector("#fav-inspector .fv-primary-link").getAttribute("href")'
    + ' === "https://example.org/1"'
    + ' && document.querySelector("#fav-inspector .fv-primary-link").textContent.includes("打开站外原文")'
    + ' && !document.querySelector(`#fav-inspector a[href*="/doc/"]`)'));
  if (process.env.KB_LIFECYCLE_SCREENSHOT) {
    const screenshot = await send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(process.env.KB_LIFECYCLE_SCREENSHOT, Buffer.from(screenshot.data, 'base64'));
  }
  await evaluate('document.querySelector("#fav-note").value = "原文移除后仍能保存备注";'
    + ' document.querySelector("#fav-note").dispatchEvent(new Event("input", {bubbles:true}));'
    + ' saveFavNote(1)');
  await waitFor('favItems.find(item => item.id === 1).note === "原文移除后仍能保存备注"');
  check('detached favorite note saves through the real API', await evaluate(
    'fetch("/api/favorites?status=all").then(response => response.json())'
    + '.then(data => data.favorites.find(item => item.id === 1).note === "原文移除后仍能保存备注")'));
  await evaluate('document.querySelector(`[data-fav-preview="4"]`).click()');
  check('saved quote remains visible after document removal', await evaluate(
    'document.querySelector("#fav-inspector blockquote").textContent'
    + ' === "先写下你想用它解决的问题，再决定要不要收藏。"'));
  await click('[data-fav-card="6"] .fv-original-missing');
  check('clicking the card explanation does not open a detail or navigation', await evaluate(
    'favActive === 4 && location.hash === "#fav"'));
  await evaluate('document.querySelector(`[data-fav-preview="6"]`).click()');
  check('missing original without URL has an honest non-clickable label', await evaluate(
    'document.querySelector("#fav-inspector .fv-original-missing").textContent === "原文已不在索引里"'
    + ' && !document.querySelector("#fav-inspector .fv-primary-link")'
    + ' && !document.querySelector(`[data-fav-card="6"] .fv-read-link`)'
    + ' && !document.querySelector(`#fav-inspector a[href*="/doc/"]`)'));
  check('card and detail explanations look like muted text, not buttons or links', await evaluate(`(() => {
    const labels = [...document.querySelectorAll(
      '[data-fav-card="6"] .fv-original-missing, #fav-inspector .fv-original-missing')];
    const muted = getComputedStyle(document.querySelector('.fv-card-meta')).color;
    return labels.length === 2 && labels.every(label => {
      const style = getComputedStyle(label);
      return label.tagName === 'SPAN' && !label.querySelector('svg')
        && !label.matches('[href], [onclick], [tabindex], [role]')
        && style.color === muted && style.backgroundColor === 'rgba(0, 0, 0, 0)'
        && style.padding === '0px' && style.borderRadius === '0px'
        && style.cursor === 'default' && style.textDecorationLine === 'none';
    });
  })()`));
  const beforeClick = await evaluate('location.href');
  const beforeTargets = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json())
    .map(target => target.id).sort();
  await click('#fav-inspector .fv-original-missing');
  await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  const afterTargets = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json())
    .map(target => target.id).sort();
  check('clicking the detail explanation neither navigates nor opens a new tab',
    await evaluate(`location.href === ${JSON.stringify(beforeClick)} && favActive === 6`)
    && JSON.stringify(beforeTargets) === JSON.stringify(afterTargets));
  if (process.env.KB_LIFECYCLE_SCREENSHOT) {
    const screenshot = await send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(process.env.KB_LIFECYCLE_SCREENSHOT.replace(/\.png$/, '-missing.png'),
      Buffer.from(screenshot.data, 'base64'));
  }
  await click('#fav-inspector .fv-detail-actions button');
  await waitFor('favItems.find(item => item.id === 6).status === "done"');
  check('detached favorite remains manageable', await evaluate(
    'fetch("/api/favorites?status=all").then(response => response.json())'
    + '.then(data => data.favorites.length === 10'
    + ' && data.favorites.find(item => item.id === 6).status === "done")'));
  check('no JavaScript exceptions', errors.length === 0);
  console.log(JSON.stringify({ checks, errors }));
} finally {
  socket.close();
  proc.kill();
}
