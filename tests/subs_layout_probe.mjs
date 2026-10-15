import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { launchShell } from './probe_shell.mjs';
import { checkSubscriptionUsability } from './subs_usability_checks.mjs';
import { checkSubscriptionNotices, checkWechatNotices } from './subs_notices_checks.mjs';
import { checkSourceNoticeRecovery } from './subs_notice_recovery_checks.mjs';
import { checkWechatCollectorNotices, checkWechatTargetSizing } from './subs_wechat_review_checks.mjs';
import { checkFirstWechatNotice, checkAutomaticSourceNotice, checkManualSourceNotices, checkNoticeGestureSequences } from './subs_notice_hit_checks.mjs';
import { checkSubscriptionEditorViews } from './subs_editor_view_checks.mjs';

const base = process.argv[2];
const output = 'output/subs-layout';
mkdirSync(output, { recursive: true });
const results = [], errors = [];
const { proc, port } = await launchShell(['--window-size=1440,1000'], { profile: 'kb-subs-layout-' });
const tabs = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const socket = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }));
let nextId = 0;
const pending = new Map();
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message);
    pending.delete(message.id);
  } else if (message.method === 'Runtime.exceptionThrown') {
    errors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
  }
});
const send = (method, params = {}) => new Promise(resolve => {
  const callId = ++nextId;
  pending.set(callId, resolve);
  socket.send(JSON.stringify({ id: callId, method, params }));
});
const evaluate = async expression => {
  const response = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (response.result?.exceptionDetails) throw new Error(JSON.stringify(response.result.exceptionDetails));
  return response.result.result.value;
};
const wait = async expression => {
  for (let attempt = 0; attempt < 120; attempt++) {
    if (await evaluate(expression)) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${expression}`);
};
const check = (name, ok, detail = '') => results.push({ name, ok: !!ok, detail });
const measure = () => evaluate(`(() => {
  const bench = $('source-workbench').getBoundingClientRect();
  const stream = document.querySelector('.source-stream').getBoundingClientRect();
  const pane = $('source-list-pane').getBoundingClientRect();
  const rows = [...document.querySelectorAll('.source-entry')].map(node => node.getBoundingClientRect());
  return { width: bench.width, listWidth: stream.width, top: bench.top, bottom: bench.bottom,
    height: bench.height, listHeight: pane.height,
    fullRows: rows.filter(row => row.top >= pane.top && row.bottom <= pane.bottom).length,
    firstHeight: rows[0]?.height,
    pageHeight: document.documentElement.scrollHeight, viewport: innerHeight,
    horizontal: document.documentElement.scrollWidth > innerWidth,
    readerVisible: $('source-reader').getClientRects().length > 0 };
})()`);
const viewport = async (width, height) => {
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
  await evaluate(`new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`);
};
const screenshot = async name => {
  const response = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  writeFileSync(`${output}/${name}.png`, Buffer.from(response.result.data, 'base64'));
};
const click = async (selector, vertical = .5) => {
  const point = await evaluate(`(() => {
    const box = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
    return { x: box.x + box.width / 2, y: box.y + box.height * ${vertical} };
  })()`);
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 });
};

try {
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: base + '/#social' });
  await wait(`typeof sourceRows !== 'undefined' && sourceRows.length >= 30`);
  await evaluate(`clearTimeout(sourcePoll); $('wechat-welcome').hidden = true; true`);
  let metrics = await measure();
  check('默认列表至少占工作台 38%', metrics.listWidth / metrics.width >= .38, JSON.stringify(metrics));
  check('长观点单条不超过 150px', metrics.firstHeight <= 150, JSON.stringify(metrics));
  check('首屏至少完整显示五条长观点', metrics.fullRows >= 5, JSON.stringify(metrics));
  check('两层工具栏之后工作台延伸到视口底部', metrics.top <= 125 && metrics.height >= metrics.viewport * .86 && metrics.pageHeight <= metrics.viewport + 1 && metrics.bottom >= metrics.viewport - 1, JSON.stringify(metrics));
  check('列表自身可以滚动', await evaluate(`$('source-list-pane').scrollHeight > $('source-list-pane').clientHeight`));
  check('完整正文保留而非被摘要截断', await evaluate(`$('source-reader-body').textContent.length > 2000`));
  check('页尾没有同步设置', await evaluate(`!document.querySelector('#subspanel .pipe-schedule')`));
  await screenshot('social-1440');
  check('来源状态提示仍可见而详情默认收起', await evaluate(`!$('source-notices').hidden && !$('source-notices').open && $('source-notices-summary').textContent.includes('待配置')`));
  await click('#source-notices > summary');
  check('展开后能看到完整待配置原因', await evaluate(`$('source-notices').open && $('source-configuration').getClientRects().length > 0 && $('source-configuration').textContent.includes('51 个账号待配置')`));
  const noticesMetrics = await measure();
  check('展开状态详情不会挤压列表或正文', Math.abs(noticesMetrics.listHeight - metrics.listHeight) < 1 && noticesMetrics.pageHeight <= noticesMetrics.viewport + 1, JSON.stringify(noticesMetrics));
  await screenshot('source-notices-1440');
  await click('#source-notices > summary');
  await evaluate(`sourceError('示例读取失败：本地服务暂不可用'); true`);
  check('读取失败自动展开原因及重试入口', await evaluate(`$('source-notices').open && $('source-error').getClientRects().length > 0 && $('source-error-text').textContent.includes('本地服务暂不可用')`));
  await evaluate(`sourceError(); true`);
  await checkAutomaticSourceNotice({ evaluate, wait, check, click, screenshot, send });
  await checkManualSourceNotices({ evaluate, check, click, screenshot, send });
  await checkNoticeGestureSequences({ evaluate, wait, check, click, screenshot, send, viewport });
  await checkSourceNoticeRecovery({ evaluate, wait, check, click, screenshot, send });
  await checkSubscriptionNotices({ evaluate, check, click, screenshot });

  await evaluate(`$('source-list-pane').scrollTop = 350; true`);
  check('滚列表不带动整页与正文', await evaluate(`window.scrollY === 0 && $('source-reader-scroll').scrollTop === 0 && $('source-list-pane').scrollTop > 0`));
  await evaluate(`$('source-list-pane').scrollTop = 0; $('source-list-expand').click(); true`);
  metrics = await measure();
  check('只看列表获得完整宽度', metrics.listWidth >= metrics.width - 3 && !metrics.readerVisible, JSON.stringify(metrics));
  check('只看列表按钮有可访问状态', await evaluate(`$('source-list-expand').getAttribute('aria-pressed') === 'true'`));
  await screenshot('list-only-1440');
  await evaluate(`$('source-list-expand').click(); true`);
  check('显示正文恢复双栏', (await measure()).readerVisible);
  await evaluate(`$('source-list-expand').click(); readSourceItem(sourceRows[2].key, false); true`);
  check('自动更新不会取消只看列表', await evaluate(`$('source-workbench').classList.contains('is-list-only')`));
  await click('.source-entry:nth-of-type(2) .source-excerpt');
  check('选中列表条目重新打开完整正文', await evaluate(`!$('source-workbench').classList.contains('is-list-only') && sourceReaderItem.key === sourceRows[1].key`));
  await evaluate(`document.querySelector('.source-entry').focus(); true`);
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  check('键盘也可以展开列表全文', await evaluate(`sourceReaderItem.key === sourceRows[0].key && document.activeElement === $('source-reader')`));
  await evaluate(`$('source-reader-expand').click(); true`);
  check('专注阅读仍可用', await evaluate(`$('source-workbench').classList.contains('is-focused') && !document.querySelector('.source-stream').getClientRects().length`));
  await evaluate(`$('source-reader-expand').click(); switchSourceView('manage'); true`);
  await wait(`$('source-workbench').classList.contains('is-managing')`);
  check('订阅管理保留统一外壳并切换到同步设置', await evaluate(`document.body.classList.contains('subs-mode') && !document.body.classList.contains('subs-reading') && !$('source-settings').hidden`));
  await evaluate(`switchSourceView('items'); true`);
  await wait(`sourceRows.length >= 30 && sourceView === 'items'`);

  for (const kind of ['rss', 'digest', 'web']) {
    await evaluate(`openSubsPanel('${kind}'); true`);
    await wait(`sourceKind === '${kind}' && sourceRows.length >= 30 && sourceReaderItem`);
    await evaluate(`clearTimeout(sourcePoll); true`);
    metrics = await measure();
    check(`${kind} 的来源操作与视图切换在共享工具栏`, await evaluate(`!!document.querySelector('#subs-feeds > .source-toolbar .source-view-tabs') && !document.querySelector('.source-stream > .source-heading') && !document.getElementById('source-description')`));
    check(`${kind} 使用加宽列表`, metrics.listWidth / metrics.width >= .38, JSON.stringify(metrics));
    check(`${kind} 有数据也不撑高整页`, metrics.pageHeight <= metrics.viewport + 1, JSON.stringify(metrics));
  }
  await screenshot('column-1440');
  await evaluate(`$('source-progress').hidden = false; $('source-progress-text').textContent = '正在同步'; $('source-issues').hidden = false; $('source-issues').textContent = '部分来源需重试'; updateSourceNotices(); true`);
  metrics = await measure();
  check('同步及失败状态不会占整页横幅', metrics.pageHeight <= metrics.viewport + 1 && metrics.top <= 125 && metrics.bottom <= metrics.viewport && await evaluate(`!$('source-notices').hidden && $('source-notices-summary').textContent === '正在同步'`), JSON.stringify(metrics));
  await evaluate(`$('wechat-welcome').hidden = false; true`);
  metrics = await measure();
  check('首次连接提示不会顶出工作台', metrics.pageHeight <= metrics.viewport + 1, JSON.stringify(metrics));
  await evaluate(`$('wechat-welcome').hidden = true; $('source-progress').hidden = true; $('source-issues').hidden = true; updateSourceNotices(); true`);

  await viewport(2160, 1600);
  metrics = await measure();
  check('大屏没有旧的 1600px 总宽上限', metrics.width >= 2100, JSON.stringify(metrics));
  check('高窗口工作区也随视口长高而不封顶', metrics.height >= metrics.viewport * .9 && metrics.bottom >= metrics.viewport - 1 && metrics.pageHeight <= metrics.viewport + 1, JSON.stringify(metrics));
  await screenshot('large-2160-1600');
  await viewport(1440, 1000);

  for (const [width, height] of [[900, 900], [820, 760], [390, 844], [1440, 420]]) {
    await viewport(width, height);
    metrics = await measure();
    check(`${width}×${height} 没有横向溢出`, !metrics.horizontal, JSON.stringify(metrics));
    check(`${width}×${height} 列表没有被压扁`, metrics.listHeight >= 220, JSON.stringify(metrics));
    if (width > 760 && height >= 760) check(`${width}×${height} 首屏不滚动`, metrics.pageHeight <= metrics.viewport + 1, JSON.stringify(metrics));
    await screenshot(`responsive-${width}-${height}`);
  }
  await viewport(390, 844);
  await evaluate(`readSourceItem(sourceRows[2].key); true`);
  check('手机点列表可打开正文', await evaluate(`$('source-reader').getClientRects().length > 0 && !document.querySelector('.source-stream').getClientRects().length`));
  await evaluate(`backToSourceList(); true`);
  check('手机可以返回列表', await evaluate(`document.querySelector('.source-stream').getClientRects().length > 0`));
  await viewport(1440, 1000);
  await evaluate(`window.scrollTo(0, 0); true`);
  await evaluate(`openSubsPanel('wechat'); true`);
  await wait(`!$('subs-wechat').hidden`);
  check('公众号也使用统一页头', await evaluate(`!!document.querySelector('#subs-wechat > .source-heading') && !$('subs-wechat-sync').hidden && !document.body.classList.contains('subs-reading')`));
  await checkFirstWechatNotice({ evaluate, wait, check, click, screenshot, send });
  await checkWechatNotices({ evaluate, wait, check, click, screenshot });
  await checkWechatCollectorNotices({ evaluate, wait, check, click, screenshot, send });
  await checkWechatTargetSizing({ evaluate, wait, check, viewport, screenshot });
  await checkSubscriptionUsability({ evaluate, wait, check, click, viewport, screenshot, send });
  await checkSubscriptionEditorViews({ evaluate, wait, check, click, screenshot, send });
  await evaluate(`switchTab('gallery'); true`);
  check('离开订阅页面恢复普通布局', await evaluate(`!document.body.classList.contains('subs-reading') && !document.body.classList.contains('subs-mode')`));
  check('浏览器没有未捕获异常', errors.length === 0, errors.join('\n'));
} finally {
  socket.close();
  proc.ref();
  proc.kill();
  await new Promise(resolve => proc.once('exit', resolve));
}
writeFileSync(`${output}/results.json`, JSON.stringify(results, null, 2));
process.stdout.write(JSON.stringify(results) + '\n');
process.exitCode = results.some(result => !result.ok) ? 1 : 0;
