// X 采集通道在**真页面**上的回归:三条通道各要填的东西互斥显示、改了选择不许
// 冒充当前事实、保存时真把三个字段发出去、登录窗口和「检查连接」各打各的端点。
//
// mock 后端在 `x_channel_mock.mjs`(单文件 700 行上限,按职责切开:那一份只管
// 「后端会怎么回答」,这一份只管「页面上该发生什么」)。CDP 直连 playwright 缓存里
// 的 chrome-headless-shell(不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/x_channel_probe.mjs
import { launchShell } from "./probe_shell.mjs";
import { CHANNELS, NEXT_STEP, posted, saved, server, base, mock }
  from "./x_channel_mock.mjs";

// ── 无头浏览器 ───────────────────────────────────────────────────────────────
const { proc, port } = await launchShell(["--window-size=1440,1000"]);
const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));
let msgId = 0; const pend = new Map(); const pageErrors = [];
ws.addEventListener("message", ev => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pend.has(msg.id)) { pend.get(msg.id)(msg); pend.delete(msg.id); return; }
  if (msg.method === "Runtime.exceptionThrown")
    pageErrors.push(msg.params.exceptionDetails?.exception?.description || msg.params.exceptionDetails?.text);
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error")
    pageErrors.push(msg.params.args.map(a => a.value ?? a.description ?? "").join(" "));
});
const send = (method, params = {}) => { const id = ++msgId;
  return new Promise(res => { pend.set(id, res); ws.send(JSON.stringify({ id, method, params })); }); };
const evalJs = async expression => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (expr, ms = 10000) => { const dl = Date.now() + ms;
  while (Date.now() < dl) { if (await evalJs(expr)) return true; await sleep(100); } return false; };
// 真鼠标点在元素中心:直接调 .click() 绕开了"这颗按钮在页面上点不点得到"。
const clickAt = async sel => {
  const box = await evalJs(`(() => { const e = document.querySelector('${sel}');
    if (!e || !e.getClientRects().length) return null; const r = e.getBoundingClientRect();
    return JSON.stringify({ x: r.left + r.width / 2, y: r.top + r.height / 2 }); })()`);
  if (!box) return false;
  const { x, y } = JSON.parse(box);
  for (const type of ["mousePressed", "mouseReleased"])
    await send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 });
  return true;
};
await send("Runtime.enable"); await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride",
           { width: 1440, height: 1600, deviceScaleFactor: 1, mobile: false });

const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));
const shown = sel => evalJs(`(() => { const e = document.querySelector('${sel}');
  return !!e && e.getClientRects().length > 0; })()`);
// 在「换一种」那张清单上选一档(和用户点那一行一样走 change)。
const pick = id => evalJs(`(() => {
  const r = document.querySelector('#source-x-picker input[value="${id}"]');
  if (!r) return false; r.checked = true; r.dispatchEvent(new Event('change')); return true; })()`);
const picked = () => evalJs(`xChannelValue()`);
const text = sel => evalJs(`document.querySelector('${sel}')?.textContent || ''`);
const state = () => text('#source-x-state');

// 每个新文档一开张就把三个原生弹窗换成记录器。用 addScriptToEvaluateOnNewDocument
// 是因为它跑在页面脚本之前 —— 导航之后再替换,中间那段时间的调用就漏掉了。
await send("Page.addScriptToEvaluateOnNewDocument", { source: `
  window.__nativeDialogs = [];
  for (const name of ['alert', 'confirm', 'prompt'])
    window[name] = (...args) => { window.__nativeDialogs.push(name + ':' + args.join(' ')); };
` });
await send("Page.navigate", { url: base + "/" });
await until(`typeof openSubsPanel === 'function' && typeof subsCatalog !== 'undefined'
             && subsCatalog.length === 3`, 25000);
await evalJs(`switchTab('subs')`);
if (!await until(`document.querySelectorAll('#subs-switch button').length === 3`, 20000))
  throw new Error("来源切换器没渲染出来:" + await evalJs(
    `JSON.stringify({tab: typeof activeTab !== 'undefined' ? activeTab : null,
                     cat: (subsCatalog||[]).map(m=>m.id),
                     html: document.getElementById('subs-switch').innerHTML.slice(0,200),
                     err: document.getElementById('subs-error-text').textContent})`));
await clickAt("#subs-switch-social");
if (!await until(`sourceKind === 'social' && xChan !== null`, 20000))
  throw new Error("大V观点视图没打开:" + await evalJs(
    `JSON.stringify({kind: sourceKind, chan: xChan,
                     err: document.getElementById('source-x-state').textContent})`));
// **来源设置整块只在「订阅管理」那一档显示**(`updateSourceReadingView`:
// `$('source-settings').hidden = sourceView !== 'manage'`)—— 不切过去的话这一页
// 默认停在「最新内容」,整个 `#source-settings` 是 hidden,于是下面每一条
// `shown(...)` 都假、每一条 `!shown(...)` 都真:**这份探针会一半红一半"通过"，
// 而两边都不是在验它想验的东西**。这就是 2026-10-15 实测的形态:HEAD 上
// 27/49 红,基线和改动同样红,红的清单里全是"这一块该显示"那一类,
// 而"改了选择不冒充当前事实"之类不碰可见性的照旧绿。
// 所以这一步不是摆盘的装饰,是这份探针成立的前提。
await evalJs(`switchSourceView('manage')`);
if (!await until(`sourceView === 'manage'
                  && !document.getElementById('source-settings').hidden`, 20000))
  throw new Error("订阅管理那一档没打开:" + await evalJs(
    `JSON.stringify({view: typeof sourceView !== 'undefined' ? sourceView : null,
                     hidden: document.getElementById('source-settings').hidden})`));

// 1. 通道清单在大V观点的设置里,名字、说明、当前值都来自后端
check("当前这一档的名字写在卡片上", (await text('#source-x-name')) === "feedgrab(cookie)",
      await text('#source-x-name'));
check("「换一种」那张清单默认收着(来看状态的人占九成)", !await shown("#source-x-picker"));
check("点「换一种」把清单打开", await clickAt("#source-x-switch") && await shown("#source-x-picker"));
check("三条通道都列出来,名字和说明逐字来自后端",
      await evalJs(`[...document.querySelectorAll('#source-x-picker .xchan-opt')]
                      .map(o => o.querySelector('input').value + ':'
                                + o.querySelector('b').textContent + ':'
                                + o.querySelector('span').textContent).join('|')`)
      === CHANNELS.map(c => `${c.id}:${c.label}:${c.hint}`).join("|"),
      await evalJs(`[...document.querySelectorAll('#source-x-picker .xchan-opt')]
                      .map(o => o.querySelector('input').value).join()`));
check("选中的是后端说的那一条", (await picked()) === "feedgrab");
check("feedgrab 那块可见、浏览器那块不可见",
      await shown("#source-x-feedgrab") && !await shown("#source-x-browser"));
check("登录命令照抄得出来",
      (await text('#source-x-login')).includes("feedgrab login"), await text('#source-x-login'));
// 那条命令就是这一档的「下一步」(后端 readiness 的第三项必须带着它 —— 待配置
// 横幅那边没有可复制块),状态行里并排再写一遍只是噪音。**去重按内容判**。
check("同一条命令在整块面板上只出现一次",
      Number(await evalJs(`(() => { const t = document.getElementById('source-xchan').textContent;
        const n = ${JSON.stringify("feedgrab login twitter")};
        let i = 0, at = 0; while ((at = t.indexOf(n, at)) >= 0) { i++; at += n.length; }
        return i; })()`)) === 1,
      await state());

// 2. 换成浏览器那一档:要填的东西跟着换,**但还没存下来**
await pick("browser");
check("选浏览器后只显示它那一块",
      await shown("#source-x-browser") && !await shown("#source-x-feedgrab"));
check("改了选择不冒充当前事实、并写明还没存下来",
      (await state()).includes("还没存下来"), await state());
check("那条终端命令跟着收走了(它是另一档的事)", !await shown("#source-x-cmd"));
check("色点在草稿上是中性的(没有结论可给)",
      (await evalJs(`document.getElementById('source-x-dot').className`)) === "xchan-dot",
      await evalJs(`document.getElementById('source-x-dot').className`));

// 3. **先保存再执行**:草稿没存下来时按钮照旧可点,点下去先替用户存一次。
// 原来是把按钮灰掉、把「先点保存设置」藏进 hover 才出得来的 title 里 —— 用户
// 看到的是一颗按不动的按钮(他原话:「那肯定不合常理」)。
check("「打开登录窗口」看得见、而且不是禁用的",
      await shown("#source-x-login-btn")
      && (await evalJs(`document.getElementById('source-x-login-btn').disabled`)) === false);
check("「导入登录态」看得见、而且不是禁用的",
      await shown("#source-x-import-btn")
      && (await evalJs(`document.getElementById('source-x-import-btn').disabled`)) === false);
check("「检查连接」任何时候都在(不再按保存与否藏起来)", await shown("#source-x-check"));
const beforeSave = saved.length, beforePost = posted.length;
check("「打开登录窗口」真点得到", await clickAt("#source-x-login-btn"));
await until(`document.querySelector('#toast-host')?.textContent.includes('已打开')`, 15000);
check("点下去先把草稿存了(而不是白跑一趟换回一句拒绝)",
      saved.length === beforeSave + 1 && saved[saved.length - 1].x_channel === "browser",
      JSON.stringify(saved.slice(beforeSave)));
check("保存这件事说出来了,不是闷头替他改设置",
      (await text('#toast-host')).includes("已先保存"), await text('#toast-host'));
check("登录请求打的是 login 端点,而且是 POST",
      posted.includes("POST /api/intake/social/x-channel/login"), posted.slice(beforePost).join());
check("登录结果如实显示在提示里", (await text('#toast-host')).includes("关掉它"),
      await text('#toast-host'));
check("页面上不出现那句自相矛盾的拒绝",
      !(await text('#toast-host')).includes("打不开登录窗口"), await text('#toast-host'));
check("保存之后这条就是当前通道", await evalJs(`xChan && xChan.current === 'browser'`));
// **反过来钉一边:已经存下来的那一档不许再多存一次** —— 否则这颗按钮每点一下
// 都往数据库写一遍,而用户什么都没改。
const settled = saved.length;
await evalJs(`(document.getElementById('toast-host')||{}).innerHTML = ''; true`);
check("再点一次「打开登录窗口」", await clickAt("#source-x-login-btn"));
await until(`document.querySelector('#toast-host')?.textContent.includes('已打开')`, 15000);
check("已经存下来时不再多发一次保存", saved.length === settled,
      JSON.stringify(saved.slice(settled)));
check("也不再说一遍「已先保存」", !(await text('#toast-host')).includes("已先保存"),
      await text('#toast-host'));

// 4. 「保存设置」自己那条路:字段真的发出去了
await evalJs(`document.getElementById('source-x-chrome').value =
                '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'; true`);
const beforeForm = saved.length;
await evalJs(`document.getElementById('source-settings-form')
                .dispatchEvent(new Event('submit', {cancelable:true})); true`);
// `saved` 在 node 这一侧,页面里的 `until` 看不见它 —— 这一等要在这边轮询。
for (let i = 0; i < 100 && saved.length === beforeForm; i++) await sleep(100);
const sent = saved[saved.length - 1] || {};
check("保存时带上了通道选择", sent.x_channel === "browser", JSON.stringify(sent));
check("保存时带上了 Chrome 路径",
      (sent.x_chrome_path || "").endsWith("Google Chrome"), JSON.stringify(sent.x_chrome_path));

// 4.6 「从日常浏览器导入登录态」
check("「导入登录态」真点得到", await clickAt("#source-x-import-btn"));
// 趁 mock 还没回,先把那句预告读下来(见 mock 里那段 1.2 秒的理由)。
const importPreface =
  await evalJs(`document.getElementById('source-x-import-state').textContent`);
await until(`document.getElementById('source-x-import-state')
               .textContent.includes('上次从日常浏览器导入')`, 20000);
// 方法要对:真路由只收 POST,发 GET 的话这颗按钮在真页面上永远是 405
// (口径同「检查连接」那两条,2026-10-08 实测过一次)。
check("导入请求打的是 import-login 端点,而且是 POST",
      posted.includes("POST /api/intake/social/x-channel/import-login"), posted.join());
check("导入没有发出任何 GET",
      !posted.some(p => p.startsWith("GET /api/intake/social/x-channel/import-login")),
      posted.join());
// **留痕那行字要落在页面上。** 它是这个功能唯一的"上次什么时候搬的"露头 ——
// 不显示的话用户只能靠记忆判断该不该再导一次(cookie 过期是常态)。
check("留痕落到面板上(带时刻和条数)",
      (await evalJs(`document.getElementById('source-x-import-state').textContent`))
        .includes("13 个 cookie"),
      await evalJs(`document.getElementById('source-x-import-state').textContent`));
// **按下去之后要先预告那个授权弹窗。** 这一步会停在弹窗上最长 90 秒,而弹窗出现
// 在另一个窗口里 —— 不预告的话用户看到的是一颗按下去没反应的按钮。
check("点下去立刻预告了那个授权弹窗",
      importPreface.includes("授权"), importPreface);
// **导入成功之后,就绪那一行不许还说导入之前的话。** 页面上同时挂着「当场验证
// 通过」和「浏览器通道还没有登录过 X」时,后者说的是导入**之前**那一刻 —— 用户
// 得自己再点一次「检查连接」才看得到真相(本轮审查 A 项,它有截图)。
check("导入成功后那句「还没有登录过 X」不再挂在页面上",
      !(await evalJs(`document.getElementById('source-x-state').textContent`))
        .includes("还没有登录过"),
      await evalJs(`document.getElementById('source-x-state').textContent`));
check("导入没有顺手再发一次 GET 去问就绪(后端已经带回来了)",
      posted.filter(q => q === "POST /api/intake/social/x-channel/import-login").length === 1,
      posted.join());

// 4.6b 迟到的回包不许盖掉用户刚改出来的「还没保存」提醒
// **这一条是异步结果归属。** 导入最长等 240 秒,这期间用户完全可能改了下拉 ——
// 那时状态行上挂的是「改完记得点保存设置」,而迟到的成功文案一盖,页面看着像
// 已经生效,实际存下来的还是旧档(第 2 轮审查在真页面上实测,`import-race.png`)。
check("「导入登录态」再点一次", await clickAt("#source-x-import-btn"));
// 趁那 1.2 秒还没回,把取法改走(和用户点那一行一样走 change)。
await pick('feedgrab');
const racedSay = await state();
check("改完之后状态行先说的是「还没存下来」", racedSay.includes("还没存下来"), racedSay);
await sleep(2200);                       // 等那个回包确实回来过
check("迟到的导入回包没有盖掉「还没存下来」那句提醒",
      (await state()).includes("还没存下来"), await state());
check("也没把成功文案写到留痕那行上",
      (await evalJs(`document.getElementById('source-x-import-state').textContent`)) === "",
      await evalJs(`document.getElementById('source-x-import-state').textContent`));
// 收尾:把取法改回去,后面几步仍按 browser 这一档继续。
await pick('browser');

// 4.6c 同一道闸的另一半:**迟到的那条读也不许盖**
// 上面两条钉的是两颗按钮的回包;读那一条原来只判「来源类型变没变」,而类型在
// 这个场景里一直是 social。摆法见上面 `getHold` 那段注释:**读还在路上,用户
// 改了取法** —— 那条读回来之后重建下拉、`sel.value = data.current`,把他刚选的
// 那一档静默换回去,顺带把「改完记得点保存设置」盖成「当前通道已就绪」,而面板上
// 一切正常(第 3 轮审查 A 项,真页面实测)。
await evalJs(`fetch('/__probe/slow-get?ms=900').then(() => true)`);
await evalJs(`loadXChannel('social'); true`);     // 这一条要在路上待 900 毫秒
await sleep(120);
// 趁它还没回,用户改了取法(和手点一样走 change)。
await pick('feedgrab');
await sleep(1400);                                // 等那条读确实回来过
check("迟到的那条读没把用户刚选的那一档换回去", (await picked()) === "feedgrab", await picked());
check("也没把「还没存下来」那句提醒盖成「已就绪」",
      (await state()).includes("还没存下来"), await state());
// 收尾:改回 browser,后面几步仍按这一档继续。
await pick('browser');

// 4.6d 同一道闸的最后一半:**后来的事实要让此前发出的读作废**
// 上面那条钉的是「读在途、用户改了下拉」。这一条换成「读在途、**导入成功了**」:
// 先发一条读(它说的是导入之前那一刻:还没登录过 X),再点导入,导入先回来、把
// 成功记录写上去,那条比它更早发出的读随后才到 —— 它们属于同一代,所以旧代码里
// 它照旧通得过代次检查,把面板盖回「浏览器通道还没有登录过 X」,成功 toast 和那句
// 否定同时挂在屏幕上(第 4 轮审查在真页面上实测)。出路是:**写入落地时推进代次**。
await evalJs(`fetch('/__probe/slow-get?ms=3000&forget=1').then(() => true)`);
await evalJs(`loadXChannel('social'); true`);     // 这条读要在路上待 3 秒
await sleep(150);
check("「导入登录态」在这一档上点得到", await clickAt("#source-x-import-btn"));
await until(`document.getElementById('source-x-import-state').textContent.includes('导入')`,
            15000);
const importedSay = await evalJs(`document.getElementById('source-x-import-state').textContent`);
await sleep(3200);                                // 等那条旧读确实回来过
check("迟到的那条读没把刚导入成功那行留痕清掉",
      (await evalJs(`document.getElementById('source-x-import-state').textContent`)) === importedSay,
      await evalJs(`document.getElementById('source-x-import-state').textContent`));
check("也没把状态行盖回「还没有登录过 X」",
      !(await evalJs(`document.getElementById('source-x-state').textContent`)).includes("还没有登录过"),
      await evalJs(`document.getElementById('source-x-state').textContent`));

// 4.6e 两条读交错:**算谁最后发起,不算谁最后回来**
// 上面两条摆的都是「慢回包落地时面板已经换了人」。这一条摆的是另一半:两条读都
// 还属于这块面板,但**先发那条先回来**。「谁最后回来谁算」的话,先发那条(说的是
// 保存之前那一刻)会赢,而后发那条随后落地时反倒被判成过期 —— 面板停在旧事实上。
// 判据是**发起顺序**:后发那条才代表用户最新的意图。
//
// **摆法是把 `pipeJson` 换成一个可控时序的壳,不走网络层** —— 两条 URL 完全一样
// 的 GET 被浏览器合流排队了(诊断见上面 `getHold` 那段),慢服务端摆不出交错;
// 而要钉的恰恰是 `loadXChannel` 自己那段归属逻辑,传输层打桩不影响它。
await evalJs(`(() => {
  window.__realPipeJson = window.pipeJson;
  let n = 0;
  window.pipeJson = async (path, opts, t) => {
    if (!String(path).endsWith('/x-channel')) return window.__realPipeJson(path, opts, t);
    // **发起时就定序**:放到 await 之后去数的话,序号取决于两条网络 promise 谁先
    // 结算(同 URL 的 GET 还被浏览器合流),于是这条用例自己会飘。
    const which = ++n;
    const r = await window.__realPipeJson(path, opts, t);
    // **两条都要压时间,而且先发那条也不许秒回** —— 不压的话它在第二条发出之前就
    // 落地了,两条读压根不重叠,这条用例于是对「发起时取代次」不敏感(摆这一条时
    // 实测:60 毫秒的间隔里先发那条已经应用完了,注入旧代码照旧全绿)。
    await new Promise(res => setTimeout(res, which === 1 ? 600 : 1300));
    // 先发那条说的是另一档(保存之前那一刻的事实)
    if (which === 1) return { ...r, current: 'feedgrab', ready: false, why: '保存之前那一刻' };
    return r;
  };
  return true;
})()`);
await evalJs(`loadXChannel('social'); true`);     // 先发(600 毫秒后回)
await sleep(80);
await evalJs(`loadXChannel('social'); true`);     // 后发(1300 毫秒后回),它才是最新的意图
// **摆盘自检**:发第二条时第一条确实还在路上 —— 不然这条用例测的是另一件事。
check("两条读确实重叠在途(摆盘自检)", (await picked()) === "browser", await picked());
await sleep(2000);
check("先回来的那条旧读没赢", (await picked()) === "browser", await picked());
check("后发那条读的事实落地了",
      (await evalJs(`xChan && xChan.current`)) === "browser",
      await evalJs(`xChan && xChan.current`));
check("状态行不是「保存之前那一刻」",
      !(await evalJs(`document.getElementById('source-x-state').textContent`))
        .includes("保存之前那一刻"),
      await evalJs(`document.getElementById('source-x-state').textContent`));
await evalJs(`window.pipeJson = window.__realPipeJson; true`);   // 收尾:还回去

// 4.6f 反方向那一半:**旧读先抵达,不许因此废掉随后回来的动作结果**
// 4.6d 摆的是「动作先回来、旧读随后到」(旧读不许盖)。这一条把抵达顺序倒过来:
// 旧读**先**抵达 —— 原来它自己落地时会顺手推进代次,于是随后回来的导入反被判成
// 过期,成功记录、成功 toast、失败原因一个都不显示,面板停在「还没有登录过 X」
// (第 5 轮审查在隔离浏览器里实测:后端 `ok=true`、页面什么都没有,
//  `import-login-read-first-ok.png`)。
// 出路:**面板归属和「谁说的更新」分开记** —— 动作一开始就让在途的读作废
// (`xChanAct`),而读落地不再碰面板归属,所以它废不掉动作。
await evalJs(`fetch('/__probe/stamps?reset=1').then(() => true)`);
await evalJs(`(document.getElementById('toast-host')||{}).innerHTML = ''; true`);  // 只看这一轮的
await evalJs(`fetch('/__probe/slow-get?ms=600&forget=1').then(() => true)`);
await evalJs(`loadXChannel('social'); true`);     // 这条读说的是导入之前那一刻
await sleep(150);
check("「导入登录态」在这一档上点得到(4.6f)", await clickAt("#source-x-import-btn"));
await sleep(900);                                 // 旧读已经回来了,导入还没
// **摆盘自检**:这一轮真的是「读先到、动作还在路上」。顺序反了的话这条用例测的
// 是 4.6d 那个场景,而不是它自己要钉的那一个。
const mid = await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`);
check("旧读先抵达、导入还没回来(摆盘自检)", mid === "get", mid);
await until(`document.getElementById('source-x-import-state')
               .textContent.includes('上次从日常浏览器导入')`, 15000);
check("旧读先抵达也没把随后回来的导入结果判成过期",
      (await evalJs(`document.getElementById('source-x-import-state').textContent`))
        .includes("13 个 cookie"),
      await evalJs(`document.getElementById('source-x-import-state').textContent`));
check("成功提示也还在(不是只有留痕那行)",
      (await evalJs(`document.querySelector('#toast-host')?.textContent || ''`))
        .includes("13 个 x.com cookie"),
      await evalJs(`document.querySelector('#toast-host')?.textContent || ''`));
check("状态行没停在旧读那句「还没有登录过 X」",
      !(await evalJs(`document.getElementById('source-x-state').textContent`))
        .includes("还没有登录过"),
      await evalJs(`document.getElementById('source-x-state').textContent`));
const order = await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`);
check("这一轮的抵达顺序确实是「读、再动作」", order === "get,import", order);

// 4.6g 同一族的第三种时序:**动作在途时顺手保存一次**
// 4.6d/4.6f 摆的都是「读在动作之前发出」。这一条摆的是「读在动作**开始之后**才发出,
// 可它取到的后端快照仍然在动作提交之前」:导入还没回来,用户不改任何设置又点了一次
// 「保存设置」—— 保存收尾会重填字段并重新读一遍通道(`source_forms.js`),那条读几十
// 毫秒就回来了,带着「还没有登录过 X」;按代次判的话它既推进了代次、又先落了地,
// 随后回来的导入结果反被判成过期,成功记录和成功提示一个都不出现(审查实测:保存
// +34ms、它那条 GET +49ms、导入成功 +1203ms,`ui-save-during-import.png`)。
// 出路:**动作的归属按「它说的是哪一档」判,不按代次判**(来源 + 下拉选的那一档 +
// 存下来的那一档都没变就还是它的),同时记一个「已完成版本」让更早发出的读不许回头盖。
await evalJs(`fetch('/__probe/stamps?reset=1').then(() => true)`);
await evalJs(`(document.getElementById('toast-host')||{}).innerHTML = ''; true`);
check("「导入登录态」在这一档上点得到(4.6g)", await clickAt("#source-x-import-btn"));
await sleep(60);
// `forget=1`:让保存触发的那条读说「导入之前那一刻」—— 真后端就是这样,那一刻
// 导入还没提交(mock 要等 1.2 秒才把 `imported` 置上)。
await evalJs(`fetch('/__probe/slow-get?ms=0&forget=1').then(() => true)`);
check("导入途中「保存设置」点得到", await clickAt("#source-settings-save"));
await sleep(500);                                 // 保存和它那条读都回来了,导入还没
const midSave = await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`);
check("保存那条读先抵达、导入还在路上(摆盘自检)", midSave === "get", midSave);
// **在途动作的交代不许被那次重画盖掉**:那个授权弹窗这会儿正等在另一个窗口里,
// 这一行是它唯一的说明。
check("保存收尾那次重画没把「正在导入」的预告清掉",
      (await evalJs(`document.getElementById('source-x-import-state').textContent`))
        .includes("授权"),
      await evalJs(`document.getElementById('source-x-import-state').textContent`));
await until(`document.getElementById('source-x-import-state')
               .textContent.includes('上次从日常浏览器导入')`, 15000);
check("途中保存没让导入结果被判成过期",
      (await evalJs(`document.getElementById('source-x-import-state').textContent`))
        .includes("13 个 cookie"),
      await evalJs(`document.getElementById('source-x-import-state').textContent`));
check("成功提示也还在",
      (await evalJs(`document.querySelector('#toast-host')?.textContent || ''`))
        .includes("13 个 x.com cookie"),
      await evalJs(`document.querySelector('#toast-host')?.textContent || ''`));
check("状态行没停在那条读带来的「还没有登录过 X」",
      !(await evalJs(`document.getElementById('source-x-state').textContent`))
        .includes("还没有登录过"),
      await evalJs(`document.getElementById('source-x-state').textContent`));
check("这一轮的抵达顺序确实是「读、再动作」(4.6g)",
      (await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`))
        === "get,import",
      await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`));

// 4.6h 4.6g 的另一半:**那条读慢一点回来,也不许回头把结果盖掉**
// 4.6g 里保存触发的读先回来;这一条让它压 2 秒,于是导入结果先落地、它后到。
// 它是在动作**开始之后**才发出的,所以「动作一开始就作废在途的读」那一道(`xChanActs`)
// 放它过 —— 可它取到的后端快照仍然在导入提交之前,盖上来页面就又说「还没有登录过 X」,
// 而成功记录刚刚才写上去。挡住它的是另一样:**后端已完成版本**(`xChanLands`)——
// 有动作结果落过地之后,比它更早发出的读一律不许再写。
await evalJs(`fetch('/__probe/stamps?reset=1').then(() => true)`);
check("「导入登录态」再点一次(4.6h)", await clickAt("#source-x-import-btn"));
await sleep(60);
await evalJs(`fetch('/__probe/slow-get?ms=2000&forget=1').then(() => true)`);
check("导入途中「保存设置」再点一次", await clickAt("#source-settings-save"));
await until(`document.getElementById('source-x-import-state')
               .textContent.includes('上次从日常浏览器导入')`, 15000);
const beforeLate = await evalJs(`document.getElementById('source-x-import-state').textContent`);
// **等那条压慢的读确实回来过,别用固定睡眠**:`until` 那一步本身耗时不定,睡死的
// 话这条用例自己会飘(摆这一条时实测:注入版里它先红在自检上,而不是红在判据上)。
let lateOrder = "";
for (let i = 0; i < 40 && lateOrder !== "import,get"; i++) {
  lateOrder = await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`);
  if (lateOrder !== "import,get") await sleep(100);
}
check("导入先落地、那条读后到(摆盘自检)", lateOrder === "import,get", lateOrder);
await sleep(150);                                 // 让它那一下渲染真的发生过
check("晚到的那条读没把刚写上的导入记录盖掉",
      (await evalJs(`document.getElementById('source-x-import-state').textContent`))
        === beforeLate,
      await evalJs(`document.getElementById('source-x-import-state').textContent`));
check("也没把状态行盖回「还没有登录过 X」(4.6h)",
      !(await evalJs(`document.getElementById('source-x-state').textContent`))
        .includes("还没有登录过"),
      await evalJs(`document.getElementById('source-x-state').textContent`));

// 4.6i 同一族的最后一种:**请求断在半路,这件事不许被一次重画抹掉**
// 前面几条管的都是「谁的回包算数」;这一条管的是「**没有**回包时页面说什么」。
// 后端那条链是「改 profile 之前先登记、写完才落账」,所以连接断掉证明不了它没发生 ——
// 而原来的失败路只把那一行 DOM 清空,缓存里的 `login_state`/`ready` 一个字没动:
// 用户把取法切走再切回来,重画就按那份旧缓存把「当场验证通过」恢复成当前结论,
// 这一次没跑完一个字都不剩(审查在真页面上实测,`ui-old-trace-after-interruption.png`)。
// 出路:记成**动作状态**(`xChanUnsure`),重画时顶在旧留痕前面。
await evalJs(`fetch('/__probe/stamps?reset=1').then(() => true)`);
await evalJs(`fetch('/__probe/kill-import').then(() => true)`);
check("「导入登录态」再点一次(4.6i)", await clickAt("#source-x-import-btn"));
await until(`document.getElementById('source-x-import-state')
               .textContent.includes('没跑完')`, 20000);
const unsureLine = await evalJs(`document.getElementById('source-x-import-state').textContent`);
check("断掉的那一次说的是「不知道」,不是一句空白",
      unsureLine.includes("没跑完") && unsureLine.includes("不知道"), unsureLine);
// **切走再切回来** —— 审查复现的就是这两下。
await pick('feedgrab');
await sleep(120);
check("切到别档时那条交代不跟着跑(反过来钉一边)",
      !(await evalJs(`document.getElementById('source-x-import-state').textContent`))
        .includes("没跑完"),
      await evalJs(`document.getElementById('source-x-import-state').textContent`));
await pick('browser');
await sleep(120);
const backLine = await evalJs(`document.getElementById('source-x-import-state').textContent`);
check("切回来之后这一次没跑完还在,没被旧留痕顶掉", backLine.includes("没跑完"), backLine);
check("旧留痕没被删,只是退到后面当历史",
      backLine.includes("上次从日常浏览器导入"), backLine);
// **摆盘自检**:这一轮真的只到了一条导入请求(浏览器会在「复用连接上一个字节都
// 没收到」时自动重发一次 —— 那一下走的是正常分支,页面压根进不了失败路)。
check("这一轮只有那一条被掐断的导入(摆盘自检)",
      (await evalJs(`fetch('/__probe/stamps').then(r => r.json())
                       .then(j => j.stamps.filter(s => s.startsWith('import')).join())`))
        === "import-killed",
      await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`));
// **反过来钉一边:它必须撤得掉。** 再导一次、这回真回来了 —— 那条「不知道」就
// 不该再挂着(一条撤不掉的提醒会把面板永远钉在一次早就过去的故障上)。
check("「导入登录态」再点一次(4.6i 收尾)", await clickAt("#source-x-import-btn"));
// **等它真的回来**(别等「正在导入…」那一瞬间:那句话里本来就没有「没跑完」,
// 按它判的话这一条会在请求还在飞的时候就放行,后面那节点「检查连接」会撞上
// `xChanBusy` 压根发不出请求)。
await until(`document.getElementById('source-x-import-state')
                .textContent.includes('上次从日常浏览器导入')`, 20000);
// **判据要落在重画上,不是落在那一次动作自己写的那行字上** —— 成功那一下是直接
// 往 DOM 上写的,所以就算那条记录没撤掉这里也看不出来(注入「不撤」实测:一条红
// 都没有)。切走再切回来走的才是重画那条路,记录没撤的话它当场又冒出来。
for (const one of ['feedgrab', 'browser']) { await pick(one); await sleep(120); }
check("拿到确定答复之后那条「不知道」撤掉了,重画也不再冒出来",
      !(await evalJs(`document.getElementById('source-x-import-state').textContent`))
        .includes("没跑完"),
      await evalJs(`document.getElementById('source-x-import-state').textContent`));

// 5. 「检查连接」真连一次,结论写回状态行
check("「检查连接」点得到", await clickAt("#source-x-check"));
await until(`document.getElementById('source-x-state').textContent.includes('推文')`, 20000);
// **方法要对**:后端那条路由只收 POST,而 `pipeJson(path, {}, 120000)` 发的是
// GET(选项里没有 method,fetch 默认 GET)—— 于是这颗按钮在真页面上永远是
// 405 Method Not Allowed,状态行把它当成结论贴出来(2026-10-08 审查实测)。
check("检查请求打的是 check 端点,而且是 POST",
      posted.includes("POST /api/intake/social/x-channel/check"), posted.join());
check("检查没有发出任何 GET",
      !posted.some(p => p.startsWith("GET /api/intake/social/x-channel/check")),
      posted.join());
check("检查结论写回状态行",
      (await evalJs(`document.getElementById('source-x-state').textContent`)).includes("登录态有效"),
      await evalJs(`document.getElementById('source-x-state').textContent`));
check("状态行里没有 405 这类方法错误",
      !(await evalJs(`document.getElementById('source-x-state').textContent`)).includes("Method"),
      await evalJs(`document.getElementById('source-x-state').textContent`));

// 6. 失败的检查如实说,不留一句"成功"
mock.checkReply = { ready: false, detail: "X 登录态已失效:点「打开登录窗口」重新登录一次,或者再点一次「从日常浏览器导入登录态」" };
await clickAt("#source-x-check");
await until(`document.getElementById('source-x-state').textContent.includes('已失效')`, 20000);
check("失败的检查如实写出原因",
      (await evalJs(`document.getElementById('source-x-state').textContent`)).includes("已失效"),
      await evalJs(`document.getElementById('source-x-state').textContent`));

// 9. 待配置横幅说的必须是**当前通道**缺什么,不是写死某一档的前提
mock.unconfigured = true;
await evalJs(`loadSourceModule(true)`);
await until(`!document.getElementById('source-configuration').hidden`, 15000);
const banner = await evalJs(`document.getElementById('source-configuration').textContent`);
check("待配置横幅按当前通道说话", banner.includes("登录"), banner);
mock.unconfigured = false;
await evalJs(`loadSourceModule(true)`);
await until(`document.getElementById('source-configuration').hidden`, 15000);

// 10. 「我日常用的浏览器」那一档 —— 它一个输入框都不要,所以这一节同时是
// 「新增一条通道不用改这个 JS」那条口径的回归:前端只按后端给的 id 决定显示
// 哪一块,没有自己那一块的通道就一块都不显示,下一步并进状态行那一句。
// 这里**不要**再点一次来源切换器:上一节结束时就在这个视图里,再点一次会重新
// 拉一遍通道清单,而 `loadXChannel` 收尾那句 `xChanSelect(data.current)` 会把下面
// 这次选择盖回去 —— 清单上看着是 attach 之前那一档,十条断言一起红。
await pick('attach');
check("清单真的切到这一档了(摆盘自检)", (await picked()) === "attach", await picked());
check("选它之后一个输入块都不显示",
      !await shown("#source-x-feedgrab") && !await shown("#source-x-browser")
      && !await shown("#source-x-cmd"));
check("「可执行文件位置」那个折叠块也跟着收走(空折叠块比没有更糟)",
      !await shown("#source-x-adv"));
// **逐字比后端给的那条**:前端不许自己攒一句说明,这一档的全部文案都来自
// `/x-channel` 的 `channels[].hint`。
check("它自己的说明逐字来自后端",
      (await text('#source-x-picker .xchan-opt:nth-child(2) span'))
      === CHANNELS.find(c => c.id === "attach").hint,
      await text('#source-x-picker .xchan-opt:nth-child(2) span'));
check("卡片上的名字跟着换成这一档",
      (await text('#source-x-name')) === CHANNELS.find(c => c.id === "attach").label,
      await text('#source-x-name'));
check("改了还没存下来时「下一步」不混进来(那是当前通道的事实)",
      !(await state()).includes("chrome://inspect"), await state());
check("「检查连接」照旧在", await shown("#source-x-check"));
// 保存:这一档没有要填的东西,保存之后它就是当前通道
await clickAt("#source-settings-save");
await until(`xChan && xChan.current === 'attach'`, 20000);
check("保存真把这一档发出去了",
      saved[saved.length - 1]?.x_channel === "attach",
      JSON.stringify(saved[saved.length - 1] || {}));
check("「下一步怎么办」并进了状态行(后端 readiness 的第三项)",
      (await state()).includes("chrome://inspect"), await state());
check("状态行说的是这一档缺什么", (await state()).includes("远程调试"), await state());
check("色点在「没配好」时是提醒色,不是绿的",
      (await evalJs(`document.getElementById('source-x-dot').className`)) === "xchan-dot warn",
      await evalJs(`document.getElementById('source-x-dot').className`));
check("「打开登录窗口」在这一档上压根不出现(它属于独立 profile 那一块)",
      !await shown("#source-x-login-btn"));
check("「从日常浏览器导入」同理不出现", !await shown("#source-x-import-btn"));
// 「检查连接」返回的 detail 已经把原因和地址写法指引拼在一起了(真后端
// `x_browser.probe` 就是 `f'{problem}。{ENDPOINT_HINT}'`),而「下一步」那行装的
// 正是同一条指引 —— 原来检查那一下只改状态行、不碰「下一步」,于是同一句话在同
// 一屏上连着显示两遍(2026-10-09 第 2 轮审查截图 `02-check-endpoint-error.png`)。
// 去重按**内容**判,所以这一项数的是它在整块面板上出现几次。
const hintTimes = async () => Number(await evalJs(
  `(() => { const t = document.getElementById('source-xchan').textContent;
            const n = ${JSON.stringify(NEXT_STEP.attach)};
            let i = 0, at = 0;
            while ((at = t.indexOf(n, at)) >= 0) { i++; at += n.length; }
            return i; })()`));
mock.checkReply = { ready: false,
               detail: `KB_CDP_ENDPOINT 填的地址认不出来：ws://127.0.0.1:65536。${NEXT_STEP.attach}` };
await clickAt("#source-x-check");
await until(`document.getElementById('source-x-state').textContent.includes('认不出来')`, 20000);
check("整块面板上那条指引只出现一次", (await hintTimes()) === 1, String(await hintTimes()));
// 反过来钉一边:结论里没有那条指引时「下一步」照旧要显示 —— 否则一个
// 「检查完就把下一步清空」的实现也能过,而那是把真有用的那句话丢掉。
mock.checkReply = { ready: false, detail: "连不上那个浏览器" };
await clickAt("#source-x-check");
await until(`document.getElementById('source-x-state').textContent.includes('连不上')`, 20000);
check("结论里没有指引时,「下一步」照旧跟在后面",
      (await state()).includes("chrome://inspect"), await state());
// 这一档的拒绝文案不许建议他换档 —— 他要的登录态就在自己那个浏览器里。
// 按钮看不见,所以走 JS 入口兜底那条路(真实来法:键盘/脚本)。
await evalJs(`openXLoginWindow(); true`);
await until(`document.querySelector('#toast-host')?.textContent.includes('不在这里登录')`, 8000);
const attachRefusal = await evalJs(`document.querySelector('#toast-host')?.textContent || ''`);
check("拒绝文案说清在哪儿登录,而且不叫他改档",
      attachRefusal.includes("现成的登录态") && !attachRefusal.includes("保存设置"),
      attachRefusal);
// 待配置横幅同理按这一档说话
mock.unconfigured = true;
await evalJs(`loadSourceModule(true)`);
await until(`!document.getElementById('source-configuration').hidden`, 15000);
const attachBanner = await evalJs(`document.getElementById('source-configuration').textContent`);
check("待配置横幅按这一档说话,不提别档的前提",
      attachBanner.includes("远程调试") && !attachBanner.includes("登录窗口"), attachBanner);
mock.unconfigured = false;
await evalJs(`loadSourceModule(true)`);
await until(`document.getElementById('source-configuration').hidden`, 15000);

// 7. RSS 视图上整块不出现 —— 通道是 X 的事
await clickAt("#subs-switch-rss");
// 等 `sourceData` 到位,不是只等 sourceKind:后者在发请求**之前**就同步赋值了,
// 拿它当信号会在 fillSourceSettings 还没跑的时候就去量这一块的可见性。
await until(`sourceKind === 'rss' && sourceData && sourceData.settings
             && !('x_channel' in sourceData.settings)`, 20000);
check("RSS 的设置里没有通道那一块", !await shown("#source-xchan"));
check("RSS 也没有那张通道清单", !await shown("#source-x-picker"));

// 8. 原生弹窗一个都不许有(项目硬性约定:它们跟这套配色不搭,还阻塞主线程)
check("整轮没有调过原生 alert/confirm/prompt",
      (await evalJs(`JSON.stringify(window.__nativeDialogs || [])`)) === "[]",
      await evalJs(`JSON.stringify(window.__nativeDialogs || [])`));

check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 400));
const out = [...oks.map(n => `  ok ${n}`), ...fails.map(n => `  FAIL ${n}`)].join("\n");
process.stdout.write(`${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`);
ws.close(); proc.kill(); server.close();
process.exitCode = fails.length ? 1 : 0;
