// 绿泡泡采集通道改成设置项之后的页面回归：两条互斥通道不再出现在任何一处
// 「同步范围」里，只在设置（绿泡泡接入）里二选一，默认本机数据库。
//
// 自带一个 mock 后端（真的实现那一条二选一的服务端语义），CDP 直连 playwright
// 缓存里的 chrome-headless-shell —— 不装 playwright、不连网、始终无头、临时 profile。
//
//   HEADLESS_SHELL=<bin> node tests/wechat_channel_probe.mjs
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
const BIN = process.env.HEADLESS_SHELL;
if (!BIN) { console.error("需要 HEADLESS_SHELL=<chrome-headless-shell 路径>"); process.exit(2); }

// ── mock 后端 ────────────────────────────────────────────────────────────────
// 通道那两个接口按真实语义实现（选中的才列出来、labels 含两条），其余给足以
// 让页面初始化的最小 fixture。
const WECHAT = {
  wechat_local: { id: "wechat_local", label: "绿泡泡本地数据库", hint: "读取本机已同步的公众号推送" },
  wechat_oa: { id: "wechat_oa", label: "凭证抓取（原通道）", hint: "用各公众号的文章凭证获取列表" },
};
const FEEDS = [{ id: "social", label: "大V观点", hint: "" }, { id: "rss", label: "RSS", hint: "" }];
let selected = "wechat_local";
let failNext = false;          // 下一次保存故意失败，看页面怎么收场
let failGetOnce = false;       // 下一次读通道清单故意 503（重新核对失败那一档）
let holdChannelsWanted = false;  // 下一次读通道清单扣住不返回（慢响应那一档）
const heldChannels = [];         // 扣住的那些响应，release 时按序放出
let holdAccounts = false;        // 扣住订阅读取，好观察「读取期间不许开跑」
const heldAccounts = [];
const posts = [];
const runs = [];               // /api/pipeline/run 真正收到的同步范围
const channelsPayload = () => ({
  channels: [WECHAT[selected], ...FEEDS],
  default: selected,
  labels: { wechat_local: WECHAT.wechat_local.label, wechat_oa: WECHAT.wechat_oa.label,
            social: "大V观点", rss: "RSS" },
  wechat: { selected, options: [WECHAT.wechat_local, WECHAT.wechat_oa] },
});
// 上次那一轮跑的是**没被选中**的那条通道：面板要把它显示成人话，不是裸 id。
const LAST_RUN = { crawl_filter: { channel: "wechat_oa", accounts: [] },
                   started_at: "2026-09-29 08:00", finished_at: "2026-09-29 08:30",
                   stages: {}, log: [] };
const releaseChannels = () => { for (const f of heldChannels.splice(0)) f(); };
const releaseAccounts = () => { holdAccounts = false; for (const f of heldAccounts.splice(0)) f(); };
const FIXTURES = {
  "/api/stats": { documents: 0, vectors: 0, entities: 0, extracted: 0 },
  "/api/facets": { sources: [], accounts: [], topics: [], entities: [] },
  "/api/projects": { tools: [], kinds: {}, total_entries: 0, topics: [] },
  "/api/favorites": { doc_paths: [], favorites: [], counts: { inbox: 0, done: 0, archived: 0 }, stale: 0 },
  "/api/models": { models: [], default: "" },
  "/api/keys": { keys: { anthropic: { set: true }, openai: { set: false } } },
  "/api/stages": { stages: [], providers: [] },
  "/api/mcp/status": { config: "{}", command: "", tools: [], cli_available: false },
  "/api/intake/modules": { modules: [] },
  "/api/conversations": { conversations: [] },
  "/api/pipeline/schedule": { enabled: false, time: "07:30", channel: selected, channels: [selected] },
  "/api/pipeline/status": { running: false, stages: {}, source: { online: true, ready: true },
                            log: [], last_run: LAST_RUN },
  "/api/pipeline/accounts": { accounts: ["测试公众号甲", "测试公众号乙"] },
  "/api/sources/targets": { manageable: true, label: "公众号", targets: [] },
  "/api/wechat/setup": { state: "ready", ready: true, running: false, provider_ready: true,
                         models_ready: true, message: "绿泡泡数据库已验证可读。" },
  "/api/daily/window": { since: "2026-09-29 08:00", until: "2026-09-30 08:00" },
  "/api/daily/reports": { reports: [] },
  "/api/daily/status": { running: false, phase: "", channels: [] },
};
const MIME = { html: "text/html", js: "text/javascript", css: "text/css", json: "application/json",
               svg: "image/svg+xml", ico: "image/x-icon" };

const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  const json = (data, code = 200) => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(code, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  if (path === "/api/pipeline/channels" && req.method === "GET") {
    if (failGetOnce) { failGetOnce = false; return json({ detail: "测试故障：来源读取失败" }, 503); }
    // 响应内容在**此刻**定下来：扣住之后设置再被改掉，放出来的仍是那份旧的，
    // 正是「慢响应回来时设置已经变了」这个形态。
    const payload = channelsPayload();
    if (holdChannelsWanted) { holdChannelsWanted = false; heldChannels.push(() => json(payload)); return; }
    return json(payload);
  }
  if (path === "/api/pipeline/accounts" && holdAccounts) {
    heldAccounts.push(() => json(FIXTURES[path]));
    return;
  }
  if (path === "/api/pipeline/run" && req.method === "POST") {
    let raw = "";
    req.on("data", c => { raw += c; });
    req.on("end", () => { runs.push(JSON.parse(raw || "{}")); json({ ok: true }); });
    return;
  }
  if (path === "/api/pipeline/channels/wechat" && req.method === "POST") {
    let raw = "";
    req.on("data", c => { raw += c; });
    req.on("end", () => {
      const wanted = JSON.parse(raw || "{}").channel;
      posts.push(wanted);
      if (failNext) { failNext = false; return json({ detail: "测试故障：写盘失败" }, 500); }
      if (!WECHAT[wanted]) return json({ detail: "未知的公众号采集通道" }, 400);
      selected = wanted;
      json(channelsPayload());
    });
    return;
  }
  if (path === "/api/daily/settings") {
    return json(req.method === "POST"
      ? { enabled: false, time: "08:00", sync: true, first_days: 2, channels: [selected, "social", "rss"] }
      : { settings: { enabled: false, time: "08:00", sync: true, first_days: 2,
                      channels: [selected, "social", "rss"] },
          channels: channelsPayload().channels,
          default_channels: [selected, "social", "rss"] });
  }
  if (path.startsWith("/api/")) return json(FIXTURES[path] ?? {});
  const file = path === "/" ? "index.html" : path.replace(/^\/static\//, "");
  const target = normalize(join(STATIC, file));
  if (!target.startsWith(STATIC)) { res.writeHead(403); return res.end(); }
  try {
    const body = readFileSync(target);
    res.writeHead(200, { "Content-Type": MIME[file.split(".").pop()] || "application/octet-stream",
                         "Content-Length": body.length, "Cache-Control": "no-store" });
    res.end(body);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(r => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}`;

// ── 无头浏览器 ───────────────────────────────────────────────────────────────
const proc = spawn(BIN, ["--headless", "--remote-debugging-port=0", "--disable-gpu",
                         "--hide-scrollbars", "--window-size=1440,1000", "about:blank"],
                   { stdio: ["ignore", "pipe", "pipe"] });
const port = await new Promise((res, rej) => {
  let buf = "";
  proc.stderr.on("data", d => {
    buf += d;
    const m = buf.match(/ws:\/\/127\.0\.0\.1:(\d+)\//);
    if (m) res(+m[1]);
  });
  proc.on("exit", c => rej(new Error(`exit ${c}\n${buf}`)));
  setTimeout(() => rej(new Error("no devtools port")), 20000);
});
const sleep = ms => new Promise(r => setTimeout(r, ms));
// node 侧的等待：等 mock 后端那边出现某个状态（扣住的响应就位、真的收到了一次 run）。
const waitFor = async (fn, ms = 8000) => {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) { if (fn()) return true; await sleep(80); }
  return false;
};
const waitRuns = n => waitFor(() => runs.length >= n);

// 一个 page target 一套连接：第 9 节要开第二个页面（跨页面切换那条），所以做成工厂。
async function connectPage(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise(r => ws.addEventListener("open", r, { once: true }));
  let id = 0;
  const pend = new Map();
  const errors = [];
  ws.addEventListener("message", ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
    if (m.method === "Runtime.exceptionThrown")
      errors.push(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
      errors.push(m.params.args.map(a => a.value ?? a.description ?? "").join(" "));
  });
  const send = (method, params = {}) => {
    const mid = ++id;
    return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); });
  };
  const evalJs = async expression => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
    return r.result?.result?.value;
  };
  const until = async (expr, ms = 8000) => {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline) {
      if (await evalJs(expr)) return true;
      await sleep(120);
    }
    return false;
  };
  // 勾选框的 value 是 "on"，通道名记在 data-* 上：dataset 优先，别拿默认值当 id。
  const ids = sel => evalJs(`[...document.querySelectorAll('${sel}')].map(e => e.dataset.runChannel
    || e.dataset.channel || e.dataset.schedChannel || e.value)`);
  await send("Runtime.enable");
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  return { ws, send, evalJs, until, ids, errors };
}

// 打开（或重新打开）知识库页面并等脚本就绪。第 8 节靠重新加载来模拟"升级后第一次打开"。
async function boot(page) {
  await page.send("Page.navigate", { url: base + "/" });
  if (!await page.until(`typeof loadPipeChannels === 'function' && typeof openWechatSetup === 'function'`, 20000))
    throw new Error("页面没起来");
}

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const A = await connectPage(list.find(t => t.type === "page").webSocketDebuggerUrl);
const { evalJs, until, ids } = A;
let B = null;

const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));

await boot(A);
await evalJs(`localStorage.clear()`);

// ── 1. 设置里能选，默认本机数据库 ──
await evalJs(`openWechatSetup()`);
await until(`document.querySelectorAll('#wechat-channels label').length === 2`);
check("设置里列出两条互斥通道", (await ids("#wechat-channels input")).length === 2);
check("默认选中本机数据库",
      await evalJs(`document.querySelector('#wechat-channels input:checked')?.value`) === "wechat_local");
check("每条通道各带自己的说明",
      await evalJs(`[...document.querySelectorAll('#wechat-channels small')].filter(e => e.textContent.trim()).length`) === 2);
check("弹窗里的通道选择是单选不是勾选",
      await evalJs(`[...document.querySelectorAll('#wechat-channels input')].every(i => i.type === 'radio')`));

// ── 2. 同步范围里不再出现第二条绿泡泡通道 ──
await evalJs(`closeWechatSetup(); switchTab('pipe')`);
await evalJs(`openPipeModal()`);
await until(`document.querySelectorAll('#pipe-channel-list label').length === 3`);
const runList = await ids("#pipe-channel-list input");
check("新建同步的来源里只有一条绿泡泡通道", runList.join() === "wechat_local,social,rss", runList.join());
check("顶部来源下拉也只有一条",
      (await ids("#pipe-channel-current option")).join() === "wechat_local,social,rss");
await evalJs(`closePipeModal()`);
const schedList = await ids("#sched-channels input");
check("定时同步范围同样只有一条", schedList.join() === "wechat_local,social,rss", schedList.join());

// ── 3. 没被选中的那条仍然翻得出人话（上次运行记录里写着的就是它）──
await until(`document.getElementById('pipe-running-channel').textContent.includes('来源')`);
const lastLine = await evalJs(`document.getElementById('pipe-running-channel').textContent`);
check("上次来源显示人话而不是裸 id",
      lastLine.includes("凭证抓取") && !lastLine.includes("wechat_oa"), lastLine);

// ── 4. 日报的同步范围跟着走 ──
await evalJs(`switchTab('daily')`);
await until(`document.querySelectorAll('#daily-channels label').length === 3`);
const dailyList = await ids("#daily-channels input");
check("日报同步范围只有一条绿泡泡通道", dailyList.join() === "wechat_local,social,rss", dailyList.join());

// ── 5. 在设置里换一条，各处跟着换 ──
// 故意停在日报页上换：这几组勾选是各自加载时渲染一次的，不在换通道时就地更新的话，
// 界面上会一直列着一条已经不存在的来源，勾了它保存也只会被后端改写成新的那条。
await evalJs(`openWechatSetup()`);
await until(`document.querySelectorAll('#wechat-channels label').length === 2`);
await evalJs(`changeWechatChannel('wechat_oa')`);
await until(`document.getElementById('wechat-channel-msg').textContent.includes('已切换')`);
check("切换通道发给后端一次", posts.length === 1 && posts[0] === "wechat_oa", posts.join());
check("切换后设置里选中的是新通道",
      await evalJs(`document.querySelector('#wechat-channels input:checked')?.value`) === "wechat_oa");
check("切换后给出落到实处的反馈",
      (await evalJs(`document.getElementById('wechat-channel-msg').textContent`)).includes("凭证抓取"));
// 还停在日报页、也没重新读过设置:这两组必须当场就换过来
const dailyAfter = await ids("#daily-channels input");
check("日报范围当场换成新通道（没重新读设置）",
      dailyAfter.join() === "wechat_oa,social,rss", dailyAfter.join());
check("日报范围里原来勾着的绿泡泡还勾着",
      await evalJs(`[...document.querySelectorAll('#daily-channels input:checked')]
        .map(i => i.dataset.channel).join()`) === "wechat_oa,social,rss");
await evalJs(`closeWechatSetup(); switchTab('pipe')`);
const schedAfter = await ids("#sched-channels input");
check("定时范围当场换成新通道（没重新读定时设置）",
      schedAfter.join() === "wechat_oa,social,rss", schedAfter.join());
await evalJs(`openPipeModal()`);
await until(`document.querySelectorAll('#pipe-channel-list label').length === 3`);
const after = await ids("#pipe-channel-list input");
check("同步范围换成了新通道", after.join() === "wechat_oa,social,rss", after.join());
check("这次同步默认就勾着新通道",
      await evalJs(`pipeRunSel.join()`).then(v => v.includes("wechat_oa") && !v.includes("wechat_local")));
check("存下来的选择也换了",
      await evalJs(`localStorage.getItem('kb-sync-channels')`) === '["wechat_oa"]',
      await evalJs(`localStorage.getItem('kb-sync-channels')`));
await evalJs(`closePipeModal()`);

// ── 6. 同一条再点一次不该再发一次请求 ──
await evalJs(`switchTab('pipe'); openWechatSetup()`);
await until(`document.querySelectorAll('#wechat-channels label').length === 2`);
await evalJs(`changeWechatChannel('wechat_oa')`);
await sleep(300);
check("点同一条不重复保存", posts.length === 1, posts.join());

// ── 7. 保存失败要如实说，并且把单选按钮拨回去 ──
// 留在用户刚点的那一项上等于谎报保存成功：下一次同步走的还是旧通道。
failNext = true;
await evalJs(`changeWechatChannel('wechat_local')`);
await until(`document.getElementById('wechat-channel-msg').textContent.includes('未能切换')`);
check("保存失败时单选拨回原来那条",
      await evalJs(`document.querySelector('#wechat-channels input:checked')?.value`) === "wechat_oa",
      await evalJs(`document.querySelector('#wechat-channels input:checked')?.value`));
check("保存失败时给出原因",
      (await evalJs(`document.getElementById('wechat-channel-msg').textContent`)).includes("测试故障"));
check("保存失败时同步范围一个字没动",
      (await ids("#pipe-channel-list input")).join() === "wechat_oa,social,rss");
check("保存失败后还能再试", await evalJs(`!wechatChannelBusy`));

// ── 8. 升级过来的存量选择：旧通道名跟着设置走，别的来源一个不动 ──
// 上一版两条绿泡泡通道都能勾，所以浏览器里存着的可能是另一条、也可能两条都在。当成
// "不认识的来源"直接丢掉的话，只要还勾着 RSS 之类，「一个都没剩才补默认」那句兜底就不
// 触发 —— 公众号被静默地漏掉，界面上一点提示都没有。
for (const m of [
  { setting: "wechat_local", stored: ["wechat_oa", "rss"], want: ["wechat_local", "rss"] },
  { setting: "wechat_oa", stored: ["wechat_local", "social"], want: ["wechat_oa", "social"] },
  { setting: "wechat_local", stored: ["wechat_oa", "wechat_local", "rss"], want: ["wechat_local", "rss"] },
  { setting: "wechat_oa", stored: ["没这个通道", "rss"], want: ["rss"] },
]) {
  const tag = `${m.stored.join("+")} → ${m.setting}`;
  selected = m.setting;
  await evalJs(`localStorage.setItem('kb-sync-channel', ${JSON.stringify(m.stored[0])});
    localStorage.setItem('kb-sync-channels', ${JSON.stringify(JSON.stringify(m.stored))}); true`);
  await boot(A);
  await evalJs(`switchTab('pipe'); openPipeModal()`);
  await until(`document.querySelectorAll('#pipe-channel-list label').length === 3`);
  const checked = await evalJs(`[...document.querySelectorAll('#pipe-channel-list input:checked')]
    .map(i => i.dataset.runChannel).join()`);
  check(`存量选择迁移后仍勾着（${tag}）`, checked === m.want.join(), checked);
  // 勾选框对了而真正派发出去的范围漏掉公众号，等于没修
  await until(`pipeRunBody().channels.length === ${m.want.length}`);
  const scoped = JSON.parse(await evalJs(`JSON.stringify(pipeRunBody().channels.map(c => c.channel))`));
  check(`提交范围跟着一起迁移（${tag}）`, scoped.join() === m.want.join(), scoped.join());
  const stored = await evalJs(`localStorage.getItem('kb-sync-channels')`);
  check(`迁移后的选择落了盘（${tag}）`, stored === JSON.stringify(m.want), stored);
  await evalJs(`closePipeModal()`);
}

// ── 9. 另一个页面改了设置：这一个不刷新也得跟着走 ──
// 通道是个全局单值，CustomEvent 不跨文档，所以靠 localStorage 的 storage 事件广播。
selected = "wechat_local";
// 页面 A 勾着公众号 + 两个订阅来源：换通道是「改道」，另外两个来源必须原样留着。
await evalJs(`localStorage.clear();
  localStorage.setItem('kb-sync-channels', '["wechat_local","social","rss"]'); true`);
await boot(A);
await evalJs(`switchTab('pipe')`);
await until(`typeof pipeChannels !== 'undefined' && pipeChannels.length === 3`);
await (await fetch(`http://127.0.0.1:${port}/json/new`, { method: "PUT" })).json()
  .then(async t => { B = await connectPage(t.webSocketDebuggerUrl); await boot(B); });
await B.evalJs(`openWechatSetup()`);
await B.until(`document.querySelectorAll('#wechat-channels label').length === 2`);
await B.evalJs(`changeWechatChannel('wechat_oa')`);
await B.until(`document.getElementById('wechat-channel-msg').textContent.includes('已切换')`);
check("另一个页面把通道换成了新的", await B.evalJs(`pipeWechat.selected`) === "wechat_oa");
await until(`pipeWechat.selected === 'wechat_oa'`);
check("旧页面不刷新也跟到了新通道", await evalJs(`pipeWechat.selected`) === "wechat_oa",
      await evalJs(`pipeWechat.selected`));
await evalJs(`openPipeModal()`);
await until(`document.querySelectorAll('#pipe-channel-list label').length === 3`);
const crossList = await ids("#pipe-channel-list input");
check("旧页面的同步范围换成了新通道", crossList.join() === "wechat_oa,social,rss", crossList.join());
await until(`pipeRunBody().channels.length === 3`);
const crossScope = JSON.parse(await evalJs(`JSON.stringify(pipeRunBody().channels.map(c => c.channel))`));
check("旧页面派发的范围也是新通道", crossScope.join() === "wechat_oa,social,rss", crossScope.join());
await evalJs(`closePipeModal()`);

// ── 10. 设置被 CLI/API 直接改掉（没有 storage 广播可听）：打开时重新核对服务端 ──
// 自己 boot 一次，别吃上一节的结果：页面内存里是 wechat_oa，随后终端把落盘值改掉。
selected = "wechat_oa";
await boot(A);
await until(`typeof pipeWechat !== 'undefined' && pipeWechat.selected === 'wechat_oa'`);
selected = "wechat_local";
check("终端改完、页面还没核对时内存里仍是旧值", await evalJs(`pipeWechat.selected`) === "wechat_oa");
await evalJs(`openWechatSetup()`);
check("重开设置会重新核对服务端",
      await until(`document.querySelector('#wechat-channels input:checked')?.value === 'wechat_local'`),
      await evalJs(`document.querySelector('#wechat-channels input:checked')?.value`));
await evalJs(`closeWechatSetup()`);
selected = "wechat_oa";
await evalJs(`openPipeModal()`);
check("重开新建同步也会重新核对服务端",
      await until(`[...document.querySelectorAll('#pipe-channel-list input')]
        .map(i => i.dataset.runChannel).join() === 'wechat_oa,social,rss'`),
      (await ids("#pipe-channel-list input")).join());
await evalJs(`closePipeModal()`);

// 复用页面 B 切一次通道：场景 11、12 都要「另一个页面把设置改掉」。
// 分两步是给场景 12 用的：那一节要先把弹窗开好，切换那一下才够快（见那一节的注释）。
async function openSetupInB() {
  await B.evalJs(`openWechatSetup()`);
  return B.until(`document.querySelectorAll('#wechat-channels label').length === 2`);
}
async function pickInB(to) {
  await B.evalJs(`changeWechatChannel(${JSON.stringify(to)})`);
  return B.until(`document.getElementById('wechat-channel-msg').textContent.includes('已切换')`);
}
const switchInB = async to => { await openSetupInB(); return pickInB(to); };
// 在页面 A 自己的设置里换通道:同页面那条路走的是 POST 回包直接改内存,没有 storage 广播。
async function switchInA(to) {
  await evalJs(`openWechatSetup()`);
  await until(`document.querySelectorAll('#wechat-channels label').length === 2`);
  await evalJs(`changeWechatChannel(${JSON.stringify(to)})`);
  const ok = await until(`document.getElementById('wechat-channel-msg').textContent.includes('已切换')`);
  await evalJs(`closeWechatSetup()`);
  return ok;
}
const acctsOf = (scope, channel) => (scope.find(c => c.channel === channel)?.accounts || []).join();
// 每一节都从「设置 = 本机库、勾着公众号 + RSS」这个已知状态起跑。
async function freshPipe() {
  selected = "wechat_local";
  await evalJs(`localStorage.clear();
    localStorage.setItem('kb-sync-channels', '["wechat_local","rss"]'); true`);
  await boot(A);
  await evalJs(`switchTab('pipe')`);
  await until(`typeof pipeWechat !== 'undefined' && pipeWechat.selected === 'wechat_local'`);
}

// ── 11. 新建同步开着的时候换通道：那份订阅状态也得跟着改道 ──
// pipeRunSel 改道了而 pipeAccounts 没有的话，新通道既不算 ready 也不算 loading：
// pipeRunBody() 直接把它漏掉，开始按钮还照样能点 —— 勾着公众号点下去只同步了 RSS。
await freshPipe();
await evalJs(`openPipeModal()`);
await until(`pipeReadyChannels().join() === 'wechat_local,rss'`);
// 只留一个号：换通道时这份选择要带过去，不能被重置成全选
await evalJs(`pipeSel.set('wechat_local', new Set(['测试公众号甲'])); updatePipeSelection(); true`);
await boot(B);
await B.until(`typeof pipeWechat !== 'undefined' && pipeWechat.selected === 'wechat_local'`);
holdAccounts = true;                      // 换通道后那次订阅读取扣住，好看清读取中的状态
await switchInB("wechat_oa");
await until(`pipeLoadingChannels().length > 0`);
check("换通道后新通道在读订阅期间不能开始",
      await evalJs(`document.getElementById('pipe-start').disabled`) === true);
const heldRuns = runs.length;
await evalJs(`startPipeFromModal()`);
await sleep(250);
check("读订阅期间提交入口也拦住", runs.length === heldRuns, JSON.stringify(runs.slice(heldRuns)));
releaseAccounts();
await until(`pipeReadyChannels().join() === 'wechat_oa,rss'`);
const openScope = JSON.parse(await evalJs(`JSON.stringify(pipeRunBody().channels)`));
check("开着的窗口换通道后一个来源都不漏",
      openScope.map(c => c.channel).join() === "wechat_oa,rss",
      JSON.stringify(openScope.map(c => c.channel)));
check("换通道把原来勾的那几个订阅带过去（不是重置成全选）",
      openScope.find(c => c.channel === "wechat_oa")?.accounts.join() === "测试公众号甲",
      JSON.stringify(openScope.find(c => c.channel === "wechat_oa")?.accounts));
const openRuns = runs.length;
await evalJs(`startPipeFromModal()`);
check("点开始真的按新通道派发",
      await waitRuns(openRuns + 1)
      && runs[openRuns].channels?.map(c => c.channel).join() === "wechat_oa,rss",
      JSON.stringify(runs[openRuns]));

// ── 12. 慢响应期间设置被改掉：那份旧响应不许当成最新 ──
// 强制重读复用在途请求的话，回来的是切换**之前**生成的那份，页面按它读订阅、按它开跑。
await freshPipe();
await boot(B);
await B.until(`typeof pipeWechat !== 'undefined' && pipeWechat.selected === 'wechat_local'`);
// 探测这个页面到底处理过切换通知没有 —— 监听器按注册顺序跑，pipe_channels.js 那个在前，
// 所以计数变 1 时它已经同步走完（补发了读取，或者错误地复用了在途的那个）。没有这个等待，
// 「慢响应回来」和「通知到达」谁先谁后纯看运气：通知后到的那一跳会自己补一次正常读取，
// 于是复用在途请求这个缺陷在探针里时而不复现（2026-09-30 注入回归实测漏过）。
await evalJs(`window.__chanNotice = 0;
  window.addEventListener('storage', e => { if (e.key === 'kb-wechat-channel') window.__chanNotice++; }); true`);
// B 先把设置弹窗开好：扣住响应到放开这一整段必须跑在 `pipeJson` 那 15 秒超时之内 ——
// 超时的话请求已经 reject、`pipeChannelsLoading` 早被清空，这个缺陷压根不会发生
// （2026-09-30 注入回归实测：`await openPipeModal()` 自己就把 probe 阻塞满 15 秒）。
await openSetupInB();
holdChannelsWanted = true;
// 故意不 await：这次读取要一直扣着，await 它等于 probe 陪它一起卡到超时。
await evalJs(`openPipeModal(); true`);
check("慢响应那次读取真的被扣住了", await waitFor(() => heldChannels.length === 1, 4000));
await pickInB("wechat_oa");
check("切换通知送到了那个还在等响应的页面", await until(`window.__chanNotice === 1`, 6000));
releaseChannels();                        // 旧响应现在才回来
await until(`pipeReadyChannels().join() === 'wechat_oa,rss'`, 12000);
const racyList = await ids("#pipe-channel-list input");
check("慢响应回来后列表是切换后的通道", racyList.join() === "wechat_oa,social,rss", racyList.join());
const racyRuns = runs.length;
await evalJs(`startPipeFromModal()`);
check("慢响应回来后派发的也是新通道",
      await waitRuns(racyRuns + 1)
      && runs[racyRuns].channels?.map(c => c.channel).join() === "wechat_oa,rss",
      JSON.stringify(runs[racyRuns]));

// ── 13. 重新核对失败时，上次那份就绪状态不许还能开跑 ──
await freshPipe();
await evalJs(`openPipeModal()`);
await until(`pipeReadyChannels().join() === 'wechat_local,rss'`);
await evalJs(`closePipeModal()`);
selected = "wechat_oa";                   // 别的客户端改了设置，没有 storage 广播可听
failGetOnce = true;                       // 而这次重新核对 503
await evalJs(`openPipeModal()`);
await until(`document.getElementById('pipe-setup-error').hidden === false`);
check("重读失败时开始按钮禁用",
      await evalJs(`document.getElementById('pipe-start').disabled`) === true);
check("重读失败时写清为什么不能开始",
      (await evalJs(`document.getElementById('pipe-est').textContent`)).includes("尚未确认"),
      await evalJs(`document.getElementById('pipe-est').textContent`));
const failRuns = runs.length;
await evalJs(`startPipeFromModal()`);
await sleep(250);
check("重读失败时提交入口也拦住（不会拿旧通道开跑）",
      runs.length === failRuns, JSON.stringify(runs.slice(failRuns)));
await evalJs(`openPipeModal()`);         // 「重新读取来源」
await until(`pipeReadyChannels().join() === 'wechat_oa,rss'`);
check("重试成功后恢复可开始",
      await evalJs(`!document.getElementById('pipe-start').disabled`));
const retryRuns = runs.length;
await evalJs(`startPipeFromModal()`);
check("重试成功后派发的是当前设置那条通道",
      await waitRuns(retryRuns + 1)
      && runs[retryRuns].channels?.map(c => c.channel).join() === "wechat_oa,rss",
      JSON.stringify(runs[retryRuns]));

// ── 14. 关着新建同步的时候换通道:订阅选择不许被重置成全选 ──
// 迁移写在「弹窗开着吗」后面的话,关着的时候换就整段跳过 —— 新通道没有草稿,下次打开
// loadChannelAccounts 按整份订阅全选初始化:上次只勾了一个号,换个采集方式回来变成全选,
// 点开始把没勾的那些一起采了(第 3 轮审查实测 accounts 从「甲」变成「甲+乙」)。
await freshPipe();
await evalJs(`openPipeModal()`);
await until(`pipeReadyChannels().join() === 'wechat_local,rss'`);
await evalJs(`pipeSel.set('wechat_local', new Set(['测试公众号甲'])); updatePipeSelection(); true`);
await evalJs(`closePipeModal()`);
check("关着窗口也换得动通道", await switchInA("wechat_oa"));
await evalJs(`openPipeModal()`);
await until(`pipeReadyChannels().join() === 'wechat_oa,rss'`);
const closedScope = JSON.parse(await evalJs(`JSON.stringify(pipeRunBody().channels)`));
check("关着窗口换通道后来源跟着设置走",
      closedScope.map(c => c.channel).join() === "wechat_oa,rss",
      JSON.stringify(closedScope.map(c => c.channel)));
check("关着窗口换通道后订阅选择没被扩大",
      acctsOf(closedScope, "wechat_oa") === "测试公众号甲", acctsOf(closedScope, "wechat_oa"));
const closedRuns = runs.length;
await evalJs(`startPipeFromModal()`);
check("关着窗口换通道后派发的范围也没扩大",
      await waitRuns(closedRuns + 1)
      && acctsOf(runs[closedRuns].channels || [], "wechat_oa") === "测试公众号甲",
      JSON.stringify(runs[closedRuns]));
// 「一个都不采」同样是用户的选择:当成"没有草稿"的话它也会变成全选
await freshPipe();
await evalJs(`openPipeModal()`);
await until(`pipeReadyChannels().join() === 'wechat_local,rss'`);
await evalJs(`pipeSel.set('wechat_local', new Set()); updatePipeSelection(); true`);
await evalJs(`closePipeModal()`);
await switchInA("wechat_oa");
await evalJs(`openPipeModal()`);
await until(`pipeReadyChannels().join() === 'wechat_oa,rss'`);
check("关着窗口换通道后「一个都不勾」也保持原样",
      await evalJs(`pipeSelected('wechat_oa').size`) === 0,
      await evalJs(`[...pipeSelected('wechat_oa')].join()`));
await evalJs(`closePipeModal()`);

// ── 15. 来回换两次:用的是最新那份选择,不是目标通道自己的旧草稿 ──
// 目标通道已经有草稿就不迁移的话,换回去时那份**更早**的选择会盖掉用户刚改过的这份。
await freshPipe();
await boot(B);
await B.until(`typeof pipeWechat !== 'undefined' && pipeWechat.selected === 'wechat_local'`);
await evalJs(`openPipeModal()`);
await until(`pipeReadyChannels().join() === 'wechat_local,rss'`);
await evalJs(`pipeSel.set('wechat_local', new Set(['测试公众号甲'])); updatePipeSelection(); true`);
await switchInB("wechat_oa");
await until(`pipeReadyChannels().join() === 'wechat_oa,rss'`);
check("第一次改道把当时那份选择带过去",
      await evalJs(`[...pipeSelected('wechat_oa')].join()`) === "测试公众号甲",
      await evalJs(`[...pipeSelected('wechat_oa')].join()`));
// 在新通道上改成只选乙 —— 这才是最新的那份选择
await evalJs(`pipeSel.set('wechat_oa', new Set(['测试公众号乙'])); updatePipeSelection(); true`);
await switchInB("wechat_local");
await until(`pipeReadyChannels().join() === 'wechat_local,rss'`);
const backScope = JSON.parse(await evalJs(`JSON.stringify(pipeRunBody().channels)`));
check("换回曾用过的通道:不恢复它自己那份更早的选择",
      acctsOf(backScope, "wechat_local") === "测试公众号乙", acctsOf(backScope, "wechat_local"));
const backRuns = runs.length;
await evalJs(`startPipeFromModal()`);
check("换回来点开始派发的是最后一次用户选择",
      await waitRuns(backRuns + 1)
      && acctsOf(runs[backRuns].channels || [], "wechat_local") === "测试公众号乙",
      JSON.stringify(runs[backRuns]));

// ── 16. 同一个页面:保存成功之后,保存之前那份读取不许把它拨回去 ──
// 代次只在发起读取时推进的话,保存成功不会作废在途的那次读取 —— 慢响应回来时代次仍然
// 相等,一份保存前生成的旧清单被当成最新结果应用(第 3 轮审查实测:单选跳回本地数据库、
// kb-sync-channel 也写回旧值,而服务端存着的是 wechat_oa)。跨页面那条路救不了它:自己
// 写 storage 不会触发自己的监听器。
// **这一节必须只剩 A 一个页面**:报告里这条的条件正是「没有另一个页面触发新读取」。
// B 还在的话它会收到 storage 通知、自己重读一遍,顺手把同源 localStorage 那两个键写回
// 正确值 —— 于是下面「落盘值有没有被拨回去」那两项变成假绿(注入回归实测它们确实没红,
// 只有内存和单选那几项红)。
await B.send("Page.navigate", { url: "about:blank" });
await freshPipe();
await evalJs(`openWechatSetup()`);     // 先开关一次,让单选按钮已经有正常内容
await until(`document.querySelectorAll('#wechat-channels label').length === 2`);
await evalJs(`closeWechatSetup()`);
holdChannelsWanted = true;
// openWechatSetup 自己不 await 那次核对,所以这里不会陪它卡到 pipeJson 的 15 秒超时
await evalJs(`openWechatSetup(); true`);
check("设置里那次重新核对真的被扣住了", await waitFor(() => heldChannels.length === 1, 4000));
await evalJs(`changeWechatChannel('wechat_oa')`);
await until(`document.getElementById('wechat-channel-msg').textContent.includes('已切换')`);
check("保存成功时单选停在新通道",
      await evalJs(`document.querySelector('#wechat-channels input:checked')?.value`) === "wechat_oa");
check("服务端存下来的确实是新通道", selected === "wechat_oa", selected);
releaseChannels();                     // 保存之前那份响应现在才回来
check("保存之前那份响应已经处理完", await until(`pipeChannelsLoading === null`, 8000));
check("旧响应没把单选拨回去",
      await evalJs(`document.querySelector('#wechat-channels input:checked')?.value`) === "wechat_oa",
      await evalJs(`document.querySelector('#wechat-channels input:checked')?.value`));
check("旧响应没把内存里那条通道拨回去", await evalJs(`pipeWechat.selected`) === "wechat_oa",
      await evalJs(`pipeWechat.selected`));
check("旧响应没把落盘的来源选择拨回去",
      await evalJs(`localStorage.getItem('kb-sync-channel')`) === "wechat_oa",
      await evalJs(`localStorage.getItem('kb-sync-channel')`));
check("旧响应没把这次同步的范围拨回去",
      await evalJs(`localStorage.getItem('kb-sync-channels')`) === '["wechat_oa","rss"]',
      await evalJs(`localStorage.getItem('kb-sync-channels')`));
check("保存成功那句反馈没被过期的核对清掉",
      (await evalJs(`document.getElementById('wechat-channel-msg').textContent`)).includes("已切换"),
      await evalJs(`document.getElementById('wechat-channel-msg').textContent`));
check("保存回来的那份清单算核对过（开始按钮不该被无故拦住）",
      await evalJs(`pipeChannelsFresh`) === true);
await evalJs(`closeWechatSetup()`);
const staleRuns = runs.length;
await evalJs(`openPipeModal()`);
await until(`pipeReadyChannels().join() === 'wechat_oa,rss'`);
await evalJs(`startPipeFromModal()`);
check("随后开跑用的是保存下来那条通道",
      await waitRuns(staleRuns + 1)
      && runs[staleRuns].channels?.map(c => c.channel).join() === "wechat_oa,rss",
      JSON.stringify(runs[staleRuns]));

check("页面无脚本异常", A.errors.length === 0, A.errors.join(" | "));
check("第二个页面也无脚本异常", !B || B.errors.length === 0, (B?.errors || []).join(" | "));

for (const page of [A, B].filter(Boolean)) {
  // B 已经被挪到 about:blank,那儿没有这些函数
  await page.evalJs(`invalidatePipeStatus(); clearTimeout(wechatPoll)`).catch(() => {});
  page.ws.close();
}
proc.kill(); server.close();
console.log(`通过 ${oks.length} 项`);
for (const name of oks) console.log("  ok  " + name);
if (fails.length) {
  console.error(`\n失败 ${fails.length} 项:`);
  for (const name of fails) console.error("  FAIL " + name);
  process.exit(1);
}
