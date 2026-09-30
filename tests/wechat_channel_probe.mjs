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
const posts = [];
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
  "/api/pipeline/accounts": { accounts: ["测试公众号"] },
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
  if (path === "/api/pipeline/channels" && req.method === "GET") return json(channelsPayload());
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

check("页面无脚本异常", A.errors.length === 0, A.errors.join(" | "));
check("第二个页面也无脚本异常", !B || B.errors.length === 0, (B?.errors || []).join(" | "));

for (const page of [A, B].filter(Boolean)) {
  await page.evalJs(`invalidatePipeStatus(); clearTimeout(wechatPoll)`);
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
