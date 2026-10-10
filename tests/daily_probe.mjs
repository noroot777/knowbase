// 日报面板的浏览器回归:侧栏入口、列表与正文渲染、覆盖区间(按发布时间、接上
// 一份日报)、定时设置保存、生成按钮、来源面板显示译文而不是英文原文。
//
// 需要一个跑着沙箱数据的 kb-web:
//   python3 tests/llm_stub.py --port 8801 &
//   python3 tests/daily_sandbox.py /tmp/kb-daily-sandbox 8801
//   KB_DATA_DIR=... KB_VAULT_ROOT=... uv run uvicorn kb.server:app --port 8799
// CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、不连网、
// 始终无头、临时 profile),与 tests/redesign_probe.mjs 同一套连法。
//
//   HEADLESS_SHELL=<bin> node tests/daily_probe.mjs http://127.0.0.1:8799
import { launchShell } from "./probe_shell.mjs";

const base = process.argv[2];

const { proc, port } = await launchShell(["--window-size=1440,1000"]);

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));

let id = 0;
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
  const mid = ++id;
  return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); });
};
const evalJs = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
// 界面上的状态要等接口回来才落地,拍固定时长会在机器慢的时候假红。
const waitFor = async (expression, ms = 8000) => {
  for (let i = 0; i * 200 < ms; i++) {
    if (await evalJs(expression)) return true;
    await sleep(200);
  }
  return false;
};
// 等运行状态时得自己驱动刷新:面板平时靠 setTimeout 轮询,而无头浏览器把
// 后台页面的定时器节流到分钟级 —— 干等的话客户端会拿着一份二十秒前的
// "还在跑"不放,下一次 runDaily 当场被它自己那道「已在运行」闸门挡掉。
const waitStatus = async (expression, ms = 20000) => {
  for (let i = 0; i * 400 < ms; i++) {
    await evalJs(`refreshDailyStatus()`);
    if (await evalJs(expression)) return true;
    await sleep(400);
  }
  return false;
};

await send("Runtime.enable");
await send("Page.enable");

const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));

await send("Page.navigate", { url: base + "/#daily" });
// initWorkspace 要先把来源模块拉回来才 restoreWorkspaceLocation,首帧之后还有
// 一串接口要落地;等 dailyLoaded 而不是拍一个固定时长。
for (let i = 0; i < 60 && !(await evalJs(`typeof dailyLoaded !== 'undefined' && dailyLoaded`)); i++) await sleep(250);
// 再显式等一次面板自己的三个接口:列表/正文/覆盖区间各走各的,
// dailyLoaded 只代表定时设置回来了。
await evalJs(`loadDailyPanel()`);

// 地址栏 #daily 是 initWorkspace 收尾时才恢复的,比 dailyLoaded 还晚一步。
await waitFor(`document.getElementById('page-crumb').textContent === '每日日报'
  && getComputedStyle(document.getElementById('dailypanel')).display !== 'none'`, 20000);
const where = await evalJs(`JSON.stringify({crumb: document.getElementById('page-crumb').textContent,
  tab: typeof activeTab === 'undefined' ? '?' : activeTab, loaded: dailyLoaded, hash: location.hash,
  shown: getComputedStyle(document.getElementById('dailypanel')).display})`);
check("侧栏有「每日日报」入口", await evalJs(`!!document.getElementById('tab-daily')`));
check("面板打开后标题是每日日报",
      await evalJs(`document.getElementById('page-crumb').textContent`) === "每日日报", where);
check("面板可见",
      await evalJs(`getComputedStyle(document.getElementById('dailypanel')).display !== 'none'`), where);

const today = new Date().toLocaleDateString("sv");
// 沙箱只预置"上一份日报"(昨天那份);今天这一份由探针自己先生成一次,全新
// 沙箱和跑过一次的沙箱才是同一个起点。之前默认它已经在那儿,于是脚本只在
// 复用过的沙箱里全绿,换个干净目录当场四项红。
// 有来源没同步成功时,面板上要有一条留得住的说明(toast 会自己消失,而"这份
// 日报只用了已入库的素材"是看它时一直要知道的事)。状态喂的是真实形状 ——
// 真去跑一轮失败的同步既要连外网、又要等采集超时,那不是这一项要验的东西;
// 「后端确实把通道结果放进状态」由 tests/test_daily_report.py 钉住。
const noteOf = async (state) => JSON.parse(await evalJs(
  `(() => { renderDailySyncNote(${JSON.stringify(state)});
    const el = document.getElementById('daily-sync-note');
    return JSON.stringify({ hidden: el.hidden,
      text: document.getElementById('daily-sync-note-text').textContent }); })()`));
const failed = { channel: 'rss', label: 'RSS', status: 'error', detail: '无法连接来源' };
const okChannel = { channel: 'dr-gzh', label: '公众号', status: 'ok', detail: '同步完成' };
check("状态接口带着各通道的结果",
      'channels' in JSON.parse(await evalJs(
        `(async () => JSON.stringify(await (await fetch('/api/daily/status')).json()))()`)));
let gap = await noteOf({ phase: 'done', channels: [failed, okChannel] });
check("没同步成功的来源写在面板上",
      !gap.hidden && /没有同步成功/.test(gap.text) && /RSS/.test(gap.text)
        && /无法连接来源/.test(gap.text) && !/公众号/.test(gap.text), JSON.stringify(gap));
gap = await noteOf({ phase: 'done', channels: [], last_run: { channels: [failed] } });
check("重启后从上一轮的落盘结果里读回来", !gap.hidden && /RSS/.test(gap.text),
      JSON.stringify(gap));
gap = await noteOf({ phase: 'done', channels: [okChannel] });
check("全都同步成功时没有这条横幅", gap.hidden, JSON.stringify(gap));

const hasToday = `(async () => (await (await fetch('/api/daily/reports')).json())
  .reports.some(r => r.date === '${today}'))()`;
if (!(await evalJs(hasToday))) {
  await evalJs(`(() => { runDaily(false); })()`);
  await waitStatus(hasToday, 40000);
  await evalJs(`loadDailyPanel()`);
}
// dailyLoaded 只说明定时设置回来了;列表、正文、覆盖区间是另外三个接口。
await waitFor(`!!document.querySelector('[data-report]')`);
// 等的是"今天那一份已经在阅读区里",不是"随便有段正文":列表刷新和打开正文
// 是两个请求,只等正文的话会读到上一份日报的元信息(全新沙箱首跑假红的原因)。
await waitFor(`document.getElementById('daily-reader-title').textContent === '日报 · ${today}'`);
await waitFor(`!!document.querySelector('#daily-reader-body h2')`);
await waitFor(`!/正在算/.test(document.getElementById('daily-window-label').textContent)`);
check("日报列表出现了今天这一份",
      await evalJs(`[...document.querySelectorAll('[data-report]')].some(b => b.dataset.report === '${today}')`));
check("已生成份数写在页头", (await evalJs(`document.getElementById('daily-count').textContent`)) !== "—");
check("列表里标出了补录条数",
      /补录 1/.test(await evalJs(`document.querySelector('[data-report]').textContent`)));
const bodyHtml = await evalJs(`document.getElementById('daily-reader-body').innerHTML`);
check("正文渲染成 markdown", bodyHtml.includes("<h2") && bodyHtml.includes("今日要点"), bodyHtml.slice(0, 80));
check("正文没有重复一遍大标题", !bodyHtml.includes("<h1"), bodyHtml.slice(0, 120));
check("正文没有重复一遍覆盖区间", !bodyHtml.includes("blockquote"), bodyHtml.slice(0, 120));
// 正文里的出处编号要能点。新日报在落盘时就写成链接(kb/daily.py 的 _link_refs),
// 所以这里读的是真产物;外链一律新标签打开,否则点一下就把 kb-web 连同正在跑
// 的管道面板一起顶掉。
const refOf = async () => JSON.parse(await evalJs(`(() => {
  const a = [...document.querySelectorAll('#daily-reader-body a')]
    .find(x => /^\\[\\d+\\]$/.test(x.textContent.trim()));
  const list = [...document.querySelectorAll('#daily-reader-body a')]
    .filter(x => /^https?:/.test(x.getAttribute('href') || ''));
  return JSON.stringify({ text: a ? a.textContent : '', href: a ? a.getAttribute('href') : '',
    title: a ? a.title : '', cls: a ? a.className : '', target: a ? a.target : '',
    links: list.length, blank: list.filter(x => x.target === '_blank').length }); })()`));
let ref = await refOf();
check("正文里的出处编号是链接", ref.text === "[1]" && /^https:\/\/example\.org\//.test(ref.href),
      JSON.stringify(ref));
check("出处链接带着标题与来源当提示", /—/.test(ref.title), ref.title);
check("出处链接套上角标样式", /daily-ref/.test(ref.cls), ref.cls);
check("正文和素材清单的外链都在新标签打开",
      ref.links > 1 && ref.links === ref.blank, JSON.stringify(ref));

const meta = await evalJs(`document.getElementById('daily-reader-meta').textContent`);
check("元信息写明覆盖的是发布时间", /覆盖 .+ → .+ 发布的内容/.test(meta) && /补录 1 条/.test(meta), meta);

// 那一份"有来源没同步成功"的日报:文件本身带着说明,所以翻回去还看得见
const prev = JSON.parse(await evalJs(`(async () => {
  const rs = (await (await fetch('/api/daily/reports')).json()).reports
    .filter(r => r.date !== '${today}');
  return JSON.stringify(rs[0] || {}); })()`));
check("同步缺口随日报一起存下来", (prev.sync_failed || []).length === 1,
      JSON.stringify(prev.sync_failed));
check("列表行上标出那一份缺了来源",
      /1 个来源未同步/.test(await evalJs(`[...document.querySelectorAll('[data-report]')]
        .find(b => b.dataset.report === ${JSON.stringify(prev.date)})?.textContent || ''`)),
      prev.date);
await evalJs(`openDailyReport(${JSON.stringify(prev.date)})`);
await waitFor(`document.getElementById('daily-reader-title').textContent === '日报 · ${prev.date}'`);
const prevBody = await evalJs(`document.getElementById('daily-reader-body').innerHTML`);
check("日报正文写明哪些来源没同步成功",
      /本轮有 1 个来源没有同步成功/.test(prevBody) && /RSS/.test(prevBody),
      prevBody.slice(0, 160));
// 加链接之前生成的日报:正文里编号还是纯文本,素材清单的 url 也没套尖括号。
// 它们是花过模型钱的产物,不该为了加链接重新生成一份(重算窗口还会换掉内容),
// 面板照那份清单补上就是。
ref = await refOf();
check("老日报的纯文本编号也补成了链接",
      ref.text === "[1]" && ref.href === "https://example.org/a", JSON.stringify(ref));
check("补出来的链接同样新标签打开", ref.target === "_blank" && /daily-ref/.test(ref.cls),
      JSON.stringify(ref));
// 清单那几行还有两种真实形态:带平衡圆括号的裸 url —— 自己写正则解析会在 url 里
// 第一个 `)` 处断掉,那一条的编号永远补不出链接(2026-09-30 审查在生产日报的 107
// 条素材里实测到一条);以及根本没有 url 的素材,补不出来就该留纯文本。合并编号
// (`[1、2]`)在落盘那条路上是认的,面板这边必须同一套口径,否则同一种写法在新旧
// 日报上一个能点一个不能。行内代码里的数字方括号不是出处,一个字都不能动。
const legacy = JSON.parse(await evalJs(`(() => {
  const body = document.getElementById('daily-reader-body');
  const head = [...body.querySelectorAll('h2')].find(h => /^素材清单/.test(h.textContent.trim()));
  const list = head && head.nextElementSibling;
  const refs = [...body.querySelectorAll('a')].filter(x => !list || !list.contains(x));
  const href = t => { const a = refs.find(x => x.textContent.trim() === t);
                      return a ? a.getAttribute('href') : ''; };
  const count = t => refs.filter(x => x.textContent.trim() === t).length;
  return JSON.stringify({ two: href('[2]'), three: href('[3]'),
    ones: count('[1]'), twos: count('[2]'),
    // 读 innerHTML 不是 textContent:往 <code> 里塞一个 <a> 并不改变文本,
    // 拿 textContent 断言的话这一项对『代码里别建链接』根本不敏感。
    code: (body.querySelector('code') || {}).innerHTML || '',
    plainThree: /\\[3\\] 没有原文/.test(body.textContent) }); })()`));
check("带圆括号的裸网址也认得出来",
      legacy.two === "https://example.org/paper-(clean).pdf", legacy.two);
check("合并写法里的编号逐个成链接", legacy.ones === 2 && legacy.twos === 2,
      JSON.stringify(legacy));
check("没有原文的那个编号留成纯文本", !legacy.three && legacy.plainThree,
      JSON.stringify(legacy));
check("行内代码里的数字方括号没被动过", legacy.code === "items[1]", legacy.code);

await evalJs(`openDailyReport('${today}')`);
await waitFor(`document.getElementById('daily-reader-title').textContent === '日报 · ${today}'`);

// 覆盖区间:默认接上一份日报,截止时刻是生成那一刻(不预先钉死)
const label = await evalJs(`document.getElementById('daily-window-label').textContent`);
check("页头写着这一份要覆盖哪一段", /覆盖 \d\d-\d\d \d\d:\d\d 起到生成时刻/.test(label), label);
// 日报是钉在某一天的产物,生成完不会自己跟着新内容变 —— 重试失败项或新加一个
// 订阅之后,页头得看得出"攒下了东西",否则用户没有任何线索去重新生成。
// 这一份刚生成过,所以此刻该是"暂无新素材"而不是一个数字。
check("页头报出还有多少素材没进过日报", /· (已攒 \d+ 条未上报素材|暂无新素材)$/.test(label), label);
check("刚生成完就不该再说有积压", /暂无新素材$/.test(label), label);
// 有积压时那句话长什么样。摆盘自检:探针跑到这里时今天那一份刚生成完,真实状态
// 必然是"暂无新素材",所以这一档只能把 pending 摆进去再渲染一次(摆完复原)。
check("有积压时页头报出条数", /· 已攒 7 条未上报素材$/.test(await evalJs(
  `(() => { const keep = dailyWindow.pending;
            dailyWindow.pending = { items: 5, late: 2, total: 7 };
            renderDailyWindowLabel();
            const t = document.getElementById('daily-window-label').textContent;
            dailyWindow.pending = keep; renderDailyWindowLabel(); return t; })()`)));
check("后端把这个计数一起给了出来",
      await evalJs(`(async () => {
        const w = await (await fetch('/api/daily/window')).json();
        return w.pending && typeof w.pending.total === 'number'; })()`));
await evalJs(`toggleDailyWindow()`);
check("改区间能展开两个时刻输入",
      await evalJs(`!document.getElementById('daily-window').hidden
        && document.getElementById('daily-since').type === 'datetime-local'`));
const since = await evalJs(`document.getElementById('daily-since').value`);
const last = (await evalJs(`(async () => (await (await fetch('/api/daily/reports')).json())
  .reports.filter(r => r.date !== '${today}')[0].window_until)()`)).slice(0, 16).replace(' ', 'T');
check("起点预填成上一份日报的截止时刻", since === last, `${since} / ${last}`);

// 自定义一段没有内容的历史区间:不该悄悄出一份空日报,而是把原因写在横幅上,
// 并且这句原因要留得住(紧接着会刷一次日报列表)。
await evalJs(`(() => {
  document.getElementById('daily-since').value = '2026-01-01T00:00';
  document.getElementById('daily-until').value = '2026-01-02T00:00';
  markDailyWindow(); runDaily(false); })()`);
await waitStatus(`!document.getElementById('daily-error').hidden`);
await evalJs(`loadDailyReports()`);
const banner = await evalJs(`document.getElementById('daily-error-text').textContent`);
check("没有内容的区间给出原因而不是空日报", /没有未上报过的新内容/.test(banner),
      banner || await evalJs(`JSON.stringify(dailyState)`));
check("这一段没有被写成日报",
      !(await evalJs(`[...document.querySelectorAll('[data-report]')].some(b => b.dataset.report === '2026-01-02')`)));
check("改过的区间照原样显示",
      /覆盖 01-01 00:00 → 01-02 00:00$/.test(
        await evalJs(`document.getElementById('daily-window-label').textContent`)));
await evalJs(`resetDailyWindow()`);
check("「接上一份日报」能把区间收回默认",
      /起到生成时刻/.test(await evalJs(`document.getElementById('daily-window-label').textContent`)));

// 重新生成今天这一份:先弹确认框,确认后生成时间会变
// 比 window_until 而不是 generated:后者只到分钟,同一分钟内重新生成逐字相同。
const stamp = `(async () => (await (await fetch('/api/daily/reports/${today}')).json()).window_until)()`;
const before = await evalJs(stamp);
// 不能直接 await runDaily:它自己会停在确认框上等点击,await 等于死锁。
await evalJs(`(() => { runDaily(false); })()`);
const asked = await waitFor(`!!document.querySelector('.confirm-mask [data-act=ok]')`, 5000);
check("覆盖已有日报前先确认", asked,
      asked ? "" : await evalJs(`JSON.stringify({dates: dailyReports.map(r => r.date), busy: dailyBusy,
        running: dailyState && dailyState.running, phase: dailyState && dailyState.phase,
        masks: document.querySelectorAll('.confirm-mask').length, today: new Date().toLocaleDateString('sv')})`));
if (asked) await sleep(1100);   // window_until 只到秒:同一秒内重生成逐字相同
if (asked) await evalJs(`document.querySelector('.confirm-mask [data-act=ok]').click()`);
await waitStatus(`${stamp}.then(v => v !== ${JSON.stringify(before)})`);
const after = await evalJs(stamp);
check("确认后这一份被重新生成", !!after && after !== before, `${before} → ${after}`);
await evalJs(`refreshDailyStatus()`);
check("重新生成后不再显示上一次的失败",
      await evalJs(`document.getElementById('daily-error').hidden`));

// 同步范围一个都不勾:不能把「同步全部并生成」静静地降级成「直接生成」
// ——那一轮素材根本没刷新,面板上却一路"成功"。
await evalJs(`(() => { document.querySelectorAll('#daily-channels input:checked')
  .forEach(i => { i.checked = false; }); })()`);
const stampBefore = await evalJs(stamp);
await evalJs(`(() => { runDaily(true); })()`);
await sleep(600);
check("空同步范围没有起跑", !(await evalJs(`!!(dailyState && dailyState.running)`)),
      await evalJs(`JSON.stringify({phase: dailyState && dailyState.phase})`));
check("空同步范围就地说清怎么办",
      /先勾选至少一个同步范围/.test(await evalJs(`document.getElementById('toast-host')?.textContent || ''`)),
      await evalJs(`document.getElementById('toast-host')?.textContent || ''`));
check("空同步范围没有顺手重算今天那一份", (await evalJs(stamp)) === stampBefore);
const rejected = await evalJs(`(async () => (await fetch('/api/daily/run', { method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ sync: true, channels: [] }) })).status)()`);
check("后端也拒绝空同步范围", rejected === 400, String(rejected));
await evalJs(`(() => { document.querySelectorAll('#daily-channels input')
  .forEach(i => { i.checked = true; }); })()`);

// 来源面板:英文条目显示译文,弹窗能切回原文。老深链 #rss 现在落到
// 「我的订阅」页的 RSS 视图,地址栏写的是统一的 #subs。
await evalJs(`switchTab('rss')`);
await sleep(2500);
// 恢复地址栏之后照常记录位置(那道"恢复前不写地址栏"的闸门只管启动那一下,
// 卡死的话所有深链和前进后退一起没了)。
check("切走之后地址栏跟着更新", (await evalJs(`location.hash`)) === "#subs",
      await evalJs(`location.hash`));
check("老深链落到订阅页的 RSS 视图",
      await evalJs(`activeTab === 'subs' && subsKind === 'rss'
                    && !document.getElementById('subs-feeds').hidden`));
const excerpt = await evalJs(`document.querySelector('.source-excerpt')?.textContent || ''`);
check("订阅内容显示的是中文译文", /[一-鿿]/.test(excerpt) && !/^The runtime schedules/.test(excerpt), excerpt.slice(0, 60));
check("条目标注了已译为中文",
      /已译为中文/.test(await evalJs(`document.querySelector('.source-entry-footer span')?.textContent || ''`)));
await evalJs(`document.querySelector('[data-read-item]').click()`);
await sleep(500);
check("阅读弹窗给了看原文的入口", !(await evalJs(`document.getElementById('source-reader-original').hidden`)));
await evalJs(`toggleSourceOriginal()`);
check("切回原文后正文是英文",
      /The runtime schedules/.test(await evalJs(`document.getElementById('source-reader-body').textContent`)));

// 还排在翻译队列里的那一条:vault 里没有这一份,面板也不能拿英文原文充数
const waiting = await evalJs(`(() => {
  const el = [...document.querySelectorAll('.source-entry')]
    .find(r => /正在等翻译/.test(r.textContent));
  return el ? JSON.stringify({ name: el.querySelector('.source-entry-name')?.textContent || '',
    excerpt: el.querySelector('.source-excerpt')?.textContent || '',
    expandable: !!el.querySelector('[data-read-item]') }) : ''; })()`);
check("待翻译的条目标成「正在等翻译」", !!waiting,
      await evalJs(`document.getElementById('source-list').textContent.slice(0, 200)`));
const w = waiting ? JSON.parse(waiting) : {};
check("待翻译的条目不展示英文正文",
      !!waiting && !/still waiting for its translation/.test(w.excerpt), w.excerpt);
// 标题也不行:这是进 RSS 页面就看得见的默认内容,露一行英文标题和"英文内容
// 都译成中文"一样矛盾(2026-09-30 复审实测)。
check("待翻译的条目不展示英文标题",
      !!waiting && !/A second English post/.test(w.name) && /[一-鿿]/.test(w.name), w.name);
check("待翻译的条目不给展开阅读", !!waiting && !w.expandable);
check("面板写明还有几条在等翻译",
      /条外文内容还在等翻译/.test(await evalJs(`document.querySelector('.source-note')?.textContent || ''`)),
      await evalJs(`document.querySelector('.source-note')?.textContent || ''`));

// 老解析器留下的半成品(正文中文、标题还是原文):页脚不能说「已译为中文」
const half = await evalJs(`(() => {
  const el = [...document.querySelectorAll('.source-entry')]
    .find(r => /A half translated post/.test(r.textContent));
  return el ? JSON.stringify({ foot: el.querySelector('.source-entry-footer span')?.textContent || '',
    excerpt: el.querySelector('.source-excerpt')?.textContent || '' }) : ''; })()`);
const h = half ? JSON.parse(half) : {};
check("半成品条目仍在列表里", !!half,
      await evalJs(`document.getElementById('source-list').textContent.slice(0, 300)`));
// 「· 已译为中文」是完成态那句话的完整形态(以它收尾);半成品那句是
// 「正文已译为中文,标题还是原文…」,所以只能按结尾判,不能按包含判。
check("半成品不谎称整条已译为中文", !!half && !/·\s*已译为中文\s*$/.test(h.foot), h.foot);
check("半成品页脚写清标题还是原文", !!half && /标题还是原文/.test(h.foot), h.foot);
check("半成品的中文正文照常显示", !!half && /这一条只有正文回来了中文/.test(h.excerpt),
      h.excerpt.slice(0, 60));

check("页面没有脚本报错", pageErrors.length === 0, pageErrors.join(" | "));

console.log(oks.map(o => "  ok  " + o).join("\n"));
if (fails.length) console.log(fails.map(f => "FAIL  " + f).join("\n"));
console.log(`\n${oks.length} ok, ${fails.length} failed`);
ws.close(); proc.kill();
process.exit(fails.length ? 1 : 0);
