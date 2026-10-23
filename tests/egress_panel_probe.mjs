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
  // **「清空这一项之后还有东西接着生效吗」是另一个问题**,不能从 sources 推 —— 占位符
  // 那句「留空 = 关」照它说话,下面三个格各钉一条。
  fallbacks: { endpoint: false, seconds: false, proxy: true },
  seconds_when_cleared: 8,
  default_seconds: 8,
  // 预设端点由后端给(名单归 resolve.JSON_API_PRESETS)。这儿**刻意用一个真名单里
  // 没有的地址**:页面要是自己写死了一份,下面那条就会拿到别的东西。
  presets: [{ name: "测试端点", url: "https://from-backend.example/q" },
            { name: "关掉", url: "" }],
};
const FIXTURES = {
  "/api/stats": { documents: 0 },
  "/api/projects": { tools: [], kinds: {}, total_entries: 0 },
  "/api/facets": { tags: [], entities: [], accounts: [] },
  // "/api/egress" 不在这儿:它的 GET 要能被挂住、能回 500(下面那道闸)。
};
const posted = [];
// 下一次 POST 回什么。null = 回默认(保存成功回新的 view / 探测回一组地址)
let nextReply = null;
// GET /api/egress 这一道闸:holdGet 不为空时**下一次**请求挂着不回,直到测试放它过去
// (只挂那一次,之后的照常回);getCode 非 200 就是读取失败。**两种形态下表单都是空的**,
// 而空表单发出去在后端是「清空」。heldView 给挂住那一次单独的回包 —— 「晚到的那一份有没有
// 把新的盖掉」要能分辨出来,两次都回同一份 VIEW 的话盖没盖长得一模一样。
let holdGet = null;
let heldView = null;
let getCode = 200;

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
  if (path === "/api/egress") {
    const serve = body => () => (getCode === 200 ? reply(body)
                                                 : reply({ detail: "读不出来" }, getCode));
    if (holdGet) {
      const gate = holdGet, body = heldView || VIEW;
      holdGet = heldView = null;            // 只挂住这一次
      return void gate.then(serve(body));
    }
    return serve(VIEW)();
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
// 回填完没有,判据是**那个值真的落到框里了** —— 读取失败时状态也会离开「读取中…」,
// 只看状态文字的话那一下会被当成「读到了」。
const filled = () => until(`document.getElementById('egress-endpoint')?.value
  === ${JSON.stringify(VIEW.endpoint)}`, 15000);
const snap = () => evalJs(`JSON.stringify({
  open: !!document.getElementById('egress-modal'),
  endpoint: document.getElementById('egress-endpoint')?.value,
  seconds: document.getElementById('egress-seconds')?.value,
  proxy: document.getElementById('egress-proxy')?.value,
  srcs: ['endpoint','seconds','proxy'].map(f => document.getElementById('egress-'+f+'-src')?.textContent),
  errs: ['endpoint','seconds','proxy'].map(f => document.getElementById('egress-'+f+'-err')?.textContent),
  state: document.getElementById('egress-state')?.textContent,
  ph: document.getElementById('egress-endpoint')?.placeholder,
  phSeconds: document.getElementById('egress-seconds')?.placeholder,
  saveDisabled: !!document.getElementById('egress-save')?.disabled,
  probeDisabled: !!document.getElementById('egress-probe-btn')?.disabled,
  endpointDisabled: !!document.getElementById('egress-endpoint')?.disabled,
  retryHidden: !!document.getElementById('egress-retry')?.hidden,
  chips: [...document.querySelectorAll('#egress-chips button')].map(b => b.textContent),
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
// 预设:名单是后端给的,页面一个地址都不许自己写死 —— 判据是**点一下填进去的那个
// 地址来自接口**。实测过的反面:前端自己列了 OpenDNS,而它只认另一种 DoH 协议,
// 保存之后下一次抓取当场 HTTP 400。
check("预设按钮是按接口给的名单画的",
      s.chips.join("|") === "测试端点|关掉", JSON.stringify(s.chips));
await setup("点得到第一颗预设", clickAt("#egress-chips button"));
s = await snap();
check("点一下填进去的是接口给的那个地址",
      s.endpoint === VIEW.presets[0].url, s.endpoint);
// 「留空」到底是不是「关」,**取决于环境变量里有没有可回落的值**,不取决于当前生效的
// 那一份来自哪。这句话只在框空着时看得见,而那正是人刚点完「关掉」的那一下 ——
// 三个格各钉一条。
const applyView = async extra => {
  await evalJs(`egressApply(Object.assign({}, ${JSON.stringify(VIEW)}, ${JSON.stringify(extra)}))`);
  return snap();
};
check("端点来自设置、环境变量里也没有:留空就是关", /留空 = 关/.test(s.ph), s.ph);
s = await applyView({ sources: { ...VIEW.sources, endpoint: "env" },
                      where: { ...VIEW.where, endpoint: "来自环境变量 KB_FEED_DOH" },
                      fallbacks: { ...VIEW.fallbacks, endpoint: true } });
check("环境变量在兜底、而且正生效:留空不许说成关",
      /回落到环境变量/.test(s.ph) && s.srcs[0] === "来自环境变量 KB_FEED_DOH", s.ph);
// **这一格是按「谁在生效」判的那一版漏掉的**:两层都有、设置那层生效时它说
// 「留空 = 关(走系统解析)」,而人照着点完「关掉」保存,端点立刻被环境变量那份回填、
// 状态仍是「开」—— 他刚看到的那句话写的是反面。
s = await applyView({ fallbacks: { ...VIEW.fallbacks, endpoint: true } });
check("设置在生效、环境变量也在:留空照样不是关",
      /回落到环境变量/.test(s.ph) && s.srcs[0] === "来自设置页", JSON.stringify(s));
// 秒数那一格同一个毛病,但它的框只放得下一个数(实测:一句话被截成「留空 =」)
// —— 所以判据是**它显示的就是清空之后真正会用的那个数**。
s = await applyView({ fallbacks: { ...VIEW.fallbacks, seconds: true }, seconds_when_cleared: "12" });
check("秒数的占位符写的是清空之后真正会用的那个数,不是写死的 8",
      s.phSeconds === "12", s.phSeconds);
await evalJs(`egressApply(${JSON.stringify(VIEW)})`);   // 换回去,下面几步按原夹具走

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

// ── 6. 配置还没读到之前,保存一个空表单就是「清空」 ──────────────────────────
// 后端按「空串 = 清掉这一项」处理(那是「关掉」那一下的语义),三项都空就把
// secrets.json 里整段 egress 删掉 —— 它分不开「人要清空」和「页面还没读到」,
// 所以判据在页面上:**回填成功过一次才放行**。
posted.length = 0;
let release;
holdGet = new Promise(r => { release = r; });
await evalJs(`document.querySelector('.workspace-settings').open = true`);
await setup("又点开了一次面板", clickAt("#egress-btn"));
await setup("状态停在「读取中…」",
            until(`/读取中/.test(document.getElementById('egress-state')?.textContent || '')`, 6000));
s = await snap();
check("配置没回来时保存点不动,框也动不了",
      s.saveDisabled && s.probeDisabled && s.endpointDisabled, JSON.stringify(s));
// 真鼠标点在 disabled 的按钮上什么也不会发生;**函数本身也得拦** —— disabled 是给人
// 看的,判据得落在那一行上(别的脚本、别的入口照样能调到它)。
await clickAt("#egress-save");
await evalJs(`(() => { saveEgress(); probeEgress(); return true; })()`);
check("点了也一个请求都不许发出去(空表单在后端是「清空」)",
      posted.length === 0, JSON.stringify(posted));
release();
await setup("闸放开之后回填上了", filled());
s = await snap();
check("回填过一次才放行", !s.saveDisabled && !s.probeDisabled && !s.endpointDisabled,
      JSON.stringify(s));

// ── 7. 读取失败:保持点不动,给一颗「重试」 ──────────────────────────────────
posted.length = 0;
await evalJs(`closeEgress()`);
getCode = 500;
await evalJs(`document.querySelector('.workspace-settings').open = true`);
await setup("第三次点开面板", clickAt("#egress-btn"));
await setup("「重试」露出来了",
            until(`document.getElementById('egress-retry') &&
                   !document.getElementById('egress-retry').hidden`, 8000));
s = await snap();
check("读不到配置时保存仍然点不动,只给一颗「重试」",
      s.saveDisabled && !s.retryHidden && /读不到/.test(s.state), JSON.stringify(s));
await evalJs(`(() => { saveEgress(); return true; })()`);
check("读取失败之后点保存也不许发(那一下就是清空)", posted.length === 0, JSON.stringify(posted));
getCode = 200;
await setup("点得到「重试」", clickAt("#egress-retry"));
await setup("重试之后回填上了", filled());
s = await snap();
check("「重试」真能把面板救回来(不用关掉重开)",
      !s.saveDisabled && s.retryHidden && /开/.test(s.state), JSON.stringify(s));

// ── 8. 晚到的那次读取不许回填 ────────────────────────────────────────────────
// 人点「重试」、或者关掉再开一次时,先发出去的那一份可能在新的之后才回来 —— 盖掉的
// 正好是新的那一份,而页面上零报错(看到的值凭空变回旧的)。
await evalJs(`closeEgress()`);
const STALE = { ...VIEW, endpoint: "https://stale.example/q" };
holdGet = new Promise(r => { release = r; });
heldView = STALE;
await evalJs(`document.querySelector('.workspace-settings').open = true`);
await setup("第四次点开面板(这一次的读取被挂住)", clickAt("#egress-btn"));
await setup("状态停在「读取中…」",
            until(`/读取中/.test(document.getElementById('egress-state')?.textContent || '')`, 6000));
await evalJs(`refreshEgress()`);                 // 后发的这一次不挂,立刻回来
await setup("后发的那一次回填上了", filled());
release();                                        // 先发的那一份现在才到
await new Promise(r => setTimeout(r, 300));
s = await snap();
check("晚到的那一次不许把新的盖回去",
      s.endpoint === VIEW.endpoint && !/stale/.test(s.endpoint), JSON.stringify(s));

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
