// 来源面板上「翻译积压那行提示 + 自己动手的入口」在**真页面**上的回归
// (后端口径在 kb/feeds/translate.py 的 manual_sql / stuck / requeue_failed)。
//
// 这颗按钮解除的是一个原来**只能靠手改 feeds.db 才解除得了**的终态:试满
// MAX_TRIES 的条目按设计离开翻译队列、按原文导出,于是 vault 里躺着英文,而
// `queued_sql` 的 `translate_tries<MAX_TRIES` 让「同步」和「重试失败项」都
// 选不到它们(2026-10-13 实测:中转站一轮 503 造出 rss 42 条 + social 66 条)。
//
// 断言一律**先验后果**:「页面上那句话有没有说出已入库那一档」「点下去真的发了
// 什么请求、带了什么射程」排在「DOM 里有没有那个按钮」前面 —— 只验元素在不在的
// 话,一个按钮点了不发请求、或者发给整个来源的实现也能全绿。
// 另外**反过来钉几边**:①没有可用翻译模型时不许给按钮(那是在承诺一件做不到的
// 事);②一条都不缺时整句不许出现;③筛到某条订阅时请求的射程要跟着收 ——
// 不然用户看着「1 条」点下去,放回队列的是整个来源的上百条。
//
// 另外三组是审查打回来的(自由工作流第 1 轮):
//   ④ **没失败过的普通积压也要有入口** —— 定时同步默认关着,不给按钮的话那批
//      内容在系统里没有别的办法动起来(用户原话:"脱离了 ai 我都没法重新翻译");
//   ⑤ **那一轮跑完要自己把页面读新**,而且筛选、已加载条数、阅读位置都留着 ——
//      这条路不跑采集,`sources` 和 `total` 一个都不变,原来两个判据对它全是瞎的;
//   ⑥ **提示不许跟着列表空态一起消失** —— 它的射程只跟订阅走,搜索词只决定列
//      出哪几条,搜一个恰好不中的词时那句话说的事实一个字都没变。
//
// 自带 mock 后端,CDP 直连 playwright 缓存里的 chrome-headless-shell
// (不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/translate_manual_retry_probe.mjs
import { createServer } from "node:http";
import { readFileSync, writeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { launchShell } from "./probe_shell.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
const REASON = "Claude 调用失败: Error code: 503 - service unavailable: no available account";

const item = (key, title, extra = {}) => ({
  key, title, body: "Some English body text here.", author: "Feed",
  url: "https://example.org/" + key, published: "2026-10-11 09:30",
  content_kind: "content", title_zh: "", body_zh: "", lang: "en", ...extra });

// 45 条:默认一屏 30 条,点一次「加载更多」到 45 —— ⑤ 那条"已加载条数要留着"
// 只有在它和默认值不同的时候才钉得住。
const ALL = Array.from({ length: 45 },
                       (_, i) => item("k" + i, `English post #${i}`, { translating: i < 3 }));
// `shipped`(真落了盘的)和 `exhausted`(试满额度、离开了队列的)**故意摆成不同
// 的数**:射程必须按后者算(没落盘的照样会被放回队列),而那句「已按原文写进
// 知识库」只能用前者。两个数字一样的话,前端错用哪一个都看不出来。
const STUCK = { failed: 108, shipped: 60, exhausted: 66, queued: 180, reason: REASON };
let mode = "all";
// 摆盘靠 query 切换。`queued` 是「还排在队列里的」(不含试满次数的那批),
// `shipped` 是「已按原文入库」—— 两段互不重叠,按钮的射程是它们的和。
const MODES = {
  all:    { stuck: STUCK, translate_ready: true, translating: 3 },
  // 筛到一条订阅:数字跟着那条订阅收窄(后端 `_retry_where` 那一半)
  one:    { stuck: { failed: 3, shipped: 2, exhausted: 2, queued: 1, reason: REASON },
            translate_ready: true, translating: 1 },
  // ④ 没失败过、只是还没排到:照样要有入口
  plain:  { stuck: { failed: 0, shipped: 0, exhausted: 0, queued: 18, reason: "" },
            translate_ready: true, translating: 18 },
  // 一条都不缺:整句和按钮都不许出现
  clean:  { stuck: { failed: 0, shipped: 0, exhausted: 0, queued: 0, reason: "" },
            translate_ready: true, translating: 0 },
  nokey:  { stuck: STUCK, translate_ready: false, translating: 0 },
  // ⑥ 搜索零结果(列表空了,但库里那些事实一个字没变)
  empty:  { stuck: STUCK, translate_ready: true, translating: 0, blank: true },
};

const overview = kind => ({
  sources: [{ id: 7, name: "测试订阅", url: "https://example.org/f.xml", group_name: "测试",
              enabled: 1, item_count: 45, last_success: "2026-10-13 10:47", last_error: "",
              last_note: "", configuration_error: "", retry_at: 0, fulltext: 0, extract: 0 }],
  settings: { enabled: false, interval_minutes: 1440, bridges: [], next_run: 0 },
  last_run: null, total: 45, enabled: 1, ready: 1, unconfigured: 0, errors: 0, kind,
});
// 管道状态也要能变:⑤ 那组要让页面看到「一轮带着本来源的运行结束了」。
const PIPE = {
  idle:    { running: false, stages: {}, source: { online: true, ready: true }, log: [], last_run: null },
  running: { running: true, run_id: "run-42", current_stage: "export",
             crawl_filter: { channel: "rss", translate_retry: { channel: "rss", source_id: null } },
             stages: { export: { status: "running", detail: "正在翻译 40 条" } },
             source: { online: true, ready: true }, log: [], last_run: null },
  done:    { running: false, run_id: "run-42", current_stage: "extract",
             crawl_filter: { channel: "rss", translate_retry: { channel: "rss", source_id: null } },
             stages: { export: { status: "ok", detail: "已译 40 条" } },
             source: { online: true, ready: true }, log: [], last_run: null },
};
let pipeMode = "idle";
const FIXTURES = {
  "/api/stats": { documents: 0, vectors: 0, entities: 0, extracted: 0 },
  "/api/facets": { sources: [], accounts: [], topics: [], entities: [] },
  "/api/projects": { tools: [], kinds: {}, total_entries: 0, topics: [] },
  "/api/favorites": { doc_paths: [], favorites: [], counts: { inbox: 0, done: 0, archived: 0 }, stale: 0 },
  "/api/models": { models: [], default: "" },
  "/api/keys": { keys: {} }, "/api/stages": { stages: [], providers: [] },
  "/api/mcp/status": { config: "{}", command: "", tools: [], cli_available: false },
  "/api/conversations": { conversations: [] },
  "/api/intake/modules": { modules: [
    { id: "rss", label: "RSS", description: "让值得读的文章，自己来到这里。", note: "",
      channels: ["rss"], manager: "feeds", icon: "rss" }] },
  "/api/pipeline/channels": { channels: [{ id: "rss", label: "RSS", hint: "" }],
                              default: "rss", labels: { rss: "RSS" }, aliases: {} },
  "/api/pipeline/schedule": { enabled: false, time: "07:30", channel: "rss", channels: ["rss"] },
  "/api/daily/window": { since: "", until: "" }, "/api/daily/reports": { reports: [] },
  "/api/daily/status": { running: false, phase: "", channels: [] },
};

const posted = [];      // 真实请求留痕:射程是不是跟着筛选收窄,只能看这里
const itemReqs = [];    // ⑤ 刷新那一次带了多大的 limit、筛选还在不在
const MIME = { html: "text/html", js: "text/javascript", css: "text/css", json: "application/json",
               svg: "image/svg+xml", ico: "image/x-icon" };
const server = createServer((req, res) => {
  const url = new URL(req.url, "http://probe.invalid");
  const path = url.pathname;
  const json = (data, code = 200) => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(code, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  // **任何来源的这个端点都记一笔,不只 rss 那一条。** 只认 rss 的话
  // 「确认期间换了来源」那条检查是假绿:请求跟着 `sourceKind` 打到
  // `/api/intake/social/translate-retry` 去了,这里看不见,于是
  // `posted.length === 0` 成立而请求其实发出去了(实测:退回「确认之后才读」
  // 的写法时那条检查照旧是绿的)。判据是「一个请求都没发」,那就得真的全看。
  if (path.endsWith("/translate-retry")) {
    let raw = "";
    req.on("data", c => { raw += c; });
    return req.on("end", () => {
      posted.push({ path, body: raw });
      json({ ok: true, pending: 246,
             detail: "共 246 条，这一轮先译 40 条，其余留到下一轮，译完会自动写入知识库。" });
    });
  }
  if (path === "/api/intake/rss/items") {
    const q = url.searchParams;
    itemReqs.push({ limit: Number(q.get("limit") || 30), offset: Number(q.get("offset") || 0),
                    q: q.get("q") || "", source_id: q.get("source_id") || "" });
    const { blank, ...rest } = MODES[mode];
    const off = Number(q.get("offset") || 0), lim = Number(q.get("limit") || 30);
    return json(blank ? { items: [], total: 0, offset: off, fetching: 0, ...rest }
                      : { items: ALL.slice(off, off + lim), total: ALL.length,
                          offset: off, fetching: 0, ...rest });
  }
  if (path === "/api/pipeline/status") return json(PIPE[pipeMode]);
  if (path === "/api/intake/rss") return json(overview("rss"));
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
const { proc, port } = await launchShell(["--window-size=1440,1000"]);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));
let msgId = 0; const pend = new Map(); const pageErrors = []; const dialogs = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    pageErrors.push(m.params.args.map(a => a.value ?? a.description ?? "").join(" "));
  // 原生弹窗是硬性禁止的(见 CLAUDE.md「前端反馈」),出现一次就记下来
  if (m.method === "Page.javascriptDialogOpening") dialogs.push(m.params.message);
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
const until = async (expr, ms = 15000) => {
  const dl = Date.now() + ms;
  while (Date.now() < dl) { if (await evalJs(expr)) return true; await sleep(120); }
  return false;
};
// 真鼠标点在元素中心:要证明的是"页面上点下去真的会发生",不是"函数调得通"
const clickAt = async sel => {
  const box = await evalJs(`(() => { const e = document.querySelector(${JSON.stringify(sel)});
    if (!e) return null; e.scrollIntoView({block:'center'}); const r = e.getBoundingClientRect();
    return r.width && r.height ? JSON.stringify({x:r.left+r.width/2, y:r.top+r.height/2}) : null; })()`);
  if (!box) return false;
  const { x, y } = JSON.parse(box);
  for (const type of ["mousePressed", "mouseReleased"])
    await send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 });
  return true;
};
const noteText = () => evalJs(`document.querySelector('#source-list .source-note')?.textContent || ''`);
const hasButton = () => evalJs(`!!document.querySelector('[data-retry-translate]')`);
await send("Runtime.enable"); await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride",
           { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });

const results = [];
const check = (name, ok, detail = "") => results.push({ name, ok: !!ok, detail: String(detail) });
const reload = async m => {
  mode = m;
  await evalJs(`renderSourceView(true)`);
  await until(`!!document.querySelector('#source-list .source-note, #source-list .source-empty')`);
  await sleep(80);
};

await send("Page.navigate", { url: base + "/" });
if (!await until(`typeof openSubsPanel === 'function' && typeof retryTranslate === 'function'`, 25000))
  throw new Error("页面没起来");
await evalJs(`localStorage.clear(); switchTab('subs'); openSubsPanel('rss')`);
if (!await until(`sourceKind === 'rss'
      && document.querySelectorAll('#source-list .source-entry').length === 30`))
  throw new Error("条目没渲染出来");

// ── 1. 后果:那句话把三件事都说出来了 ─────────────────────────────────────────
// 原来这一档和「还在等翻译」共用同一句话,于是「试过了没成功」和「还没轮到」
// 在面板上长得一模一样,而两者的下一步完全相反。
let note = await noteText();
check("说出了译不动的条数", note.includes("108 条外文内容翻译失败"), note.slice(0, 160));
check("说出了已按原文入库那一档(它不在「还在等翻译」的计数里)",
      note.includes("60 条已按原文写进知识库"), note.slice(0, 200));
// 「不再自动重试」(66)和「真的写进了知识库」(60)是两个数:这句话说的是后者,
// 而按钮的射程按前者算(没落盘的照样会被放回队列)。
check("那句话说的是真落了盘的条数,不是「不再自动重试」的条数",
      !note.includes("66 条已按原文"), note.slice(0, 200));
check("说出了最近一次失败的原因", note.includes("no available account"), note.slice(0, 240));
// 射程 = 还排着队的 180 + 试满额度的 66。按 `shipped`(60)算会得到 240 ——
// 那就又变成"说一个数、动另一批"。
check("说出了按钮真会动多少条(失败的 108 ≠ 射程 246)",
      note.includes("共 246 条等着译"), note.slice(0, 260));
check("「还在等翻译」那句照旧单独说它自己那一档",
      note.includes("3 条外文内容还在等翻译"), note.slice(0, 120));
check("两句各占一行(下一步相反,挤一行读起来像同一件事)",
      await evalJs(`getComputedStyle(document.querySelector('.source-note-line')).display === 'block'`));

// ── 2. 后果:按钮点下去真的发了请求,而且没有原生弹窗 ─────────────────────────
check("提示里有「重新翻译」按钮",
      (await evalJs(`document.querySelector('[data-retry-translate]')?.textContent || ''`)) === "重新翻译");
check("按钮点得着(有实际尺寸、不是零宽)", await clickAt("[data-retry-translate]"));
check("确认框是自绘的,不是原生 confirm",
      await until(`!!document.querySelector('.confirm-mask [data-act="ok"]')`) && dialogs.length === 0,
      dialogs.join(" | "));
const ask = await evalJs(`(() => { const m = document.querySelector('.confirm-mask');
  return (m?.querySelector('h2')?.textContent || '') + ' ‖ ' + (m?.querySelector('.confirm-body')?.textContent || ''); })()`);
// 这颗按钮会真的调模型花钱,而数量可能是上百条:确认框必须把代价说清楚。
check("确认框说的条数是真会动的那些(246,不是失败的 108)", ask.includes("246 条"), ask);
check("确认框说清了会调模型(按字数计费)", ask.includes("计费"), ask);
check("确认框说清了这一轮只译得完一截", ask.includes("一轮译不完"), ask);
check("确认框说清了译完会写进知识库", ask.includes("写进知识库"), ask);
await clickAt('.confirm-mask [data-act="ok"]');
check("点确认之后真的发了 POST", await (async () => {
  for (let i = 0; i < 40 && !posted.length; i++) await sleep(120);
  return posted.length === 1 && posted[0].path.endsWith("/translate-retry");
})(), JSON.stringify(posted));
// 没筛订阅时整个来源都算射程:body 里不许带 source_id(带了就是把射程写死成某一条)
check("没筛订阅时请求不带 source_id", posted[0] && !("source_id" in JSON.parse(posted[0].body || "{}")),
      posted[0]?.body);
check("成功之后给的是 ok 的轻提示,文案是后端那句",
      await until(`!!document.querySelector('.toast.ok')`)
      && (await evalJs(`document.querySelector('.toast.ok')?.textContent || ''`)).includes("这一轮先译 40 条"));

// ── 3. 反向钉边一:筛到一条订阅,数字和射程一起收窄 ───────────────────────────
posted.length = 0;
await evalJs(`$('source-filter').value = '7'`);
await reload("one");
note = await noteText();
check("筛到订阅时那句话改口说「这条订阅」", note.includes("这条订阅有 3 条"), note.slice(0, 140));
await clickAt("[data-retry-translate]");
await until(`!!document.querySelector('.confirm-mask [data-act="ok"]')`);
check("确认框里的条数也跟着收窄",
      (await evalJs(`document.querySelector('.confirm-mask h2')?.textContent || ''`)).includes("3 条"));
await clickAt('.confirm-mask [data-act="ok"]');
for (let i = 0; i < 40 && !posted.length; i++) await sleep(120);
check("请求的射程跟着筛选收窄(带上了那条订阅的 id)",
      posted[0] && JSON.parse(posted[0].body || "{}").source_id === 7, posted[0]?.body);

// ── 3.5 确认期间范围漂了:一个请求都不许发出去 ───────────────────────────────
// 确认框是一个 await。它开着的那十几秒里面板自己会刷新一轮,而**被删掉的订阅
// 会让筛选值回退成空**(`loadSourceModule` 重建 option 那一句),空在后端正好是
// 整个来源 —— 于是用户确认「这条订阅的 3 条」,发出去的是整个来源
// (2026-10-14 第 4 轮审查实测:确认 1 条、实际译了 2 条,多出来那条属于他压根
// 没选的订阅)。判据落在**请求上**:一个都没发,而不是"少发了点什么"。
posted.length = 0;
await evalJs(`$('source-filter').value = '7'`);
await reload("one");
await clickAt("[data-retry-translate]");
await until(`!!document.querySelector('.confirm-mask [data-act="ok"]')`);
check("(前提)确认框说的是那条订阅的条数",
      (await evalJs(`document.querySelector('.confirm-mask h2')?.textContent || ''`)).includes("3 条"));
// 原样照抄 `loadSourceModule` 那一句的后果:认不出的订阅 id 被顶回空串
await evalJs(`$('source-filter').value = ''`);
await clickAt('.confirm-mask [data-act="ok"]');
await sleep(600);                       // 给它充分的时间把请求发出去
check("确认期间订阅被删掉:一个请求都不发(不许降级成整个来源)",
      posted.length === 0, JSON.stringify(posted));
check("而且说清了为什么没开始",
      await until(`!!document.querySelector('.toast.err')`)
      && (await evalJs(`document.querySelector('.toast.err')?.textContent || ''`)).includes("确认期间"));
// 切来源 tab 同理:`sourceKind` 决定这个请求打到哪条通道上,它也得在确认之前定死
posted.length = 0;
await evalJs(`$('source-filter').value = '7'`);
await reload("one");
await clickAt("[data-retry-translate]");
await until(`!!document.querySelector('.confirm-mask [data-act="ok"]')`);
await evalJs(`sourceKind = 'social'`);
await clickAt('.confirm-mask [data-act="ok"]');
await sleep(600);
check("确认期间换了来源:同样一个请求都不发", posted.length === 0, JSON.stringify(posted));
await evalJs(`sourceKind = 'rss'`);
// **反过来钉一边**:范围一个字没变的那一次照旧发得出去(上面第 3 组那条检查
// 已经钉住带 id 的情形,这里再钉一次「没筛订阅」那一档不被这道闸误伤)。
posted.length = 0;
await evalJs(`$('source-filter').value = ''`);
await reload("all");
await clickAt("[data-retry-translate]");
await until(`!!document.querySelector('.confirm-mask [data-act="ok"]')`);
await clickAt('.confirm-mask [data-act="ok"]');
check("范围没变那一次照旧发得出去", await (async () => {
  for (let i = 0; i < 40 && !posted.length; i++) await sleep(120);
  return posted.length === 1 && !("source_id" in JSON.parse(posted[0].body || "{}"));
})(), JSON.stringify(posted));

// ── 4. ④ 没失败过的普通积压也要有入口 ───────────────────────────────────────
// 定时同步默认关着,不给按钮的话那 18 条在系统里没有别的办法动起来 —— 而原来
// 这一档只有一句"还在等翻译:译成中文之后才写进知识库",读完无事可做。
posted.length = 0;
await evalJs(`$('source-filter').value = ''`);
await reload("plain");
note = await noteText();
check("没失败过时不说「翻译失败」(那是另一档)", !note.includes("翻译失败"), note.slice(0, 160));
check("但照样说出这个范围共有多少条等着译", note.includes("共 18 条等着译"), note.slice(0, 160));
check("按钮在,而且叫「现在就译」而不是「重新翻译」",
      (await evalJs(`document.querySelector('[data-retry-translate]')?.textContent || ''`)) === "现在就译");
await clickAt("[data-retry-translate]");
await until(`!!document.querySelector('.confirm-mask [data-act="ok"]')`);
check("确认框说的是这 18 条",
      (await evalJs(`document.querySelector('.confirm-mask h2')?.textContent || ''`)).includes("18 条"));
await clickAt('.confirm-mask [data-act="ok"]');
for (let i = 0; i < 40 && !posted.length; i++) await sleep(120);
check("普通积压那颗按钮打的是同一个端点",
      posted[0]?.path.endsWith("/translate-retry"), JSON.stringify(posted));

// ── 5. ⑤ 那一轮跑完,页面自己读新;筛选、已加载条数、阅读位置都留着 ──────────
// 这条路**不跑采集**:条目一条没新增(`total` 不变)、订阅行的 `last_success`
// 也不动,于是「比 sources」「比 total」两个判据对它全是瞎的。
await reload("all");
await evalJs(`$('source-q').value = ''; $('source-filter').value = '7'`);
await evalJs(`moreSourceItems()`);
await until(`sourceRows.length === 45`);
await evalJs(`$('source-list-pane').scrollTop = 420`);
// 无头浏览器把后台页面的定时器节流到分钟级,所以自己驱动一次轮询 —— 验的是
// "轮询到了之后它自己决定重渲染",不是手动调 renderSourceView。
pipeMode = "running";
await evalJs(`refreshPipe()`);
await until(`pipeSnapshot?.running === true`);
await evalJs(`loadSourceModule(false)`);
pipeMode = "done";
mode = "clean";                                   // 译完了:库里那些积压清零
await evalJs(`refreshPipe()`);
await until(`pipeSnapshot?.running === false && pipeSnapshot?.run_id === 'run-42'`);
itemReqs.length = 0;
await evalJs(`loadSourceModule(false)`);
check("跑完之后那句提示自己没了(没有手动重渲染)",
      await until(`!(document.querySelector('#source-list .source-note')?.textContent || '').includes('翻译失败')`),
      await noteText());
const refetch = itemReqs[itemReqs.length - 1] || {};
check("重读时筛选留着(请求还带着那条订阅的 id)", refetch.source_id === "7", JSON.stringify(refetch));
check("重读时已加载的 45 条一起读回来(没缩回默认一屏 30 条)",
      refetch.limit === 45, JSON.stringify(refetch));
check("重读时阅读位置没被重置",
      await evalJs(`$('source-list-pane').scrollTop > 0`),
      await evalJs(`String($('source-list-pane').scrollTop)`));
check("同一轮运行不会被重复认成「刚跑完」",
      await (async () => { itemReqs.length = 0; await evalJs(`loadSourceModule(false)`);
                           await sleep(300); return itemReqs.length === 0; })(),
      JSON.stringify(itemReqs));

// ── 6. ⑥ 搜索零结果时,提示和按钮不许跟着列表一起消失 ────────────────────────
await evalJs(`$('source-filter').value = ''`);
await reload("empty");
note = await noteText();
check("列表空了,但那句提示还在", note.includes("108 条外文内容翻译失败"), note.slice(0, 160));
check("按钮也还在(它的射程只跟订阅走,跟搜索词无关)", await hasButton());
check("空态本身照旧显示", await evalJs(`!!document.querySelector('#source-list .source-empty')`));

// ── 7. 反向钉边二:没有可用翻译模型时不许给按钮 ──────────────────────────────
// 点下去什么也不会翻(后端那道闸也会拒),按钮摆在那里是在承诺做不到的事。
await reload("nokey");
note = await noteText();
check("没配模型时照旧说出有多少条译不动的", note.includes("108 条外文内容翻译失败"), note.slice(0, 140));
check("没配模型时不给按钮", !await hasButton());
check("没配模型时给的是「去哪儿配」这个下一步", note.includes("顶栏「模型」"), note.slice(0, 240));

// ── 8. 反向钉边三:一条都不缺时整句不出现 ────────────────────────────────────
await reload("clean");
note = await noteText();
check("一条都不缺时整句不出现", !note.includes("等着译") && !note.includes("翻译失败"), note.slice(0, 140));
check("这时也没有按钮", !await hasButton());

check("整轮没有原生 alert/confirm/prompt", dialogs.length === 0, dialogs.join(" | "));
check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.slice(0, 2).join(" | "));

ws.close(); proc.kill(); server.close();
// 收尾一律 writeSync、不用 process.exit:管道上的写是异步的,exit() 会把没刷出去
// 的那部分丢掉;而 writeSync 自己要顶住短写和 EAGAIN(fd 是非阻塞的)。
function out(fd, text) {
  let buf = Buffer.from(text, "utf8");
  while (buf.length) {
    try { buf = buf.subarray(writeSync(fd, buf)); }
    catch (e) { if (e.code !== "EAGAIN") throw e;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2); }
  }
}
const bad = results.filter(r => !r.ok);
for (const r of results)
  out(2, `${r.ok ? "  ok" : "FAIL"}  ${r.name}${r.detail ? " — " + r.detail : ""}\n`);
out(2, `${results.length - bad.length} ok, ${bad.length} failed\n`);
process.exitCode = bad.length ? 1 : 0;
