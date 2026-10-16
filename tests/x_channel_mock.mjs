// X 通道探针的 mock 后端。形状照 `kb/web/intake_api.py`:通道清单、人话名字、
// 便宜的就绪判断都由后端给。**跟场景脚本分开只是为了行数**(单文件 700 行上限),
// 边界按职责切:这一份只管「后端会怎么回答」,`x_channel_probe.mjs` 只管「页面上
// 该发生什么」。
//
// 这份模块求值时就把 server 起起来(顶层 await),导出 `base` 给场景脚本用。
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
// 形状照 kb/web/intake_api.py:通道清单、人话名字、便宜的就绪判断都由后端给。
const CHANNELS = [
  { id: "browser", label: "独立 profile 的登录态", hint: "在一个独立浏览器 profile 里登录一次。" },
  { id: "attach", label: "我日常用的浏览器", hint: "直连你正在用的那个 Chrome/Edge。" },
  { id: "feedgrab", label: "feedgrab(cookie)", hint: "用一份 X cookie 调 X 自己的接口。" },
];
const LOGIN_CMD = "FEEDGRAB_DATA_DIR=/tmp/kb/x-feedgrab/sessions feedgrab login twitter";
// 「下一步怎么办」由后端给。有的通道要做的事没法塞进输入框(去地址栏勾一次远程
// 调试开关),所以 mock 也得按通道回答 —— 一律回空串的话"前端把它显示出来了没有"
// 在页面上压根看不出来。
// feedgrab 那档的第三项**就是那条命令**(真后端如此:待配置横幅那边没有可复制
// 块,所以命令必须从这里出来)。于是设置面板上它和 `login_command` 撞在一起 ——
// 下面那一项钉住前端不把同一句话并排写两遍。
const IMPORTED = "上次从日常浏览器导入：2026-10-15 17:40:00，13 个 cookie（当场验证通过）";
const NEXT_STEP = { attach: "在浏览器地址栏打开 chrome://inspect/#remote-debugging，勾上那个开关。",
                    feedgrab: LOGIN_CMD };
let channel = "feedgrab";   // 这一局从"不是默认档"的那一条起步
// 存下来的那两条可执行文件位置。**动作端点要照它们回答** —— 真后端读的就是
// `reg.get_settings` 里这几个字段(`x_channels.probe` / `x_browser.open_login_window`
// 都拿 `settings.get('x_chrome_path')`)。mock 这边不跟着回的话,「改了路径没保存
// 就点按钮,动作用的是旧路径」在页面上压根看不出来。
let chromePath = "", feedgrabBin = "";
let unconfigured = false;   // 第 9 节用:让这条订阅变成"待配置"
const saved = [];      // PUT /settings 真发出去的 body
const posted = [];     // 通道那两个 POST
let checkReply = { ready: true, detail: "登录态有效,首页读到 12 条推文" };

const settings = () => ({ enabled: false, interval_minutes: 1440, next_run: 0,
                          x_channel: channel, x_feedgrab_bin: feedgrabBin,
                          x_chrome_path: chromePath });
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
      interval_minutes: 1440, url_hint: "输入 X / Twitter 账号主页", note: "X 的取法在下面的「取法」里选" },
    { id: "rss", label: "RSS", description: "让值得读的文章，自己来到这里。",
      channels: ["rss"], manager: "feeds", icon: "rss", adapter: "rss",
      interval_minutes: 360, url_hint: "输入 RSS 地址", note: "" },
  ] },
};

// 导入成功过没有。真后端这一格落在那个 profile 的 Cookies 文件上(`readiness`),
// 这里就用一个标志代表它。
let imported = false;
// 下一条 `/x-channel` GET 压慢多久(一次性,用完就清)。
// **用来把「读还在途中,用户已经改了下拉」摆出来** —— 那条读回来之后会重建下拉、
// 重设 `sel.value = data.current`,把用户刚选的那一档静默换回去。
//
// 原来这里想摆的是「两条同来源的 GET 在途、先发那条晚回来」。那个形态是真的
// (审查那一轮在**网络层按住**第一份已经拿到的回包、放第二条过去,复现出
// `selected=browser` 而 `saved=feedgrab`),但**拿慢服务端摆不出来**:两条 URL
// 完全一样的 GET 被浏览器去重排队了,第二条要等第一条回完才发出(诊断:第一条
// t=4549 进来、压 900ms,第二条 t=5452 才到),于是回包顺序不会倒。换成这一个 ——
// 摆得出来,而且要的恰好是「发起时取代次」那同一道闸。
let getHold = 0, getForget = false;
// **谁的回包先发出去。** 4.6f 要的那个形态(旧读先抵达、动作随后才到)靠时序摆
// 出来,所以得有个地方证明这一轮真摆成了 —— 不然用例测的是另一件事。
let stamps = [];
// 下一条导入 POST 直接把连接掐断(一次性)。**要的是传输层断掉,不是 500** ——
// 500 是一个明确答复(「后端说失败了」),掐断才是「这一次结果不知道」,而那正是
// 页面要留住的那条交代(见 `x_channel.js` 的 `xChanUnsure`)。
let killImport = false;
// 下一条 settings PUT 压慢多久(一次性)。**用来摆「保存在途时用户又改了一次」**:
// 那条旧回包落地时会重填整张表单,把他刚改的那一份静默换掉。
let putHold = 0;
// 下一条 `/x-channel` GET 直接回 503(一次性)。摆「读不出可选的取法」那个失败态。
let failGet = false;
// 下一条「检查连接」压慢多久(一次性)。**用来摆「检查在途时同一档取法下换了
// 一条路径并保存」** —— 真实那一下要起浏览器或跑一次 feedgrab doctor,几十秒里
// 换个路径再保存是真会发生的事,而旧那次检查的结论说的已经是另一份配置了。
let checkHold = 0;
// 每个动作端点**实际按哪一份配置跑的**。页面上看不见后端用了什么,所以记在这儿。
const used = [];
// 「这一份设置」的身份,照 `kb/feeds/x_channels.py` 的 `config_of`:取法 + 两条
// 可执行文件位置(去掉首尾空白)。**动作端点照它核对调用方带来的那一份** ——
// mock 不装这道闸的话,真后端 409、而探针这边三颗按钮一路绿(口径同上面那条
// 「按方法回答」:mock 不像真后端的地方,就是探针全绿而页面坏掉的地方)。
const identity = c => JSON.stringify([(c && c.x_channel) || "",
                                      ((c && c.x_feedgrab_bin) || "").trim(),
                                      ((c && c.x_chrome_path) || "").trim()]);
const storedConfig = () => ({ x_channel: channel, x_feedgrab_bin: feedgrabBin,
                              x_chrome_path: chromePath });

const body = req => new Promise(res => { let raw = ""; req.on("data", c => { raw += c; });
                                         req.on("end", () => res(JSON.parse(raw || "{}"))); });
const server = createServer(async (req, res) => {
  const path = req.url.split("?")[0];
  const json = (data, code = 200) => {
    const buf = Buffer.from(JSON.stringify(data));
    res.writeHead(code, { "Content-Type": "application/json", "Content-Length": buf.length });
    res.end(buf);
  };
  // **子路由要认连字符**:`\w` 不match `-`,于是 `/x-channel/import-login` 压根
  // 匹配不上这条正则、落到后面的静态文件分支去(404 一份不存在的文件)—— 按钮在
  // 页面上于是永远报一句看不懂的错,而 mock 这边一声不吭。口径同下面那条「按方法
  // 回答,和真后端一样」:mock 不像真后端的地方,就是探针全绿而页面坏掉的地方。
  const m = path.match(/^\/api\/intake\/(\w+)\/x-channel(\/[\w-]+)?$/);
  if (m) {
    if (m[1] !== "social") return json({ detail: "该来源没有可选的采集通道" }, 404);
    // **按方法回答,和真后端一样**:这个 mock 原来只看 URL,于是「检查连接」发
    // 的是 GET 也照样返回成功 —— 真路由只收 POST,按钮在真页面上永远报 405,
    // 而这套探针全绿(2026-10-08 审查实测)。
    if (m[2] === "/login" || m[2] === "/check" || m[2] === "/import-login") {
      posted.push(`${req.method} ${path}`);
      if (req.method !== "POST") return json({ detail: "Method Not Allowed" }, 405);
      // **那道闸在最前面,`used` 记在它之后** —— `used` 代表"这一次真按这一份跑
      // 了",被拒的那一次什么都没跑,记进去就等于让探针看不出拒绝和执行的差别。
      const expect = await body(req);
      if (!(expect.x_channel || "").trim())
        return json({ detail: "这个动作要带上你眼前那份设置（取法和两条可执行文件位置）。" }, 409);
      if (identity(expect) !== identity(storedConfig()))
        return json({ detail: "现在存着的设置已经不是你眼前这一份了（存着的是"
                              + `「${CHANNELS.find(c => c.id === channel).label}」那一档）。`
                              + "多半是另一个标签页改过并保存了——页面这就重读一次。" }, 409);
      used.push({ what: m[2].slice(1), channel, chrome: chromePath, bin: feedgrabBin });
      if (m[2] === "/import-login") {
        if (killImport) {
          // 真后端可能已经把 profile 改完了 —— 掐断的只是这条回包。
          killImport = false;
          imported = true;
          stamps.push("import-killed");
          // **先吐一截再断,不能一个字节都不发。** 复用的 keep-alive 连接上一条
          // 请求「还没收到任何回应就断了」时,Chrome 会**自动重发一次**(实测:
          // 那一下重发走到正常分支、拿到成功回包,页面于是根本没进失败路)——
          // 那是浏览器的正当行为,不是被测行为。吐出半截回包之后它不再重发,
          // 而 `r.json()` 在截断的 body 上抛,正是「这一次结果不知道」。
          res.writeHead(200, { "Content-Type": "application/json",
                               "Content-Length": 999 });
          // **要等这半截真的发出去再断**:`destroy()` 紧跟着 `write()` 的话 Node
          // 还没把它冲出去就把 socket 砍了,对端看到的仍是「一个字节都没收到」。
          res.write('{"ok": tru', () => setTimeout(() => req.socket.destroy(), 30));
          return;
        }
        // 门槛和 login 同一道,理由逐字相同:真后端读的是**存下来**那条通道。
        if (channel !== "browser")
          return json({ detail: `当前保存的取法是「${CHANNELS.find(c => c.id === channel).label}」，`
                                + "而这颗按钮写的是「独立 profile 的登录态」那一档的登录态。" }, 400);

        // 慢一点回:真实那一下要停在授权弹窗上(最长 90 秒),而"按下去之后
        // 立刻预告"正是这段时间里页面上唯一的交代 —— 秒回的 mock 测不到它。
        await new Promise(r => setTimeout(r, 1200));
        // **导入这一下正是把「还没登录过」变成「登录过」的那件事**,所以真后端
        // 会把重算过的 `ready/why` 一起回来(口径跟 GET 完全一份)。mock 不跟着
        // 回的话,「页面上那句互相打脸的旧状态」这条探不到 —— 它正是本轮审查的
        // A 项:导入成功后旁边仍挂着「浏览器通道还没有登录过 X」。
        imported = true;
        stamps.push("import");
        return json({ ok: true, login_state: IMPORTED, ready: true, why: "",
                      config: storedConfig(),
                      detail: "已把 13 个 x.com cookie 搬进独立 profile，并在里面读到 9 条推文。" });
      }
      if (m[2] === "/login") {
        // **按存下来的那条通道回答,和真后端一样**:这个 mock 原来无条件返回成功,
        // 于是"改了选择还没保存就点登录"这条路在页面上看起来永远是对的 —— 而真
        // 后端读的是 `reg.get_settings`,只会回一句"当前保存的取法是「……」"。
        // 前端如今会**先替用户保存再执行**(`xChanEnsureSaved`),这道门槛是它
        // 漏掉时的兜底 —— 两道各有射程,mock 这边照旧摆着。
        if (channel === "attach")
          return json({ detail: "这一档用的是你日常浏览器里现成的登录态，不在这里登录。" }, 400);
        if (channel !== "browser")
          return json({ detail: `当前保存的取法是「${CHANNELS.find(c => c.id === channel).label}」，`
                                + "不在这里登录。" }, 400);
        return json({ ok: true, config: storedConfig(),
                      detail: "已打开登录窗口。登录成功后关掉它。" });
      }
      // **这一次检的是哪一份,跟着结论回去**:它慢到几十秒,回包落地时面板上
      // 那一份可能已经不是它了(真后端同此,见 `intake_api.x_channel_check`)。
      // **身份在请求到达这一刻定稿**(口径同下面那条 GET):真后端读一次 settings
      // 就拿它去跑,按住期间别人改了什么都不属于这一次。在 await 之后才读的话,
      // 被按住的那条回包会携带"按住期间发生的事",这个形态就压根摆不出来。
      const ran = storedConfig();
      if (checkHold) { const ms = checkHold; checkHold = 0; await new Promise(r => setTimeout(r, ms)); }
      return json({ ...checkReply, config: ran });
    }
    // **回包内容在请求到达这一刻定稿,压慢是之后的事** —— 真后端就是这样(处理
    // 请求时读一次状态)。在 await 之后才去读 `imported` 的话,被按住的那条回包会
    // 携带「按住期间发生的事」,于是「旧读盖掉新事实」这个形态压根摆不出来。
    if (failGet) { failGet = false; return json({ detail: "测试：取法服务暂时不可用" }, 503); }
    if (getForget) { getForget = false; imported = false; }
    const snap = channel === "browser" && imported;
    const payload = {
      current: channel, channels: CHANNELS, ready: snap,
      // 完整那一份:前端拿它当「后端此刻存着什么」的身份,不必再去猜路径。
      config: storedConfig(),
      why: snap ? "" : (channel === "attach" ? "没有发现开着远程调试的浏览器"
                                             : "浏览器通道还没有登录过 X"),
      hint: snap ? "" : (NEXT_STEP[channel] || ""),
      login_command: channel === "feedgrab" ? LOGIN_CMD : "",
      needs_login_window: channel === "browser", login_state: imported ? IMPORTED : "",
    };
    if (getHold) {
      const ms = getHold;
      getHold = 0;
      await new Promise(r => setTimeout(r, ms));
    }
    stamps.push("get");
    return json(payload);
  }
  // 探针自己的控制口,只给上面那个一次性的慢回包用(真后端没有这个路径)。
  if (path === "/__probe/slow-get") {
    const q = new URL(req.url, "http://probe").searchParams;
    getHold = Number(q.get("ms") || 0);
    // `forget=1`:把「还没导入过」这个状态摆回来,好让被按住的那条读说的是
    // **导入之前**那一刻(上面 4.6 已经导入成功过了)。
    getForget = q.get("forget") === "1";
    return json({ ok: true });
  }
  // 下一条导入 POST 掐断连接(摆「这一次结果不知道」那个形态)。
  if (path === "/__probe/kill-import") {
    killImport = true;
    return json({ ok: true });
  }
  // 下一条 settings PUT 压慢(摆「保存在途时用户又改了一次」)。
  if (path === "/__probe/slow-put") {
    putHold = Number(new URL(req.url, "http://probe").searchParams.get("ms") || 0);
    return json({ ok: true });
  }
  // 下一条「检查连接」压慢(摆「检查在途时换了路径并保存」)。
  if (path === "/__probe/slow-check") {
    checkHold = Number(new URL(req.url, "http://probe").searchParams.get("ms") || 0);
    return json({ ok: true });
  }
  // 下一条取法 GET 回 503(摆读失败)。
  if (path === "/__probe/fail-get") {
    failGet = true;
    return json({ ok: true });
  }
  // 各动作端点实际按哪一份配置跑的(`?reset=1` 清一次)。
  if (path === "/__probe/used") {
    const was = used.slice();
    if (new URL(req.url, "http://probe").searchParams.get("reset") === "1") used.length = 0;
    return json({ used: was });
  }
  // 回包发出的先后(`?reset=1` 清一次,好让每个场景只看自己那几条)。
  if (path === "/__probe/stamps") {
    const was = stamps;
    if (new URL(req.url, "http://probe").searchParams.get("reset") === "1") stamps = [];
    return json({ stamps: was });
  }
  if (path.match(/^\/api\/intake\/\w+\/settings$/) && req.method === "PUT") {
    const sent = await body(req);
    saved.push(sent);
    // **收到就落账,压慢是之后的事**(口径同上面那条 GET):真后端是先写库再回包,
    // 回包在路上的那段时间里库里已经是新值了。
    if (sent.x_channel) channel = sent.x_channel;
    if ("x_chrome_path" in sent) chromePath = sent.x_chrome_path || "";
    if ("x_feedgrab_bin" in sent) feedgrabBin = sent.x_feedgrab_bin || "";
    const reply = settings();
    if (putHold) { const ms = putHold; putHold = 0; await new Promise(r => setTimeout(r, ms)); }
    return json(reply);
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

// 场景脚本要的那几样。**只读的直接导出**(ESM 的导入绑定是活的),
// **要改的两格走存取器** —— 导入绑定在那一侧是只读的,赋值会当场报错。
export { CHANNELS, NEXT_STEP, posted, saved, used, server, base };
export const mock = {
  get checkReply() { return checkReply; },
  set checkReply(v) { checkReply = v; },
  get unconfigured() { return unconfigured; },
  set unconfigured(v) { unconfigured = v; },
};
