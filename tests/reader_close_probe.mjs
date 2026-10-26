import { launchShell } from './probe_shell.mjs';

const base = process.argv[2];
const { proc, port } = await launchShell(['--window-size=1280,900'], { profile: 'kb-reader-close-' });
const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }));
const errors = [], checks = [], pending = new Map();
let sequence = 0;
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (pending.has(message.id)) {
    pending.get(message.id)(message);
    pending.delete(message.id);
  }
  if (message.method === 'Runtime.exceptionThrown')
    errors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
});
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++sequence;
  pending.set(id, message => message.error ? reject(new Error(JSON.stringify(message.error)))
    : resolve(message.result));
  socket.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expression => {
  const reply = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (reply.exceptionDetails) throw new Error(JSON.stringify(reply.exceptionDetails));
  return reply.result?.value;
};
const waitFor = async expression => {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`等待超时: ${expression}`);
};
const check = (name, ok) => checks.push({ name, ok: !!ok });
const press = async key => {
  for (const type of ['keyDown', 'keyUp'])
    await send('Input.dispatchKeyEvent', { type, key, code: key,
      windowsVirtualKeyCode: key === 'Escape' ? 27 : 9 });
};
const click = async expression => {
  const point = await evaluate(expression);
  for (const type of ['mousePressed', 'mouseReleased'])
    await send('Input.dispatchMouseEvent', { type, button: 'left', clickCount: 1, ...point });
};
const openReader = async path => {
  await evaluate(`(() => {
    const link = document.createElement('a'); link.href = ${JSON.stringify(path)};
    link.dataset.title = '阅读器关闭验收'; openReader(link);
  })()`);
  await waitFor(`$('reader-frame').contentDocument?.querySelector('#content a')`);
};
const foreignPage = async () => {
  await evaluate(`$('reader-frame').focus(); $('reader-frame').src = ${JSON.stringify(base.replace('127.0.0.1', 'localhost') + '/blocked')}`);
  await waitFor(`!$('reader-error').hidden && readerAway === null`);
};
const closed = `$('reader').style.display === 'none' && $('reader-mask').style.display === 'none'
  && !document.body.classList.contains('overlay-open') && !$('main-content').inert`;
const errorCenter = `(() => {
  const rect = $('reader-error').getBoundingClientRect();
  return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
})()`;

try {
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: base });
  await waitFor('typeof workspaceLocated !== "undefined" && workspaceLocated');
  await openReader('/doc/slow.md');
  check('图片仍在加载时外链已准备好', await evaluate(`(() => {
    const doc = $('reader-frame').contentDocument, link = doc.querySelector('#content a');
    return doc.readyState === 'interactive' && link.target === '_blank' && link.relList.contains('noopener');
  })()`));
  await click(`(() => {
    const frame = $('reader-frame').getBoundingClientRect();
    const link = $('reader-frame').contentDocument.querySelector('#content a').getBoundingClientRect();
    return {x: frame.x + link.x + link.width / 2, y: frame.y + link.y + link.height / 2};
  })()`);
  let popup;
  for (let attempt = 0; attempt < 100; attempt++) {
    popup = (await send('Target.getTargets')).targetInfos.find(target =>
      target.type === 'page' && target.url.endsWith('/blocked'));
    if (popup) break;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  check('load 前点击确实打开新标签', !!popup);
  check('load 前点击没有替换正文', await evaluate(`$('reader-frame').contentDocument?.URL.endsWith('/doc/slow.md')`));
  if (popup) await send('Target.closeTarget', { targetId: popup.targetId });
  await fetch(base + '/release-image');
  await waitFor(`$('reader-frame').contentDocument?.readyState === 'complete' && readerAway !== null`);
  await foreignPage();
  check('不可访问的 iframe 隐藏且不可交互', await evaluate(`$('reader-frame').hidden && $('reader-frame').inert
    && $('reader-frame').getAttribute('tabindex') === '-1' && !$('reader-error').hidden`));
  await click(errorCenter);
  check('点击原正文区域不能重新聚焦 iframe', await evaluate(`document.activeElement !== $('reader-frame')`));
  await press('Escape');
  await waitFor(closed);
  check('点击错误区域后 Escape 仍能完整退出', await evaluate(closed));
  await openReader('/doc/ready.md');
  await waitFor(`!$('reader-frame').hidden && !$('reader-frame').inert`);
  check('重新打开同源文章恢复交互', await evaluate(`$('reader-error').hidden && !$('reader-frame').hasAttribute('tabindex')`));
  await foreignPage();
  for (let step = 0; step < 6; step++) {
    await press('Tab');
    check(`错误页 Tab ${step + 1} 不进入 iframe`, await evaluate(`document.activeElement !== $('reader-frame')`));
  }
  await press('Escape');
  await waitFor(closed);
  check('Tab 循环后 Escape 仍能退出', await evaluate(closed));
  check('无未捕获页面异常', errors.length === 0);
} finally {
  await fetch(base + '/release-image').catch(() => {});
  socket.close();
  proc.kill();
}
console.log(JSON.stringify({ checks, errors }));
process.exitCode = errors.length || checks.some(item => !item.ok) ? 1 : 0;
