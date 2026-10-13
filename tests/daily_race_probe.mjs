// 日报面板的**时序**回归:先发后到的旧响应(正文和列表各一档)不许盖掉新生成的
// 那一份,删掉的那一份也不许被在途读取画回来。
//
// 从 tests/daily_probe.mjs 拆出来**只因为行数**(那边到了 700 行上限)。两边同一套
// 连法、同一个沙箱;那一份验的是界面本身(入口、渲染、区间、定时、按钮),这一份
// 全是「两次请求的到达顺序」—— 所以这里每一组都自己摆盘(自己生成、自己删),
// 不依赖那一份跑到哪一步。
//
// 需要一个跑着沙箱数据的 kb-web(摆法同 daily_probe.mjs):
//   python3 tests/llm_stub.py --port 8801 &
//   python3 tests/daily_sandbox.py /tmp/kb-daily-sandbox 8801
//   KB_DATA_DIR=... KB_VAULT_ROOT=... uv run uvicorn kb.server:app --port 8799
//
//   HEADLESS_SHELL=<bin> node tests/daily_race_probe.mjs http://127.0.0.1:8799
import { launchShell } from "./probe_shell.mjs";

const base = process.argv[2];

const { proc, port } = await launchShell(["--window-size=1440,1000"]);

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));

let id = 0;
const pend = new Map();
const pageErrors = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    pageErrors.push(m.params.args.map(a => a.value ?? a.description ?? "").join(" "));
});
const send = (method, params = {}) => {
  const mid = ++id;
  return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); });
};
const evalJs = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
// 界面上的状态要等接口回来才落地,拍固定时长会在机器慢的时候假红。
const waitFor = async (expression, ms = 8000) => {
  for (let i = 0; i * 200 < ms; i++) {
    if (await evalJs(expression)) return true;
    await sleep(200);
  }
  return false;
};
// 等运行状态时得自己驱动刷新:面板平时靠 setTimeout 轮询,而无头浏览器把
// 后台页面的定时器节流到分钟级 —— 干等的话客户端会拿着一份二十秒前的
// "还在跑"不放,下一次 runDaily 当场被它自己那道「已在运行」闸门挡掉。
const waitStatus = async (expression, ms = 20000) => {
  for (let i = 0; i * 400 < ms; i++) {
    await evalJs(`refreshDailyStatus()`);
    if (await evalJs(expression)) return true;
    await sleep(400);
  }
  return false;
};

await send("Runtime.enable");
await send("Page.enable");

const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));

await send("Page.navigate", { url: base + "/#daily" });
// initWorkspace 要先把来源模块拉回来才 restoreWorkspaceLocation,首帧之后还有
// 一串接口要落地;等 dailyLoaded 而不是拍一个固定时长。
for (let i = 0; i < 60 && !(await evalJs(`typeof dailyLoaded !== 'undefined' && dailyLoaded`)); i++) await sleep(250);

const today = new Date().toLocaleDateString("sv");
await send("Page.navigate", { url: base + "/#daily" });
for (let i = 0; i < 60 && !(await evalJs(`typeof dailyLoaded !== 'undefined' && dailyLoaded`)); i++) await sleep(250);
await evalJs(`loadDailyPanel()`);
await waitFor(`document.getElementById('page-crumb').textContent === '每日日报'
  && getComputedStyle(document.getElementById('dailypanel')).display !== 'none'`, 20000);
check("摆盘:日报面板打开了",
      await evalJs(`getComputedStyle(document.getElementById('dailypanel')).display !== 'none'`));
// 轮询那颗定时器一律先清掉:下面每一组都要自己控制"页面什么时候去问状态",
// 被它插一脚就测到另一个时序了。每组用完 refreshDailyStatus 都会重排一个,
// 所以各组开头再清一次。
await evalJs(`(() => { clearTimeout(dailyTimer); dailyTimer = null; })()`);

// ── 先发后到的旧正文不许盖掉新正文 ────────────────────────────────────────────
// 用户点开今天这一份(这次读取慢),紧接着点「重新生成」—— 生成完那一跳会自动重读
// **同一天**,于是两次读取的 date 逐字相同,只校验日期的那道守卫一个都挡不住:旧那份
// 先发后到,把已经显示出来的新正文盖回去,新文章闪一下就没了,而完成身份已经记下,
// 后面多少轮轮询都不会再补刷(审查实测:左边列表写着 5 条,正文只剩 4 条,只有手动
// 再点一次才回来)。
//
// 构造只控制**真实响应的到达顺序**,不碰响应体:`holdReport()` 扣住下一次读今天这份
// 正文的真实回包,放行时原样交还。两份内容的差别靠**真生成两次、各用一个区间**造
// 出来(窄区间走补录那一条,宽的把它算进要点),所以元信息那行字能分得出谁在屏幕上。
const HOLD = (match) => `(() => {
  const real = window.fetch;
  let release;
  const held = new Promise(r => { release = r; });
  let armed = true, asFailure = false;
  window.__release = release;
  // 放行成「这次读取失败了」:抛的是浏览器 fetch 真正的网络失败形态。伪造的只有
  // 「它失败了」这件事,不是响应内容 —— 失败那一侧要和成功侧走同一道判据,否则
  // 被替代的那次读取能拿一句「读取失败」盖掉一份已经读到的日报。
  window.__releaseAsFailure = () => { asFailure = true; release(); };
  window.__restoreFetch = () => {
    window.fetch = real;
    delete window.__release;
    delete window.__releaseAsFailure;
    delete window.__restoreFetch;
  };
  window.fetch = async (path, opts) => {
    const r = await real(path, opts);
    const get = !opts || !opts.method || opts.method === 'GET';
    if (armed && get && (${match})) {
      armed = false;
      await held;                    // 真回包原样扣在这儿,放行时才交给调用方
      if (asFailure) throw new TypeError('Failed to fetch');
    }
    return r;
  };
  return true;
})()`;
// 扣住下一次「读今天这一份正文」的真回包
const holdReport = () => evalJs(HOLD(
  `String(path).startsWith('/api/daily/reports/${today}')`));
// 扣住下一次「读日报列表」的真回包(路径精确相等 —— 带日期的那条是读正文)
const holdList = () => evalJs(HOLD(`String(path) === '/api/daily/reports'`));
// 后端按**本地**朴素时间解析,而 toISOString() 给的是 UTC —— 差 8 小时,早上还会把
// until 推到昨天,那就不是「重新生成今天这一份」了。
const nowLocal = () => new Date().toLocaleString('sv').replace(' ', 'T').slice(0, 16);
const genRange = (since) => evalJs(`(async () => {
  await fetch('/api/daily/run', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sync: false, since: ${JSON.stringify(since)},
                           until: ${JSON.stringify(nowLocal())} }) });
  for (let i = 0; i < 60; i++) {
    const s = await (await fetch('/api/daily/status')).json();
    if (!s.running && s.phase !== 'starting') return s.phase;
    await new Promise(r => setTimeout(r, 300));
  }
  return ''; })()`);
const metaText = () => evalJs(`document.getElementById('daily-reader-meta').textContent`);

// 窄区间那一份:今天 00:00 起 ⇒ 那条发布得更早的素材走补录,元信息写「补录 N 条」
check("摆盘:窄区间先生成一份", (await genRange(`${today}T00:00`)) === "done");
// `false` = 不保留选中项:面板打开那会儿今天这一份还不存在,选中的是沙箱预置的
// 昨天那一份,`keepSelection` 会把它一直留在阅读区,下面比的就不是今天这一份了。
await evalJs(`loadDailyReports(false)`);
const narrow = await metaText();
check("摆盘:阅读区是窄区间那一份", /补录 \d+ 条/.test(narrow), narrow);
await evalJs(`(() => { clearTimeout(dailyTimer); dailyTimer = null; })()`);

await holdReport();
await evalJs(`(() => { openDailyReport('${today}'); })()`);   // 这次读取被扣住,不能 await
check("摆盘:慢那次读取确实挂住了,阅读区还没变", (await metaText()) === narrow);
check("摆盘:宽区间重新生成了同一天", (await genRange("2026-09-01T00:00")) === "done");
await evalJs(`refreshDailyStatus()`);
check("新正文先到,阅读区换成了宽区间那一份",
      await waitFor(`!/补录 \\d+ 条/.test(document.getElementById('daily-reader-meta').textContent)`, 8000),
      await metaText());
const fresh2 = await metaText();
await evalJs(`window.__release(); true`);
await sleep(600);
check("旧正文晚到也盖不回去", (await metaText()) === fresh2,
      `${fresh2}  ⇢  ${await metaText()}`);
for (let i = 0; i < 2; i++) { await evalJs(`refreshDailyStatus()`); await sleep(300); }
check("后面两跳轮询也没被它带回旧版", (await metaText()) === fresh2, await metaText());
await evalJs(`window.__restoreFetch(); true`);

// 失败那一侧走同一道判据:被替代的那次读取**报错**时也不许落到 UI。
await holdReport();
await evalJs(`(() => { openDailyReport('${today}'); })()`);
check("摆盘:窄区间又重新生成了一次", (await genRange(`${today}T00:00`)) === "done");
await evalJs(`refreshDailyStatus()`);
check("新正文先到",
      await waitFor(`/补录 \\d+ 条/.test(document.getElementById('daily-reader-meta').textContent)`, 8000),
      await metaText());
const fresh3 = await metaText();
await evalJs(`window.__releaseAsFailure(); true`);
await sleep(600);
check("被替代的那次读取报错也盖不掉已经读到的正文",
      (await metaText()) === fresh3
      && !/读取失败/.test(await evalJs(`document.getElementById('daily-reader-body').textContent`)),
      `${await metaText()} :: ${(await evalJs(`document.getElementById('daily-reader-body').textContent`)).slice(0, 40)}`);
await evalJs(`window.__restoreFetch(); true`);

// ── 先发后到的旧**列表**也不许盖掉新生成的那一份 ──────────────────────────────
// 正文那边的序号只约束 openDailyReport 自己发的请求,挡不住上游:一次较早的列表
// 响应后到,它会整份换掉 dailyReports、据此重选日报,再**新发起**一次旧日期的正文
// 读取 —— 那一次恰好持有最新序号,于是落到屏幕上。审查实测:新生成的今天那一份
// 先显示出来,旧列表一到就从列表里消失、阅读区跳回昨天、按钮退回「生成日报」,
// 而后台列表一直有今天;连覆盖确认都失效(文件还在,前端却认为今天还没生成)。
await evalJs(`(async () => {
  await fetch('/api/daily/reports/${today}', { method: 'DELETE' });
  await loadDailyReports(); })()`);
await evalJs(`(() => { clearTimeout(dailyTimer); dailyTimer = null; })()`);
check("摆盘:今天那一份已经删了,列表里只剩别的",
      await evalJs(`!dailyReports.some(r => r.date === '${today}') && dailyReports.length > 0`),
      await evalJs(`JSON.stringify(dailyReports.map(r => r.date))`));
const genLabel = () => evalJs(`document.getElementById('daily-gen').textContent.trim()`);
await holdList();
await evalJs(`(() => { loadDailyReports(); })()`);    // 这次列表读取被扣住
check("摆盘:慢那次列表读取确实挂住了,屏幕还没变",
      await evalJs(`!dailyReports.some(r => r.date === '${today}')`),
      await evalJs(`JSON.stringify(dailyReports.map(r => r.date))`));
check("摆盘:后端真生成了今天这一份", (await genRange(`${today}T00:00`)) === "done");
await evalJs(`refreshDailyStatus()`);
check("新列表先到,今天那一份出现在屏幕上",
      await waitFor(`dailyReports.some(r => r.date === '${today}')`, 8000),
      await evalJs(`JSON.stringify(dailyReports.map(r => r.date))`));
const liveLabel = await genLabel();
check("按钮也跟着改叫「重新生成日报」", /重新生成/.test(liveLabel), liveLabel);
await evalJs(`window.__release(); true`);
await sleep(600);
check("旧列表晚到也盖不回去",
      await evalJs(`dailyReports.some(r => r.date === '${today}')`),
      await evalJs(`JSON.stringify(dailyReports.map(r => r.date))`));
check("阅读区还停在今天这一份",
      (await evalJs(`document.getElementById('daily-reader-title').textContent`))
        === `日报 · ${today}`,
      await evalJs(`document.getElementById('daily-reader-title').textContent`));
check("按钮没退回「生成日报」", (await genLabel()) === liveLabel, await genLabel());
check("也没冒出一句列表读取失败",
      await evalJs(`document.getElementById('daily-error').hidden`),
      await evalJs(`document.getElementById('daily-error').textContent`));
for (let i = 0; i < 2; i++) { await evalJs(`refreshDailyStatus()`); await sleep(300); }
check("后面两跳轮询也没把它带回旧列表",
      await evalJs(`dailyReports.some(r => r.date === '${today}')`),
      await evalJs(`JSON.stringify(dailyReports.map(r => r.date))`));
await evalJs(`window.__restoreFetch(); true`);

// 失败那一侧同理:被替代的那次列表读取**报错**时不许弹横幅 —— 列表已经是新的了,
// 贴一句「日报列表读取失败」在旁边就是谎话,而那条横幅要留到下一轮真跑起来才清。
await evalJs(`(async () => {
  await fetch('/api/daily/reports/${today}', { method: 'DELETE' });
  await loadDailyReports(false); })()`);
await evalJs(`(() => { clearTimeout(dailyTimer); dailyTimer = null; })()`);
await holdList();
await evalJs(`(() => { loadDailyReports(); })()`);
check("摆盘:后端又生成了今天这一份", (await genRange(`${today}T00:00`)) === "done");
await evalJs(`refreshDailyStatus()`);
check("摆盘:新列表已经到了",
      await waitFor(`dailyReports.some(r => r.date === '${today}')`, 8000),
      await evalJs(`JSON.stringify(dailyReports.map(r => r.date))`));
await evalJs(`window.__releaseAsFailure(); true`);
await sleep(600);
check("被替代的那次列表读取报错也不弹横幅",
      await evalJs(`document.getElementById('daily-error').hidden`),
      await evalJs(`document.getElementById('daily-error').textContent`));
check("列表也还是新的那一份",
      await evalJs(`dailyReports.some(r => r.date === '${today}')`),
      await evalJs(`JSON.stringify(dailyReports.map(r => r.date))`));
await evalJs(`window.__restoreFetch(); true`);

// 反过来钉一边:「选中的已经不是这一天了」那道守卫同样不能少 —— 删除那条路把
// dailyCurrent 清成空串却**不经过** openDailyReport(所以序号一动不动),在途的那次
// 读取只有它挡得住,否则刚删掉的正文会被画回阅读区。
await evalJs(`(async () => {
  for (const r of dailyReports.filter(r => r.date !== '${today}'))
    await fetch('/api/daily/reports/' + r.date, { method: 'DELETE' });
  await loadDailyReports(); })()`);
check("摆盘:只剩今天这一份了(删除之后没有别的可选)",
      (await evalJs(`dailyReports.length`)) === 1,
      await evalJs(`JSON.stringify(dailyReports.map(r => r.date))`));
await holdReport();
await evalJs(`(() => { openDailyReport('${today}'); })()`);
await evalJs(`(() => { deleteDailyReport(); })()`);            // 它停在确认框上等点击
check("删除前先问一句", await waitFor(`!!document.querySelector('.confirm-mask [data-act=ok]')`, 5000));
await evalJs(`document.querySelector('.confirm-mask [data-act=ok]').click()`);
check("删掉之后阅读区写着已删除",
      await waitFor(`/已删除/.test(document.getElementById('daily-reader-body').textContent)`, 8000),
      await evalJs(`document.getElementById('daily-reader-body').textContent.slice(0, 40)`));
await evalJs(`window.__release(); true`);
await sleep(600);
check("在途那次读取不许把删掉的正文画回来",
      /已删除/.test(await evalJs(`document.getElementById('daily-reader-body').textContent`)),
      await evalJs(`document.getElementById('daily-reader-body').textContent.slice(0, 60)`));
await evalJs(`window.__restoreFetch(); true`);


check("页面没有脚本报错", pageErrors.length === 0, pageErrors.join(" | "));

console.log(oks.map(o => "  ok  " + o).join("\n"));
if (fails.length) console.log(fails.map(f => "FAIL  " + f).join("\n"));
console.log(`\n${oks.length} ok, ${fails.length} failed`);
ws.close(); proc.kill();
process.exit(fails.length ? 1 : 0);
