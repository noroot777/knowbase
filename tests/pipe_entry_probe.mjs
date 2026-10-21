// 「我的订阅」里的同步按钮 = 打开起跑弹窗，**锁定在你正在看的那一页**。
//
// 改之前这三颗按钮各走各的路、跑哪几步谁也说不出来:订阅源那一侧直接
// `POST /api/intake/{kind}/sync` 固定四段;公众号那一侧 `POST /api/pipeline/run`
// **不带步骤清单**,后端 `start_pipeline` 兜底成 `STAGES` 全跑 —— 于是点一下
// 「同步全部」会顺手跑一遍全库条目化和更新 Wiki(几十分钟),页面上一个字都不说。
//
// 判据全部落在「**后端收到的是什么**」(起跑请求原样留下来比对),不落在页面上的
// 勾选快照 —— 前者才是这一块唯一会出事的地方(口径同 `pipe_stages_probe.mjs`)。
// 另外钉三件事:
// - **锁定要说清为什么和出路**:一片灰色不说话,用户只会以为坏了;
// - **要声明自己是人按的**:退避是给自动轮次用的,判和盖在 `kb/run_trigger.py` 一处,
//   页面只负责声明 —— 丢了这句声明的后果是退避中的那几条被静默跳过(只记一笔
//   `deferred`),而页面说「同步已开始」;
// - **反过来钉一边**:同步中心那颗「新建同步」照旧六个来源随便勾、步骤不被入口改。
//
// 硬约束在 `docs/decisions/sync-entry.md` 第一节。自带只读 mock 后端,CDP 直连
// chrome-headless-shell(不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/pipe_entry_probe.mjs
import { createServer } from "node:http";
import { readFileSync, writeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { attachCdp, sleep } from "./pipe_history_cdp.mjs";
import { MIME } from "./pipe_history_fixture.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
const MODULES = [
  { id: "wechat", label: "公众号", description: "关注的公众号文章", channels: ["wechat_local"],
    manager: "wechat", icon: "chat", interval_minutes: 360, url_hint: "", note: "",
    adapter: "wechat", pollable: true },
  { id: "rss", label: "RSS", description: "让值得读的文章，自己来到这里。", channels: ["rss"],
    manager: "feeds", icon: "rss", interval_minutes: 360, url_hint: "", note: "RSS 那段说明",
    adapter: "rss", pollable: true },
  // 手动收录**没有可轮询的源**:它照旧直接起跑，不进这个弹窗
  { id: "inbox", label: "手动收录", description: "看到就存", channels: ["inbox"],
    manager: "feeds", icon: "box", interval_minutes: 1440, url_hint: "", note: "",
    adapter: "manual", pollable: false },
];
const rssSource = (id, name, host) => ({
  id, name, url: `https://${host}.example/feed`, group_name: "", enabled: 1,
  last_error: "", last_success: "", last_attempt: "", failures: 0, configuration_error: "",
});
const SOURCES = {
  // **13 和 14 名字一样、地址不同**:名字不是唯一的,所以「只同步这一条」只能按
  // id 发出去(第 1 轮审查 R2)。`/api/pipeline/accounts` 给的是名字,它俩在那份
  // 名单里只占一格。
  rss: [rssSource(11, "某博客", "a"), rssSource(12, "另一个博客", "b"),
        rssSource(13, "同名博客", "c"), rssSource(14, "同名博客", "d")],
  inbox: [{ id: 21, name: "收件桶", url: "", group_name: "", enabled: 1, last_error: "",
            last_success: "", last_attempt: "", failures: 0, configuration_error: "" }],
};
const ACCOUNTS = { wechat_local: ["公众号甲", "公众号乙"],
                   rss: ["某博客", "另一个博客", "同名博客"], inbox: ["收件桶"] };
const CHANNELS = {
  channels: [{ id: "wechat_local", label: "公众号", hint: "本机绿泡泡库" },
             { id: "rss", label: "RSS", hint: "RSS / Atom" },
             { id: "inbox", label: "手动收录", hint: "粘链接" }],
  default: "wechat_local", labels: {}, aliases: {},
};
const FIXTURES = {
  "/api/stats": { documents: 100 },
  "/api/intake/modules": { modules: MODULES },
  "/api/pipeline/channels": CHANNELS,
  "/api/facets": { tags: [], entities: [], accounts: [] },
  "/api/projects": { tools: [], kinds: {}, total_entries: 0 },
  "/api/sources/catalog": { modules: [] },
  "/api/daily/list": { days: [] },
  "/api/daily/reports": { reports: [] },
  "/api/pipeline/runs": { runs: [], error: "", version: "none", keep_days: 3 },
  "/api/daily/settings": { settings: { enabled: true, time: "07:30", sync: true, library: true,
                                       first_days: 1, channels: [], next_run: "" },
                           channels: [], default_channels: [] },
  "/api/pipeline/status": { running: false, stop_requested: false, stopped: false,
                            stop_supported: true, last_run: null, log: [], result_version: "none" },
  "/api/source": { ready: true, label: "公众号", status: "ok", message: "", hint: "", targets: 2 },
};
const posted = [];

const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  const json = data => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(200, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  if (req.method === "POST") {
    let raw = "";
    req.on("data", c => { raw += c; });
    req.on("end", () => { posted.push({ path, body: raw ? JSON.parse(raw) : null }); json({ ok: true }); });
    return;
  }
  if (path === "/api/pipeline/accounts") {
    const ch = new URLSearchParams(req.url.split("?")[1] || "").get("channel") || "wechat_local";
    return json({ accounts: ACCOUNTS[ch] || [] });
  }
  const overview = path.match(/^\/api\/intake\/(rss|inbox)$/);
  if (overview) {
    const list = SOURCES[overview[1]];
    return json({ sources: list, settings: {}, last_run: null, total: 0,
                  enabled: list.length, ready: list.length, unconfigured: 0, errors: 0 });
  }
  if (/^\/api\/intake\/\w+\/items$/.test(path))
    return json({ items: [], total: 0, translating: 0, fetching: 0, stuck: null, translate_ready: true });
  if (FIXTURES[path]) return json(FIXTURES[path]);
  if (path.startsWith("/api/")) return json({});
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

const { proc, ws, send, evalJs, until, pageErrors } = await attachCdp();
const oks = [], fails = [];
const check = (name, pass, info = "") => (pass ? oks : fails).push(pass ? name : `${name} — ${info}`);
const setup = async (name, promise) => {
  if (await promise) return;
  fails.push(`摆局没成:${name}`);
  throw new Error("stop");
};
// 弹窗这一刻的样子。**锁定与否只看 input 的 disabled**(那是真点不动的那一下)
const snap = () => evalJs(`JSON.stringify({
  open: document.getElementById('pipe-modal').style.display !== 'none',
  on: [...document.querySelectorAll('#pipe-channel-list input:checked')].map(i => i.dataset.runChannel),
  off: [...document.querySelectorAll('#pipe-channel-list input:disabled')].map(i => i.dataset.runChannel),
  stages: [...document.querySelectorAll('#pipe-stage-list input:checked')].map(i => i.dataset.stage),
  hint: document.getElementById('pipe-channel-hint').textContent,
  accts: [...document.querySelectorAll('#pipe-acct-list input:checked')].map(i => i.dataset.acct),
  folded: !document.getElementById('pipe-acct-block').classList.contains('open'),
  foldBtn: getComputedStyle(document.getElementById('pipe-acct-fold')).display === 'none'
    ? '' : document.getElementById('pipe-acct-fold').textContent })`).then(JSON.parse);
const setDaily = on => evalJs(`(() => {
  const i = document.getElementById('pipe-daily');
  i.checked = ${on}; onPipeDailyToggle(); return true; })()`);
const openSubs = kind => evalJs(`(async () => { switchTab('subs'); await openSubsPanel('${kind}'); return true; })()`);

try {
await send("Page.navigate", { url: `${base}/#pipe` });
await setup("面板连上后端", until(`typeof pipeConnected !== 'undefined' && pipeConnected`, 8000));

// ── 1. RSS 那一页的「同步全部」:弹窗锁在 rss ────────────────────────────────
await openSubs("rss");
await setup("RSS 那一页摆出来了", until(`document.getElementById('source-sync') && !document.getElementById('source-sync').disabled`, 8000));
await evalJs(`document.getElementById('source-sync').click()`);
await setup("弹窗开了", until(`document.getElementById('pipe-modal').style.display !== 'none'
  && pipeReadyChannels().length === 1`, 8000));
let s = await snap();
check("从 RSS 那一页进来:只勾着 rss,整组灰掉点不动",
      s.on.join() === 'rss' && s.off.sort().join() === 'inbox,rss,wechat_local', JSON.stringify(s));
check("锁着的时候说清为什么、也给出路",
      /只同步「RSS」/.test(s.hint) && /同步中心/.test(s.hint), s.hint);
check("默认跑入库那四段,不带整理工具和更新 Wiki",
      s.stages.join() === 'crawl,export,index,extract', JSON.stringify(s.stages));
// 整条全选那一档订阅名单折着,而**折了就要能再摊开**:摊开之后按钮得还在、
// 文案换成「收起」 —— 藏了它就只能关掉弹窗重来
check("整条全选时订阅名单折着,按钮写「挑几个」", s.folded && s.foldBtn === '挑几个', JSON.stringify(s));
await evalJs(`togglePipeAcctFold()`);
let f = await snap();
check("点开之后名单摊开,而且收得回去(按钮还在,写「收起」)",
      !f.folded && f.foldBtn === '收起', JSON.stringify(f));
await evalJs(`togglePipeAcctFold()`);
f = await snap();
check("再点一下真的折回去了", f.folded && f.foldBtn === '挑几个', JSON.stringify(f));
posted.length = 0;
await evalJs(`startPipeFromModal()`);
await setup("起跑请求发出去了", sleep(500).then(() => posted.length > 0));
let p = posted[0];
check("起跑走的是管道通道,来源只有 rss、整条全选",
      p.path === '/api/pipeline/run' && p.body.channels.length === 1
      && p.body.channels[0].channel === 'rss'
      && p.body.channels[0].accounts.sort().join() === '另一个博客,同名博客,某博客', JSON.stringify(p));
check("步骤按入口那四段发下去", p.body.stages.join() === 'crawl,export,index,extract', JSON.stringify(p.body.stages));
check("声明了这一下是人按的(退避由后端照此跳过)", p.body.trigger === 'manual', JSON.stringify(p.body));

// ── 2. 行内那颗「同步」:连订阅也锁死 ────────────────────────────────────────
await openSubs("rss");
await setup("回到 RSS 那一页", until(`document.getElementById('source-sync') !== null`, 6000));
await evalJs(`syncSource([12])`);
await setup("弹窗又开了", until(`document.getElementById('pipe-modal').style.display !== 'none'
  && pipeReadyChannels().length === 1`, 8000));
s = await snap();
// 锁到具体几条时名单**必须摊开、而且折不起来**(折着的那一行写的是「全部」,
// 而这一次不是)—— 所以那一档连折叠按钮都不摆
check("点某一条的「同步」:订阅名单摊开、只勾着那一条,而且折不起来",
      !s.folded && s.accts.join() === '另一个博客' && s.foldBtn === '', JSON.stringify(s));
posted.length = 0;
await evalJs(`startPipeFromModal()`);
await setup("那一档也发出了请求", sleep(500).then(() => posted.length > 0));
p = posted[0];
// **发出去的是「明确点名编号」,不是名字、也不是裸 id**:名字不唯一,后端那道范围
// 匹配「id 相等**或** name 相等」会把同名的另一条带上(下面第 2b 节定点钉它);而裸
// 着发一串数字,那串数字同时也可能是**另一条订阅的名字** —— 前缀就是为了分开这两种
// (后端口径 `kb/feeds/targets.py`)。**真去抓哪几条**在
// `tests/test_feed_targets.py` 钉;这边的后端是夹具,只能钉发出去的那一份长什么样。
check("请求里的订阅只有点的那一条,而且写成了明确点名编号",
      p.body.channels[0].accounts.join() === 'id:12', JSON.stringify(p.body.channels));

// **反过来钉一边**:锁到单条那一次不许改写同步中心记着的订阅选择
await evalJs(`(async () => { switchTab('pipe'); await openPipeModal(); return true; })()`);
await setup("同步中心的弹窗开了(查订阅记忆)", until(`document.getElementById('pipe-modal').style.display !== 'none'
  && pipeReadyChannels().length >= 1`, 8000));
await evalJs(`(() => { const i = document.querySelector('#pipe-channel-list input[data-run-channel=rss]');
  if (!i.checked) { i.checked = true; i.dispatchEvent(new Event('change', { bubbles: true })); } return true; })()`);
await setup("rss 的订阅读出来了", until(`pipeAccounts.get('rss')?.state === 'ready'`, 8000));
check("同步中心那边 rss 照旧整条全选(单条同步没改写它记着的那一份)",
      await evalJs(`pipeSelected('rss').size`) === 3, await evalJs(`JSON.stringify([...pipeSelected('rss')])`));
await evalJs(`closePipeModal()`);

// ── 2b. 同名订阅:只跑点的那一条 ─────────────────────────────────────────────
// 夹具里 13 和 14 都叫「同名博客」,地址不同。后端那道范围匹配是「id 相等**或**
// name 相等」(`kb/feeds/registry.py` 的 `in_scope`),所以**按名字发出去等于把
// 同名的全部带上** —— 实测点第一条的「同步」两条都被抓(第 1 轮审查 R2)。
// 这一条判的就是「发出去的那一份能不能只认出一条」。
await openSubs("rss");
await setup("又回到 RSS 那一页", until(`document.getElementById('source-sync') !== null`, 6000));
await evalJs(`syncSource([13])`);
await setup("同名那一条的弹窗开了", until(`document.getElementById('pipe-modal').style.display !== 'none'
  && pipeAccounts.get('rss')?.state === 'ready'`, 8000));
posted.length = 0;
await evalJs(`startPipeFromModal()`);
await setup("同名那一条发出了请求", sleep(500).then(() => posted.length > 0));
p = posted[0];
check("两条同名订阅只点了一条:请求里是那一条的编号,不是它们共用的名字",
      p.body.channels[0].accounts.join() === 'id:13', JSON.stringify(p.body.channels));

// ── 2c. 「同步全部」就是全部,不看同步中心收窄过的那一份 ─────────────────────
// 锁定「整条来源」那一档原来也会去读 `pipeDrafts`:同步中心上次只勾了两个,
// 从订阅页点「同步全部」就只跑那两个,而按钮上写着「全部」(第 1 轮审查 R1)。
await evalJs(`(async () => { switchTab('pipe'); await openPipeModal(); return true; })()`);
await setup("同步中心的弹窗开了(准备收窄)", until(`pipeAccounts.get('rss')?.state === 'ready'`, 8000));
// **先摆回全选再收窄**:摆局不许依赖前面几节留下的勾选 —— 依赖了的话,这一节会
// 在「摆局没成」上中断,把后面几条都带着跳过,而红在哪一条看不出来。
await evalJs(`pipeSelectAll()`);
// 名单里可能同时摆着几个来源的分组,要点名 rss 那一组里的那一行
await evalJs(`(() => { const i = document.querySelector('#pipe-acct-list input[data-channel=rss]');
  i.checked = false; i.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
const narrowed = await evalJs(`pipeSelected('rss').size`);
await evalJs(`closePipeModal()`);
await setup("收窄生效了", Promise.resolve(narrowed === 2));
await openSubs("rss");
await setup("再回 RSS 那一页", until(`document.getElementById('source-sync') !== null
  && !document.getElementById('source-sync').disabled`, 8000));
await evalJs(`document.getElementById('source-sync').click()`);
await setup("「同步全部」的弹窗开了", until(`document.getElementById('pipe-modal').style.display !== 'none'
  && pipeAccounts.get('rss')?.state === 'ready'`, 8000));
check("订阅页的「同步全部」真的是全部(没沿用同步中心收窄过的那一份)",
      await evalJs(`pipeSelected('rss').size`) === 3, await evalJs(`JSON.stringify([...pipeSelected('rss')])`));
await evalJs(`closePipeModal()`);

// ── 2d. **取消也不许改同步中心那一份**(第 1 轮审查 R1 实测 A/B)───────────────
// 关窗时锁解了,但残留的 `pipeSel` 会在**下一次打开普通弹窗**时被
// `loadPipeAccounts` 开头那句 `savePipeDraft()` 存进草稿 —— 所以判据必须落在
// 「再打开一次之后库里那份是什么」和「点开始真正发出去的是什么」上。
const centerState = () => evalJs(`JSON.stringify({
  sel: [...pipeSelected('rss')].sort(),
  daily: document.getElementById('pipe-daily').checked,
  draft: (pipeDrafts.get('rss') || []).slice().sort() })`);
await evalJs(`(async () => { switchTab('pipe'); await openPipeModal(); return true; })()`);
await setup("同步中心又开了(摆原始配置)", until(`pipeAccounts.get('rss')?.state === 'ready'`, 8000));
await setDaily(true);
// 判据是「跟进去之前**一模一样**」,不是某个写死的名单 —— 这一刻同步中心正好是
// 上一节收窄过的那两条,而「锁定一次之后它还剩几条」正是出事的地方
const before = await centerState();
await evalJs(`closePipeModal()`);
await openSubs("rss");
await setup("第三次回 RSS 那一页", until(`document.getElementById('source-sync') !== null`, 6000));
await evalJs(`syncSource([11])`);
await setup("单条的弹窗开了", until(`document.getElementById('pipe-modal').style.display !== 'none'
  && pipeAccounts.get('rss')?.state === 'ready'`, 8000));
await evalJs(`closePipeModal()`);          // 不执行,直接取消
await evalJs(`(async () => { switchTab('pipe'); await openPipeModal(); return true; })()`);
await setup("同步中心最后一次开", until(`pipeAccounts.get('rss')?.state === 'ready'`, 8000));
const after = await centerState();
const back = JSON.parse(after);
check("打开再取消一条订阅的同步:同步中心那一份原样还在(订阅选择 + 草稿)",
      after === before && back.sel.length > 0, `之前 ${before} / 之后 ${after}`);
check("打开再取消也没把「跑完出日报」那个勾取消掉", back.daily === true, after);
await setDaily(false);
await evalJs(`closePipeModal()`);

// ── 3. 公众号那一页的「同步全部」 ───────────────────────────────────────────
await openSubs("wechat");
await setup("公众号那一页摆出来了", until(`!document.getElementById('subs-wechat').hidden
  && !document.getElementById('subs-wechat-sync').disabled`, 8000));
await evalJs(`document.getElementById('subs-wechat-sync').click()`);
await setup("弹窗第三次开了", until(`document.getElementById('pipe-modal').style.display !== 'none'
  && pipeReadyChannels().length === 1`, 8000));
s = await snap();
check("公众号那一页进来:锁在本机绿泡泡那条通道上",
      s.on.join() === 'wechat_local' && s.off.sort().join() === 'inbox,rss,wechat_local', JSON.stringify(s));
check("公众号那一次也只跑入库四段,不再顺手跑全库那两段慢活",
      s.stages.join() === 'crawl,export,index,extract', JSON.stringify(s.stages));
posted.length = 0;
await evalJs(`startPipeFromModal()`);
await setup("公众号那一次也发出了请求", sleep(500).then(() => posted.length > 0));
p = posted[0];
check("请求里带着步骤清单(缺了它后端会兜底成六段全跑)",
      p.body.stages?.join() === 'crawl,export,index,extract'
      && p.body.channels[0].channel === 'wechat_local', JSON.stringify(p.body));

// ── 4. 手动收录照旧直接起跑,不进这个弹窗 ───────────────────────────────────
await openSubs("inbox");
await setup("手动收录那一页摆出来了", until(`document.getElementById('source-sync') !== null
  && !document.getElementById('source-sync').disabled`, 8000));
posted.length = 0;
await evalJs(`document.getElementById('source-sync').click()`);
await setup("它自己发了请求", sleep(600).then(() => posted.length > 0));
check("手动收录没有可轮询的源:照旧直接走 intake,不开弹窗",
      posted[0].path === '/api/intake/inbox/sync'
      && await evalJs(`document.getElementById('pipe-modal').style.display === 'none'`),
      JSON.stringify(posted[0]));

// ── 5. **反过来钉一边**:同步中心那颗「新建同步」没被入口改掉 ────────────────
await evalJs(`(async () => { switchTab('pipe'); await openPipeModal(); return true; })()`);
await setup("同步中心的弹窗开了", until(`document.getElementById('pipe-modal').style.display !== 'none'
  && document.querySelectorAll('#pipe-channel-list input').length === 3`, 8000));
s = await snap();
check("同步中心进来:三个来源都点得动,一个都没灰",
      s.off.length === 0 && !/只同步/.test(s.hint), JSON.stringify(s));
check("同步中心那次步骤不被入口定死(默认六段全勾)",
      s.stages.length === 6, JSON.stringify(s.stages));

// ── 6. 几何:720 高的窗口里弹窗一屏放得下 ───────────────────────────────────
// **判据落在「还要不要滚」上,不落在「用了几栏」** —— 换个排法而没解决同屏,这一条
// 照旧该红。两档都量:默认那一档,以及**勾了「跑完出日报」**那一档(右栏那段说明会
// 长出两行,是最挤的一局,收 `.row` 间距和抬头内边距就是为了它)。
const over = () => evalJs(`(() => { const b = document.querySelector('.pipe-setup-body');
  return b.scrollHeight - b.clientHeight; })()`).then(Number);
const resize = h => send("Emulation.setDeviceMetricsOverride",
                         { width: 1280, height: h, deviceScaleFactor: 1, mobile: false });
// **量的是默认那一档**:订阅名单折着(整条全选)。收窄过的选择会强制摊开名单
// (那正是 `pipeAcctFoldable` 的本意:折起来等于把「只跑其中几个」藏了),那一档
// 本来就更高 —— 先摆回全选,别把上一节留下的收窄算进默认布局。
await evalJs(`pipeSelectAll()`);
await resize(720);
await sleep(200);
check("1280×720:默认那一档一屏全见,不用滚", await over() <= 0, `还要滚 ${await over()}px`);
await setDaily(true);
await sleep(200);
check("1280×720:勾了「跑完出日报」也一屏全见", await over() <= 0, `还要滚 ${await over()}px`);
await setDaily(false);
// **反过来钉一边**:再矮的窗口照旧滚得动 —— 滚不动就是内容被裁在弹窗外面点不着
// (`.pipe-setup-body` 得是个真能裁剪的 div,`fieldset` 兼任不了)。
await resize(420);
await sleep(200);
const tight = await over();
const scrolled = await evalJs(`(() => { const b = document.querySelector('.pipe-setup-body');
  b.scrollTop = 9999; return b.scrollTop > 0; })()`);
check("放不下的时候内容没被裁掉:正文滚得动", tight > 0 && scrolled === true,
      `溢出 ${tight}px、滚到 ${scrolled}`);
await send("Emulation.clearDeviceMetricsOverride");

// ── 5. **冷态**:刷新之后第一下就是订阅页那个弹窗 ─────────────────────────────
// 2d 那一节是**热态**(同步中心已经初始化过),钉的是「配置原样还在」。冷态是另一
// 条路:刷新之后同步中心还没读过 rss 的订阅,而锁定那一次会把它读成 ready。只还原
// `pipeSel`、把 ready 缓存留在原地的那一版,会让下一次打开时 `loadPipeAccounts`
// 开头那句 `savePipeDraft()` 把「ready 却一条都没选」当成「用户明确清空」存成空草稿:
// 三条订阅一条都不勾、「开始同步」点不动,而同一个刷新后直接进同步中心是全选的
// (第 1 轮审查 R2)。所以这一节**必须自己刷新一次**,热态那一节替不了它。
await evalJs(`localStorage.setItem('kb-sync-channels', JSON.stringify(['rss'])); true`);
// **必须是真刷新**(`Page.reload`,不是 navigate 到同一个地址):同一个 URL 只换
// hash 不重新加载文档,内存里的 `pipeAccounts` / `pipeSel` / `pipeDrafts` 一个都不
// 会清掉 —— 那就还是热态,这一节压根验不到东西(实测 navigate 那一版:刷新前后
// 两份现场逐字节相同,注入掉修复也照旧全绿)。
// 换页标记:旧文档里埋一个,等它不见了才算真换过去 —— 刷新刚发出的那一瞬间
// 旧上下文还在答话,`pipeConnected` 那一问会被它答成 true,于是下一句判据落在
// 正在换页的空档里直接抛(实测 `Uncaught :: pipeAccounts.size === 0`)。
// 判据一律带 `typeof` 守卫:新文档里脚本还没跑完时问什么都是 ReferenceError。
await evalJs(`window.__coldMark = 1; true`);
await send("Page.reload", { ignoreCache: true });
await setup("刷新后重新连上后端(真换过文档了)",
            until(`typeof window.__coldMark === 'undefined'
                   && typeof pipeConnected !== 'undefined' && pipeConnected`, 10000));
await setup("刷新后内存是空的(冷态摆成了)",
            until(`typeof pipeAccounts !== 'undefined'
                   && pipeAccounts.size === 0 && pipeDrafts.size === 0`, 8000));
await openSubs("rss");
await setup("刷新后 RSS 那一页摆出来了", until(`document.getElementById('source-sync')
  && !document.getElementById('source-sync').disabled`, 8000));
await evalJs(`document.getElementById('source-sync').click()`);
await setup("冷态下锁定弹窗开了", until(`document.getElementById('pipe-modal').style.display !== 'none'
  && pipeAccounts.get('rss')?.state === 'ready'`, 8000));
await evalJs(`closePipeModal()`);          // 暂不执行
await evalJs(`(async () => { switchTab('pipe'); await openPipeModal(); return true; })()`);
await setup("冷态下同步中心开了", until(`pipeAccounts.get('rss')?.state === 'ready'`, 8000));
const cold = () => evalJs(`JSON.stringify({ sel: [...pipeSelected('rss')].sort(),
  draft: pipeDrafts.get('rss') || null, off: document.getElementById('pipe-start').disabled })`);
let c = JSON.parse(await cold());
check("刷新后先从订阅页打开再取消:同步中心照旧全选、「开始同步」点得动",
      c.sel.length === 3 && c.off === false, await cold());
// **反过来钉一边**:自己清空的那一份照旧算数 —— 不能为了修上面那条,把「一条都
// 不勾」一概当成没初始化、再给补回全选(那三种状态必须分得开)。
await evalJs(`pipeSelectNone()`);
await evalJs(`closePipeModal()`);
await evalJs(`(async () => { switchTab('pipe'); await openPipeModal(); return true; })()`);
await setup("清空之后又开了一次", until(`pipeAccounts.get('rss')?.state === 'ready'`, 8000));
c = JSON.parse(await cold());
check("明确清空过的那一份照旧是空的(没被当成未初始化补成全选)",
      c.sel.length === 0 && c.off === true, await cold());
await evalJs(`closePipeModal()`);

check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | "));
} catch (e) {
  if (e.message !== "stop") fails.push(`探针中断 — ${e.message}`);
}

const out = [...oks.map(n => `  ✓ ${n}`), ...fails.map(n => `  ✗ ${n}`)].join("\n");
const text = `${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`;
let off = 0;
while (off < Buffer.byteLength(text)) {
  try { off += writeSync(1, text.slice(off)); }
  catch (e) { if (e.code !== "EAGAIN") throw e; }
}
ws.close();
proc.kill();
server.close();
process.exitCode = fails.length ? 1 : 0;
