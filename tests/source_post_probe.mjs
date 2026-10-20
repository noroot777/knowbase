// 「大V观点的条目不显示标题」在**真页面**上的回归。
//
// 采集器把推文正文的前 500 字符原样当成 title(`x_browser._entry` /
// `x_feedgrab._entry`;走 HTTP 的那条(专用订阅链接)走 `fetching.parse_feed`,源没给标题时回退成正文
// 首行),所以来源面板上那行大标题和紧跟着的正文是同一句话显示两遍 —— 而标题那个
// <button> 继承了 base.css 的 `white-space: nowrap`,长推文一行到底、直接撑出卡片
// 右边界(用户截图里红框圈的就是这一段)。
//
// 断言一律**先验后果**:「有没有东西画到卡片外面」「正文一个字没少」排在
// 「DOM 里还有没有那个元素」前面 —— 只验元素在不在的话,一个把标题藏起来却仍然
// 撑开卡片的实现(visibility/opacity)也能全绿。
// 另外**反过来钉一边**:RSS 照旧有标题、照旧可点、正文照旧是 muted 的摘要样式,
// 不然「一律不显示标题」或者「一律提成主体」这两种过头的实现看不出来。
//
// 自带 mock 后端,CDP 直连 playwright 缓存里的 chrome-headless-shell
// (不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/source_post_probe.mjs
import { createServer } from "node:http";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { launchShell } from "./probe_shell.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");

// ── 样本 ─────────────────────────────────────────────────────────────────────
// 用户截图里那条:正文四段,title 是采集器截出来的那 500 字符(这里整条都在 500 内,
// 所以 title 逐字等于正文 —— 面板上就是同一句话连着显示两遍)。
const TWEET = ["哇，好友余一也从鹅厂出来，加入 Manus 。",
               "记得还是去年初，余一喊我和老闫参加线上直播聊 GEO，那时真正关心 GEO 的人还不多。",
               "另外预告下，余一也会参加11 月的深圳 GEO 大会做分享嘉宾。",
               "这是她 X 账号，我猜以后会很活跃，推荐关注。"].join("\n\n");
const SHORT = "补充一个有趣的玩法：在我的一加 15 手机上装好 Paseo。";
// 中文标题里没有空格,`white-space: nowrap` 之下它一个字都断不开。
const LONG_TITLE = "为什么说检索增强这条路走到今天已经不是把文档塞进上下文那么简单了"
  + "——从分块策略到重排模型再到评测集，一篇长文讲清楚每一步的取舍和踩过的坑";

const LONG_TITLE_LEN = LONG_TITLE.length;
const post = (key, body, extra = {}) => ({
  key, title: body.slice(0, 500), body, author: "向阳乔木",
  url: "https://example.org/x/" + key, published: "2026-10-09 14:58",
  content_kind: "post", title_zh: "", body_zh: "", lang: "zh", ...extra });
const article = (key, title, body) => ({
  key, title, body, author: "测试作者", url: "https://example.org/rss/" + key,
  published: "2026-10-09 09:30", content_kind: "content",
  title_zh: "", body_zh: "", lang: "zh" });

const ITEMS = {
  social: { items: [post("tweet-long", TWEET), post("tweet-short", SHORT),
                    // 等翻译那一档:它的「标题」是状态文案,不是这条内容叫什么,
                    // 所以在大V观点上照旧显示(见 renderSourceItems 的注释)。
                    post("tweet-en", "Just shipped a new release.", { translating: true, lang: "en" })],
            total: 3, translating: 1 },
  rss: { items: [article("rss-long", LONG_TITLE, "这是一篇普通订阅文章的正文开头。"),
                 article("rss-plain", "一条普通的订阅标题", "另一篇文章的正文。")],
         total: 2, translating: 0 },
};
const overview = kind => ({
  sources: [{ id: 1, name: kind === "social" ? "向阳乔木" : "测试订阅",
              url: "https://example.org/" + kind, group_name: "测试", enabled: 1,
              item_count: ITEMS[kind].total, last_success: "2026-10-09 14:00",
              last_error: "", last_note: "", configuration_error: "", retry_at: 0,
              last_notice: kind === "social"
                ? "跳过 1 条远端明确不可用推文(跳过本身不冻结水位):https://x.com/Example/status/123 (页面不存在)"
                : "" }],
  settings: { enabled: false, interval_minutes: 1440, next_run: 0 },
  last_run: null, total: ITEMS[kind].total, enabled: 1, ready: 1, unconfigured: 0, errors: 0,
});

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
  // adapter 不给 'social':那会让 x_channel.js 去读取法清单,而这一轮验的是条目卡片。
  "/api/intake/modules": { modules: [
    { id: "social", label: "大V观点", description: "关注的人，此刻的想法。", note: "",
      channels: ["social"], manager: "feeds", icon: "chat" },
    { id: "rss", label: "RSS", description: "让值得读的文章，自己来到这里。", note: "",
      channels: ["rss"], manager: "feeds", icon: "rss" } ] },
  "/api/pipeline/channels": { channels: [{ id: "social", label: "大V观点", hint: "" },
                                         { id: "rss", label: "RSS", hint: "" }],
                              default: "rss", labels: { social: "大V观点", rss: "RSS" }, aliases: {} },
  "/api/pipeline/status": { running: false, stages: {}, source: { online: true, ready: true },
                            log: [], last_run: null },
  "/api/daily/window": { since: "", until: "" },
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
  const items = path.match(/^\/api\/intake\/(\w+)\/items$/);
  if (items) return json(ITEMS[items[1]] ?? { items: [], total: 0, translating: 0 });
  const over = path.match(/^\/api\/intake\/(social|rss)$/);
  if (over) return json(overview(over[1]));
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
let msgId = 0;
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
  const id = ++msgId;
  return new Promise(res => { pend.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
};
const evalJs = async expression => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const until = async (expr, ms = 10000) => {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) { if (await evalJs(expr)) return true; await sleep(120); }
  return false;
};
// 真鼠标点在元素中心:展开阅读那个入口要证明的是"页面上点下去真的打得开"。
const clickAt = async sel => {
  const box = await evalJs(`(() => { const e = document.querySelector(${JSON.stringify(sel)});
    if (!e) return null; e.scrollIntoView({block:'center'}); const r = e.getBoundingClientRect();
    return JSON.stringify({ x: r.left + r.width / 2, y: r.top + r.height / 2 }); })()`);
  if (!box) return false;
  const { x, y } = JSON.parse(box);
  for (const type of ["mousePressed", "mouseReleased"])
    await send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 });
  return true;
};
// 「有没有东西画到卡片外面」:逐个后代比右边界,超出多少像素原样报出来。
const overflowPx = () => evalJs(`(() => {
  let worst = 0, who = '';
  for (const card of document.querySelectorAll('#source-list .source-entry')) {
    const edge = card.getBoundingClientRect().right;
    for (const el of card.querySelectorAll('*')) {
      const over = el.getBoundingClientRect().right - edge;
      if (over > worst) { worst = over; who = el.className || el.tagName; }
    }
  }
  return JSON.stringify({ px: Math.round(worst), who });
})()`);
await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });

const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));

await send("Page.navigate", { url: base + "/" });
if (!await until(`typeof openSubsPanel === 'function' && typeof renderSourceItems === 'function'`, 25000))
  throw new Error("页面没起来");
await evalJs(`localStorage.clear(); true`);
await evalJs(`switchTab('subs'); openSubsPanel('social')`);
if (!await until(`document.querySelectorAll('#source-list .source-entry').length === 3`))
  throw new Error("大V观点的条目没渲染出来");

// ── 1. 后果:卡片里没有东西画到外面去 ─────────────────────────────────────────
// 旧实现红的就是这一条:标题那个 <button> 一行到底,撑出卡片右边界几百像素。
let over = JSON.parse(await overflowPx());
check("大V观点:没有任何东西画到卡片外面", over.px <= 1, JSON.stringify(over));

// ── 2. 后果:正文一个字没少,而且只出现一遍 ───────────────────────────────────
// 列表顶上可能还有一条「N 条外文内容还在等翻译」的 <p>,所以按元素类型数,
// 不按子元素序号数。
const firstCard = `#source-list .source-entry:nth-of-type(1)`;
const bodyText = await evalJs(`document.querySelector('${firstCard} .source-excerpt').textContent`);
check("大V观点:正文一个字没少", bodyText === TWEET, JSON.stringify(bodyText.slice(0, 40)));
const cardText = await evalJs(`document.querySelector('${firstCard}').innerText`);
const firstLine = TWEET.split("\n")[0];
check("大V观点:正文的第一句只出现一遍(标题不再重复它)",
      cardText.split(firstLine).length - 1 === 1, JSON.stringify(cardText.slice(0, 80)));

// ── 3. 标题那个元素整个不渲染 ────────────────────────────────────────────────
const SEL_TITLE = "#source-list .source-entry h3:not(.source-entry-name), "
                 + "#source-list .source-entry-title";
check("大V观点:条目卡片上没有标题元素",
      await evalJs(`document.querySelectorAll(${JSON.stringify(SEL_TITLE)}).length`) === 0);

// ── 4. 正文从"摘要"提成卡片主体 ──────────────────────────────────────────────
// 去掉标题之后它是卡片上唯一的内容,照 muted 显示整列卡片就全是次要信息。
const style = JSON.parse(await evalJs(`(() => { const e =
  document.querySelector('${firstCard} .source-excerpt');
  const s = getComputedStyle(e);
  return JSON.stringify({ cls: e.className, size: s.fontSize, color: s.color }); })()`));
check("大V观点:正文是主体样式,不是 muted 摘要",
      style.cls.includes("is-post") && style.size === "15px" && style.color === "rgb(58, 71, 80)",
      JSON.stringify(style));

// ── 5. 展开阅读照旧点得开,弹窗里也不显示标题 ────────────────────────────────
check("大V观点:页脚那个展开入口还在", await clickAt(`${firstCard} [data-read-item]`));
check("大V观点:展开阅读真的打开了",
      await until(`document.getElementById('source-reader').style.display !== 'none'`));
const reader = JSON.parse(await evalJs(`(() => {
  const t = document.getElementById('source-reader-title');
  return JSON.stringify({ hidden: t.hidden, title: t.textContent,
                          body: document.getElementById('source-reader-body').textContent,
                          meta: document.getElementById('source-reader-meta').textContent }); })()`));
check("大V观点:弹窗里不显示标题", reader.hidden && reader.title === "", JSON.stringify(reader.title.slice(0, 40)));
check("大V观点:弹窗正文是全文", reader.body === TWEET, JSON.stringify(reader.body.slice(0, 40)));
check("大V观点:作者和时间没丢(身份还在 meta 行上)",
      reader.meta.includes("向阳乔木"), JSON.stringify(reader.meta));
await evalJs(`document.getElementById('source-reader').style.display = 'none'`);

// ── 6. 等翻译那一档照旧有那行状态文案 ────────────────────────────────────────
// 它回答的是"这张卡片为什么没有内容",不是"这条内容叫什么" —— 和去掉的标题不是
// 一回事,去掉它只剩一行小字,和一条正常推文在页面上几乎分不出来。
check("大V观点:等翻译那条照旧写着「正在等翻译」",
      (await evalJs(`document.querySelector('#source-list .source-entry-name')?.textContent`) || "")
        .includes("正在等翻译"));

// ── 7. 反过来钉一边:RSS 照旧有标题 ───────────────────────────────────────────
await clickAt("#subs-switch-rss");
if (!await until(`sourceKind === 'rss'
      && document.querySelectorAll('#source-list .source-entry').length === 2`))
  throw new Error("RSS 的条目没渲染出来");
check("RSS:标题照旧渲染,每条一个",
      await evalJs(`document.querySelectorAll('#source-list .source-entry-title').length`) === 2);
check("RSS:正文照旧是 muted 的摘要样式",
      await evalJs(`document.querySelectorAll('#source-list .source-excerpt.is-post').length`) === 0
      && await evalJs(`getComputedStyle(document.querySelector('#source-list .source-excerpt')).color`)
         === "rgb(102, 114, 124)");

// ── 8. 后果:超长中文标题也不画到卡片外面 ─────────────────────────────────────
// base.css 给所有 button 设了 `white-space: nowrap`,中文标题一个字都断不开。
over = JSON.parse(await overflowPx());
check("RSS:超长中文标题也不画到卡片外面", over.px <= 1, JSON.stringify(over));
check("RSS:长标题真的折了行(不是被谁截掉了)",
      await evalJs(`(() => { const e = document.querySelector('#source-list .source-entry-title');
        return e.getBoundingClientRect().height > parseFloat(getComputedStyle(e).lineHeight) * 1.5
               && e.textContent.length === ${LONG_TITLE_LEN}; })()`));

// ── 9. RSS 的标题照旧点得开,而且弹窗里显示它 ─────────────────────────────────
await clickAt(`#source-list .source-entry:nth-of-type(1) .source-entry-title`);
check("RSS:点标题打得开阅读弹窗",
      await until(`document.getElementById('source-reader').style.display !== 'none'`));
check("RSS:弹窗照旧显示标题",
      await evalJs(`!document.getElementById('source-reader-title').hidden`)
      && await evalJs(`document.getElementById('source-reader-title').textContent`) === LONG_TITLE);

await evalJs(`document.getElementById('source-reader').style.display = 'none'`);
await clickAt("#subs-switch-social");
await until(`sourceKind === 'social' && sourceData?.sources[0]?.last_notice`);
await clickAt("#source-view-manage");
await until(`document.querySelector('#source-list .source-subscription')`);
check("不可用条目:管理页显示已同步有跳过,而非部分同步",
      await evalJs(`document.querySelector('#source-list .source-state')?.textContent`) === "已同步（有跳过）");
const noticeText = await evalJs(`document.querySelector('#source-list').innerText`);
check("不可用条目:计数、URL、原因可见,不谎称从旧水位继续",
      noticeText.includes("跳过 1 条") && noticeText.includes("https://x.com/Example/status/123")
      && noticeText.includes("页面不存在") && !noticeText.includes("从上次的位置接着读"));
if (process.env.X_SKIP_SCREENSHOT) {
  const screenshot = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(process.env.X_SKIP_SCREENSHOT, Buffer.from(screenshot.result.data, "base64"));
}
check("不可用条目:混合失败仍独立显示部分同步和跳过说明",
      await evalJs(`(() => { const row = {...sourceData.sources[0], last_note:'折叠推文完整正文没取到'};
        const host = document.createElement('div'); host.innerHTML = renderSubscription(row);
        return host.querySelector('.source-state').textContent === '部分同步'
          && host.textContent.includes('下次同步从上次的位置接着读')
          && host.textContent.includes(row.last_notice); })()`));
check("不可用条目:跳过说明中的 HTML 只显示文本",
      await evalJs(`(() => { const host = document.createElement('div');
        host.innerHTML = renderSubscription({...sourceData.sources[0],last_notice:'<img src=x onerror=alert(1)>'});
        return !host.querySelector('img') && host.textContent.includes('<img src=x onerror=alert(1)>'); })()`));
await send("Emulation.setDeviceMetricsOverride", {
  width: 390, height: 844, deviceScaleFactor: 1, mobile: false,
});
check("不可用条目:窄屏 URL 不撑出说明文字栏",
      await evalJs(`(() => { const note = document.querySelector('.source-skip-notice');
        return note.scrollWidth <= note.clientWidth + 1; })()`));
if (process.env.X_SKIP_SCREENSHOT) {
  const screenshot = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(process.env.X_SKIP_SCREENSHOT.replace(/\.png$/, "-mobile.png"),
               Buffer.from(screenshot.result.data, "base64"));
}

check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | "));

// ── 收尾 ─────────────────────────────────────────────────────────────────────
const out = [...oks.map(n => `  ✓ ${n}`), ...fails.map(n => `  ✗ ${n}`)].join("\n");
process.stdout.write(`${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`);
ws.close(); proc.kill(); server.close();
process.exitCode = fails.length ? 1 : 0;
