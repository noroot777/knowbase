// 定时窗口的**依赖就绪**那条:配置读到了,可勾选的来源目录没读到。
//
// 为什么单开一支探针:要的是**整页加载时那次目录请求就失败**那一刻(目录一个进程只
// 读一次,`pipe_channels.js` 的 `loadPipeChannels`)。在 subs_layout_probe 里复现就得把
// `pipeChannels` / `schedLoaded` / 勾选框 DOM 一起摆回去 —— 摆三个产品变量的证明力远不如
// 真的开一页。这里在**网络层**拦掉那个地址(CDP `Network.setBlockedURLs`),整页重开,
// 后面每一步(读取收尾、保存、重读)走的都是产品自己的代码,鼠标是真鼠标。
//
// 钉的后果(第 4 轮真实鼠标实测的那一组):弹窗写着「已保存:每天 07:30 自动同步」、
// 字段开放、重读藏着,而来源一个都没有 —— 关掉定时去保存只换来一句「请至少选择一个
// 同步来源」,一个都选不了、也没法重读,关掉重开还会被草稿守卫挡住,只能刷新整页。
//
//   HEADLESS_SHELL=<bin> node tests/sched_catalog_probe.mjs http://127.0.0.1:<port>

import { launchShell } from './probe_shell.mjs';

const base = process.argv[2];
const results = [];
const { proc, port } = await launchShell(['--window-size=1440,1000'], { profile: 'kb-sched-catalog-' });
const tabs = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const socket = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }));
let nextId = 0;
const pending = new Map();
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id); }
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
// 真鼠标:点之前先确认这个控件在最上层接得住(命中算到子元素 —— 按钮中心点上多半是
// 里面那枚图标或那段文字)。点不中的那一下会落到遮罩上把弹窗关掉,后面每条都跟着红。
const hit = async selector => {
  await evaluate(`document.querySelector(${JSON.stringify(selector)})
    .scrollIntoView({ block: 'nearest' }); true`);
  await wait(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    const box = el.getBoundingClientRect();
    const on = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return !!on && el.contains(on); })()`);
  const point = await evaluate(`(() => {
    const box = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; })()`);
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 });
};
const form = async () => JSON.parse(await evaluate(`JSON.stringify({
  msg: $('sched-msg').textContent, fields: $('sched-fields').disabled,
  reload: $('sched-reload').hidden, save: $('sched-save').disabled,
  boxes: $('sched-channels').querySelectorAll('input').length,
  catalog: pipeChannels.length, loaded: schedLoaded, loading: schedLoading,
  dirty: schedDirty, on: $('sched-on').checked })`));

try {
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Network.enable');
  // **先拦再开页**:目录那个地址一次都不许通(拦之前开过一页的话,重开那次会吃
  // 浏览器缓存 —— 第一版就是这样,摆局那条检查当场红出 catalog=5,如实记着)。
  // 顺手关掉缓存,免得别的请求也拿旧回包。
  await send('Network.setCacheDisabled', { cacheDisabled: true });
  await send('Network.setBlockedURLs', { urls: ['*/api/pipeline/channels*'] });
  await send('Page.navigate', { url: base + '/#rss' });
  await wait(`typeof schedLoaded !== 'undefined' && typeof sourceRows !== 'undefined'`);
  await evaluate(`new Promise(r => setTimeout(r, 400))`);
  // 关掉来源列表那个轮询(同 subs_layout_probe):它会顺手再读一次两条定时,
  // 把下面要验的那一刻**治好** —— 第一版注入「目录和配置共用一次占代次」时,
  // 共享事实确实被抹成了 null,却在断言之前被这个轮询补回来,注入全绿。如实记着。
  await evaluate(`clearTimeout(sourcePoll); true`);
  check('摆局:这一页确实没读到来源目录', await evaluate(`pipeChannels.length === 0`),
        String(await evaluate(`pipeChannels.length`)));
  // 页头那条定时存成「开启 · 07:30 · RSS」—— 保存走的是 POST,不需要目录
  await evaluate(`pipePost('/api/pipeline/schedule',
    {enabled: true, time: '07:30', channels: ['rss']})`);

  // 真鼠标打开定时窗口:配置读得到(后端是好的),目录读不到
  await hit('#subs-sched-open');
  await wait(`$('sched-modal').style.display === '' && !schedLoading`);
  const gap = await form();
  check('目录没读到:不许说「已保存」,也不许进已加载',
        !gap.loaded && !/已保存/.test(gap.msg) && gap.boxes === 0, JSON.stringify(gap));
  check('目录没读到:说清缺的是哪一份,并放出重读入口',
        /来源目录/.test(gap.msg) && !gap.reload, JSON.stringify(gap));
  check('目录没读到:字段留在禁用态,保存也按不下去',
        gap.fields && gap.save, JSON.stringify(gap));
  // **目录那趟的失败不许抹掉配置那一趟的事实**:同一页上页头按钮和来源说明读的是
  // 共享事实,原来那一版把目录的失败拿去给配置结账,它们跟着一起失忆(实测)。
  const elsewhere = JSON.parse(await evaluate(`JSON.stringify({
    entry: $('subs-sched-label').textContent,
    note: $('source-settings-status').textContent,
    shared: JSON.stringify(schedules().pipe) })`));
  check('目录读不到,不许连页头按钮和来源说明一起抹成不知道',
        /每天 07:30/.test(elsewhere.entry) && /每天 07:30/.test(elsewhere.note)
        && /07:30/.test(elsewhere.shared), JSON.stringify(elsewhere));

  // 真鼠标点启用开关:字段整片禁用,点了也形不成草稿 —— 不许把人困在
  // 「一个都选不了、却要你至少选一个」那一步
  await hit('#sched-on');
  const after = await form();
  check('点启用开关形不成草稿(不会被困在草稿守卫后面)',
        !after.dirty && !after.on && after.save, JSON.stringify(after));

  // 恢复路径就在面板上:放开目录,真鼠标点「重新读取」
  await send('Network.setBlockedURLs', { urls: [] });
  await hit('#sched-reload');
  await wait(`!schedLoading`);
  await evaluate(`new Promise(r => setTimeout(r, 200))`);
  const fixed = await form();
  check('点「重新读取」把目录和配置一起补上,表单这才算已加载',
        fixed.loaded && fixed.catalog > 0 && fixed.boxes > 0 && !fixed.fields
        && fixed.reload && /每天 07:30/.test(fixed.msg), JSON.stringify(fixed));
  check('补齐之后才允许编辑并保存', await (async () => {
    await hit('#sched-on');
    const edited = await form();
    return edited.dirty && !edited.save;
  })());
} finally {
  process.stdout.write(JSON.stringify(results) + '\n');
  socket.close();
  proc.ref();
  proc.kill();
  await new Promise(resolve => proc.once('exit', resolve));
}
process.exitCode = results.some(result => !result.ok) ? 1 : 0;
