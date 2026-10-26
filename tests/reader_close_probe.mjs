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
  await waitFor(`$('reader-frame').contentDocument?.URL.endsWith(${JSON.stringify(path)})
    && $('reader-frame').contentDocument.querySelector('#content a')`);
};
const foreignPage = async () => {
  await evaluate(`(() => {
    const link = document.createElement('a'); link.href = ${JSON.stringify(base.replace('127.0.0.1', 'localhost') + '/blocked')};
    link.dataset.title = '无法嵌入的目标'; openReader(link); $('reader-frame').focus();
  })()`);
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
  for (const [path, destination] of [['/go-external', '/blocked'], ['/missing', '/missing'],
    ['/failed', '/failed'], ['/elsewhere', '/elsewhere'], ['/doc/ready.md', '/doc/ready.md']]) {
    await click(`(() => {
      const frame = $('reader-frame').getBoundingClientRect();
      const link = $('reader-frame').contentDocument.querySelector('a[href="${path}"]').getBoundingClientRect();
      return {x: frame.x + link.x + link.width / 2, y: frame.y + link.y + link.height / 2};
    })()`);
    let target;
    for (let attempt = 0; attempt < 100; attempt++) {
      target = (await send('Target.getTargets')).targetInfos.find(item =>
        item.type === 'page' && item.url.endsWith(destination));
      if (target) break;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    check(`${path} 在新标签打开且不替换正文`, !!target && await evaluate(`$('reader-frame').contentDocument?.URL.endsWith('/doc/slow.md')`));
    if (target) await send('Target.closeTarget', { targetId: target.targetId });
  }
  await fetch(base + '/release-image');
  await waitFor(`$('reader-frame').contentDocument?.readyState === 'complete' && readerAway !== null`);
  const unchangedSource = await evaluate(`(() => {
    const frame = $('reader-frame'), before = frame.getAttribute('src');
    frame.contentWindow.location.href = ${JSON.stringify(base.replace('127.0.0.1', 'localhost') + '/blocked')};
    return frame.getAttribute('src') === before;
  })()`);
  await waitFor(`$('reader-frame').contentDocument?.URL.endsWith('/doc/slow.md') && readerAway !== null
    && $('reader-destination').style.display !== 'none'`);
  check('正文内部跨源导航后恢复正文并保留实际目标入口', await evaluate(`$('reader-destination').href === ${JSON.stringify(base.replace('127.0.0.1', 'localhost') + '/blocked')}
    && $('reader-open').href.endsWith('/doc/slow.md') && !$('reader-frame').hidden`));
  check('内部导航未把 iframe 的 src 改成目标地址', unchangedSource);
  const recoveryPopup = (await send('Target.getTargets')).targetInfos.find(target =>
    target.type === 'page' && target.url.endsWith('/blocked'));
  if (recoveryPopup) await send('Target.closeTarget', { targetId: recoveryPopup.targetId });
  const fallbackScript = await send('Page.addScriptToEvaluateOnNewDocument', { source:
    `if (window.frameElement) Object.defineProperty(window, 'navigation', { value: undefined });` });
  await openReader('/doc/legacy.md');
  await waitFor(`$('reader-frame').contentDocument?.URL.endsWith('/doc/legacy.md') && readerAway !== null`);
  await evaluate(`$('reader-frame').contentDocument.querySelector('#content a[href$="/blocked"]').target = '_self'`);
  await click(`(() => {
    const frame = $('reader-frame').getBoundingClientRect();
    const link = $('reader-frame').contentDocument.querySelector('#content a[href$="/blocked"]').getBoundingClientRect();
    return { x: frame.x + link.x + link.width / 2, y: frame.y + link.y + link.height / 2 };
  })()`);
  await waitFor(`$('reader-frame').contentDocument?.URL.endsWith('/doc/legacy.md') && readerAway !== null
    && $('reader-destination').style.display !== 'none'`);
  check('缺少 Navigation API 时真实点击仍保留离页目标', await evaluate(`!$('reader-frame').contentWindow.navigation
    && $('reader-destination').href.endsWith('/blocked') && !$('reader-frame').hidden`));
  const fallbackPopup = (await send('Target.getTargets')).targetInfos.find(target =>
    target.type === 'page' && target.url.endsWith('/blocked'));
  if (fallbackPopup) await send('Target.closeTarget', { targetId: fallbackPopup.targetId });
  await send('Page.removeScriptToEvaluateOnNewDocument', { identifier: fallbackScript.identifier });
  await foreignPage();
  check('不可访问的 iframe 隐藏且不可交互', await evaluate(`$('reader-frame').hidden && $('reader-frame').inert
    && $('reader-frame').getAttribute('tabindex') === '-1' && !$('reader-error').hidden`));
  check('错误出现后焦点留在抽屉关闭按钮', await evaluate(`document.activeElement === $('reader-close')`));
  check('错误提示的新标签指向失败目标', await evaluate(`$('reader-open').href.endsWith('/blocked')`));
  await click(errorCenter);
  check('点击原正文区域不能重新聚焦 iframe', await evaluate(`document.activeElement !== $('reader-frame')`));
  await press('Escape');
  await waitFor(closed);
  check('点击错误区域后 Escape 仍能完整退出', await evaluate(closed));
  check('关闭错误页立即清除错误状态', await evaluate(`$('reader-error').hidden
    && !$('reader-frame').hidden && !$('reader-frame').inert && !$('reader-frame').hasAttribute('tabindex')`));
  await fetch(base + '/hold-image');
  await openReader('/doc/slow.md');
  check('重开慢图文章在 load 前就恢复正文与交互', await evaluate(`(() => {
    const frame = $('reader-frame'), doc = frame.contentDocument;
    return doc.readyState === 'interactive' && !doc.images[0].complete && readerAway === null
      && !frame.hidden && !frame.inert && !frame.hasAttribute('tabindex') && $('reader-error').hidden;
  })()`));
  await fetch(base + '/release-image');
  await waitFor(`$('reader-frame').contentDocument?.readyState === 'complete' && readerAway !== null`);
  check('新文章 load 后仍保持正常正文', await evaluate(`!$('reader-frame').hidden && $('reader-error').hidden`));
  await evaluate(`$('reader-frame').src = '/doc/ready.md'`);
  await waitFor(`$('reader-frame').contentDocument?.URL.endsWith('/doc/ready.md') && $('reader-fav').dataset.path === 'ready.md'`);
  check('抬头与加载后的当前文章一致', await evaluate(`$('reader-title').textContent.endsWith('/doc/ready.md')
    && $('reader-open').href.endsWith('/doc/ready.md') && $('reader-obs').href.endsWith('file=ready.md')
    && $('reader-src').href.endsWith('doc=ready.md')`));
  await foreignPage();
  for (let step = 0; step < 6; step++) {
    await press('Tab');
    check(`错误页 Tab ${step + 1} 不进入 iframe`, await evaluate(`document.activeElement !== $('reader-frame')`));
  }
  await press('Escape');
  await waitFor(closed);
  check('Tab 循环后 Escape 仍能退出', await evaluate(closed));
  await fetch(base + '/hold-image');
  await openReader('/doc/slow.md');
  await evaluate(`$('reader-frame').contentDocument.querySelector('#content a[href$="/blocked"]').target = '_self'`);
  await click(`(() => {
    const frame = $('reader-frame').getBoundingClientRect();
    const link = $('reader-frame').contentDocument.querySelector('#content a[href$="/blocked"]').getBoundingClientRect();
    return { x: frame.x + link.x + link.width / 2, y: frame.y + link.y + link.height / 2 };
  })()`);
  await waitFor(`(() => {
    const doc = $('reader-frame').contentDocument;
    return doc?.URL.endsWith('/doc/slow.md') && doc.readyState === 'interactive'
      && doc.images.length > 0 && $('reader-destination').style.display !== 'none';
  })()`);
  check('首个 load 前离页也恢复已渲染正文并保留目标', await evaluate(`(() => {
    const frame = $('reader-frame'), doc = frame.contentDocument;
    return doc.readyState === 'interactive' && !doc.images[0].complete && readerAway === null
      && !frame.hidden && $('reader-error').hidden && $('reader-destination').href.endsWith('/blocked');
  })()`));
  await fetch(base + '/release-image');
  await waitFor(`readerAway !== null`);
  const earlyPopup = (await send('Target.getTargets')).targetInfos.find(target =>
    target.type === 'page' && target.url.endsWith('/blocked'));
  if (earlyPopup) await send('Target.closeTarget', { targetId: earlyPopup.targetId });
  await evaluate(`closeReader()`);
  const loopStart = await evaluate(`readerNavigationId`);
  await evaluate(`(() => {
    window.readerLoopDepartures = 0;
    const link = document.createElement('a'); link.href = '/doc/loop.md'; openReader(link);
  })()`);
  await waitFor(`readerRecovery?.blocked && !$('reader-error').hidden`);
  await new Promise(resolve => setTimeout(resolve, 350));
  check('重复离页只自动恢复一次且停止继续加载', await evaluate(`readerNavigationId === ${loopStart + 2}
    && window.readerLoopDepartures === 2 && $('reader-frame').hidden && $('reader-frame').inert`));
  const loopPopups = (await send('Target.getTargets')).targetInfos.filter(target =>
    target.type === 'page' && target.url.endsWith('/blocked'));
  check('重复恢复不会持续弹出新标签', loopPopups.length <= 1);
  check('停止恢复后仍保留实际目标入口', await evaluate(`$('reader-destination').style.display !== 'none'
    && $('reader-destination').href.endsWith('/blocked') && $('reader-open').href.endsWith('/blocked')`));
  for (const target of loopPopups) await send('Target.closeTarget', { targetId: target.targetId });
  await press('Escape');
  await waitFor(closed);
  check('停止恢复后 Escape 仍能退出', await evaluate(closed));
  check('无未捕获页面异常', errors.length === 0);
} finally {
  await fetch(base + '/release-image').catch(() => {});
  socket.close();
  proc.kill();
}
console.log(JSON.stringify({ checks, errors }));
process.exitCode = errors.length || checks.some(item => !item.ok) ? 1 : 0;
