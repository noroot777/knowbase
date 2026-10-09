// X 采集通道在**真页面**上的回归:三条通道各要填的东西互斥显示、改了选择不许
// 冒充当前事实、保存时真把三个字段发出去、登录窗口和「检查连接」各打各的端点。
//
// 自带 mock 后端,CDP 直连 playwright 缓存里的 chrome-headless-shell
// (不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/x_channel_probe.mjs
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
const BIN = process.env.HEADLESS_SHELL;
if (!BIN) { console.error("需要 HEADLESS_SHELL=<chrome-headless-shell 路径>"); process.exit(2); }

// ── mock 后端 ────────────────────────────────────────────────────────────────
// 形状照 kb/web/intake_api.py:通道清单、人话名字、便宜的就绪判断都由后端给。
const CHANNELS = [
  { id: "bridge", label: "RSS 桥接", hint: "由第三方桥接服务把 X 转成 RSS。" },
  { id: "feedgrab", label: "feedgrab(cookie)", hint: "用一份 X cookie 调 X 自己的接口。" },
  { id: "browser", label: "独立 profile 的登录态", hint: "在一个独立浏览器 profile 里登录一次。" },
  { id: "attach", label: "我日常用的浏览器", hint: "直连你正在用的那个 Chrome/Edge。" },
];
const LOGIN_CMD = "FEEDGRAB_DATA_DIR=/tmp/kb/x-feedgrab/sessions feedgrab login twitter";
// 「下一步怎么办」由后端给。有的通道要做的事没法塞进输入框(去地址栏勾一次远程
// 调试开关),所以 mock 也得按通道回答 —— 一律回空串的话"前端把它显示出来了没有"
// 在页面上压根看不出来。
// feedgrab 那档的第三项**就是那条命令**(真后端如此:待配置横幅那边没有可复制
// 块,所以命令必须从这里出来)。于是设置面板上它和 `login_command` 撞在一起 ——
// 下面那一项钉住前端不把同一句话并排写两遍。
const NEXT_STEP = { attach: "在浏览器地址栏打开 chrome://inspect/#remote-debugging，勾上那个开关。",
                    feedgrab: LOGIN_CMD };
let channel = "bridge";
let unconfigured = false;   // 第 9 节用:让这条订阅变成"待配置"
const saved = [];      // PUT /settings 真发出去的 body
const posted = [];     // 通道那两个 POST
let checkReply = { ready: true, detail: "登录态有效,首页读到 12 条推文" };

const settings = () => ({ enabled: false, interval_minutes: 1440, next_run: 0,
                          bridges: ["https://bridge.example/{handle}/rss"], x_channel: channel,
                          x_feedgrab_bin: "", x_chrome_path: "" });
const SOURCE = { id: 1, kind: "social", name: "Example", url: "https://x.com/example",
                 feed_url: "", group_name: "", enabled: 1, mode: "feed", config: "{}",
                 last_attempt: "", last_success: "", last_error: "", failures: 0,
                 retry_at: 0, etag: "", modified: "", resolved_url: "", history_floor: "",
                 items: 0, configuration_error: "" };
const MIME = { html: "text/html", js: "text/javascript", css: "text/css", json: "application/json",
               svg: "image/svg+xml", ico: "image/x-icon" };
const FIXTURES = {
  "/api/stats": { documents: 0, vectors: 0, entities: 0, extracted: 0 },
  "/api/facets": { sources: [], accounts: [], topics: [], entities: [] },
  "/api/projects": { tools: [], kinds: {}, total_entries: 0, topics: [] },
  "/api/favorites": { doc_paths: [], favorites: [], counts: { inbox: 0, done: 0, archived: 0 }, stale: 0 },
  "/api/models": { models: [], default: "" },
  "/api/keys": { keys: {} },
  "/api/stages": { stages: [], providers: [] },
  "/api/mcp/status": { config: "{}", command: "", tools: [], cli_available: false },
  "/api/conversations": { conversations: [] },
  "/api/pipeline/channels": { channels: CHANNELS.map(c => ({ id: c.id, label: c.label })),
                              default: "wechat_local", labels: {}, aliases: {} },
  "/api/pipeline/status": { running: false, stages: {}, log: [], last_run: null,
                            source: { online: true, ready: true } },
  "/api/pipeline/schedule": { enabled: false, time: "07:30", channel: "social", channels: [] },
  "/api/pipeline/accounts": { accounts: [] },
  "/api/sources/targets": { manageable: false, label: "公众号", targets: [] },
  "/api/wechat/setup": { state: "ready", ready: true, running: false, provider_ready: true,
                         models_ready: true, message: "隔离测试环境" },
  "/api/daily/window": { since: "", until: "" },
  "/api/daily/reports": { reports: [] },
  "/api/daily/status": { running: false, phase: "", channels: [] },
  "/api/intake/modules": { modules: [
    { id: "wechat", label: "公众号", description: "关注的公众号文章",
      channels: ["wechat_local"], manager: "wechat", icon: "chat", adapter: "rss",
      interval_minutes: 360, url_hint: "", note: "" },
    { id: "social", label: "大V观点", description: "关注的人，此刻的想法。",
      channels: ["social"], manager: "feeds", icon: "chat", adapter: "social",
      interval_minutes: 1440, url_hint: "输入 X / Twitter 账号主页", note: "三种取法" },
    { id: "rss", label: "RSS", description: "让值得读的文章，自己来到这里。",
      channels: ["rss"], manager: "feeds", icon: "rss", adapter: "rss",
      interval_minutes: 360, url_hint: "输入 RSS 地址", note: "" },
  ] },
};

const body = req => new Promise(res => { let raw = ""; req.on("data", c => { raw += c; });
                                         req.on("end", () => res(JSON.parse(raw || "{}"))); });
const server = createServer(async (req, res) => {
  const path = req.url.split("?")[0];
  const json = (data, code = 200) => {
    const buf = Buffer.from(JSON.stringify(data));
    res.writeHead(code, { "Content-Type": "application/json", "Content-Length": buf.length });
    res.end(buf);
  };
  const m = path.match(/^\/api\/intake\/(\w+)\/x-channel(\/\w+)?$/);
  if (m) {
    if (m[1] !== "social") return json({ detail: "该来源没有可选的采集通道" }, 404);
    // **按方法回答,和真后端一样**:这个 mock 原来只看 URL,于是「检查连接」发
    // 的是 GET 也照样返回成功 —— 真路由只收 POST,按钮在真页面上永远报 405,
    // 而这套探针全绿(2026-10-08 审查实测)。
    if (m[2] === "/login" || m[2] === "/check") {
      posted.push(`${req.method} ${path}`);
      if (req.method !== "POST") return json({ detail: "Method Not Allowed" }, 405);
      if (m[2] === "/login") {
        // **按存下来的那条通道回答,和真后端一样**:这个 mock 原来无条件返回成功,
        // 于是"改了下拉还没保存就点登录"这条路在页面上看起来永远是对的 —— 而真
        // 后端读的是 `reg.get_settings`,只会回一句"当前保存的取法是「RSS 桥接」",
        // 用户对着一个明明选着浏览器登录态的下拉收到这句拒绝(2026-10-09 实测)。
        if (channel === "attach")
          return json({ detail: "这一档用的是你日常浏览器里现成的登录态，不在这里登录。" }, 400);
        if (channel !== "browser")
          return json({ detail: `当前保存的取法是「${CHANNELS.find(c => c.id === channel).label}」，`
                                + "不在这里登录。" }, 400);
        return json({ ok: true, detail: "已打开登录窗口。登录成功后关掉它。" });
      }
      return json(checkReply);
    }
    const ready = channel === "bridge";
    const why = ready ? "" : (channel === "attach" ? "没有发现开着远程调试的浏览器"
                                                   : "浏览器通道还没有登录过 X");
    return json({ current: channel, channels: CHANNELS,
                  ready, why, hint: ready ? "" : (NEXT_STEP[channel] || ""),
                  login_command: channel === "feedgrab" ? LOGIN_CMD : "",
                  needs_login_window: channel === "browser" });
  }
  if (path.match(/^\/api\/intake\/\w+\/settings$/) && req.method === "PUT") {
    const sent = await body(req);
    saved.push(sent);
    if (sent.x_channel) channel = sent.x_channel;
    return json(settings());
  }
  if (path.match(/^\/api\/intake\/\w+\/items$/)) return json({ items: [], total: 0 });
  // modules 要排在 overview 那条之前:`/api/intake/<kind>` 的正则同样命中它,
  // 被它接走的话页面拿到的是一份 sources 为空的"概览",目录当场是空的。
  if (path === "/api/intake/modules") return json(FIXTURES[path]);
  if (path.match(/^\/api\/intake\/\w+$/)) {
    const kind = path.split("/").pop();
    const why = unconfigured && kind === "social"
      ? (channel === "attach" ? "待配置：没有发现开着远程调试的浏览器"
                              : "待配置：浏览器通道还没有登录过 X") : "";
    return json({ sources: kind === "social" ? [{ ...SOURCE, configuration_error: why }] : [],
                  settings: settings(), last_run: null, total: 0, enabled: 1,
                  ready: why ? 0 : 1, unconfigured: why ? 1 : 0, errors: 0 });
  }
  if (path.startsWith("/api/")) return json(FIXTURES[path] ?? {});
  const file = path === "/" ? "index.html" : path.replace(/^\/static\//, "");
  const target = normalize(join(STATIC, file));
  if (!target.startsWith(STATIC)) { res.writeHead(403); return res.end(); }
  try {
    const buf = readFileSync(target);
    res.writeHead(200, { "Content-Type": MIME[file.split(".").pop()] || "application/octet-stream",
                         "Content-Length": buf.length, "Cache-Control": "no-store" });
    res.end(buf);
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
  proc.stderr.on("data", d => { buf += d; const g = buf.match(/ws:\/\/127\.0\.0\.1:(\d+)\//); if (g) res(+g[1]); });
  proc.on("exit", c => rej(new Error(`exit ${c}\n${buf}`)));
  setTimeout(() => rej(new Error("no devtools port")), 20000);
});
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

// 1. 通道选择在大V观点的设置里,选项和当前值都来自后端
check("通道下拉出现在大V观点的设置里", await shown("#source-x-channel"));
check("四条通道都列出来,文案是后端给的",
      await evalJs(`[...document.querySelectorAll('#source-x-channel option')]
                      .map(o => o.value + ':' + o.textContent).join('|')`)
      === "bridge:RSS 桥接|feedgrab:feedgrab(cookie)|browser:独立 profile 的登录态"
          + "|attach:我日常用的浏览器",
      await evalJs(`[...document.querySelectorAll('#source-x-channel option')].map(o=>o.value).join()`));
check("当前选中的是后端说的那一条", await evalJs(`document.getElementById('source-x-channel').value`) === "bridge");
check("桥接那块可见、别的块都不可见",
      await shown("#source-bridge-field") && !await shown("#source-x-feedgrab")
      && !await shown("#source-x-browser"));
check("桥接地址照旧填进去",
      (await evalJs(`document.getElementById('source-bridges').value`)).includes("{handle}"));

// 2. 换成 feedgrab:要填的东西跟着换,登录命令照抄得出来
await evalJs(`document.getElementById('source-x-channel').value='feedgrab';
              document.getElementById('source-x-channel').dispatchEvent(new Event('change')); true`);
check("选 feedgrab 后只显示它那一块",
      await shown("#source-x-feedgrab") && !await shown("#source-bridge-field")
      && !await shown("#source-x-browser"));
check("改了选择不冒充当前事实、并写明要保存",
      (await evalJs(`document.getElementById('source-x-state').textContent`)).includes("保存"),
      await evalJs(`document.getElementById('source-x-state').textContent`));
check("改了还没保存时不给「检查连接」(否则检查的是旧通道)",
      !await shown("#source-x-check"));
// 保存成 feedgrab,看「下一步」那行:它的第三项就是上面那条可复制的命令
// (后端必须给 —— 待配置横幅那边没有可复制块),并排再写一遍只是噪音。
// **去重按内容判**,所以这一项不是"feedgrab 就不显示",而是"和上面那条一样就不显示"。
await clickAt("#source-settings-save");
await until(`xChan && xChan.current === 'feedgrab'`, 20000);
check("登录命令照抄得出来",
      (await evalJs(`document.getElementById('source-x-login').textContent`)).includes("feedgrab login"),
      await evalJs(`document.getElementById('source-x-login').textContent`));
check("同一条命令不在「下一步」那行再写一遍",
      (await evalJs(`document.getElementById('source-x-next').textContent`)) === "",
      await evalJs(`document.getElementById('source-x-next').textContent`));

// 3. 换成浏览器:那一块显示出来,但**没保存之前登录窗口打不开**
await evalJs(`document.getElementById('source-x-channel').value='browser';
              document.getElementById('source-x-channel').dispatchEvent(new Event('change')); true`);
check("选浏览器后只显示它那一块",
      await shown("#source-x-browser") && !await shown("#source-bridge-field")
      && !await shown("#source-x-feedgrab"));
// 后端那条路由读的是**存下来**的通道(`reg.get_settings`),改了下拉还没保存时
// 它只会回一句"当前保存的取法是「RSS 桥接」" —— 用户对着一个明明选着「我的浏览器
// 登录态」的下拉收到这句拒绝,看不出差在哪一步(2026-10-09 实测)。
check("改了还没保存时「打开登录窗口」是禁用的",
      await evalJs(`document.getElementById('source-x-login-btn').disabled`) === true);
check("禁用了也仍然看得见(那段说明指的就是它,藏掉说明就悬空了)",
      await shown("#source-x-login-btn"));
const beforeBlocked = posted.length;
// **真鼠标点**:disabled 上直接调 `.click()` 本来就不触发,证明不了页面上点下去会怎样。
await clickAt("#source-x-login-btn");
await sleep(500);
check("点下去一个请求都没发出去", posted.length === beforeBlocked,
      posted.slice(beforeBlocked).join());
check("页面上不出现那句自相矛盾的拒绝",
      !(await evalJs(`document.querySelector('#toast-host')?.textContent || ''`)).includes("打不开登录窗口"),
      await evalJs(`document.querySelector('#toast-host')?.textContent || ''`));

// 4. 保存设置:三个字段真的发出去了
await evalJs(`document.getElementById('source-x-chrome').value =
                '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'; true`);
await evalJs(`document.getElementById('source-settings-form')
                .dispatchEvent(new Event('submit', {cancelable:true})); true`);
await until(`xChan && xChan.current === 'browser'`, 15000);
const sent = saved[saved.length - 1] || {};
check("保存时带上了通道选择", sent.x_channel === "browser", JSON.stringify(sent));
check("保存时带上了 Chrome 路径",
      (sent.x_chrome_path || "").endsWith("Google Chrome"), JSON.stringify(sent.x_chrome_path));
check("桥接地址仍然一起保存(换通道不该把它丢了)",
      Array.isArray(sent.bridges) && sent.bridges.length === 1, JSON.stringify(sent.bridges));
check("保存之后这条就是当前通道、「检查连接」回来了",
      await evalJs(`xChan && xChan.current === 'browser'`) && await shown("#source-x-check"));

// 4.5 保存之后登录窗口才打得开 —— 反过来钉一边:上面那道闸不是"永远点不了"
check("保存之后「打开登录窗口」不再是禁用的",
      await evalJs(`document.getElementById('source-x-login-btn').disabled`) === false);
check("「打开登录窗口」真点得到", await clickAt("#source-x-login-btn"));
await until(`document.querySelector('#toast-host')?.textContent.includes('已打开')`, 8000);
check("登录请求打的是 login 端点,而且是 POST",
      posted.includes("POST /api/intake/social/x-channel/login"), posted.join());
check("登录结果如实显示在提示里",
      (await evalJs(`document.querySelector('#toast-host')?.textContent || ''`)).includes("关掉它"),
      await evalJs(`document.querySelector('#toast-host')?.textContent || ''`));

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
checkReply = { ready: false, detail: "X 登录态已失效,点「打开登录窗口」重新登录一次" };
await clickAt("#source-x-check");
await until(`document.getElementById('source-x-state').textContent.includes('已失效')`, 20000);
check("失败的检查如实写出原因",
      (await evalJs(`document.getElementById('source-x-state').textContent`)).includes("已失效"),
      await evalJs(`document.getElementById('source-x-state').textContent`));

// 9. 待配置横幅说的必须是**当前通道**缺什么,不是永远那句"请填写 X 桥接"
unconfigured = true;
await evalJs(`loadSourceModule(true)`);
await until(`!document.getElementById('source-configuration').hidden`, 15000);
const banner = await evalJs(`document.getElementById('source-configuration').textContent`);
check("待配置横幅按当前通道说话", banner.includes("登录") && !banner.includes("桥接"), banner);
unconfigured = false;
await evalJs(`loadSourceModule(true)`);
await until(`document.getElementById('source-configuration').hidden`, 15000);

// 10. 第四条通道「我日常用的浏览器」—— 它一个输入框都不要,所以这一节同时是
// 「新增一条通道不用改这个 JS」那条口径的回归:前端只按后端给的 id 决定显示
// 哪一块,没有自己那一块的通道就一块都不显示,下一步从 `hint` 那行出来。
// 这里**不要**再点一次来源切换器:上一节结束时就在这个视图里,再点一次会重新
// 拉一遍通道清单,而 `loadXChannel` 末尾那句 `sel.value = data.current` 会把下面
// 这次赋值盖回去 —— 下拉看着是 attach 之前那一档,十条断言一起红。
await evalJs(`document.getElementById('source-x-channel').value='attach';
              document.getElementById('source-x-channel').dispatchEvent(new Event('change')); true`);
check("下拉真的切到这一档了(摆盘自检)",
      await evalJs(`document.getElementById('source-x-channel').value`) === "attach",
      await evalJs(`document.getElementById('source-x-channel').value`));
check("选它之后一个输入块都不显示",
      !await shown("#source-bridge-field") && !await shown("#source-x-feedgrab")
      && !await shown("#source-x-browser"));
// **逐字比后端给的那条**:前端不许自己攒一句说明,这一档的全部文案都来自
// `/x-channel` 的 `channels[].hint`。
check("它自己的说明逐字来自后端",
      await evalJs(`document.getElementById('source-x-hint').textContent`)
      === CHANNELS.find(c => c.id === "attach").hint,
      await evalJs(`document.getElementById('source-x-hint').textContent`));
check("改了还没保存时「下一步」是空的(那是当前通道的事实)",
      (await evalJs(`document.getElementById('source-x-next').textContent`)) === "",
      await evalJs(`document.getElementById('source-x-next').textContent`));
check("改了还没保存时不给「检查连接」", !await shown("#source-x-check"));
// 保存:这一档没有要填的东西,保存之后它就是当前通道
await clickAt("#source-settings-save");
await until(`xChan && xChan.current === 'attach'`, 20000);
check("保存真把这一档发出去了",
      saved[saved.length - 1]?.x_channel === "attach",
      JSON.stringify(saved[saved.length - 1] || {}));
check("保存之后「检查连接」回来了", await shown("#source-x-check"));
check("「下一步怎么办」真的显示出来了(后端 readiness 的第三项)",
      (await evalJs(`document.getElementById('source-x-next').textContent`))
        .includes("chrome://inspect"),
      await evalJs(`document.getElementById('source-x-next').textContent`));
check("状态行说的是这一档缺什么,不是桥接",
      (await evalJs(`document.getElementById('source-x-state').textContent`)).includes("远程调试"),
      await evalJs(`document.getElementById('source-x-state').textContent`));
check("「打开登录窗口」在这一档上压根不出现(它属于独立 profile 那一块)",
      !await shown("#source-x-login-btn"));
// 这一档的拒绝文案不许建议他换档 —— 他要的登录态就在自己那个浏览器里。
// 按钮看不见,所以走 JS 入口兜底那条路(真实来法:键盘/脚本)。
await evalJs(`openXLoginWindow(); true`);
await until(`document.querySelector('#toast-host')?.textContent.includes('不在这里登录')`, 8000);
const attachRefusal = await evalJs(`document.querySelector('#toast-host')?.textContent || ''`);
check("拒绝文案说清在哪儿登录,而且不叫他改档",
      attachRefusal.includes("现成的登录态") && !attachRefusal.includes("保存设置"),
      attachRefusal);
// 待配置横幅同理按这一档说话
unconfigured = true;
await evalJs(`loadSourceModule(true)`);
await until(`!document.getElementById('source-configuration').hidden`, 15000);
const attachBanner = await evalJs(`document.getElementById('source-configuration').textContent`);
check("待配置横幅按这一档说话,不提桥接也不提登录窗口",
      attachBanner.includes("远程调试") && !attachBanner.includes("桥接"), attachBanner);
unconfigured = false;
await evalJs(`loadSourceModule(true)`);
await until(`document.getElementById('source-configuration').hidden`, 15000);

// 7. RSS 视图上整块不出现 —— 通道是 X 的事
await clickAt("#subs-switch-rss");
// 等 `sourceData` 到位,不是只等 sourceKind:后者在发请求**之前**就同步赋值了,
// 拿它当信号会在 fillSourceSettings 还没跑的时候就去量这一块的可见性。
await until(`sourceKind === 'rss' && sourceData && sourceData.settings
             && !('x_channel' in sourceData.settings)`, 20000);
check("RSS 的设置里没有通道那一块", !await shown("#source-xchan"));
check("RSS 也没有桥接输入框", !await shown("#source-bridge-field"));

// 8. 原生弹窗一个都不许有(项目硬性约定:它们跟这套配色不搭,还阻塞主线程)
check("整轮没有调过原生 alert/confirm/prompt",
      (await evalJs(`JSON.stringify(window.__nativeDialogs || [])`)) === "[]",
      await evalJs(`JSON.stringify(window.__nativeDialogs || [])`));

check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 400));
const out = [...oks.map(n => `  ok ${n}`), ...fails.map(n => `  FAIL ${n}`)].join("\n");
process.stdout.write(`${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`);
ws.close(); proc.kill(); server.close();
process.exitCode = fails.length ? 1 : 0;
