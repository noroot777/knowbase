// 起跑弹窗里的**步骤多选**和**「跑完出日报」**那个勾。
//
// 原来两个入口各自固定(「新建同步」六段不出日报、「同步全部并生成」四段出日报),
// 名字上看不出区别;而那两段全库的慢活一跑几十分钟,谁都没说一声。这一份钉的是:
//
// ① **勾了什么就跑什么**:起跑请求里的 `stages` 等于勾着的那几段,顺序按固定那一份;
// ② **不勾「抓取内容」时,来源/时间范围/订阅三块一起禁掉,请求里一个来源字段都不带**
//    —— 留着能勾而后端不跑 crawl,等于把勾选静默忽略;
// ③ **「跑完出日报」换的是另一条起跑通道**(`/api/daily/run`):步骤固定成日报那一轮
//    自己那一份并禁掉,时间范围和订阅也禁掉(那条通道不收这两样),请求体是
//    `{sync, channels}` 而不是 `{stages, ...}`;
// ④ **取消那个勾要把用户自己那一份还回去**,不是重置成全选;
// ⑤ 反过来钉一边:正常那一档(勾着抓取 + 勾着订阅)照旧带 `channels`/`accounts`/日期。
//
// 自带只读 mock 后端(两个来源、各两条订阅,状态恒为空闲),CDP 直连
// chrome-headless-shell(不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/pipe_stages_probe.mjs
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { attachCdp, sleep } from "./pipe_history_cdp.mjs";
import { MIME } from "./pipe_history_fixture.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
const CHANNELS = {
  channels: [{ id: "wechat_local", label: "公众号", hint: "本机绿泡泡库" },
             { id: "rss", label: "订阅源", hint: "RSS / Atom" }],
  default: "wechat_local", labels: {}, aliases: {},
};
const ACCOUNTS = { wechat_local: ["公众号甲", "公众号乙"], rss: ["某博客"] };
const FIXTURES = {
  "/api/stats": { documents: 21674 },
  "/api/pipeline/channels": CHANNELS,
  "/api/facets": { tags: [], entities: [], accounts: [] },
  "/api/projects": { tools: [], kinds: {}, total_entries: 0 },
  "/api/sources/catalog": { modules: [] },
  "/api/daily/list": { days: [] },
  // 覆盖确认读的就是它（`confirmDailyOverwrite`）。缺缺的那一档被
  // catch-all 的 `{}` 答成「没有日报」，那正好把要验的那道闸跳过去，
  // 所以这一格是**可改的**：两个方向各铉一次。
  "/api/daily/reports": { reports: [] },
  "/api/pipeline/runs": { runs: [], error: "", version: "none", keep_days: 3 },
  // 日报那一轮出完报之后还整不整理图鉴和 Wiki,看这里面的 `library`(GET 外面套了
  // 一层 `{settings, ...}`,见 `schedules.js`)。**不摆这一格的话读不到**,而
  // 「读不到」是第三档文案 —— 那一档一绿,"开着"和"关着"两档就都没验过。
  "/api/daily/settings": { settings: { enabled: true, time: "07:30", sync: true,
                                       library: true, first_days: 1, channels: [],
                                       next_run: "" },
                           channels: [], default_channels: [] },
  "/api/pipeline/status": { running: false, stop_requested: false, stopped: false,
                            stop_supported: true, last_run: null, log: [],
                            result_version: "none" },
};
// 起跑请求原样留下来:判据落在「后端收到的是什么」,不落在页面上的勾选快照
const posted = [];
// 「今天已经有一份日报」这个事实，用例中间要换的
const today = new Date().toLocaleDateString("sv");
let REPORTS = [];

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
    req.on("end", () => {
      posted.push({ path, body: raw ? JSON.parse(raw) : null });
      json({ ok: true });
    });
    return;
  }
  if (path === "/api/pipeline/accounts") {
    const ch = new URLSearchParams(req.url.split("?")[1] || "").get("channel") || "wechat_local";
    return json({ accounts: ACCOUNTS[ch] || [] });
  }
  if (path === "/api/daily/reports") return json({ reports: REPORTS });
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
// 勾 / 取消一段:走真事件,和手点同一条路
const setStage = (k, on) => evalJs(`(() => {
  const i = document.querySelector('#pipe-stage-list input[data-stage=${k}]');
  i.checked = ${on}; i.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
const setDaily = on => evalJs(`(() => {
  const i = document.getElementById('pipe-daily');
  i.checked = ${on}; onPipeDailyToggle(); return true; })()`);
const snap = () => evalJs(`JSON.stringify({
  stages: [...document.querySelectorAll('#pipe-stage-list input:checked')].map(i => i.dataset.stage),
  locked: [...document.querySelectorAll('#pipe-stage-list input')].every(i => i.disabled),
  count: document.getElementById('pipe-stage-count').textContent,
  hint: document.getElementById('pipe-stage-hint').textContent,
  est: document.getElementById('pipe-est').textContent,
  btn: document.getElementById('pipe-start').textContent,
  off: document.getElementById('pipe-start').disabled,
  srcOff: document.getElementById('pipe-source-fields').disabled,
  scopeOff: document.getElementById('pipe-scope-fields').disabled })`).then(JSON.parse);

try {
await send("Page.navigate", { url: `${base}/#pipe` });
await setup("面板连上后端", until(`typeof pipeConnected !== 'undefined' && pipeConnected`, 8000));
await evalJs(`openPipeModal()`);
await setup("弹窗把两个来源读出来",
            until(`document.querySelectorAll('#pipe-channel-list input').length === 2`, 8000));
await setup("步骤那一组画出来了",
            until(`document.querySelectorAll('#pipe-stage-list input').length === 6`, 4000));
// 默认只勾着默认来源,把另一条也勾上:两个来源才验得到「来源 A · B」那一句
await evalJs(`(() => {
  const i = document.querySelector('#pipe-channel-list input[data-run-channel=rss]');
  i.checked = true; i.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
await setup("两个来源的订阅都读出来了",
            until(`pipeReadyChannels().length === 2 && pipeSelCount() === 3`, 8000));

// ── 1. 默认:六段全勾 ─────────────────────────────────────────────────────────
let s = await snap();
check("默认六段全勾,按钮写几步",
      s.stages.join() === 'crawl,export,index,extract,projects,wiki'
      && /6 步/.test(s.btn) && /已选 6 \/ 6/.test(s.count), JSON.stringify(s));
// 操作条念的是**这一次的事实**,不复述跑哪几段 —— 步骤就在右栏勾着,同一份信息
// 念两遍正是操作条占掉三行的由来(弹窗改两栏时一起收的,见 pipeline.css)。
// **「含 N 段全库的慢活」一档都不许省**:慢活标记在右栏每个格子上,但"这一次一共
// 有几段"只有这里数得出来 —— 少了它,用户就会被一段几十分钟的活偷袭。
check("默认那一档汇总念这次的事实,并说清含几段慢活",
      /公众号 2 个 · 订阅源 1 个 · 不限时间/.test(s.est)
      && /含 2 段全库的慢活/.test(s.est), s.est);
// **反过来钉一边**:步骤名不再在操作条上复述一遍
check("操作条不复述步骤名",
      !/这次会跑/.test(s.est) && !/抓取内容 → 导出文章/.test(s.est), s.est);

check("两个来源的订阅都勾上之后起得了跑", !s.off, JSON.stringify(s));

// ── 2. 慢活那两段取消掉,那句话跟着走 ────────────────────────────────────────
await setStage('projects', false);
await setStage('wiki', false);
s = await snap();
check("取消两段慢活之后不再说有慢活",
      !/慢活/.test(s.est) && /4 步/.test(s.btn) && s.stages.join() === 'crawl,export,index,extract',
      JSON.stringify(s));

// ── 3. 不勾「抓取内容」、导出还在:**来源照旧带着**,只禁订阅和日期 ───────────
// 导出也是按来源跑的(`_run_export` 走 `_per_channel`),所以这一档**不许**把来源
// 一起禁掉、更不许把来源字段从请求里拿掉 —— 后端收到一个没有来源的来源敏感请求
// 会回落到默认采集器,用户选的订阅源被静默换成公众号(第 3 轮审查 R1 实页实测)。
await setStage('crawl', false);
s = await snap();
check("只导出:来源那块照旧能点,只有时间范围和订阅被禁掉",
      !s.srcOff && s.scopeOff, JSON.stringify(s));
check("只导出:就地说清订阅和日期不作数,而导出仍按选的来源跑",
      /时间范围和订阅这次不作数/.test(s.est) && /导出仍按所选来源跑/.test(s.hint),
      `${s.est} // ${s.hint}`);
check("只导出:照旧起得了跑", !s.off, JSON.stringify(s));
posted.length = 0;
await evalJs(`startPipeFromModal()`);
await setup("只导出那一档发出了请求", until(`true`, 100).then(() => sleep(400).then(() => posted.length > 0)));
check("只导出:来源整条带上、accounts 留空(那正是导出实际覆盖的范围),不带日期",
      posted[0]?.path === '/api/pipeline/run'
      && posted[0].body?.stages?.join() === 'export,index,extract'
      && JSON.stringify(posted[0].body?.channels)
         === JSON.stringify([{ channel: 'wechat_local', accounts: [] },
                             { channel: 'rss', accounts: [] }])
      && !('since_date' in posted[0].body),
      JSON.stringify(posted[0]));

// ── 3a. 只导出、而且一个订阅都没勾:**照旧起得了跑** ─────────────────────────
// 导出是按来源扫的、压根不收订阅,所以拿「一个订阅都没勾」把它拦住就等于:用户
// 没有任何办法只补一次导出(订阅那一块这会儿还是禁用的,他连勾都勾不上)。
await evalJs(`pipeSelectNone(); renderPipeEst(); 0`);
await sleep(100);
s = await snap();
check("只导出、一个订阅都没勾:照旧起得了跑",
      !s.off && /导出仍按所选来源跑/.test(s.hint), JSON.stringify(s));
posted.length = 0;
await evalJs(`startPipeFromModal()`);
await setup("那一档也发出了请求", until(`true`, 100).then(() => sleep(400).then(() => posted.length > 0)));
check("只导出、没勾订阅:来源照旧整条带上",
      JSON.stringify(posted[0]?.body?.channels)
      === JSON.stringify([{ channel: 'wechat_local', accounts: [] },
                          { channel: 'rss', accounts: [] }]),
      JSON.stringify(posted[0]));
// **把订阅勾回去**:后面那几局(尤其「正常那一档」)的前提是勾着的,
// `pipeSelectNone()` 是全局的,不还回去会让它们红在一个跟自己无关的地方。
await evalJs(`pipeSelectAll(); renderPipeEst(); 0`);
await setup("订阅勾回来了", until(`pipeSelCount() === 3`, 4000));

// ── 3b. **反过来钉一边**:只剩全库那几步时来源才真的不参与 ───────────────────
await evalJs(`openPipeModal()`);
await setup("弹窗又开回来了", until(`document.querySelectorAll('#pipe-stage-list input').length === 6`, 4000));
await setStage('crawl', false);
await setStage('export', false);
s = await snap();
check("全库那几步:来源、时间范围、订阅三块一起禁掉", s.srcOff && s.scopeOff, JSON.stringify(s));
check("全库那几步:就地说清那三块这次不作数",
      /这几步都是全库的，来源、时间范围和订阅这次都不作数/.test(s.est)
      && /这几步都是全库的/.test(s.hint), `${s.est} // ${s.hint}`);
posted.length = 0;
await evalJs(`startPipeFromModal()`);
await setup("全库那一档发出了请求", until(`true`, 100).then(() => sleep(400).then(() => posted.length > 0)));
check("全库那几步:一个来源字段都没有",
      posted[0]?.body?.stages?.join() === 'index,extract'
      && !('channels' in posted[0].body) && !('since_date' in posted[0].body),
      JSON.stringify(posted[0]));

// ── 4. 一步都没勾:按不下去,也说清为什么 ────────────────────────────────────
await evalJs(`openPipeModal()`);
await setup("弹窗又开回来了", until(`document.querySelectorAll('#pipe-stage-list input').length === 6`, 4000));
await evalJs(`pipeStageToggleAll()`);        // 现在不是全选,先补满
await evalJs(`pipeStageToggleAll()`);        // 再清空
s = await snap();
check("一步都没勾:按不下去,汇总说什么也不会发生",
      s.off && !s.stages.length && /一步都没勾，这次什么也不会发生/.test(s.est), JSON.stringify(s));
await evalJs(`pipeStageToggleAll()`);        // 还原成全选
await sleep(100);

// ── 5. 「跑完出日报」:步骤固定成日报那一份并禁掉,两块范围也禁掉 ────────────
await setStage('wiki', false);               // 先留下一份"用户自己的选择"
await setDaily(true);
s = await snap();
check("勾了出日报:步骤固定成日报那一轮自己那几段,而且改不动",
      s.stages.join() === 'crawl,export,index,extract,projects,wiki' && s.locked,
      JSON.stringify(s));
check("勾了出日报:来源还开着,时间范围和订阅禁掉",
      !s.srcOff && s.scopeOff, JSON.stringify(s));
check("勾了出日报:说清步骤由它自己排、出完报接着整理,而且不按订阅和日期筛",
      /由它自己排/.test(s.hint) && /出完报接着整理工具和更新 Wiki/.test(s.hint)
      && /不按订阅和日期筛/.test(s.hint), s.hint);
// 出完报那两段是全库的慢活。**念的顺序不许按步骤表一路念下去** —— 那样会把
// 「先整理、后出报」念成事实,而真顺序是出报插在中间(`library_tail` 在收尾的
// finally 里)。
check("勾了出日报:出报之后那两段也要念出来,顺序是出报在中间",
      /跑完生成今天的日报，再整理工具 → 更新 Wiki/.test(s.est)
      && /含 2 段全库的慢活/.test(s.est), s.est);
check("勾了出日报:汇总念来源,不念订阅数和日期段",
      /跑完生成今天的日报/.test(s.est) && /来源 公众号 · 订阅源/.test(s.est)
      && !/不限时间/.test(s.est) && !/个 ·/.test(s.est), s.est);
// 反过来钉一边:设置里把那个勾关掉之后,这句话不许再承诺整理 —— 关着的时候
// `daily_library.library_round` 压根不跑,图鉴和三张图谱就是不更新。走的是共享层
// 那道门(`schedulesSaved`,别处保存成功就是这么交过来的)。
await evalJs(`schedulesSaved('daily', { enabled: true, time: '07:30', sync: true,
  library: false, first_days: 1, channels: [], next_run: '' }); 0`);
await sleep(60);
s = await snap();
check("设置里关掉整理:步骤退回四段,话也跟着改口",
      s.stages.join() === 'crawl,export,index,extract'
      && /出完报不整理图鉴和 Wiki/.test(s.hint)
      && !/慢活/.test(s.est), `${s.hint} // ${s.est}`);
await evalJs(`schedulesSaved('daily', { enabled: true, time: '07:30', sync: true,
  library: true, first_days: 1, channels: [], next_run: '' }); 0`);
await sleep(60);
s = await snap();
check("再开回来:六段和那句承诺都回来了",
      s.stages.join() === 'crawl,export,index,extract,projects,wiki'
      && /出完报接着整理/.test(s.hint), `${s.hint} // ${s.est}`);
posted.length = 0;
await evalJs(`startPipeFromModal()`);
await sleep(400);
check("勾了出日报:走的是日报通道,请求体是 sync + 来源",
      posted[0]?.path === '/api/daily/run' && posted[0].body.sync === true
      && posted[0].body.channels.join() === 'wechat_local,rss'
      && !('stages' in posted[0].body), JSON.stringify(posted[0]));
check("今天还没有日报:不多问一句覆盖",
      (await evalJs(`document.querySelectorAll('.confirm-mask').length`)) === 0);
// 没问过他就等于没批准覆盖:请求里那份批准的版本是空的,后端据此在"那一天其实
// 已经有一份"时拒掉(审查 R4;空版本的含义见 kb/daily_overwrite.py)。
check("没问过那一档:请求带着今天这一天 + 空版本",
      posted[0]?.body.overwrite_day === today
      && posted[0]?.body.overwrite_version === "", JSON.stringify(posted[0]?.body));

// ── 5b. 今天已经有一份日报:起跑前要先问一句覆盖 ──────────────
// 日报页那颗按钮一直是先问的，这条路漏掉这一道就是静悄悄盖掉一份日报。后端在落盘
// 之前还会按这份批准核对一次（`kb/daily_overwrite.py`）：那一道管「问的是不是真正
// 要写的那一份」，这一道管「问没问过」，两道都要。
// `version` 是**整份文件的内容身份**(清单里那一格,口径在 kb/daily_overwrite.py);
// `generated` 只给人看 —— 拿它当版本的话同一分钟内换过正文的那一份会被静默盖掉。
REPORTS = [{ date: today, docs: 12, generated: "2026-10-20 07:31",
             version: "9f1c2b3d4e5f6071" }];
await evalJs(`openPipeModal()`);
await setup("弹窗再开一次", until(`document.querySelectorAll('#pipe-stage-list input').length === 6`, 4000));
await setDaily(true);
posted.length = 0;
// `evalJs` 是 awaitPromise:true —— 起跑那一步现在会停在确认框上等人点，
// 所以这几处**不等它的 Promise**（`void` 丢掉），否则探针自己挂在这里。
await evalJs(`void startPipeFromModal(); 0`);
await setup("覆盖确认弹出来了", until(`document.querySelectorAll('.confirm-mask').length === 1`, 3000));
check("问的是今天那一天,而且说清会覆盖",
      (await evalJs(`document.querySelector('.confirm-mask h2').textContent`)).includes(today)
      && /覆盖/.test(await evalJs(`document.querySelector('.confirm-body').textContent`)));
check("没点确认之前一个请求都没发", posted.length === 0, JSON.stringify(posted));
await evalJs(`document.querySelector('.confirm-mask [data-act=cancel]').click()`);
await sleep(300);
check("取消就真的不起跑", posted.length === 0, JSON.stringify(posted));
await evalJs(`void startPipeFromModal(); 0`);
await setup("确认框再弹一次", until(`document.querySelectorAll('.confirm-mask').length === 1`, 3000));
await evalJs(`document.querySelector('.confirm-mask [data-act=ok]').click()`);
await setup("确认之后才发出请求", until(`true`, 100).then(() => sleep(500).then(() => posted.length > 0)));
check("确认之后走的还是日报通道",
      posted[0]?.path === '/api/daily/run' && posted[0].body.sync === true,
      JSON.stringify(posted[0]));
// **点击那一刻的浏览器日期不是写文件时的目标身份**(这条路要先跑几小时同步),
// 所以"问过了"必须带着它问的是哪一天哪一版下去,由后端在落盘之前核对(审查 R4)。
check("批准跟着请求下去:哪一天 + 哪一版",
      posted[0]?.body.overwrite_day === today
      && posted[0]?.body.overwrite_version === "9f1c2b3d4e5f6071",
      JSON.stringify(posted[0]?.body));

// ── 5c. 那一天有日报、而清单给不出它是哪一版 ──────────────────────────────
// 这一档要**说出「有」**(不是「查不到有没有」),而批准送 '*'(含义相同:那一天
// 不管哪一版都盖)。两档合成一句的后果是用户以为自己在生成头一份(审查 R1)。
REPORTS = [{ date: today, docs: 12, generated: "2026-10-20 07:31" }];
await evalJs(`openPipeModal()`);
await setup("弹窗第四次开回来", until(`document.querySelectorAll('#pipe-stage-list input').length === 6`, 4000));
await setDaily(true);
posted.length = 0;
await evalJs(`void startPipeFromModal(); 0`);
await setup("查不到版本时照旧弹确认", until(`document.querySelectorAll('.confirm-mask').length === 1`, 3000));
check("说的是「已经有一份、但查不到是哪一版」",
      /已经有一份日报/.test(await evalJs(`document.querySelector('.confirm-body').textContent`))
      && /查不到它是哪一版/.test(await evalJs(`document.querySelector('.confirm-body').textContent`)),
      await evalJs(`document.querySelector('.confirm-body').textContent`));
await evalJs(`document.querySelector('.confirm-mask [data-act=ok]').click()`);
await setup("确认之后才发出请求", until(`true`, 100).then(() => sleep(500).then(() => posted.length > 0)));
check("批准送的是「那一天不管哪一版」",
      posted[0]?.body.overwrite_day === today
      && posted[0]?.body.overwrite_version === "*", JSON.stringify(posted[0]?.body));
REPORTS = [];

// ── 6. 取消那个勾:还回用户自己那一份,不是重置成全选 ────────────────────────
await evalJs(`openPipeModal()`);
await setup("弹窗第三次开回来", until(`document.querySelectorAll('#pipe-stage-list input').length === 6`, 4000));
await setDaily(false);
s = await snap();
check("取消出日报:还回勾上之前那一份(少了更新 Wiki),不是重置成全选",
      s.stages.join() === 'crawl,export,index,extract,projects' && !s.locked
      && !s.scopeOff, JSON.stringify(s));

// ── 7. 反过来钉一边:正常那一档照旧带来源、订阅和日期 ───────────────────────
await setStage('wiki', true);
await evalJs(`applyPipePreset(1)`);
await sleep(200);
posted.length = 0;
await evalJs(`startPipeFromModal()`);
await sleep(400);
const b = posted[0]?.body || {};
check("正常那一档照旧带来源、订阅和日期段",
      posted[0]?.path === '/api/pipeline/run' && b.stages?.length === 6
      && b.channels?.length === 2 && b.channels[0].accounts.length === 2
      && !!b.since_date && !!b.until_date, JSON.stringify(posted[0]));

// ── 8. **每一行步骤都点得到**:操作条不许盖在它们上面 ──────────────────────
// 判据落在「真按一次鼠标,这一行有没有反应」,不落在截图上:弹窗正文原来是
// fieldset 自己滚、而操作条又提了一层叠层(两条都是为别的事加的),内容于是绘到
// 了操作条底下 —— 1452×806 这一档清空步骤之后点「抓取内容」「重建索引」都不
// 改变选择,而那两行正好在操作条的覆盖区里(第 4 轮审查 N2 现场观察)。
await send("Emulation.setDeviceMetricsOverride",
           { width: 1452, height: 806, deviceScaleFactor: 1, mobile: false });
await evalJs(`openPipeModal()`);
await setup("弹窗第五次开回来", until(`document.querySelectorAll('#pipe-stage-list input').length === 6`, 4000));
await evalJs(`pipeStageToggleAll()`);   // 清空:审查现场就是这一步之后点不动的
await sleep(200);
// 每一行滚进来之后问两件事:**它在不在正文那个滚动盒里**(溢出到操作条底下或
// 弹窗外面的那几行,在页面上反倒"点得到",所以光做命中测试会漏),以及那个点上
// 是谁(被盖住时命中的是操作条)。
const reach = await evalJs(`JSON.stringify([...document.querySelectorAll('#pipe-stage-list .pipe-channel')].map(l => {
  l.scrollIntoView({ block: 'center' });
  const b = document.querySelector('.pipe-setup-body, #pipe-fields').getBoundingClientRect();
  const r = l.getBoundingClientRect(), x = Math.round(r.left + 18), y = Math.round(r.top + r.height / 2);
  const hit = document.elementFromPoint(x, y);
  return { stage: l.querySelector('input')?.dataset.stage,
           inBox: r.top >= b.top - 1 && r.bottom <= b.bottom + 1,
           reachable: !!hit && l.contains(hit),
           by: !hit ? 'none' : hit.closest('.pipe-setup-footer') ? 'footer'
               : (hit.className || hit.tagName) }; }))`).then(JSON.parse);
check("六行步骤都待在正文的滚动盒里,也没有被别的东西盖住",
      reach.length === 6 && reach.every(r => r.inBox && r.reachable),
      JSON.stringify(reach.filter(r => !(r.inBox && r.reachable))));
// **真按一次鼠标**:命中测试说得通,还要让这一下确实改变选择(程序化 click
// 无视遮挡,证明不了这件事)。挑「重建索引」那一行 —— 现场点不动的正是它。
const spot = await evalJs(`(() => {
  const l = document.querySelector('#pipe-stage-list input[data-stage=index]').closest('.pipe-channel');
  l.scrollIntoView({ block: 'center' });
  const r = l.getBoundingClientRect();
  return JSON.stringify({ x: Math.round(r.left + 18), y: Math.round(r.top + r.height / 2) }); })()`).then(JSON.parse);
for (const type of ["mousePressed", "mouseReleased"])
  await send("Input.dispatchMouseEvent", { type, x: spot.x, y: spot.y, button: "left", clickCount: 1 });
await sleep(200);
s = await snap();
check("真按一次鼠标,「重建索引」那一行就勾上了", s.stages.join() === 'index',
      `${JSON.stringify(s.stages)} @ ${JSON.stringify(spot)}`);

check("页面没有脚本报错", pageErrors.length === 0, pageErrors.join(" | "));
} catch (e) {
  if (e.message !== "stop") fails.push(`探针自己炸了:${e.message}`);
}

console.log(oks.map(o => "  ok  " + o).join("\n"));
if (fails.length) console.log(fails.map(f => "FAIL  " + f).join("\n"));
console.log(`\n${oks.length} ok, ${fails.length} failed`);
ws.close(); proc.kill(); server.close();
process.exit(fails.length ? 1 : 0);
