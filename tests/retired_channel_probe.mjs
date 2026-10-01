// 退役通道名在**真页面**上的回归:公众号只剩一条通道,而浏览器 localStorage 里
// 存着的可能正是那条退役的 `wechat_oa`(上一版两条能二选一,更早的版本两条都能勾)。
// 当成"不认识的来源"过滤掉的话,只要还勾着 RSS 之类,「一个都没剩才补默认」那句兜底
// 就不触发 —— 升级后打开新建同步只剩 RSS,公众号被静默地漏掉,一句提示都没有。
//
// 顺带钉住:通道二选一那套机制(单选区、全局单值、跨页面广播、开跑前强制核对)和
// 逐号凭证那一栏确实从页面上消失了,而且消失得干净 —— 没有残留的 JS 错误。
//
// 自带 mock 后端,CDP 直连 playwright 缓存里的 chrome-headless-shell
// (不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/retired_channel_probe.mjs
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
const BIN = process.env.HEADLESS_SHELL;
if (!BIN) { console.error("需要 HEADLESS_SHELL=<chrome-headless-shell 路径>"); process.exit(2); }

// ── mock 后端 ────────────────────────────────────────────────────────────────
// 按 /api/pipeline/channels 的真实语义:只列现役通道,labels 多给退役名,
// aliases 是「退役名 → 接替它的那条」。
const WECHAT = { id: "wechat_local", label: "公众号", hint: "读取本机已同步的公众号推送" };
const FEEDS = [{ id: "social", label: "大V观点", hint: "大V动态" }, { id: "rss", label: "RSS", hint: "订阅源" }];
const CHANNELS = {
  channels: [WECHAT, ...FEEDS],
  default: WECHAT.id,
  labels: { wechat_local: WECHAT.label, wechat_oa: WECHAT.label, social: "大V观点", rss: "RSS" },
  aliases: { wechat_oa: WECHAT.id },
};
// 上次那一轮是用退役通道跑的:面板要把它显示成人话,不是一个裸 id。
const LAST_RUN = { crawl_filter: { channel: "wechat_oa", accounts: [] },
                   started_at: "2026-09-29 08:00", finished_at: "2026-09-29 08:30",
                   stages: {}, log: [] };
const runs = [];
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
  // 范围类接口在服务端就过了 known_channels,交到前端的只会是现役名(定时范围
  // 里那个退役名换成现役名由 tests/test_retired_channel.py 的 ScopeTests 钉住)。
  // 浏览器这一侧真正会碰到退役名的只有两处:localStorage 和历史运行记录。
  "/api/pipeline/schedule": { enabled: false, time: "07:30",
                              channel: "wechat_local", channels: ["wechat_local", "rss"] },
  "/api/pipeline/status": { running: false, stages: {}, source: { online: true, ready: true },
                            log: [], last_run: LAST_RUN },
  "/api/pipeline/accounts": { accounts: ["测试公众号甲", "测试公众号乙"] },
  // 本机推送不分号授权:每个目标都是 ready=null(面板据此完全不显示就绪那一列)
  "/api/sources/targets": { manageable: true, label: "公众号", targets: [
    { id: "biz-1", name: "测试公众号甲", display_name: "", enabled: true,
      detail: "最近采集 2026-09-29", ready: null, state: "按本机推送采集", tone: "" }] },
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
  if (path === "/api/pipeline/channels" && req.method === "GET") return json(CHANNELS);
  if (path === "/api/pipeline/run" && req.method === "POST") {
    let raw = "";
    req.on("data", c => { raw += c; });
    req.on("end", () => { runs.push(JSON.parse(raw || "{}")); json({ ok: true }); });
    return;
  }
  // 这个端点已经删掉了:页面上任何一处还在调它,这里就会被记一笔
  if (path === "/api/pipeline/channels/wechat") { runs.push({ forbidden: path }); return json({}, 404); }
  if (path === "/api/sources/targets/refresh") { runs.push({ forbidden: path }); return json({}, 404); }
  if (path === "/api/daily/settings") {
    const settings = { enabled: false, time: "08:00", sync: true, first_days: 2,
                       channels: ["wechat_local", "social", "rss"] };
    return json(req.method === "POST" ? settings
      : { settings, channels: CHANNELS.channels,
          default_channels: ["wechat_local", "social", "rss"] });
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
const waitFor = async (fn, ms = 8000) => {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) { if (fn()) return true; await sleep(80); }
  return false;
};

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));
let msgId = 0;
const pend = new Map();
const pageErrors = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails?.exception?.description
                    || m.params.exceptionDetails?.text);
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    pageErrors.push(m.params.args.map(a => a.value ?? a.description ?? "").join(" "));
});
const send = (method, params = {}) => {
  const id = ++msgId;
  return new Promise(res => { pend.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
};
const evalJs = async expression => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const until = async (expr, ms = 8000) => {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) { if (await evalJs(expr)) return true; await sleep(120); }
  return false;
};
const ids = sel => evalJs(`[...document.querySelectorAll('${sel}')].map(e => e.dataset.runChannel
  || e.dataset.channel || e.dataset.schedChannel || e.value)`);
await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride",
           { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });

const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));

async function boot() {
  await send("Page.navigate", { url: base + "/" });
  if (!await until(`typeof loadPipeChannels === 'function' && typeof openWechatSetup === 'function'`, 20000))
    throw new Error("页面没起来");
}

// ── 0. 先写进"升级前"那份存量选择,再当成第一次打开 ────────────────────────────
await boot();
await evalJs(`localStorage.clear();
  localStorage.setItem('kb-sync-channel', 'wechat_oa');
  localStorage.setItem('kb-sync-channels', JSON.stringify(['wechat_oa', 'rss']));
  true`);
await boot();                       // 带着存量选择重新进一次
await evalJs(`switchTab('pipe')`);
await until(`pipeChannels.length === 3`);

// ── 1. 退役通道名跟着别名表走,公众号不会被静默漏掉 ────────────────────────────
check("同步范围里只有一条公众号通道", (await ids("#pipe-channel-list input")).join() === "wechat_local,social,rss",
      (await ids("#pipe-channel-list input")).join());
check("存量里那条退役名换成了公众号、RSS 原样留着",
      await evalJs(`JSON.stringify(pipeRunSel)`) === '["wechat_local","rss"]',
      await evalJs(`JSON.stringify(pipeRunSel)`));
check("落盘的选择也改了道",
      await evalJs(`localStorage.getItem('kb-sync-channels')`) === '["wechat_local","rss"]',
      await evalJs(`localStorage.getItem('kb-sync-channels')`));
check("顶部那个「看哪个来源」也换成了公众号",
      await evalJs(`document.getElementById('pipe-channel-current').value`) === "wechat_local"
      && await evalJs(`localStorage.getItem('kb-sync-channel')`) === "wechat_local");

// ── 2. 真的派发一次同步:提交上去的范围必须带着公众号 ──────────────────────────
await evalJs(`openPipeModal()`);
await until(`document.querySelectorAll('#pipe-channel-list label').length === 3`);
check("弹窗里公众号是勾上的",
      await evalJs(`!!document.querySelector('#pipe-channel-list input[data-run-channel=wechat_local]')?.checked`));
await until(`pipeReadyChannels().join() === 'wechat_local,rss'`);
await evalJs(`startPipeFromModal()`);
check("开跑不再被「来源尚未确认」挡住", await waitFor(() => runs.length >= 1));
const scope = (runs[0]?.channels || []).map(c => c.channel);
check("提交上去的范围里有公众号", scope.join() === "wechat_local,rss", JSON.stringify(runs[0]));

// ── 3. 历史运行记录里那条退役通道仍然显示成人话 ────────────────────────────────
check("上次来源不是一个裸 id",
      (await evalJs(`document.getElementById('pipe-running-channel').textContent`)) === "上次来源：公众号",
      await evalJs(`document.getElementById('pipe-running-channel').textContent`));

// ── 4. 定时范围 / 日报范围里的旧名字也只显示现役那条 ──────────────────────────
// 这两组勾选原来靠 kb:channels-changed 事件在换通道时就地改道,监听器随二选一一起
// 撤了 —— 所以要确认撤完它们照旧渲染得出来、勾得对。
await evalJs(`loadSched()`);
await until(`document.querySelectorAll('#sched-channels input').length === 3`);
check("定时范围列出现役三条、公众号勾着",
      (await ids("#sched-channels input")).join() === "wechat_local,social,rss"
      && (await ids("#sched-channels input:checked")).join() === "wechat_local,rss",
      (await ids("#sched-channels input:checked")).join());
await evalJs(`switchTab('daily')`);
await until(`document.querySelectorAll('#daily-channels input').length === 3`);
check("日报范围也列得出现役三条",
      (await ids("#daily-channels input")).join() === "wechat_local,social,rss",
      (await ids("#daily-channels input")).join());

// ── 5. 通道二选一那套机制从页面上消失了 ────────────────────────────────────────
await evalJs(`switchTab('pipe'); closePipeModal(); openWechatSetup()`);
await until(`document.getElementById('wechat-modal').style.display !== 'none'`);
check("接入向导里没有采集通道单选区",
      await evalJs(`!document.getElementById('wechat-channels')
                    && !document.querySelector('.wechat-channel-picker')`));
check("二选一那套全局单值机制一个符号都不剩",
      await evalJs(`['pipeWechat','saveWechatChannel','pipeChannelsFresh','markChannelsStale',
        'adoptWechatChannel','renderWechatChannels','changeWechatChannel','loadWechatChannels']
        .every(n => typeof window[n] === 'undefined')`));
check("别名映射留下来了(浏览器里存的选择只能靠它)",
      await evalJs(`typeof adoptChannels === 'function'
                    && JSON.stringify(adoptChannels(['wechat_oa'])) === '["wechat_local"]'`));

// 向导收尾那一步原来要先存一次采集通道设置(saveWechatChannel)。设置没有了,它改成
// 只把「看哪个来源」和「这次同步抓哪几个」定在公众号上 —— 先故意把公众号从范围里拿掉,
// 点完必须回来,否则用户走完向导点开新建同步会发现公众号压根没勾。
await evalJs(`pipeRunSel = ['rss']; saveRunChannels(); changePipeChannel('rss'); true`);
await until(`document.getElementById('wechat-next') &&
            !document.getElementById('wechat-next').hidden`);
await evalJs(`document.getElementById('wechat-next').click()`);
await until(`document.getElementById('pipe-channel-current').value === 'wechat_local'`);
check("向导收尾把来源定在公众号上",
      await evalJs(`localStorage.getItem('kb-sync-channel')`) === "wechat_local");
check("向导收尾也把公众号勾进这次同步的范围",
      await evalJs(`pipeRunSel.includes('wechat_local')
                    && !!document.querySelector('#pipe-channel-list input[data-run-channel=wechat_local]')?.checked`),
      await evalJs(`JSON.stringify(pipeRunSel)`));
check("向导收尾后落在同步中心、弹窗关掉了",
      await evalJs(`document.getElementById('wechat-modal').style.display === 'none'
                    && document.getElementById('pipepanel').offsetParent !== null`));
await evalJs(`openWechatSetup()`);
await until(`document.getElementById('wechat-modal').style.display !== 'none'`);

// ── 6. 逐号凭证那一栏也没了,而订阅列表照常 ────────────────────────────────────
await evalJs(`closeWechatSetup(); openTargets()`);
await until(`document.querySelectorAll('#tgt-list .tgt-row').length === 1`);
check("订阅列表照常列出来", (await evalJs(
  `document.querySelector('#tgt-list .tgt-name')?.textContent.trim()`)) === "测试公众号甲");
check("逐号凭证那一栏不在了",
      await evalJs(`!document.getElementById('tgt-ready-bar')
                    && !document.getElementById('tgt-refresh')
                    && !document.getElementById('tgt-auto')`));
check("入口按钮上没有「N/N 就绪」",
      !(await evalJs(`document.getElementById('src-manage').textContent`)).includes("就绪"),
      await evalJs(`document.getElementById('src-manage').textContent`));
check("ready=null 的目标不画那颗小圆点",
      await evalJs(`!document.querySelector('#tgt-list .tgt-dot')`));
check("凭证那套文案一个字都不剩",
      !(await evalJs(`document.body.innerText`)).includes("凭证"));

// ── 7. 删掉的两个端点没有任何一处还在调 ────────────────────────────────────────
await evalJs(`closeTargets()`); await sleep(500);
const forbidden = runs.filter(r => r.forbidden).map(r => r.forbidden);
check("没有谁还在调删掉的那两个端点", forbidden.length === 0, forbidden.join());
check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | "));

// ── 收尾 ─────────────────────────────────────────────────────────────────────
const out = [...oks.map(n => `  ✓ ${n}`), ...fails.map(n => `  ✗ ${n}`)].join("\n");
process.stdout.write(`${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`);
ws.close(); proc.kill(); server.close();
process.exitCode = fails.length ? 1 : 0;
