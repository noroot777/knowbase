// 「出口与解析」那块面板,在**真页面**上。后端口径在 kb/feeds/egress.py,
// 硬约束与形态见 docs/decisions/dns-egress.md。
//
// 这一块的方向是仓库主人定的(B / C / F):**页面是正经入口**、**保存即生效**、
// **代理也可配**。所以判据全落在「人看到什么、页面发出去什么」:
//
// - **看到的就是生效的那一份**:值和出处都得摆出来 —— 看不见出处时,「页面上填了但
//   没生效」和「环境变量在兜底」长得一模一样。
// - **坏值就地红字,不飘 toast**(项目约定:字段级校验写在那个框下面)。反过来钉一边:
//   后端没说是哪个框的失败照旧走 toast,否则那句话就谁也看不见了。
// - **「测一下」用的是框里还没保存的值**:否则这颗按钮只能复述已经存下来的东西,
//   而人点它正是为了在保存之前先试一把。
// - **代理那一位原样回传**:页面拿到的是 `user:***@host`,改端口时不许把 `***` 抹掉或
//   自己补一个空密码 —— 后端认着这个字样才能沿用存着的密码(merge_proxy)。
// - **Esc 关得掉**:这个弹窗的标记是 js 现建的(index.html 顶着 700 行上限),关闭链
//   认的是 id → 关函数那张表,现建的弹窗得自己报名。漏了报名的后果不是「Esc 没反应」,
//   是焦点被陷在弹窗里出不来。
//
// 自带只读 mock 后端,CDP 直连 chrome-headless-shell(不装 playwright、不连网、
// 始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/egress_panel_probe.mjs
import { createServer } from "node:http";
import { readFileSync, writeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { attachCdp } from "./pipe_history_cdp.mjs";
import { MIME } from "./pipe_history_fixture.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
// 开着 DoH、代理来自环境变量、密码位已脱敏 —— 三种出处各占一格
const VIEW = {
  endpoint: "https://doh.example/dns-query", seconds: "", proxy: "http://me:***@127.0.0.1:7897",
  sources: { endpoint: "settings", seconds: "none", proxy: "env" },
  where: { endpoint: "来自设置页", seconds: "", proxy: "来自环境变量 HTTPS_PROXY" },
  default_seconds: 8,
};
const FIXTURES = {
  "/api/stats": { documents: 0 },
  "/api/projects": { tools: [], kinds: {}, total_entries: 0 },
  "/api/facets": { tags: [], entities: [], accounts: [] },
  "/api/egress": VIEW,
};
const posted = [];
// 下一次 POST 回什么。null = 回默认(保存成功回新的 view / 探测回一组地址)
let nextReply = null;

const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  const reply = (data, code = 200) => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(code, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  if (req.method === "POST") {
    let raw = "";
    req.on("data", c => { raw += c; });
    req.on("end", () => {
      posted.push({ path, body: raw ? JSON.parse(raw) : null });
      if (nextReply) { const r = nextReply; nextReply = null; return reply(r.body, r.code); }
      if (path === "/api/egress/probe")
        return reply({ ok: true, host: "example.org", via: "doh", ms: 42,
                       addresses: ["93.184.216.34"] });
      return reply(VIEW);
    });
    return;
  }
  if (FIXTURES[path]) return reply(FIXTURES[path]);
  if (path.startsWith("/api/")) return reply({});
  const file = normalize(join(STATIC, path === "/" ? "index.html" : path.replace(/^\/static/, "")));
  if (!file.startsWith(STATIC)) { res.writeHead(403); return res.end(); }
  try {
    const body = readFileSync(file);
    res.writeHead(200, { "Content-Type": MIME[file.split(".").pop()] || "application/octet-stream",
                         "Content-Length": body.length });
    res.end(body);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(r => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}`;

const { proc, ws, send, evalJs, until, untilHere, clickAt, pageErrors } = await attachCdp();
const oks = [], fails = [];
const check = (name, pass, info = "") => (pass ? oks : fails).push(pass ? name : `${name} — ${info}`);
const setup = async (name, promise) => {
  if (await promise) return;
  fails.push(`摆局没成:${name}`);
  throw new Error("stop");
};
// **等的是事实,不是秒数。** 这一份原先用固定 sleep 和「元素在不在」做判据,在全量里
// 红过一次:弹窗元素是同步建出来的,而回填它的那次 /api/egress 还在路上(状态停在
// 「读取中…」)—— 机器一忙就随机红,单跑永远绿。
const loaded = () => until(`(() => { const e = document.getElementById('egress-state');
  return e && !/读取中/.test(e.textContent); })()`, 15000);
// 按钮在请求期间是 disabled 的,真鼠标点在上面什么也不会发生
const clickReady = async sel => {
  if (!await until(`(() => { const e = document.querySelector('${sel}');
    return e && !e.disabled; })()`, 15000)) return false;
  return clickAt(sel);
};
const snap = () => evalJs(`JSON.stringify({
  open: !!document.getElementById('egress-modal'),
  endpoint: document.getElementById('egress-endpoint')?.value,
  seconds: document.getElementById('egress-seconds')?.value,
  proxy: document.getElementById('egress-proxy')?.value,
  srcs: ['endpoint','seconds','proxy'].map(f => document.getElementById('egress-'+f+'-src')?.textContent),
  errs: ['endpoint','seconds','proxy'].map(f => document.getElementById('egress-'+f+'-err')?.textContent),
  state: document.getElementById('egress-state')?.textContent,
  probe: document.getElementById('egress-probe')?.textContent,
  toasts: [...document.querySelectorAll('#toast-host .toast')].map(t => t.textContent),
})`).then(JSON.parse);

try {
await send("Page.navigate", { url: `${base}/` });
await setup("页面起来了", until(`typeof openEgress === 'function'`, 8000));

// ── 1. 入口:设置菜单里那一项 ────────────────────────────────────────────────
await evalJs(`document.querySelector('.workspace-settings').open = true`);
await setup("菜单展开了", until(`!!document.getElementById('egress-btn')`, 4000));
await setup("点得到那一项", clickAt("#egress-btn"));
await setup("弹窗开了、而且回填完了", loaded());
let s = await snap();
check("看到的就是生效的那一份(端点、代理都回填了)",
      s.endpoint === VIEW.endpoint && s.proxy === VIEW.proxy, JSON.stringify(s));
check("每一项都说清出处,环境变量那一项还带上是哪个名字",
      s.srcs[0] === "来自设置页" && s.srcs[2] === "来自环境变量 HTTPS_PROXY" && s.srcs[1] === "",
      JSON.stringify(s.srcs));
check("状态那一格说开着", /开/.test(s.state), s.state);

// ── 2. 保存:三项原样发出去,脱敏那一位不许被动过 ────────────────────────────
posted.length = 0;
await evalJs(`document.getElementById('egress-seconds').value = '6'`);
await setup("保存点出去了", clickReady("#egress-save"));
await setup("请求发出去了", untilHere(() => posted.length > 0, 15000));
await setup("保存回来了", until(`[...document.querySelectorAll('#toast-host .toast')]
  .some(t => /已保存/.test(t.textContent))`, 15000));
let p = posted[0];
check("保存发的是这三项,`***` 原样带回去(后端据此沿用存着的密码)",
      p.path === "/api/egress" && p.body.endpoint === VIEW.endpoint
      && p.body.seconds === "6" && p.body.proxy === VIEW.proxy, JSON.stringify(p));
s = await snap();
check("保存成功说一句就完了,不要人去重启", s.toasts.some(t => /已保存/.test(t)), JSON.stringify(s.toasts));

// ── 3. 坏值:就地红字,不飘 toast,也没有原生弹窗 ────────────────────────────
await evalJs(`[...document.querySelectorAll('#toast-host .toast')].forEach(t => t.remove())`);
nextReply = { code: 400, body: { detail: "DoH 端点里没有主机名", field: "endpoint" } };
await setup("又点了一次保存", clickReady("#egress-save"));
await setup("坏值那一次也发出去了", untilHere(() => posted.length > 1, 15000));
await setup("红字落下来了", until(
  `!!document.getElementById('egress-endpoint-err').textContent`, 15000));
s = await snap();
check("错在哪个框就写在哪个框下面", s.errs[0] === "DoH 端点里没有主机名", JSON.stringify(s.errs));
check("字段级校验不飘 toast", s.toasts.length === 0, JSON.stringify(s.toasts));

// ── 3b. 反过来钉一边:认不出字段的失败照旧走 toast ──────────────────────────
nextReply = { code: 500, body: { detail: "出口配置写不进去" } };
await setup("再点一次保存", clickReady("#egress-save"));
await setup("第三次也发出去了", untilHere(() => posted.length > 2, 15000));
await setup("这一次飘了 toast", until(`[...document.querySelectorAll('#toast-host .toast')]
  .some(t => /保存失败/.test(t.textContent))`, 15000));
s = await snap();
check("说不清是哪个框的失败走 toast(否则那句话谁也看不见)",
      s.toasts.some(t => /保存失败/.test(t)), JSON.stringify(s.toasts));

// ── 4. 测一下:用框里还没保存的值 ────────────────────────────────────────────
posted.length = 0;
await evalJs(`(() => { document.getElementById('egress-endpoint').value = 'https://typed.example/q';
  document.getElementById('egress-host').value = 'example.org'; return true; })()`);
await setup("测一下点出去了", clickReady("#egress-probe-btn"));
await setup("探测请求发出去了", untilHere(() => posted.length > 0, 15000));
await setup("结果渲染出来了", until(
  `/93\\.184\\.216\\.34/.test(document.getElementById('egress-probe').textContent)`, 15000));
p = posted[0];
check("测的是框里刚敲进去的那个端点,不是存着的那一份",
      p.path === "/api/egress/probe" && p.body.endpoint === "https://typed.example/q"
      && p.body.host === "example.org", JSON.stringify(p));
s = await snap();
check("结果里有地址和耗时", /93\.184\.216\.34/.test(s.probe) && /42/.test(s.probe), s.probe);

// ── 5. Esc 关得掉(现建的弹窗得自己报名进关闭链) ────────────────────────────
await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Escape", code: "Escape",
                                       windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape",
                                       windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
await setup("Esc 之后弹窗没了", until(`!document.getElementById('egress-modal')`, 4000));
check("Esc 关得掉(关闭链认得这个现建的弹窗)", true);
check("主内容区的 inert 跟着解掉了(焦点没被陷住)",
      !(await evalJs(`document.getElementById('main-content').inert`)), "main-content 还是 inert");

} catch (e) {
  if (e.message !== "stop") fails.push(`探针自己崩了:${e.stack || e}`);
} finally {
  const noise = pageErrors.filter(t => t && !/favicon|ERR_/.test(t));
  if (noise.length) fails.push(`页面报错:${noise.join(" | ")}`);
  for (const line of oks) writeSync(1, `ok   ${line}\n`);
  for (const line of fails) writeSync(1, `FAIL ${line}\n`);
  try { ws.close(); } catch {}
  try { proc.kill("SIGKILL"); } catch {}
  server.close();
  writeSync(1, `${oks.length} ok, ${fails.length} fail\n`);
  process.exit(fails.length ? 1 : 0);
}
