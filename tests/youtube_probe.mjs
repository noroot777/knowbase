import { mkdirSync, writeFileSync } from 'node:fs';
import { launchShell } from './probe_shell.mjs';

const base = process.argv[2];
const output = 'output/youtube';
mkdirSync(output, { recursive: true });
const results = [], errors = [];
const { proc, port } = await launchShell(['--window-size=1440,1000'], { profile: 'kb-youtube-' });
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
  for (let attempt = 0; attempt < 300; attempt++) {
    if (await evaluate(expression)) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Timed out: ' + expression);
};
const check = (name, ok, detail = '') => results.push({ name, ok: !!ok, detail });
const click = async selector => {
  const point = await evaluate(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) throw new Error('Missing target');
    element.scrollIntoView({block:'center'});
    const bounds = element.getBoundingClientRect();
    return {x:bounds.x+bounds.width/2, y:bounds.y+bounds.height/2};
  })()`);
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', button: 'left', clickCount: 1, ...point });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', button: 'left', clickCount: 1, ...point });
};
const fill = async (selector, value) => {
  await click(selector);
  await send('Input.dispatchKeyEvent', {type:'keyDown', key:'a', code:'KeyA', modifiers:2, windowsVirtualKeyCode:65, commands:['selectAll']});
  await send('Input.dispatchKeyEvent', {type:'keyUp', key:'a', code:'KeyA', modifiers:2, windowsVirtualKeyCode:65});
  await send('Input.insertText', {text:value});
};
const screenshot = async name => {
  const response = await send('Page.captureScreenshot', {format:'png'});
  writeFileSync(`${output}/${name}.png`, Buffer.from(response.result.data, 'base64'));
};
try {
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', {url: base + '/#subs'});
  await wait(`!!document.querySelector('#subs-switch-youtube')`);
  await click('#subs-switch-youtube');
  await wait(`sourceKind === 'youtube' && sourceData?.sources?.length === 52`);
  check('52 个频道出现在订阅管理', true);
  await click('#source-view-manage');
  await wait(`!!document.querySelector('#youtube-ranking') && !!document.querySelector('#youtube-max_videos')`);
  check('热度和价值榜显示且非 AI 视频被过滤', await evaluate(`
    document.querySelectorAll('.youtube-rank-item').length === 4 &&
    $('youtube-ranking').textContent.includes('轻量过滤')`));
  await evaluate(`$('youtube-rank-order').value='value_rank'; $('youtube-rank-order').dispatchEvent(new Event('change')); true`);
  check('可以切换价值榜', await evaluate(`$('youtube-rank-order').value === 'value_rank'`));
  await fill('#youtube-max_videos', '1');
  await click('#source-settings-save');
  await wait(`fetch('/api/intake/youtube').then(response=>response.json()).then(value=>value.settings.youtube.max_videos===1)`);
  check('处理数量预算保存到服务端', true);
  await click('#source-add');
  await wait(`$('source-editor').style.display !== 'none' && !!$('youtube-weight')`);
  check('频道编辑器隐藏通用全文抓取与专用 feed', await evaluate(`$('source-edit-feed').hidden && $('source-edit-fulltext').hidden`));
  await fill('#source-edit-name', '隔离验证频道');
  await fill('#source-edit-url', 'https://www.youtube.com/@fixture-channel');
  await fill('#youtube-weight', '1.5');
  await click('#source-edit-save');
  await wait(`sourceData?.sources?.length === 53`);
  check('频道权重与订阅保存', await evaluate(`
    sourceData.sources.some(source=>source.name==='隔离验证频道' && JSON.parse(source.config).score_multiplier===1.5)`));
  await evaluate(`$('source-list-pane').scrollTop=0; $('youtube-ranking').scrollIntoView({block:'start',behavior:'instant'}); true`);
  await screenshot('subscriptions-desktop');
  await click('#source-sync');
  await wait(`!!document.querySelector('#pipe-stage-list input[data-stage="extract"]') && !$('pipe-start').disabled`);
  for (const stage of ['extract', 'projects', 'wiki']) {
    if (await evaluate(`!!document.querySelector('#pipe-stage-list input[data-stage="${stage}"]:checked')`)) {
      await click(`#pipe-stage-list input[data-stage="${stage}"]`);
    }
  }
  check('同步仅运行抓取、导出和索引，不调用额外内容模型', await evaluate(`JSON.stringify(pipeStageList()) === JSON.stringify(['crawl','export','index'])`));
  await click('#pipe-start');
  await wait(`fetch('/api/intake/youtube/ranking').then(response=>response.json()).then(value=>value.counts.ready===1)`);
  await wait(`fetch('/api/pipeline/status').then(response=>response.json()).then(value=>!value.running)`);
  await wait(`!pipeSnapshot?.running && !$('source-add').disabled`);
  await click('#source-view-items');
  await wait(`!!document.querySelector('.source-entry')`);
  await click('.source-entry');
  await wait(`$('source-reader-body').textContent.includes('完整中文转录')`);
  check('排名预算只准入一条中文成品', await evaluate(`
    fetch('/api/intake/youtube/ranking').then(response=>response.json()).then(value=>value.counts.ready===1)`));
  check('阅读区展示中文标题、完整译文及画面边界', await evaluate(`
    $('source-reader-title').textContent.includes('智能体编程') &&
    $('source-reader-body').textContent.includes('未分析视频画面') &&
    $('source-reader-body').textContent.includes('不能仅凭生成结果')`));
  check('时间戳链接回到原视频', await evaluate(`!!document.querySelector('#source-reader-body a[href*="&t=300"]')`));
  await screenshot('chinese-reader-desktop');
  await click('#subs-switch-rss');
  await wait(`sourceKind === 'rss' && $('source-title').textContent === 'RSS' && !$('source-add').disabled`);
  check('切换 RSS 不遗留 YouTube 排名', await evaluate(`!document.querySelector('#youtube-ranking')`));
  await click('#source-add');
  await wait(`$('source-editor').style.display !== 'none'`);
  check('切换 RSS 后恢复通用输入框', await evaluate(`!$('source-edit-feed').hidden && ! $('source-edit-fulltext').hidden && $('youtube-channel-fields').hidden`));
  await evaluate(`closeSourceEditor(); true`);
  await send('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
  await click('#subs-switch-youtube');
  await wait(`sourceKind === 'youtube'`);
  await click('#source-view-manage');
  await wait(`!!document.querySelector('#youtube-ranking')`);
  check('窄屏无页面横向溢出', await evaluate(`document.documentElement.scrollWidth <= innerWidth + 1`));
  await screenshot('subscriptions-mobile');
  check('没有浏览器脚本异常', errors.length === 0, errors.join('\n'));
  writeFileSync(`${output}/checks.json`, JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results));
  if (results.some(result=>!result.ok)) process.exitCode=1;
} catch (error) {
  await screenshot('failure');
  console.error(JSON.stringify(results));
  console.error(error);
  console.error(errors);
  console.error(await evaluate(`Promise.all(['/api/intake/youtube/ranking','/api/pipeline/status'].map(path=>fetch(path).then(response=>response.json())))`));
  console.error(await evaluate(`document.body.innerText.slice(0,7000)`));
  process.exitCode=1;
} finally {
  socket.close();
  proc.kill();
}
