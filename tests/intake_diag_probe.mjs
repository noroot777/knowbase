// 「问题与待办」(kb/static/js/diag*.js + kb/web/intake_diag_api.py)在真页面上的回归。
//
// 验的是这块面板**存在的理由**那几件事,不是像素:
//   ① 还有多少在等 / 卡在哪一环 —— 三档分开显示,不统称「抓取中」;
//   ② 屏幕上**不出现「需处理」**(那个词在同步中心已经有主,两边数字天生不一样);
//   ③ 「立即执行」确认框里那段话**由服务端给**,而且如实写着「整类来源」「条目化
//      提取不在这一步」—— 这是用户据以决定要不要花这笔钱的全部信息;
//   ④ X 那两类问题的措辞:限流那条不许引到「重新登录」,覆盖未完成那条不许承诺
//      断点续传;来源问题没有「不再收录」;
//   ⑤ **「从浏览器采集」是正文那几档的主入口**,「手动补正文」退成兜底:顺序、主次、
//      按钮旁边那句前提、点下去那个地址带的是真实 key,以及**同一道闸管两颗**
//      (反过来也钉:合格的那几类照旧两颗都开);
//   ⑥ 四个宽度无横向溢出、整轮零 JS 错误零 console warning、没有原生弹窗、
//      页面里不出现 ↗ ↑ ↓ → ▾ ▴ 这些字符(箭头一律走 icons.js)。
//
// 需要一个跑着沙箱数据的 kb-web:
//   .venv/bin/python tests/serve_intake_diag.py --port 8893
//
//   HEADLESS_SHELL=<bin> node tests/intake_diag_probe.mjs http://127.0.0.1:8893
import { writeSync } from "node:fs";
import { launchShell } from "./probe_shell.mjs";

const base = process.argv[2];

const { proc, port } = await launchShell(["--window-size=1440,1000"]);
const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));
let id = 0; const pend = new Map(); const pageErrors = []; const warnings = []; const dialogs = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
  if (m.method === "Runtime.consoleAPICalled") {
    const text = m.params.args.map(a => a.value ?? a.description ?? "").join(" ");
    if (m.params.type === "error") pageErrors.push(text);
    if (m.params.type === "warning") warnings.push(text);
  }
  if (m.method === "Page.javascriptDialogOpening") dialogs.push(m.params.message);
});
const send = (method, params = {}) => { const mid = ++id; return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); }); };
const evalJs = async expression => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (expr, ms = 15000) => { const dl = Date.now() + ms; while (Date.now() < dl) { if (await evalJs(expr)) return true; await sleep(120); } return false; };
await send("Runtime.enable"); await send("Page.enable");

const results = [];
const check = (name, ok, detail = "") => results.push({ name, ok: !!ok, detail: String(detail) });

await send("Page.navigate", { url: base + "/" });
await until(`typeof switchTab === 'function' && typeof loadCarePanel === 'function'`, 25000);

// ── 1. 常驻入口:侧栏上就有,点一下进得去 ───────────────────────────────────
check("侧栏有「问题与待办」入口",
      await evalJs(`!!document.querySelector('#tab-care')`));
check("入口挨着「同步中心」放",
      await evalJs(`$('tab-pipe').parentElement === $('tab-care').parentElement`));
await evalJs(`switchTab('care')`);
await until(`!!CARE.overview && CARE.sections.length > 0`, 20000);
check("地址栏落到 #care", (await evalJs(`location.hash`)) === "#care");
check("页头标题是「问题与待办」",
      (await evalJs(`document.querySelector('#carepanel h1').textContent`)) === "问题与待办");

// ── 2. 页头就一行:标题 + 同一行上的事实数字,没有英文 eyebrow / 副标题 ──────
const headFacts = await evalJs(`$('care-head-facts').textContent`);
check("页头同一行上有事实数字", /\d+\s*条内容在等/.test(headFacts), headFacts);
// 来源连接问题**单独说一个数**:它说的是订阅读不读得通,不是一篇文章(审查第 1 轮 B08)。
check("来源连接问题在页头上另算一个数", /另有\s*\d+\s*条来源连接问题/.test(headFacts), headFacts);
check("页头没有英文 eyebrow 或副标题",
      await evalJs(`(() => {
        const head = document.querySelector('#carepanel .care-heading');
        return !head.querySelector('.eyebrow, p, h2, small');
      })()`),
      await evalJs(`document.querySelector('#carepanel .care-heading').innerHTML.slice(0,120)`));
// 窄屏下那一行按**组**换行,不按字换行(审查第 2 轮 B6):每一组「数字 + 量词」是一个
// nowrap 单元,容器自己允许换行。不钉这一条的话 320px 下屏幕上是「条内容在/等」
// 「待决/定」这样的碎字 —— 没有横向溢出,所以别的检查一条都不红。
check("页头数字按组换行、不按字换行", await evalJs(`(() => {
        const host = $('care-head-facts');
        const kids = [...host.children];
        return kids.length > 0
            && getComputedStyle(host).flexWrap === 'wrap'
            && kids.every(el => el.tagName === 'SPAN'
                              && getComputedStyle(el).whiteSpace === 'nowrap');
      })()`),
      await evalJs(`$('care-head-facts').innerHTML.slice(0, 120)`));

// ── 3. 「需处理」这个词不许出现在这块面板上(同步中心那个区才念它)──────────
const labelText = await evalJs(`(() => {
  const parts = [$('care-tabs'), $('care-list'), $('care-detail'), $('care-head-facts'),
                 ...document.querySelectorAll('#carepanel .pipe-actions, #carepanel .care-filters')];
  return parts.map(el => el ? el.textContent : '').join(' ');
})()`);
check("面板自己的标签上不出现「需处理」", !labelText.includes("需处理"),
      (labelText.match(/.{0,18}需处理.{0,18}/) || [""])[0]);
check("页头下面那句明说和同步中心数的不是一回事",
      await evalJs(`/同步中心/.test($('care-note').textContent) && /条内容/.test($('care-note').textContent)`),
      await evalJs(`$('care-note').textContent`));

// ── 4. 三档分开,不统称「抓取中」────────────────────────────────────────────
const sectionTitles = await evalJs(`JSON.stringify([...document.querySelectorAll('#care-list .care-section-head h3')].map(h => h.textContent))`);
const titles = JSON.parse(sectionTitles);
check("「待人工确认」自己一档", titles.some(t => t.includes("待人工确认")), sectionTitles);
check("「重试已停止」自己一档", titles.some(t => t.includes("重试已停止")), sectionTitles);
check("「来源连接」自己一档", titles.some(t => t.includes("来源连接")), sectionTitles);
check("没有把它们统称「抓取中」",
      !(await evalJs(`$('care-list').textContent.includes('抓取中')`)));
await evalJs(`careSetGroup('queued')`);
await until(`CARE.group === 'queued' && CARE.sections.length > 0
             && CARE.sections.every(s => s.lane.group === 'queued')`);
const queuedTitles = await evalJs(`JSON.stringify([...document.querySelectorAll('#care-list .care-section-head h3')].map(h => h.textContent))`);
check("「待执行」一档只说排队、不说在运行",
      JSON.parse(queuedTitles).every(t => t.includes("待执行"))
        && !(await evalJs(`$('care-list').textContent.includes('正在运行')`)), queuedTitles);
await evalJs(`careSetGroup('running')`);
await until(`CARE.group === 'running'`);
check("空闲时「执行中」明说没有东西在跑,而且不列条目",
      await evalJs(`$('care-list').textContent.includes('现在没有同步在跑')
                    && !document.querySelector('#care-list .care-row-why')`));
await evalJs(`careSetGroup('attention')`);
await until(`CARE.group === 'attention' && CARE.sections.length > 0
             && CARE.sections.some(s => s.lane.id === 'body_exhausted')`);

// ── 5. X 那两类问题的措辞 ──────────────────────────────────────────────────
const boot = await evalJs(`JSON.stringify(CARE.overview.sources.find(s => s.problem === 'x_boot') || null)`);
const bootRow = JSON.parse(boot);
check("限流那条的标题是「页面应用没能启动（限流，等冷却）」",
      bootRow && bootRow.label === "页面应用没能启动（限流，等冷却）", bootRow && bootRow.label);
check("限流那条明说不是掉登录、不要反复登录",
      bootRow && /不是登录过期/.test(bootRow.explanation) && /不要反复登录/.test(bootRow.explanation),
      bootRow && bootRow.explanation);
check("限流那条说清冷却由后端等、而且本来就会重试一次",
      bootRow && /45 秒/.test(bootRow.advice) && /排到队尾重试一次/.test(bootRow.advice),
      bootRow && bootRow.advice);
check("限流那条不承诺冷却结束就会成功",
      bootRow && /不保证/.test(bootRow.advice));
const cover = JSON.parse(await evalJs(`JSON.stringify(CARE.overview.sources.find(s => s.problem === 'coverage' && s.actionable) || null)`));
check("覆盖未完成那条的标题是「没有读到上次位置（覆盖未完成）」",
      cover && cover.label === "没有读到上次位置（覆盖未完成）", cover && cover.label);
check("覆盖未完成那条不承诺断点续传",
      cover && /不是从上次停下的位置接着滚/.test(cover.advice), cover && cover.advice);
check("覆盖未完成那条说清水位不前移、已取得的照旧入库",
      cover && /水位不前移/.test(cover.explanation) && /照旧入库/.test(cover.explanation));
// 来源问题的详情:走它自己那四步,而且没有「不再收录」
await evalJs(`careSelect(${JSON.stringify(cover.id)})`);
await until(`$('care-detail').textContent.includes('访问来源')`);
const sourceSteps = await evalJs(`JSON.stringify([...document.querySelectorAll('#care-detail .care-step b')].map(b => b.textContent))`);
check("来源问题按「访问来源 → 读取时间线 → 检查覆盖 → 结束检查」显示",
      JSON.parse(sourceSteps).join("|") === "访问来源|读取时间线|检查覆盖|结束检查", sourceSteps);
check("来源问题不套文章那四步",
      !JSON.parse(sourceSteps).includes("补齐正文"));
check("来源问题没有「不再收录」按钮",
      await evalJs(`[...document.querySelectorAll('#care-detail .care-exits button')]
                     .every(b => b.textContent !== '不再收录')`));
check("来源问题明说复查成功不等于发现了新内容",
      await evalJs(`$('care-detail').textContent.includes('不代表发现了新内容')`));
check("它说清自己不是「不再收录」的射程",
      await evalJs(`$('care-detail').textContent.includes('不该被用来停用一整条订阅')`));

// ── 6. 「立即执行」确认框:那段话由服务端给,而且说实话 ──────────────────────
const exhausted = await evalJs(`(() => {
  const s = CARE.sections.find(s => s.lane.id === 'body_exhausted');
  return s ? 'item:' + s.items[0].key : '';
})()`);
check("重试耗尽那一档有条目", !!exhausted, exhausted);
await evalJs(`careSelect(${JSON.stringify(exhausted)})`);
await until(`$('care-detail').textContent.includes('重试已停止')`);
const exits = await evalJs(`JSON.stringify([...document.querySelectorAll('#care-detail .care-exits button')].map(b => b.textContent))`);
check("出口齐全:立即执行 / 仅重新排队 / 从浏览器采集 / 手动补正文 / 换链接 / 暂缓 / 不再收录",
      ["立即执行", "仅重新排队", "从浏览器采集", "手动补正文", "换链接", "暂缓", "不再收录"]
        .every(name => JSON.parse(exits).includes(name)), exits);
await evalJs(`[...document.querySelectorAll('#care-detail .care-exits button')]
               .find(b => b.textContent === '立即执行').click()`);
await until(`!!document.querySelector('.confirm-mask .confirm-body')`);
const scopeText = await evalJs(`document.querySelector('.confirm-mask .confirm-body').textContent`);
check("确认框说出真实射程:整类来源,不只是你点的那一条",
      /整类/.test(scopeText) && /不只是你点的/.test(scopeText), scopeText.slice(0, 90));
check("确认框明说会联网并可能调用翻译模型",
      /联网/.test(scopeText) && /翻译模型/.test(scopeText));
check("确认框明说索引是全库的", /整个知识库/.test(scopeText));
check("确认框明说条目化提取不在这一步", /条目化提取不在这一步/.test(scopeText));
await evalJs(`document.querySelector('.confirm-mask [data-act=cancel]').click()`);
await until(`!document.querySelector('.confirm-mask')`);

// ── 7. 「仅重新排队」不宣称成功、不联网 ───────────────────────────────────
await evalJs(`[...document.querySelectorAll('#care-detail .care-exits button')]
               .find(b => b.textContent === '仅重新排队').click()`);
await until(`!!document.querySelector('.confirm-mask .confirm-body')`);
const requeueText = await evalJs(`document.querySelector('.confirm-mask .confirm-body').textContent`);
check("放回队列那句明说不联网、不宣称成功",
      /不联网/.test(requeueText) && /不宣称成功/.test(requeueText), requeueText.slice(0, 80));
await evalJs(`document.querySelector('.confirm-mask [data-act=cancel]').click()`);
await until(`!document.querySelector('.confirm-mask')`);

// ── 8. 换链接:PDF 地址就地红字,不弹原生对话框 ────────────────────────────
await evalJs(`[...document.querySelectorAll('#care-detail .care-exits button')]
               .find(b => b.textContent === '换链接').click()`);
await until(`!!document.querySelector('.care-form input[type=url]')`);
await evalJs(`(() => { const el = document.querySelector('.care-form input[type=url]');
  el.value = 'https://example.org/paper.pdf'; el.dispatchEvent(new Event('input')); })()`);
await evalJs(`document.querySelector('.care-form .confirm-acts button:last-child').click()`);
await until(`!!document.querySelector('.care-form .field-err:not([hidden])')`, 8000);
const fieldErr = await evalJs(`document.querySelector('.care-form .field-err:not([hidden])')?.textContent || ''`);
check("PDF 地址被挡住,而且是字段级红字",
      /PDF|网页地址/.test(fieldErr), fieldErr);
await evalJs(`document.querySelector('.care-form .confirm-acts button').click()`);
await until(`!document.querySelector('.care-form')`);

// ── 9. 暂缓 → 已搁置 → 恢复:可撤销,而且不计为解决 ────────────────────────
await evalJs(`careSetGroup('queued')`);
await until(`CARE.group === 'queued' && CARE.sections.some(s => s.lane.id === 'body_queued')`);
const queuedKey = await evalJs(`(() => {
  const s = CARE.sections.find(s => s.lane.id === 'body_queued');
  return s ? s.items[0].key : '';
})()`);
check("待执行那一档有条目(下面几条都挂在它身上)", /^[0-9a-f]{32}$/.test(queuedKey), queuedKey);
const archivedBefore = await evalJs(`CARE.overview.groups.archived`);
// `void` 不能省:`evalJs` 带 awaitPromise,而 `careShelf` 返回的那个 promise
// 要等弹窗关掉才 resolve —— 直接 await 它就是等一个只有自己能去点的按钮。
await evalJs(`void careShelf('rss', [${JSON.stringify(queuedKey)}], 'snooze')`);
await until(`!!document.querySelector('.care-form textarea')`);
check("暂缓的确认框明说不会自己恢复、文件不动",
      await evalJs(`(() => { const t = document.querySelector('.care-form .confirm-body').textContent;
        return /不会自己到点恢复/.test(t) && /一个字都不动/.test(t); })()`),
      await evalJs(`document.querySelector('.care-form .confirm-body').textContent`));
await evalJs(`document.querySelector('.care-form .confirm-acts button:last-child').click()`);
await until(`CARE.overview.groups.archived === ${archivedBefore + 1}`, 10000);
check("暂缓之后进「已搁置」,不计为解决",
      (await evalJs(`CARE.overview.groups.resolved`)) === 1,
      await evalJs(`JSON.stringify(CARE.overview.groups)`));
await evalJs(`careSetGroup('archived')`);
await until(`CARE.group === 'archived' && CARE.sections.length > 0
             && CARE.sections.every(s => s.lane.group === 'archived')`);
check("已搁置里暂缓和不再收录分开标",
      await evalJs(`(() => { const t = $('care-list').textContent;
        return t.includes('暂缓') && t.includes('不再收录'); })()`));
// 恢复要绑**那一次决定**(`shelf_decision_id`),不是「这个 key」:同一条内容
// 先暂缓、恢复、再「不再收录」之后,只按 key 删会把后来那次决定一起取消。
const releaseArgs = await evalJs(`(() => {
  const row = CARE.sections.flatMap(s => s.items)
    .find(one => one.key === ${JSON.stringify(queuedKey)});
  return JSON.stringify([row ? row.key : '', row ? row.shelf_decision_id : '']);
})()`);
const [relKey, relDecision] = JSON.parse(releaseArgs);
check("已搁置那一行带着「这一次决定」的编号", /^[0-9a-f]{32}$/.test(relDecision), releaseArgs);
await evalJs(`careRelease('rss', [${JSON.stringify(relKey)}], [${JSON.stringify(relDecision)}])`);
await until(`CARE.overview.groups.archived === ${archivedBefore}`, 10000);
check("恢复之后回到原来那一档",
      await evalJs(`CARE.overview.lane_totals.body_queued === 1`),
      await evalJs(`JSON.stringify(CARE.overview.lane_totals)`));

// ── 9b. 「展开更多」往后翻用 offset,不是把 limit 加大 ──────────────────────
// 服务端把 `limit` 夹在 `lanes.PAGE` 封顶,所以加大 limit 拿回来的还是最前那一页:
// 一个总数 41 的档位反复点「展开更多」永远看不到第 41 条(审查第 1 轮 F09)。
// 样例每一档只摆一条(别的检查认「列表第一行」),所以这里**只盯前端发出去的那一
// 问**:记下 URL、把这一页的回答原样给它,看它要的是 offset 还是更大的 limit。
const paging = await evalJs(`(async () => {
  const section = CARE.sections[0];
  const before = section.items.length;
  const asked = [];
  const real = window.fetch;
  window.fetch = (input, init) => {
    const url = String(input);
    if (url.includes('/items?')) {
      asked.push(url);
      const extra = { ...section.items[0], key: 'f'.repeat(32), title: '下一页那一条' };
      return Promise.resolve(new Response(JSON.stringify({ sections: [
        { lane: section.lane, total: before + 1, items: [extra] }] }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }));
    }
    return real(input, init);
  };
  try { await careMore(section.lane.id); } finally { window.fetch = real; }
  const url = new URL(asked[0] || 'https://x/none', location.origin);
  return JSON.stringify({
    offset: url.searchParams.get('offset'), limit: url.searchParams.get('limit'),
    page: CARE.overview.page, before, after: section.items.length,
    firstKept: section.items[0].key, appended: section.items[before]?.key || '',
  });
})()`);
const page = JSON.parse(paging);
check("往后翻带的是 offset = 已经拿到的条数", page.offset === String(page.before), paging);
check("limit 还是定长那一页,没有被加大", page.limit === String(page.page), paging);
check("新那一页是**追加**,原来那几行还在", page.after === page.before + 1
      && page.appended === "f".repeat(32), paging);
await evalJs(`careLoadSections()`);
await until(`CARE.sections.length > 0 && !CARE.sections[0].items.some(r => r.key === 'f'.repeat(32))`);

// ── 9c. 写请求带着「页面上那一刻那一世」 ───────────────────────────────────
// 服务端把它 AND 进每条写语句(`kb/intake_diag/warrant.py`),所以**真实页面必须把
// 那个值发出去** —— 不发的话那道闸在真机上永远收不到它、等于没启用,而所有后端
// 回归照旧全绿(那正是「实例级修好、表示级还漏」那一类)。
// 这一问被拦下来、原样给它一个成功回答,所以库里一个字节都没动。
await evalJs(`careSetGroup('queued')`);
await until(`CARE.group === 'queued' && CARE.sections.some(s => s.lane.id === 'body_queued')`);
await evalJs(`(() => {
  const section = CARE.sections.find(s => s.lane.id === 'body_queued');
  const row = section.items[0];
  const lane = section.lane;
  window.__probeLife = { life: row.life, key: row.key, sent: null };
  const real = window.fetch;
  window.fetch = (input, init) => {
    if (String(input).includes("/requeue")) {
      window.__probeLife.sent = (init && init.body) || "";
      window.fetch = real;
      return Promise.resolve(new Response(JSON.stringify({ requeued: 0, released: 0,
        body: 0, translate: 0, skipped: [], asked: 1, detail: "探针拦下了这一问" }),
        { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    return real(input, init);
  };
  careRunExit("requeue", row, lane);
})()`);
await until(`!!document.querySelector(".confirm-mask [data-act=ok]")`, 8000);
await evalJs(`document.querySelector(".confirm-mask [data-act=ok]").click()`);
await until(`!!window.__probeLife.sent`, 8000);
const carried = JSON.parse(await evalJs(`JSON.stringify(window.__probeLife)`));
check("列表那一行手上有「这一世」", /^[0-9a-f]{32}$/.test(carried.life), carried.life);
check("页面发出去的那一问带着它",
      (() => { const body = JSON.parse(carried.sent || "{}");
        return (body.lives || {})[carried.key] === carried.life; })(),
      carried.sent);

// ── 10. 处理记录:有记录,而且不给「撤销」那种快照回滚 ──────────────────────
await evalJs(`openCareLog()`);
await until(`!!document.querySelector('.modal-mask .care-log-rows')`, 8000);
const logText = await evalJs(`document.querySelector('.modal-mask .care-log-rows').textContent`);
check("处理记录里有刚才那两下", /暂缓/.test(logText) && /恢复/.test(logText), logText.slice(0, 80));
check("处理记录明说重试次数一类没有撤销",
      await evalJs(`document.querySelector('.modal-mask .confirm-body').textContent.includes('没有「撤销」')`));
await evalJs(`document.querySelector('.modal-mask .confirm-acts button').click()`);
await until(`!document.querySelector('.modal-mask')`);
await evalJs(`careSetGroup('attention')`);
await until(`CARE.group === 'attention' && CARE.sections.length > 0
             && CARE.sections.every(s => s.lane.group === 'attention')`);

// ── 10b. 时间一律按项目时区显示,不按宿主机 ────────────────────────────────
// 库里存的是 `reg.now()` 的 UTC ISO(`2026-10-09T07:56:11+00:00`)。上一版直接把
// `T` 换成空格、把偏移截掉,于是屏幕上那个时刻比真实时间早 8 小时,而它看起来
// 完全像一个本地时间(CLAUDE.md 元规则:时间一律按 Asia/Shanghai,不按宿主机)。
check("UTC 存的那一刻按 Asia/Shanghai 显示",
      (await evalJs(`careTime('2026-10-09T07:56:11+00:00', { seconds: true })`))
        === "2026-10-09 15:56:11",
      await evalJs(`careTime('2026-10-09T07:56:11+00:00', { seconds: true })`));
check("时间格式化不受宿主机时区影响",
      (await evalJs(`careTime('2026-10-09T23:30:00+00:00')`)) === "2026-10-10 07:30",
      await evalJs(`careTime('2026-10-09T23:30:00+00:00')`));

// ── 10c. 「已完成」那一组不给批量重试 ─────────────────────────────────────
// 它们没有任何处理出口(`lane.exits` 是空的),给了复选框就是请人去点一组后端
// 一定会拒的动作 —— 而那句拒绝和他看到的按钮互相矛盾。
await evalJs(`careSetGroup('resolved')`);
// **等到这几档真换成这一组的**:`careSetGroup` 同步改 `CARE.group`、异步才取数,
// 只等 `group` 的话这一刀会切在上一组的档位和上一组的 DOM 上 —— 下面那两条断言
// 就变成在待决定那几档上问「有没有复选框」,恒红(实测如此)。
await until(`CARE.group === 'resolved' && CARE.sections.length > 0
             && CARE.sections.every(s => s.lane.group === 'resolved')`);
check("「已完成」那一组真的有条目(否则下面这条是空洞的通过)",
      await evalJs(`CARE.sections.some(s => s.items.length > 0)`),
      await evalJs(`JSON.stringify(CARE.sections.map(s => [s.lane.id, s.items.length]))`));
check("「已完成」的行上没有复选框",
      await evalJs(`!document.querySelector('#care-list input[type=checkbox]')`));
check("「已完成」那几档后端就没给处理出口",
      await evalJs(`CARE.sections.every(s => (s.lane.exits || []).length === 0)`),
      await evalJs(`JSON.stringify(CARE.sections.map(s => s.lane.exits))`));
// **反过来钉一边**:待决定那一组照旧勾得上(不然「整个不给复选框」也是绿的)。
await evalJs(`careSetGroup('attention')`);
await until(`CARE.group === 'attention' && CARE.sections.length > 0
             && CARE.sections.every(s => s.lane.group === 'attention')`);
check("待决定那一组照旧勾得上",
      await evalJs(`!!document.querySelector('#care-list input[type=checkbox]')`));

// ── 10d. 「从浏览器采集」:正文那几档的主入口,「手动补正文」退成兜底 ─────────
// 会落到这几档的多半是「页面上没有识别出正文,可能需要登录或由脚本渲染」「这一页
// 要求先完成验证或登录」「HTTP 403」—— 那些页人**已经在浏览器里开着、登录态也在**,
// 从那个标签抓比让人手工全选复制粘贴顺得多,也不容易粘坏。所以这里钉四件事:
// 顺序与主次(读不出主次人就会先点那颗兜底的)、按钮旁边那句前提(不许让人点进去
// 才发现没装扩展)、那个地址带的是**真实 `feed_items.key`**(补抓页按它绑条目)、
// 以及**同一道闸管两颗**(反过来也钉:合格的那几类照旧两颗都开)。
await evalJs(`careSetGroup('attention')`);
await until(`CARE.group === 'attention' && CARE.sections.some(s => s.lane.id === 'body_exhausted')`);
const capRow = JSON.parse(await evalJs(`(() => {
  const s = CARE.sections.find(s => s.lane.id === 'body_exhausted');
  const row = s.items[0];
  return JSON.stringify({ id: 'item:' + row.key, key: row.key });
})()`));
const capPick = name =>
  `document.querySelector('#care-detail .care-exits button[data-exit="${name}"]')`;
const reselect = async () => {
  await evalJs(`CARE.selected = ''; careSelect(${JSON.stringify(capRow.id)})`);
  await until(`!!${capPick("capture")}`);
};
await reselect();
const exitOrder = JSON.parse(await evalJs(`JSON.stringify(
  [...document.querySelectorAll('#care-detail .care-exits button')].map(b => b.dataset.exit))`));
// 下面那几条都挂在这颗按钮身上,所以先单独问一句它在不在 —— 不问的话出口清单一
// 变,余下几条会**抛在取值那一行**,整轮结果一条都报不出来(实测注入出来的形态)。
check("动作区里真有「从浏览器采集」这颗(下面几条都挂在它身上)",
      exitOrder.includes("capture"), JSON.stringify(exitOrder));
check("「从浏览器采集」排在「手动补正文」前面,而且紧挨着它",
      exitOrder.includes("capture")
        && exitOrder[exitOrder.indexOf("capture") + 1] === "body",
      JSON.stringify(exitOrder));
const emphasis = JSON.parse(await evalJs(`(() => {
  const cap = ${capPick("capture")}, body = ${capPick("body")};
  if (!cap || !body) return JSON.stringify({ cap: '(没有这颗按钮)', body: '', label: '' });
  return JSON.stringify({ cap: cap.className, body: body.className,
    capBg: getComputedStyle(cap).backgroundColor,
    bodyBg: getComputedStyle(body).backgroundColor, label: cap.textContent });
})()`));
check("它是这两颗里的主项,「手动补正文」退成次级",
      emphasis.label === "从浏览器采集" && emphasis.cap.includes("care-exit-main")
        && !emphasis.cap.includes("ghost") && emphasis.body.includes("ghost")
        && emphasis.capBg !== emphasis.bodyBg, JSON.stringify(emphasis));
const capNote = await evalJs(
  `document.querySelector('#care-detail .care-exit-note')?.textContent || ''`);
check("按钮旁边就写着前提:文章此刻开着 + 装过配对好的扩展 + 条件不凑手用兜底那颗",
      /开着/.test(capNote) && /扩展/.test(capNote) && /手动补正文/.test(capNote), capNote);
// 点下去那一下:**不弹确认框**(确认之后再 open 已经不是用户手势,会被弹窗拦截挡掉),
// 直接开新标签。这里把 `window.open` 拦下来看它要去哪儿,所以没有真的开标签。
const opened = JSON.parse(await evalJs(`(() => {
  const real = window.open;
  let got = null;
  window.open = (url, target, feat) => { got = [url, target, feat]; return null; };
  try { ${capPick("capture")}?.click(); } finally { window.open = real; }
  return JSON.stringify(got);
})()`));
check("那个 key 是真实的 feed_items.key(32 位 hex),不是展示编号",
      /^[0-9a-f]{32}$/.test(capRow.key), capRow.key);
check("点下去进的是带这个 key 的补抓页、开在新标签、不带 opener",
      !!opened && opened[0] === "/browser-capture?item=" + capRow.key
        && opened[1] === "_blank" && String(opened[2]).includes("noopener"),
      JSON.stringify(opened));
check("点下去没有弹确认框(弹了就会被弹窗拦截挡住)",
      !(await evalJs(`!!document.querySelector('.confirm-mask')`)));
// **批量栏里压根不给这一颗**:一个浏览器标签就是一篇文章,勾 20 条采不了 20 篇。
await evalJs(`(() => {
  const s = CARE.sections.find(s => s.lane.id === 'body_exhausted');
  carePick(s.items[0], s.lane, true);
})()`);
await until(`!$('care-batch').hidden`);
check("批量栏里不给「从浏览器采集」",
      !(await evalJs(`$('care-batch').textContent.includes('从浏览器采集')`)),
      await evalJs(`$('care-batch').textContent.slice(0, 80)`));
await evalJs(`CARE.picked.clear(); careRenderList()`);
// 闸关着的那一侧。**合格的来源压根到不了这几档**(`fulltext._eligible` 里那道
// `link_body_sql` 已经把 X 观点 / YouTube / 简报挡在补抓队列外面),所以这里把详情
// 那一问的 `can_paste` 拦下来改成假 —— 验的是**前端拿到这个答案之后怎么画**。
// 后端对那几类真答什么,由 `tests/test_intake_diag_capture.py` 钉。
const gate = JSON.parse(await evalJs(`(async () => {
  const real = window.fetch;
  const why = 'YouTube 这一类的正文是视频转录稿，而链接后面那一页是播放页（样例）';
  window.fetch = async (input, init) => {
    const r = await real(input, init);
    if (!String(input).includes('/items/')) return r;
    const data = await r.json();
    return new Response(JSON.stringify({ ...data, can_paste: false, paste_block: why }),
      { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  try {
    CARE.selected = '';
    careSelect(${JSON.stringify(capRow.id)});
    for (let i = 0; i < 150; i++) {
      if (${capPick("capture")} && ${capPick("capture")}.disabled) break;
      await new Promise(r => setTimeout(r, 60));
    }
  } finally { window.fetch = real; }
  const note = document.querySelector('#care-detail .care-exit-note');
  const one = name => {
    const b = document.querySelector(
      '#care-detail .care-exits button[data-exit="' + name + '"]');
    return { off: !!b && b.disabled, title: (b && b.title) || '' };
  };
  return JSON.stringify({ capture: one('capture'), body: one('body'),
    relink: one('relink'), note: note ? note.textContent : '',
    blocked: !!(note && note.classList.contains('care-exit-blocked')), why });
})()`));
check("闸关着时「从浏览器采集」和「手动补正文」一起禁用(换链接也跟着)",
      gate.capture.off && gate.body.off && gate.relink.off, JSON.stringify(gate));
check("禁用态把原因说清了,而且用的是后端那一句、不只塞在 title 里",
      gate.capture.title === gate.why && gate.body.title === gate.why
        && gate.note === gate.why && gate.blocked, JSON.stringify(gate));
// **反过来钉一边**:合格的那几类照旧两颗都开,旁边写的是前提不是拒绝。
await reselect();
await until(`${capPick("capture")} && !${capPick("capture")}.disabled`);
const reopened = JSON.parse(await evalJs(`(() => {
  const one = name => !!document.querySelector(
    '#care-detail .care-exits button[data-exit="' + name + '"]')?.disabled;
  const note = document.querySelector('#care-detail .care-exit-note');
  return JSON.stringify({ capture: one('capture'), body: one('body'),
    note: note ? note.textContent : '' });
})()`));
check("合格的那几类照旧两颗都开,旁边那行写的是前提",
      reopened.capture === false && reopened.body === false && /扩展/.test(reopened.note),
      JSON.stringify(reopened));

// ── 11. 四个宽度无横向溢出 ────────────────────────────────────────────────
for (const [w, h] of [[320, 760], [390, 844], [768, 1024], [1440, 900]]) {
  await send("Emulation.setDeviceMetricsOverride",
             { width: w, height: h, deviceScaleFactor: 1, mobile: w < 700 });
  await sleep(220);
  const over = await evalJs(`(() => {
    const doc = document.documentElement;
    const wide = [...document.querySelectorAll('#carepanel *')]
      .filter(el => el.getBoundingClientRect().right > doc.clientWidth + 1)
      .map(el => el.className || el.tagName);
    return JSON.stringify({ scroll: doc.scrollWidth - doc.clientWidth, wide: wide.slice(0, 3) });
  })()`);
  const info = JSON.parse(over);
  check(`${w}px 没有横向溢出`, info.scroll <= 1 && info.wide.length === 0, over);
}
await send("Emulation.clearDeviceMetricsOverride");

// ── 12. 箭头字符、原生弹窗、JS 错误、console warning ───────────────────────
check("面板里不出现 ↗ ↑ ↓ → ▾ ▴ 字符",
      !(await evalJs(`/[\\u2197\\u2191\\u2193\\u2192\\u25be\\u25b4]/.test($('carepanel').textContent)`)));
check("没有原生 alert/confirm/prompt", dialogs.length === 0, dialogs.join(" | "));
check("没有 JS 错误", pageErrors.length === 0, pageErrors.slice(0, 2).join(" | "));
check("没有 console warning", warnings.length === 0, warnings.slice(0, 2).join(" | "));

ws.close(); proc.kill();
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
out(1, JSON.stringify(results) + "\n");
process.exitCode = bad.length ? 1 : 0;
