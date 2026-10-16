// X 采集通道在**真页面**上的回归:三条通道各要填的东西互斥显示、改了选择不许
// 冒充当前事实、保存时真把三个字段发出去、登录窗口和「检查连接」各打各的端点。
//
// mock 后端在 `x_channel_mock.mjs`(只管「后端会怎么回答」),浏览器外壳和判据工具
// 在 `x_channel_kit.mjs`,归属那一族的时序在 `x_channel_race.mjs` —— 四份都受
// 单文件 700 行上限管。CDP 直连 playwright 缓存里的 chrome-headless-shell
// (不装 playwright、不连网、始终无头、临时 profile)。**入口只有这一份。**
//
//   HEADLESS_SHELL=<bin> node tests/x_channel_probe.mjs
import { evalJs, send, sleep, until, clickAt, check, shown, pick, picked, text, fill,
         state, finish } from "./x_channel_kit.mjs";
import { ownershipSections } from "./x_channel_race.mjs";
import { CHANNELS, NEXT_STEP, posted, saved, used, mock, base } from "./x_channel_mock.mjs";

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
check("选之前清单是开着的(摆盘自检)", await shown("#source-x-picker"));
await pick("browser");
// **选中即收起。** 清单是"换一种"时才要的东西,选完该回到状态卡 —— 否则那三句
// 长说明一直占着屏幕,把刚换这一档的状态和按钮挤到下面去。
check("选完一档之后清单自动收起", !await shown("#source-x-picker"));
check("「换一种」那颗按钮也跟着说自己收起来了",
      (await evalJs(`document.getElementById('source-x-switch').getAttribute('aria-expanded')`)) === "false",
      await evalJs(`document.getElementById('source-x-switch').getAttribute('aria-expanded')`));
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
await fill('#source-x-chrome', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
const beforeForm = saved.length;
await evalJs(`document.getElementById('source-settings-form')
                .dispatchEvent(new Event('submit', {cancelable:true})); true`);
// `saved` 在 node 这一侧,页面里的 `until` 看不见它 —— 这一等要在这边轮询。
for (let i = 0; i < 100 && saved.length === beforeForm; i++) await sleep(100);
const sent = saved[saved.length - 1] || {};
check("保存时带上了通道选择", sent.x_channel === "browser", JSON.stringify(sent));
check("保存时带上了 Chrome 路径",
      (sent.x_chrome_path || "").endsWith("Google Chrome"), JSON.stringify(sent.x_chrome_path));

// 4.2 **「先保存再执行」要按整份配置判,不是按通道名判。**
// 原来 `xChannelSaved()` 只比「选中的通道 === 存下来的通道」,于是改了可执行文件
// 位置、不点保存就按按钮:那个比较当场为真、保存那一步直接放行,三颗按钮都拿**旧
// 路径**跑了一遍,而输入框里明明是新值、服务端连一次 PUT 都没收到(审查实测
// `path-ignored.jpg`,三个入口对称复现)。
await fill('#source-x-chrome', '/review/new-chrome');
check("改了可执行文件位置,状态行当场说这一份还没存下来",
      (await state()).includes("还没存下来"), await state());
check("色点也跟着回到中性(没有结论可给)",
      (await evalJs(`document.getElementById('source-x-dot').className`)) === "xchan-dot",
      await evalJs(`document.getElementById('source-x-dot').className`));
await evalJs(`fetch('/__probe/used?reset=1').then(() => true)`);
const beforePath = saved.length;
check("「检查连接」点得到(4.2)", await clickAt("#source-x-check"));
await until(`document.getElementById('source-x-state').textContent.includes('推文')`, 20000);
check("点下去先把新路径存了", saved.length === beforePath + 1
      && saved[saved.length - 1].x_chrome_path === "/review/new-chrome",
      JSON.stringify(saved.slice(beforePath)));
const usedPath = await evalJs(`fetch('/__probe/used').then(r => r.json()).then(j => JSON.stringify(j.used))`);
check("这一次检查真的是按新路径跑的",
      JSON.parse(usedPath).every(u => u.chrome === "/review/new-chrome")
      && JSON.parse(usedPath).length === 1, usedPath);
// **三个入口对称钉一次**:它们共用同一个 helper,但「共用一段 JS」不等于
// 「共用一次验证」—— 上一轮正是三个都坏了。
await fill('#source-x-chrome', '/review/newer-chrome');
await evalJs(`fetch('/__probe/used?reset=1').then(() => true)`);
check("「打开登录窗口」点得到(4.2)", await clickAt("#source-x-login-btn"));
await until(`document.querySelector('#toast-host')?.textContent.includes('已打开')`, 15000);
await fill('#source-x-bin', '/review/newer-feedgrab');
check("「从日常浏览器导入」点得到(4.2)", await clickAt("#source-x-import-btn"));
await until(`document.getElementById('source-x-import-state')
               .textContent.includes('上次从日常浏览器导入')`, 25000);
const usedBoth = await evalJs(`fetch('/__probe/used').then(r => r.json()).then(j => JSON.stringify(j.used))`);
check("登录和导入这两条也按眼前那一份跑",
      JSON.parse(usedBoth).length === 2
      && JSON.parse(usedBoth).every(u => u.chrome === "/review/newer-chrome")
      && JSON.parse(usedBoth)[1].bin === "/review/newer-feedgrab", usedBoth);
check("两条路径字段都存下去了",
      saved[saved.length - 1].x_chrome_path === "/review/newer-chrome"
      && saved[saved.length - 1].x_feedgrab_bin === "/review/newer-feedgrab",
      JSON.stringify(saved[saved.length - 1]));
check("存完之后状态行不再说「还没存下来」",
      !(await state()).includes("还没存下来"), await state());

// 4.3 **保存之后那次通道重读要等它落地。** 不等的话紧接着的 `xChanAct()` 会把它
// 当成「动作之前那一刻」的读作废掉,于是新那一档的 `hint`/`login_command`/`ready`
// 永远停在旧档的值上 —— 实测形态:从 browser 切到 feedgrab、检查都跑完了,卡片上
// 那条 `feedgrab login twitter` 始终不出现(审查 `slow-read-after.jpg`)。
await evalJs(`fetch('/__probe/slow-get?ms=1500').then(() => true)`);
await pick('feedgrab');
check("「检查连接」点得到(4.3)", await clickAt("#source-x-check"));
await until(`xChan && xChan.current === 'feedgrab'`, 20000);
await until(`document.getElementById('source-x-state').textContent.includes('推文')`, 20000);
check("慢一点的那次重读照旧落地:登录命令出来了",
      (await text('#source-x-login')).includes("feedgrab login")
      && await shown("#source-x-cmd"), await text('#source-x-login'));
check("那条命令在整块面板上仍然只出现一次(去重还在)",
      Number(await evalJs(`(() => { const t = document.getElementById('source-xchan').textContent;
        const n = ${JSON.stringify("feedgrab login twitter")};
        let i = 0, at = 0; while ((at = t.indexOf(n, at)) >= 0) { i++; at += n.length; }
        return i; })()`)) === 1, await state());

// 4.4 **保存在途时用户又改了一次:旧回包不许重写新草稿,这一次动作也不许接着跑。**
// 审查实测(`race-during.jpg` / `race-after.jpg`):保存还没回来时把单选改回独立
// profile,回包一落地选择又变回 feedgrab,随后那次检查也按 feedgrab 跑了下去。
// 这正是前几轮给 GET / 动作 POST 修过的归属问题,在这一轮新加的「PUT → 填表 →
// 接着做」这条链上复发。
await evalJs(`(document.getElementById('toast-host')||{}).innerHTML = ''; true`);
await evalJs(`fetch('/__probe/used?reset=1').then(() => true)`);
await evalJs(`fetch('/__probe/slow-put?ms=2500').then(() => true)`);
await pick('browser');
check("「检查连接」点得到(4.4)", await clickAt("#source-x-check"));
await sleep(300);                                 // 保存在路上
await pick('feedgrab');                           // 用户又改了一次
check("改完之后页面上选的就是他刚选的那一档(摆盘自检)",
      (await picked()) === "feedgrab", await picked());
await sleep(3500);                                // 等那条慢回包确实落地过
check("迟到的保存回包没把他刚选的那一档换回去", (await picked()) === "feedgrab",
      await picked());
check("状态行如实说这一份还没存下来", (await state()).includes("还没存下来"), await state());
check("这一次检查没有接着跑下去",
      (await evalJs(`fetch('/__probe/used').then(r => r.json()).then(j => j.used.length)`)) === 0,
      await evalJs(`fetch('/__probe/used').then(r => r.json()).then(j => JSON.stringify(j.used))`));
check("而且说清了为什么停下来,不是无声退场",
      (await text('#toast-host')).includes("又改过"), await text('#toast-host'));
// 收尾:回到 browser 这一档并真存下去,后面几节按它继续。
await pick('browser');
await clickAt("#source-settings-save");
for (let i = 0; i < 100 && !(await evalJs(`xChan && xChan.current === 'browser'`)); i++) await sleep(100);
check("收尾:又回到独立 profile 这一档(摆盘自检)",
      await evalJs(`xChannelSaved() && xChan.current === 'browser'`),
      await evalJs(`JSON.stringify([xChannelValue(), xChan && xChan.current])`));

// 4.5 **慢检查在途时,同一档取法下换了一条路径并保存:旧结论不许贴到新那一份旁边。**
// 审查实测(`check-race-after.jpg`):检查按旧路径跑着,这期间把位置改成一个不存在
// 的文件并保存,页面已经读到「路径不可执行」;随后旧检查成功回来,状态行被写成
// 「feedgrab 已就绪」+ 绿点,而输入框和那条可复制命令里都是那条不存在的路径。
// 根子是归属只比「还是同一档取法吗」—— 同一档里换路径,它当场为真。
await evalJs(`(document.getElementById('toast-host')||{}).innerHTML = ''; true`);
await evalJs(`fetch('/__probe/used?reset=1').then(() => true)`);
await pick('feedgrab');
await fill('#source-x-bin', '/review/first-bin');
await clickAt("#source-settings-save");
await until(`xChannelSaved() && xChan.current === 'feedgrab'`, 20000);
await evalJs(`fetch('/__probe/slow-check?ms=2500').then(() => true)`);
check("「检查连接」点得到(4.5)", await clickAt("#source-x-check"));
await sleep(400);                                  // 检查在路上(按旧路径跑着)
await fill('#source-x-bin', '/review/missing-bin');   // 他改了位置
await clickAt("#source-settings-save");               // 并且真存下去了
await until(`xChannelSaved() && xChanDraft().includes('missing-bin')`, 20000);
check("那一次检查确实是按旧路径跑的(摆盘自检)",
      (await evalJs(`fetch('/__probe/used').then(r => r.json())
                     .then(j => j.used.some(u => u.what === 'check' && u.bin === '/review/first-bin'))`)) === true,
      await evalJs(`fetch('/__probe/used').then(r => r.json()).then(j => JSON.stringify(j.used))`));
await sleep(3000);                                 // 等旧检查的回包确实落地过一次
check("旧检查的结论没有贴到新那一份旁边",
      !(await state()).includes("登录态有效"), await state());
check("色点也没有被写成绿的", !(await evalJs(
      `document.getElementById('source-x-dot').className`)).includes("ok"),
      await evalJs(`document.getElementById('source-x-dot').className`));
// **反过来钉一边:回执照旧给。** 它说的是「用户自己刚点的那颗按钮有结果了」,
// 守上它的后果是用户在这期间顺手保存一次,就再也不知道那次检查到底回了什么。
check("那次检查的回执照旧给了用户", (await text('#toast-host')).includes("登录态有效"),
      await text('#toast-host'));

// 4.5b **另一个标签页改过设置:这一页按下去的动作不许拿别人那一份跑。**
// 审查实测(`multi-page-after.txt`):第二页把位置改成 `/review/second-page-missing`
// 并保存,第一页不刷新直接点「检查连接」—— 这一页的「已经保存了」只是**它自己的
// 缓存**,证不了库里存着的还是它,于是检的是第二页那条路径,结论却贴在第一页那条
// 可用的路径旁边。判据因此下沉到服务端(`intake_api._x_act`):请求带上「我眼前
// 这一份」,对不上就一个字都不跑。
// 这里**从 node 这一侧直接 PUT**,页面无从知道 —— 那正是"另一个客户端"的形态。
await evalJs(`(document.getElementById('toast-host')||{}).innerHTML = ''; true`);
await evalJs(`fetch('/__probe/used?reset=1').then(() => true)`);
await fetch(`${base}/api/intake/social/settings`, {
  method: "PUT", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ enabled: false, interval_minutes: 1440, x_channel: "feedgrab",
                         x_feedgrab_bin: "/review/second-page-bin", x_chrome_path: "" }) });
check("这一页还以为自己那一份存着(摆盘自检)", await evalJs(`xChannelSaved()`));
check("「检查连接」点得到(4.5b)", await clickAt("#source-x-check"));
await until(`document.querySelector('#toast-host')?.textContent.includes('不是你眼前这一份')`, 20000);
check("后端拒绝了,这一次一个动作都没跑",
      (await evalJs(`fetch('/__probe/used').then(r => r.json()).then(j => j.used.length)`)) === 0,
      await evalJs(`fetch('/__probe/used').then(r => r.json()).then(j => JSON.stringify(j.used))`));
check("说清了是存着的那一份变了,不是一句看不懂的失败",
      (await text('#toast-host')).includes("另一个标签页"), await text('#toast-host'));
// **不许记成「这一次检查没跑完,当前状态不知道」** —— 那一档是留给"真跑过、只是
// 没拿到答复"的;这一次后端明确说了它什么都没做。
check("没有把一次明确的拒绝说成一次悬着的检查",
      !(await state()).includes("不知道"), await state());
await until(`!xChannelSaved()`, 20000);
check("重读之后如实说:眼前这一份还没存下来(下一步是再点一次)",
      (await state()).includes("还没存下来"), await state());

// 收尾:把面板摆回「独立 profile、两条路径都空」,后面几节按它继续。
await fill('#source-x-bin', '');
await pick('browser');
await clickAt("#source-settings-save");
await until(`xChannelSaved() && xChan.current === 'browser'`, 20000);
// (Chrome 那条路径是 4.2 留下的,后面几节按它继续,这里只把本节加的那条清掉。)
check("收尾:回到独立 profile、feedgrab 那条路径已清掉(摆盘自检)",
      (await evalJs(`JSON.parse(xChanDraft()).slice(0, 2).join('|')`)) === 'browser|',
      await evalJs(`xChanDraft()`));

await ownershipSections();

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

// 11. **读不出可选的取法时,那条终端命令要跟着一起收走。**
// 这一轮把命令从 feedgrab 那个输入块挪到了卡片上(`source-x-cmd`),而读失败分支
// 只藏了两个输入块 —— 于是显示过 feedgrab 命令之后,一次 503 得到的是「读不出
// 可选的取法」,下面却还挂着上一档那条可以照抄的命令(审查 N1)。
await pick('feedgrab');
await clickAt("#source-settings-save");
await until(`xChan && xChan.current === 'feedgrab'`, 20000);
check("先让那条命令真的显示出来(摆盘自检)",
      await shown("#source-x-cmd") && (await text('#source-x-login')).includes("feedgrab login"),
      await text('#source-x-login'));
await evalJs(`fetch('/__probe/fail-get').then(() => true)`);
await evalJs(`loadXChannel('social'); true`);
await until(`document.getElementById('source-x-state').textContent.includes('读不出')`, 20000);
check("读失败之后那条可照抄的命令收走了",
      !await shown("#source-x-cmd") && (await text('#source-x-login')) === "",
      await text('#source-x-login'));
check("留痕那一行也清掉了(连「现在是哪一档」都答不上来)",
      (await text('#source-x-import-state')) === "", await text('#source-x-import-state'));
check("清单也收干净了,不留一份读不出来的旧选项", !await shown("#source-x-picker"));

// 11b **读失败之后第一次点按钮:先把那次读补上,别拿一份空草稿去"保存"。**
// 审查 N1 实测:读失败那一档 `xChan` 是 null,`xChannelPayload` 于是交出一份空草稿,
// 于是「先保存再执行」发出一条 **`PUT {}`**、再被恢复出来的真实设置判成「保存完成时
// 这份设置已经又改过了」—— 而用户一个字都没改,检查也压根没发出去。
await evalJs(`(document.getElementById('toast-host')||{}).innerHTML = ''; true`);
await evalJs(`fetch('/__probe/used?reset=1').then(() => true)`);
const beforeRecover = saved.length;
check("「检查连接」点得到(11b)", await clickAt("#source-x-check"));
await until(`xChan && xChan.current === 'feedgrab'`, 20000);
check("那次读被补上了,面板回到了真实的那一档",
      (await text('#source-x-name')).includes("feedgrab"), await text('#source-x-name'));
check("没有拿一份空草稿去保存(一条 PUT 都不该有)", saved.length === beforeRecover,
      JSON.stringify(saved.slice(beforeRecover)));
check("更没有把这件事说成「用户又改过设置」",
      !(await text('#toast-host')).includes("又改过"), await text('#toast-host'));
// **反过来钉一边:这一次要真的接着做下去** —— 读补上之后草稿本来就等于存着的
// 那一份,把它堵在这儿等于让用户再点一次,而他压根不知道刚才那下为什么没反应。
for (let i = 0; i < 100; i++) {
  if ((await evalJs(`fetch('/__probe/used').then(r => r.json()).then(j => j.used.length)`)) > 0) break;
  await sleep(100);
}
check("这一次检查照常跑了,而且跑的是恢复出来的那一份",
      (await evalJs(`fetch('/__probe/used').then(r => r.json())
                     .then(j => j.used.some(u => u.what === 'check' && u.channel === 'feedgrab'))`)) === true,
      await evalJs(`fetch('/__probe/used').then(r => r.json()).then(j => JSON.stringify(j.used))`));

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

finish();
