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
/** 等前端这一轮收尾:**存进服务端 ≠ 面板闲下来**,保存期间两颗按钮都是 disabled 的。 */
const settled = () => wait(`!$('source-settings-save').disabled && !$('source-add').disabled`);
// **要确认那一下真落在目标上**:坐标点上盖着别的东西时,鼠标事件打在盖着的那个上面,
// 页面毫无反应 —— 后面只会表现成某个 wait 莫名超时,看不出是点空了(实测踩过)。
const click = async selector => {
  const point = await evaluate(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) throw new Error('Missing target');
    element.scrollIntoView({block:'center'});
    const bounds = element.getBoundingClientRect();
    const x = bounds.x+bounds.width/2, y = bounds.y+bounds.height/2;
    const hit = document.elementFromPoint(x, y);
    const blocked = !hit || hit === element || element.contains(hit) || hit.contains(element)
      ? '' : (hit.id || hit.className || hit.tagName) + ' @ ' + Math.round(x) + ',' + Math.round(y);
    return {x, y, blocked};
  })()`);
  if (point.blocked) throw new Error(`点不到 ${selector}：那个坐标上盖着 ${point.blocked}`);
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

  // ── 登录态与工具那一块 ───────────────────────────────────────────────────
  await wait(`$('youtube-login').textContent.includes('已配置从')`);
  check('登录态那一块有浏览器下拉和两颗按钮', await evaluate(`
    $('youtube-cookies_browser').options.length >= 7 && $('youtube-cookies_browser').value === 'chrome' &&
    !!$('youtube-login-check') && !!$('youtube-ytdlp-upgrade') &&
    $('youtube-login').getAttribute('role') === 'status' && !$('youtube-login').classList.contains('field-err')`));
  check('装着的 yt-dlp 报出版本和来源', await evaluate(`
    $('youtube-ytdlp').textContent.includes('2026.06.09') &&
    $('youtube-ytdlp').textContent.includes('数据目录自备')`));
  await click('#youtube-login-check');
  await wait(`$('youtube-login').textContent.includes('没有登录')`);
  // 「没登录」要一眼看出是异常、而且要说清去哪儿做什么,不能和普通说明长一个样。
  check('没登录报成异常态并给出下一步', await evaluate(`
    $('youtube-login').classList.contains('field-err') &&
    $('youtube-login').getAttribute('role') === 'alert' &&
    $('youtube-login').textContent.includes('Chrome') &&
    $('youtube-login').textContent.includes('打开 youtube.com 登录一次') &&
    $('youtube-login-detail').textContent.includes('yt-dlp 原文') &&
    $('youtube-login-detail').textContent.includes('not a bot')`));
  // **版本旧不是「抓不到」的原因**(实测新旧两版撞的是同一道墙),所以它只报不报错。
  check('版本旧只提示可更新，不混成故障', await evaluate(`
    $('youtube-ytdlp').textContent.includes('上游已到 2026.10.26') &&
    !$('youtube-ytdlp').classList.contains('field-err')`));
  check('检查登录态用完恢复按钮', await evaluate(`
    !$('youtube-login-check').disabled && $('youtube-login-check').textContent === '检查登录态'`));
  // **轻提示不许挡住底下的操作。** 它居中压在底部、报错那一档存活 6 秒,而这里
  // 紧接着就是改下拉、点保存 —— 钉的是「那个坐标上点下去落在谁身上」,不是
  // 「pointer-events 写的是什么」。
  check('报错的轻提示不挡住底下的操作', await evaluate(`(() => {
    const node = document.querySelector('.toast.err');
    if (!node) throw new Error('这一步本该有一条报错轻提示');
    const box = node.getBoundingClientRect();
    const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    return !!hit && (!node.contains(hit) || hit.classList.contains('toast-x'));
  })()`));
  await click('#youtube-ytdlp-upgrade');
  await wait(`!!document.querySelector('.confirm-mask')`);
  check('更新 yt-dlp 走项目自己的确认框，不是原生弹窗', await evaluate(`
    document.querySelector('.confirm-mask h2').textContent.includes('更新 yt-dlp') &&
    document.querySelector('.confirm-mask .confirm-body').textContent.includes('数据目录')`));
  await click('.confirm-mask [data-act=cancel]');
  await wait(`!document.querySelector('.confirm-mask')`);
  await evaluate(`new Promise(resolve=>setTimeout(resolve,300)).then(()=>true)`);
  check('确认框取消就真的什么都不装', await evaluate(`
    $('youtube-ytdlp').textContent.includes('2026.06.09') &&
    !$('youtube-ytdlp').textContent.includes('正在下载') &&
    !$('youtube-ytdlp-upgrade').disabled`));
  // 改了下拉没保存就点检查:结论是按**已保存**那一份跑出来的,得说明白是哪一个。
  await evaluate(`$('youtube-cookies_browser').value='firefox'; true`);
  await click('#youtube-login-check');
  await wait(`$('youtube-login-detail').textContent.includes('先点保存设置')`);
  check('结论说明自己属于哪个浏览器', await evaluate(`
    $('youtube-login-detail').textContent.includes('chrome') &&
    $('youtube-login-detail').textContent.includes('firefox')`));
  await click('#source-settings-save');
  await wait(`fetch('/api/intake/youtube').then(r=>r.json()).then(v=>v.settings.youtube.cookies_browser==='firefox')`);
  check('浏览器选择保存到服务端', true);
  await settled();
  // ── 在途的那一次结论,代次过了就不许再落地 ───────────────────────────────
  // **这一条只有慢回包才看得见**:检查要真连一次 YouTube(最长 120 秒),这期间人会
  // 接着改设置、点保存 —— 而保存会重新填一遍整块面板。没有代次闸的话那条迟到的结论
  // 会盖在新面板上(审查实测:它贴在新选择旁边说「登录态有效」,说的却是上一轮)。
  // 这里把第一次检查的回包替换成一份好认的旧结论,**而且让它声称的浏览器跟保存后
  // 那一份一致** —— 否则拦住它的是「身份不符」那一档,代次闸本身仍然没被验到。
  await evaluate(`(() => {
    const real = window.fetch;
    let first = true;
    window.fetch = (url, options) => {
      if (String(url).includes('/api/intake/youtube/check') && first) {
        first = false;
        return new Promise(resolve => setTimeout(() => resolve(new Response(JSON.stringify({
          login: {ready: true, confirmed: true, browser: 'firefox', message: '旧结论不该出现'},
          ytdlp: {installed: '1999.01.01'},
        }), {status: 200, headers: {'Content-Type': 'application/json'}})), 1500));
      }
      return real(url, options);
    };
    window.__restoreFetch = () => { window.fetch = real; };
    document.querySelectorAll('.toast').forEach(node => node.remove());
    return true;
  })()`);
  await click('#youtube-login-check');
  await wait(`$('youtube-login').textContent.includes('正在连一次')`);
  await click('#source-settings-save');
  await wait(`$('youtube-login').textContent.includes('已配置从')`);
  await evaluate(`new Promise(resolve => setTimeout(resolve, 2200)).then(() => true)`);
  check('面板重填之后，在途的旧结论不再落地', await evaluate(`
    !$('youtube-login').textContent.includes('旧结论不该出现') &&
    !$('youtube-ytdlp').textContent.includes('1999.01.01') &&
    ![...document.querySelectorAll('.toast')].some(node => node.textContent.includes('登录态有效'))`),
    await evaluate(`$('youtube-login').textContent + ' || ' + $('youtube-ytdlp').textContent + ' || '
      + [...document.querySelectorAll('.toast')].map(node => node.textContent).join(' / ')`));
  await evaluate(`window.__restoreFetch(); true`);
  await wait(`!$('youtube-login-check').disabled`);
  await settled();
  check('候选榜显示且非 AI 视频被过滤', await evaluate(`
    document.querySelectorAll('.youtube-rank-item').length === 4 &&
    $('youtube-ranking').textContent.includes('轻量过滤')`));
  // **只有一张榜**:没有排序下拉，每条只报一个名次和一个分数，不再出现「热度/价值」。
  // 这段刻意不用正则 —— 它住在模板字面量里，`\d`、`\/` 这类转义会先被字面量吃掉。
  check('一张榜一个名次，没有排序下拉也没有两榜字样', await evaluate(`(() => {
    const text = $('youtube-ranking').textContent;
    const cards = [...document.querySelectorAll('.youtube-rank-item')];
    const ranks = cards.map(node => (node.textContent.split('第 ')[1] || '').split(' 名')[0]);
    return !document.querySelector('#youtube-rank-order') &&
      !text.includes('热度') && !text.includes('价值') &&
      cards[0].textContent.includes('第 1 名 / ') && cards[0].textContent.includes(' 分 · ') &&
      ranks.length === 4 && new Set(ranks).size === 4 && ranks[0] === '1';
  })()`), await evaluate(`document.querySelector('.youtube-rank-item')?.textContent`));
  check('设置里只剩一个准入名次，说明文字也不再提两榜', await evaluate(`
    !!$('youtube-rank_limit') && !document.querySelector('#youtube-heat_limit')
    && !document.querySelector('#youtube-value_limit')
    && !$('youtube-settings-fields').textContent.includes('热度')
    && !$('youtube-settings-fields').textContent.includes('价值榜')`),
    await evaluate(`$('youtube-settings-fields')?.textContent.slice(0, 160)`));
  await fill('#youtube-max_videos', '1');
  await click('#source-settings-save');
  await wait(`fetch('/api/intake/youtube').then(response=>response.json()).then(value=>value.settings.youtube.max_videos===1)`);
  await settled();
  check('处理数量预算保存到服务端', true);
  check('只改预算不会把登录态选择顶回默认', await evaluate(`
    fetch('/api/intake/youtube').then(r=>r.json()).then(v=>v.settings.youtube.cookies_browser==='firefox')`));
  // ── 浏览器用户资料那一格 ────────────────────────────────────────────────
  // 多 profile 是常态(工作/个人两个 Chrome 资料),登录 YouTube 的往往不是默认那个。
  await fill('#youtube-cookies_profile', '坏 名字');
  await click('#youtube-login-check');
  check('资料名不合法就地红字、不飘轻提示、也不白连一次', await evaluate(`
    $('youtube-browser-err').textContent.includes('只能用字母') &&
    ![...document.querySelectorAll('.toast')].some(node => node.textContent.includes('登录态')) &&
    !$('youtube-login').textContent.includes('正在连一次')`));
  await fill('#youtube-cookies_profile', 'Profile 1');
  await wait(`!$('youtube-browser-err').textContent`);
  await click('#source-settings-save');
  await wait(`fetch('/api/intake/youtube').then(r=>r.json()).then(v=>v.settings.youtube.cookies_browser==='firefox:Profile 1')`);
  check('下拉和资料名拼成一份规格存下去', true);
  await settled();
  // **回填要能拆回两格。** 拆不回去的话下拉框会把整串显示成第一项,而人只是进来
  // 看一眼预算 —— 下一次保存就把资料名丢了。走一趟换来源再回来:那才是真正重新
  // 从服务端填一遍的那条路(切视图不重填,设置那几格一直留在 DOM 里)。
  await click('#subs-switch-rss');
  await wait(`sourceKind === 'rss' && $('source-settings-form').hidden`);
  await click('#subs-switch-youtube');
  await wait(`sourceKind === 'youtube' && sourceView === 'manage' && !$('source-settings-form').hidden
    && $('youtube-login').textContent.includes('已配置从')`);
  check('存下去的规格回填时拆回两格', await evaluate(`
    $('youtube-cookies_browser').value === 'firefox' && $('youtube-cookies_profile').value === 'Profile 1'`),
    await evaluate(`$('youtube-cookies_browser').value + ' | ' + $('youtube-cookies_profile').value`));

  // ── 状态回包不许改掉人正在填的那两格 ────────────────────────────────────
  // 选浏览器和填资料名都不发请求、也就不推代次,所以代次闸管不到这一类。
  // `/youtube/status` 要等后端查完工具和登录态(审查实测把它压到 12 秒):切过来趁
  // 「正在检查登录态配置…」填上 Profile 2,回包落地把它改回已存的 Profile 1,一句提示
  // 都没有。这里把这一包压 1.5 秒,走真接口、只改到达时刻。
  await evaluate(`(() => {
    const real = window.fetch;
    window.fetch = (url, options) => {
      if (String(url).includes('/api/intake/youtube/status')) {
        return new Promise(resolve => setTimeout(() => resolve(real(url, options)), 1500));
      }
      return real(url, options);
    };
    window.__restoreFetch = () => { window.fetch = real; };
    return true;
  })()`);
  await click('#subs-switch-rss');
  await wait(`sourceKind === 'rss' && $('source-settings-form').hidden`);
  await click('#subs-switch-youtube');
  await wait(`sourceKind === 'youtube' && $('youtube-login').textContent.includes('正在检查登录态配置')`);
  // 回包还没到,已存的那一份就该在两格里 —— 空着的话这会儿保存预算就把资料名丢了。
  check('status 还在途，已存的浏览器和资料名就已经填好了', await evaluate(`
    $('youtube-cookies_browser').value === 'firefox' && $('youtube-cookies_profile').value === 'Profile 1'`),
    await evaluate(`$('youtube-cookies_browser').value + ' | ' + $('youtube-cookies_profile').value`));
  await fill('#youtube-cookies_profile', 'Profile 2');
  await wait(`$('youtube-tools').textContent && !$('youtube-tools').textContent.includes('正在检查')`);
  // 反过来钉一边:草稿留住的同时,下拉清单照旧要被这一包补齐(七个浏览器),
  // 否则一把「什么都不写」的闸也能让上面那条绿。
  check('迟到的状态回包只补清单，不改正在填的资料名', await evaluate(`
    $('youtube-cookies_profile').value === 'Profile 2' &&
    $('youtube-cookies_browser').value === 'firefox' &&
    $('youtube-cookies_browser').options.length >= 7 &&
    !$('youtube-cookies_browser').textContent.includes('当前设置')`),
    await evaluate(`$('youtube-cookies_browser').value + ' | ' + $('youtube-cookies_profile').value
      + ' | ' + $('youtube-cookies_browser').options.length`));
  await evaluate(`window.__restoreFetch(); true`);
  await fill('#youtube-cookies_profile', 'Profile 1');
  await click('#source-settings-save');
  await wait(`fetch('/api/intake/youtube').then(r=>r.json()).then(v=>v.settings.youtube.cookies_browser==='firefox:Profile 1')`);
  await settled();

  // 下面两条要看的是「结论本身在不在」,所以先把保存的那一份调成夹具声称的那一个
  // (`chrome`,不带资料名)—— 否则检查回来会先撞上「期间换过浏览器」那一档作废掉,
  // 而那一档本来就该作废,拿它去验这两条等于什么都没验。
  await evaluate(`$('youtube-cookies_browser').value='chrome'; $('youtube-cookies_profile').value=''; true`);
  await click('#source-settings-save');
  await wait(`fetch('/api/intake/youtube').then(r=>r.json()).then(v=>v.settings.youtube.cookies_browser==='chrome')`);
  await settled();

  // ── 三个独立动作不许互相吞掉有效结果 ────────────────────────────────────
  // 原来三个慢动作共用一个代次:开始任何一个,另外两个在途的**有效**回包全部作废,
  // 而作废它们的那个动作并不替它们收尾。审查在真页面上实测两种形态,这里各钉一条。
  //
  // 形态一:刚切过来、状态还在途时点一次检查 —— 浏览器下拉永远只剩一项、工具行一直
  // 停在「正在检查工具和模型配置…」,检查按钮却已恢复,重开页面才恢复正常。
  await evaluate(`(() => {
    const real = window.fetch;
    window.fetch = (url, options) => {
      if (String(url).includes('/api/intake/youtube/status')) {
        return new Promise(resolve => setTimeout(() => resolve(real(url, options)), 1500));
      }
      return real(url, options);
    };
    window.__restoreFetch = () => { window.fetch = real; };
    return true;
  })()`);
  await click('#subs-switch-rss');
  await wait(`sourceKind === 'rss' && $('source-settings-form').hidden`);
  await click('#subs-switch-youtube');
  await wait(`sourceKind === 'youtube' && $('youtube-login').textContent.includes('正在检查登录态配置')`);
  await click('#youtube-login-check');
  await wait(`$('youtube-login').textContent.includes('没有登录')`);
  await evaluate(`new Promise(resolve => setTimeout(resolve, 2200)).then(() => true)`);
  check('检查期间到达的状态回包照旧补齐清单和工具行', await evaluate(`
    $('youtube-cookies_browser').options.length >= 7 &&
    !$('youtube-tools').textContent.includes('正在检查') &&
    !!$('youtube-tools').textContent`),
    await evaluate(`$('youtube-cookies_browser').options.length + ' | ' + $('youtube-tools').textContent`));
  // 反过来钉一边:那一包不许把刚检出来的结论盖成一句笼统的「已配置」。
  check('便宜的状态快照不许盖掉刚检出来的结论', await evaluate(`
    $('youtube-login').textContent.includes('没有登录') &&
    !$('youtube-login').textContent.includes('已配置从')`),
    await evaluate(`$('youtube-login').textContent`));
  await evaluate(`window.__restoreFetch(); true`);
  await wait(`!$('youtube-login-check').disabled`);
  await settled();

  // 形态二:更新 yt-dlp 期间点一次检查 —— 更新装完回来的新版本号写不进页面,版本行
  // 停在旧版、按钮已恢复(页面于是同时挂着「已更新」和一个旧版本号)。
  // 更新那一包压 2 秒并换成一份好认的新版本;检查走真接口(它会顺带带回旧版本号)。
  await evaluate(`(() => {
    const real = window.fetch;
    window.fetch = (url, options) => {
      if (String(url).includes('/api/intake/youtube/ytdlp/upgrade')) {
        return new Promise(resolve => setTimeout(() => resolve(new Response(JSON.stringify({
          ytdlp: {installed: '2099.01.01', origin: 'bundled', latest: '2099.01.01', outdated: false},
        }), {status: 200, headers: {'Content-Type': 'application/json'}})), 2000));
      }
      return real(url, options);
    };
    window.__restoreFetch = () => { window.fetch = real; };
    document.querySelectorAll('.toast').forEach(node => node.remove());
    return true;
  })()`);
  await click('#youtube-ytdlp-upgrade');
  await wait(`!!document.querySelector('.confirm-mask [data-act=ok]')`);
  await click('.confirm-mask [data-act=ok]');
  await wait(`$('youtube-ytdlp').textContent.includes('正在下载并安装')`);
  await click('#youtube-login-check');
  // **要确认检查真的先回来了**(它顺带带回的是旧版本号),否则下面那条断言可能只是
  // 「检查还没落地」而不是「没盖掉」。它结束时会飘一条轻提示,而上面刚把 toast 清空过。
  await wait(`[...document.querySelectorAll('.toast')].some(node => node.textContent.includes('没有登录'))`);
  check('更新还在途时，版本行停在「正在下载并安装」不被检查改写', await evaluate(`
    $('youtube-ytdlp').textContent.includes('正在下载并安装')`),
    await evaluate(`$('youtube-ytdlp').textContent`));
  await wait(`$('youtube-ytdlp').textContent.includes('2099.01.01')`);
  check('更新装完的版本号写得进页面，检查顺带查到的旧版本不许盖掉它', await evaluate(`
    $('youtube-ytdlp').textContent.includes('2099.01.01') &&
    !$('youtube-ytdlp').textContent.includes('2026.06.09') &&
    !$('youtube-ytdlp-upgrade').disabled`),
    await evaluate(`$('youtube-ytdlp').textContent`));
  await evaluate(`window.__restoreFetch(); true`);
  await wait(`!$('youtube-login-check').disabled && !$('youtube-ytdlp-upgrade').disabled`);
  await settled();

  // 形态三:**更新结束之后**再点检查,版本信息要回得来。「档位只升不降」的写法在这里
  // 坏掉:更新一结束就没人再写那块区了,它留下的占用却把之后每一次检查都挡在外面 ——
  // 登录结论照旧刷新、版本行永远停在更新时那句话,看着像只坏了半边,重开页面才好。
  // 这里用「更新失败」这一档收尾:**失败分支也必须松开占用**(成功那一档形态二走过)。
  await evaluate(`(() => {
    const real = window.fetch;
    window.fetch = (url, options) => {
      if (String(url).includes('/api/intake/youtube/ytdlp/upgrade')) {
        return Promise.resolve(new Response(JSON.stringify({detail: '探针：安装源不可达'}),
          {status: 500, headers: {'Content-Type': 'application/json'}}));
      }
      return real(url, options);
    };
    window.__restoreFetch = () => { window.fetch = real; };
    document.querySelectorAll('.toast').forEach(node => node.remove());
    return true;
  })()`);
  await click('#youtube-ytdlp-upgrade');
  await wait(`!!document.querySelector('.confirm-mask [data-act=ok]')`);
  await click('.confirm-mask [data-act=ok]');
  await wait(`$('youtube-ytdlp').textContent.includes('更新失败')`);
  await evaluate(`window.__restoreFetch(); true`);
  await click('#youtube-login-check');
  await wait(`$('youtube-login').textContent.includes('正在连一次')`);
  await wait(`!$('youtube-login-check').disabled`);
  check('更新结束之后再点检查，版本信息要回得来', await evaluate(`
    $('youtube-ytdlp').textContent.includes('2026.06.09') &&
    $('youtube-ytdlp').textContent.includes('上游已到 2026.10.26') &&
    !$('youtube-ytdlp').textContent.includes('更新失败')`),
    await evaluate(`$('youtube-ytdlp').textContent`));
  await settled();

  // 形态四:反过来钉一边 —— **真正重叠**的那次旧检查晚回包,不许反向覆盖。更新装完之后
  // 它才回来,「在途的里面有没有更高档位」这道闸已经不拦了,而它带的版本号是更新前那个:
  // 只按「谁还在途」判归属就会把刚装上的版本倒回旧版。检查压 3 秒,更新 1 秒就装完。
  await evaluate(`(() => {
    const real = window.fetch;
    window.fetch = (url, options) => {
      if (String(url).includes('/api/intake/youtube/ytdlp/upgrade')) {
        return new Promise(resolve => setTimeout(() => resolve(new Response(JSON.stringify({
          ytdlp: {installed: '2099.01.01', origin: 'bundled', latest: '2099.01.01', outdated: false},
        }), {status: 200, headers: {'Content-Type': 'application/json'}})), 1000));
      }
      if (String(url).includes('/api/intake/youtube/check')) {
        return new Promise(resolve => setTimeout(() => resolve(real(url, options)), 3000));
      }
      return real(url, options);
    };
    window.__restoreFetch = () => { window.fetch = real; };
    document.querySelectorAll('.toast').forEach(node => node.remove());
    return true;
  })()`);
  await click('#youtube-ytdlp-upgrade');
  await wait(`!!document.querySelector('.confirm-mask [data-act=ok]')`);
  await click('.confirm-mask [data-act=ok]');
  await wait(`$('youtube-ytdlp').textContent.includes('正在下载并安装')`);
  await click('#youtube-login-check');
  await wait(`$('youtube-ytdlp').textContent.includes('2099.01.01')`);
  // 等那次检查真的回来(它结束时飘一条轻提示,上面刚把 toast 清空过),否则下面那条
  // 断言可能只是「它还没落地」而不是「它没盖掉」。
  await wait(`[...document.querySelectorAll('.toast')].some(node => node.textContent.includes('没有登录'))`);
  check('更新结束后才回来的那次旧检查，不许把版本号倒回旧版', await evaluate(`
    $('youtube-ytdlp').textContent.includes('2099.01.01') &&
    !$('youtube-ytdlp').textContent.includes('2026.06.09')`),
    await evaluate(`$('youtube-ytdlp').textContent`));
  await evaluate(`window.__restoreFetch(); true`);
  await wait(`!$('youtube-login-check').disabled && !$('youtube-ytdlp-upgrade').disabled`);
  await settled();
  // 上面几条点检各留下一条轻提示(错误那档活 6 秒),而下一步要点的保存按钮正好在它们
  // 底下。提示本体不接点击,但那颗关闭按钮接 —— 探针的命中检测于是报「盖着 toast-x」。
  // **这是探针自己要收的尾**,不是页面的毛病:先清干净再往下点。
  await evaluate(`document.querySelectorAll('.toast').forEach(node => node.remove()); true`);
  await wait(`!document.querySelector('.toast')`);

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
  // **别打整个 body**:频道列表有 52 条,7000 字全是它,真正的错误在截断之后 ——
  // 实测一次失败就这么浪费掉了。要看的是编辑器、那两处错误位和最近的 toast。
  console.error(await evaluate(`JSON.stringify({
    editor: $('source-editor')?.style.display,
    editorError: document.querySelector('#source-editor .field-err')?.textContent || '',
    settingsError: $('source-settings-error')?.textContent || '',
    toasts: [...document.querySelectorAll('.toast')].map(node=>node.textContent),
    count: sourceData?.sources?.length,
    names: (sourceData?.sources||[]).slice(-3).map(source=>source.name),
    fields: {name: $('source-edit-name')?.value, url: $('source-edit-url')?.value,
             weight: $('youtube-weight')?.value},
  }, null, 2)`));
  process.exitCode=1;
} finally {
  socket.close();
  proc.kill();
}
