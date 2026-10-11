// 「我的订阅」/「同步中心」拆分后的浏览器回归:各类来源并列的页内切换、公众号与
// feeds 共用同一套页头、定时同步收进页头弹窗、「最新内容」视图锁在可用视口高度上、
// 老深链(#social 等)落到对应视图、同步中心跑完后把失败步骤统一列进总览问题块
// (运行中不弹,最后统一处理)。
//
// 需要一个跑着沙箱数据的 kb-web(和 tests/daily_probe.mjs 同一套):
//   python3 tests/llm_stub.py --port 8801 &
//   python3 tests/daily_sandbox.py /tmp/kb-subs-sandbox 8801
//   KB_DATA_DIR=... KB_VAULT_ROOT=... uv run uvicorn kb.server:app --port 8799
// CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、不连网)。
//
//   HEADLESS_SHELL=<bin> node tests/subs_probe.mjs http://127.0.0.1:8799
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { launchShell } from "./probe_shell.mjs";
const base = process.argv[2];
const profile = mkdtempSync(join(tmpdir(), "kb-subs-merge-"));
const { proc, port } = await launchShell(["--window-size=1440,1000", `--user-data-dir=${profile}`]);
const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));
let id = 0; const pend = new Map(); const pageErrors = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    pageErrors.push(m.params.args.map(a => a.value ?? a.description ?? "").join(" "));
});
const send = (method, params = {}) => { const mid = ++id; return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); }); };
const evalJs = async expression => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (expr, ms = 10000) => { const dl = Date.now() + ms; while (Date.now() < dl) { if (await evalJs(expr)) return true; await sleep(120); } return false; };
await send("Runtime.enable"); await send("Page.enable");
const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));
// 真实鼠标事件(口径同 tests/x_channel_probe.mjs):禁用的控件点不动,而
// `el.dispatchEvent(new Event('input'))` 对禁用控件照样触发 —— 拿后者去验
// 「读取期间改不进去」等于自己把结论做没了。
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

await send("Page.navigate", { url: base + "/" });
await until(`typeof openSubsPanel === 'function' && typeof subsCatalog !== 'undefined' && subsCatalog.length > 0`, 20000);

// 1. 订阅页:各类来源并列,默认落公众号视图
// **来源清单不许写死**(口径同 CLAUDE.md「没有 feed 的站点」那节):写死
// `公众号,大V观点,RSS` 的话,加任何一类来源都会红,而红的地方跟改动毫无关系 ——
// 这一条真正要钉的是「切换器把目录里的每一类都列出来、顺序和目录一致」。
await evalJs(`switchTab('subs')`);
await until(`document.querySelectorAll('#subs-switch button').length === subsCatalog.length`);
check("切换器逐项列出来源目录、顺序一致",
      await evalJs(`[...document.querySelectorAll('#subs-switch button span')].map(e=>e.textContent).join()
                    === subsCatalog.map(m=>m.label).join()`),
      await evalJs(`[...document.querySelectorAll('#subs-switch button span')].map(e=>e.textContent).join()
                    + ' vs ' + subsCatalog.map(m=>m.label).join()`));
check("默认打开公众号视图", await evalJs(`!document.getElementById('subs-wechat').hidden && document.getElementById('subs-feeds').hidden`));
check("地址栏是 #subs", await evalJs(`location.hash`) === "#subs", await evalJs(`location.hash`));
check("公众号视图和 feeds 用同一个页头部件", await evalJs(`!!document.querySelector('#subs-wechat > header.source-heading')
      && !!document.querySelector('#subs-feeds .source-stream > header.source-heading')`));
check("公众号的连接说明是一条横幅,不再是独立的连接卡", await evalJs(`!document.querySelector('#subs-wechat .pipe-source')
      && document.getElementById('src-detail').classList.contains('source-banner')`));
// 定时同步是整页一份的设置:入口收在页头,正文区不再被页尾那张常驻大卡片挤掉一截
check("定时设置收进页头的弹窗、页内不再常驻", await evalJs(`!document.querySelector('#subspanel .pipe-schedule, #subspanel #sched-fields')
      && document.getElementById('subs-sched-open')?.getClientRects().length > 0
      && document.getElementById('sched-modal')?.style.display === 'none'`));
await until(`schedLoaded && !schedLoading`, 12000);
check("页头按钮不打开也写着当前定时状态",
      (await evalJs(`document.getElementById('subs-sched-label')?.textContent || ''`)).includes("未启用"),
      await evalJs(`document.getElementById('subs-sched-label')?.textContent`));
// 入口/弹窗缺了要报红,不能让页面异常把整趟探针打断(定点注入时正是这里先炸)
await evalJs(`try { openSchedModal(); } catch (e) {} true`);
await until(`document.getElementById('sched-modal')?.style.display === '' && !document.getElementById('sched-fields').disabled`, 12000);
check("弹窗里就是原来那套整组设置", await evalJs(`document.getElementById('sched-modal')?.style.display === ''
      && document.querySelectorAll('#sched-modal #sched-channels input').length >= 3
      && !!document.getElementById('sched-time').value`));
await evalJs(`try { closeSchedModal(); } catch (e) {} true`);
check("关掉弹窗不留遮罩", await evalJs(`document.getElementById('sched-modal')?.style.display === 'none'`));
const connectionStates = await evalJs(`(async () => {
  const realJson = window.pipeJson, states = [];
  try {
    for (const source of [
      { online: true, channel_label: '公众号', message: '', hint: '' },
      { online: true, channel_label: '公众号', message: '本机只读到部分会话', hint: '检查采集器权限' },
      { online: false, channel_label: '公众号', message: '采集器连接失败', hint: '可以重新检查', console_url: 'https://example.org/collector' },
    ]) {
      window.pipeJson = (path, ...rest) => path.startsWith('/api/pipeline/status')
        ? Promise.resolve({ source }) : realJson(path, ...rest);
      await refreshPipeSource();
      states.push({ hidden: $('src-detail').hidden, text: $('src-detail').textContent,
        error: $('src-detail').classList.contains('error'), console: $('src-console').style.display });
    }
  } finally { window.pipeJson = realJson; await refreshPipeSource(); }
  return states;
})()`);
check("已连接且没有说明时不显示冗余横幅", connectionStates[0].hidden);
check("已连接时仍完整保留采集器说明和提示", !connectionStates[1].hidden && !connectionStates[1].error
      && connectionStates[1].text.includes('本机只读到部分会话') && connectionStates[1].text.includes('检查采集器权限'));
check("连接失败保留原因、提示和配置入口", !connectionStates[2].hidden && connectionStates[2].error
      && connectionStates[2].text.includes('采集器连接失败') && connectionStates[2].text.includes('可以重新检查')
      && connectionStates[2].console === '');
// 慢读取 × 用户编辑:弹窗每打开一次就重读一次,而这时字段已经是启用的 —— 回包不许
// 把这期间敲进去的值盖掉,更不许让接下来那次保存发出被盖回的那份(2026-10-11 审查
// 实测:读取中改成 18:10,两秒后变回 16:45,POST 的也是 16:45)。
await evalJs(`(() => {
  window.__sched = { posted: null };
  const realJson = window.pipeJson, realPost = window.pipePost;
  window.pipeJson = async (path, ...rest) => {
    if (path !== '/api/pipeline/schedule') return realJson(path, ...rest);
    await new Promise(r => setTimeout(r, 1200));
    return { enabled: true, time: '16:45', channels: [], next_run: '' };
  };
  window.pipePost = async (path, body, ...rest) => {
    if (path !== '/api/pipeline/schedule') return realPost(path, body, ...rest);
    window.__sched.posted = body; return { ...body, next_run: '' };
  };
  openSchedModal();
})(); true`);
await sleep(250);
check("读取还没回来时字段是禁用的(改不进去,也就盖不掉)",
      await evalJs(`document.getElementById('sched-fields').disabled === true`));
// 真的去点一下「启用定时」,再等那一趟读取跑完(等 schedLoading,不是等 disabled
// —— 缺陷版里字段从头到尾就没禁用过,等它等于立刻返回,什么都没验到)
await clickAt("#sched-on");
await until(`schedLoading === false`, 8000);
check("回包到了之后字段才放开,显示的是服务端那份",
      await evalJs(`document.getElementById('sched-fields').disabled === false
        && document.getElementById('sched-time').value === '16:45'`),
      await evalJs(`document.getElementById('sched-time').value`));
// **承重的是这一条**:缺陷版里那一笔敲得进去,于是读完之后屏幕上是服务端那份
// 16:45、底下却写着「有未保存的修改」、保存键还亮着 —— 点下去存的正是被盖回的
// 16:45。刚读完的这一份不该是草稿。
check("刚读完的这一份不是草稿(没有被悄悄吞掉的编辑)",
      await evalJs(`schedDirty === false && document.getElementById('sched-save').disabled === true`),
      await evalJs(`JSON.stringify({ dirty: schedDirty, msg: document.getElementById('sched-msg').textContent,
                                     saveOff: document.getElementById('sched-save').disabled })`));
// 反过来钉一边:字段放开之后改的那一笔必须活到保存(别为了防覆盖把正常编辑也吃掉)
await evalJs(`(() => { const t = document.getElementById('sched-time');
  t.value = '18:10'; t.dispatchEvent(new Event('input'));
  document.getElementById('sched-on').checked = true; markSchedDirty();
  document.querySelector('#sched-channels input').checked = true; })(); true`);
await evalJs(`saveSched()`);
await until(`window.__sched.posted !== null`, 8000);
check("放开之后改的时间真的发了出去",
      (await evalJs(`JSON.stringify(window.__sched.posted)`) || '').includes('"time":"18:10"'),
      await evalJs(`JSON.stringify(window.__sched.posted)`));
// 打桩挂在 window 上,重新导航把它连同这一页一起扔掉
await send("Page.navigate", { url: base + "/" });
await until(`typeof openSubsPanel === 'function' && typeof subsCatalog !== 'undefined' && subsCatalog.length > 0`, 20000);
await evalJs(`switchTab('subs')`);
await until(`document.querySelectorAll('#subs-switch button').length === subsCatalog.length`);
await until(`document.getElementById('tgt-list').textContent.length > 0`, 12000);
check("订阅列表就地渲染(沙箱无采集器,如实报原因)",
      (await evalJs(`document.getElementById('tgt-list').textContent`)).length > 0,
      (await evalJs(`document.getElementById('tgt-list').textContent`)).slice(0, 50));

// 2. 页内切到 RSS 视图
await evalJs(`document.getElementById('subs-switch-rss').click()`);
await until(`!document.getElementById('subs-feeds').hidden`);
check("RSS 视图接管页面、公众号视图收起", await evalJs(`document.getElementById('subs-wechat').hidden`));
check("来源名降为二级标题", await evalJs(`document.getElementById('source-title').tagName`) === "H2");
check("列表内页头只剩标题 + 事实数字(标语整段去掉)", await evalJs(`(() => {
      const header = document.querySelector('#subs-feeds .source-stream > header.source-heading');
      return !document.getElementById('source-description') && header.getClientRects().length > 0
        && header.getBoundingClientRect().height > 0 && header.getBoundingClientRect().height <= 64;
    })()`),
      await evalJs(`Math.round(document.querySelector('#subs-feeds .source-stream > header.source-heading').getBoundingClientRect().height)`));
// 阅读区锁在可用视口高度上。判据钉的是**后果**(整页要不要滚、工作台底边在不在
// 视口里),不是"类名挂上了没有"。
await until(`document.body.classList.contains('subs-reading')`, 8000);
const roomy = async () => await evalJs(`(() => {
  const r = document.getElementById('source-workbench').getBoundingClientRect();
  return { scroll: document.documentElement.scrollHeight, view: innerHeight,
           h: Math.round(r.height), bottom: Math.round(r.bottom) };
})()`);
let box = await roomy();
check("「最新内容」视图整页不出现滚动条", box.scroll <= box.view + 1, JSON.stringify(box));
check("工作台吃掉页头之外的剩余高度(底边落在视口里)",
      box.h > box.view * 0.6 && box.bottom <= box.view + 1, JSON.stringify(box));
// **承重的是这一档**:原来那个 clamp(…, 100dvh - 290px, …) 里的 290 是照着"没有任何
// 横幅"的页头量出来的,而 #source-progress / #source-configuration 这几条随时会冒出来
// —— 每多一条工作台就被整体顶下去一截,正文被切掉底下一块。不摆横幅去问的话,这两条
// 用例对常数版实现也是绿的(定点注入实测:撤掉视口锁后无横幅那一档照样全绿)。
await evalJs(`for (const id of ['source-progress','source-configuration']) {
  const el = document.getElementById(id);
  el.hidden = false; el.textContent = '占位横幅:同步进行中，自动任务会顺延。';
} true`);
await sleep(200);
box = await roomy();
check("横幅冒出来之后仍然不出现滚动条、工作台底边仍在视口里",
      box.scroll <= box.view + 1 && box.bottom <= box.view + 1, JSON.stringify(box));
await evalJs(`for (const id of ['source-progress','source-configuration'])
  document.getElementById(id).hidden = true; true`);
// **承重的还有这一档:列表里真的有东西。** 上面那几条都是在空列表上问的,而空列表
// 本来就矮 —— 一个高度不 definite 的 flex 列在那种内容下照样"刚好放得下",于是
// `min-height` 版实现能全绿地混过去,等库里有了 30 条文章才当场发作:工作台被撑到
// 7598px、两栏各自那条 overflow-y 一行都滚不动(clientHeight == scrollHeight),
// 只能滚整页(2026-10-11 审查实测)。所以这里走真实渲染路径灌 30 条带长正文的条目,
// 判据钉在**后果**上:整页不滚、列表滚得动、正文滚得动、而且两者互不牵动。
const LOADED = `(async () => {
  const items = Array.from({ length: 30 }, (_, i) => ({
    key: 'probe-' + i, title: '验证条目 ' + i + ':一条足够长的标题用来撑开列表行',
    body: Array.from({ length: 70 }, (_, n) => '第 ' + n + ' 段正文。' + '阅读区要能自己滚动，'.repeat(6)).join(String.fromCharCode(10, 10)),
    url: 'https://example.org/probe-' + i, author: '测试作者', source_name: '验证订阅',
    published: '2026-10-11 09:0' + (i % 10), content_kind: 'content', lang: 'zh',
  }));
  const real = window.pipeJson;
  window.pipeJson = async (path, ...rest) => path.includes('/items')
    ? { items, total: items.length, translating: 0, fetching: 0 } : real(path, ...rest);
  try { await renderSourceView(true); } finally { window.pipeJson = real; }
  return document.querySelectorAll('#source-list [data-source-key]').length;
})()`;
check("真实渲染路径灌进 30 条可读条目", await evalJs(LOADED) === 30, String(await evalJs(`sourceRows.length`)));
await evalJs(`readSourceItem('probe-0', false); true`);
await sleep(400);
const scrolls = await evalJs(`(() => {
  const pane = document.getElementById('source-list-pane');
  const body = document.getElementById('source-reader-scroll');
  pane.scrollTop = 230; body.scrollTop = 180;
  return { page: document.documentElement.scrollHeight, view: innerHeight,
           wb: Math.round(document.getElementById('source-workbench').getBoundingClientRect().height),
           paneTop: Math.round(pane.scrollTop), bodyTop: Math.round(body.scrollTop),
           paneOver: pane.scrollHeight > pane.clientHeight, bodyOver: body.scrollHeight > body.clientHeight };
})()`);
check("列表里有内容时整页仍然不滚", scrolls.page <= scrolls.view + 1, JSON.stringify(scrolls));
check("内容列表自己滚得动(不是整页在滚)", scrolls.paneOver && scrolls.paneTop > 0, JSON.stringify(scrolls));
check("正文自己滚得动、和列表互不牵动", scrolls.bodyOver && scrolls.bodyTop > 0 && scrolls.paneTop > 0,
      JSON.stringify(scrolls));
await evalJs(`switchSourceView('manage')`);
await sleep(300);
check("「订阅管理」不锁视口(长列表照旧自然流)", await evalJs(`!document.body.classList.contains('subs-reading')`));
check("「订阅管理」整页照旧能滚到底(列表比视口长)",
      await evalJs(`document.documentElement.scrollHeight >= innerHeight`));
await evalJs(`switchSourceView('items')`);
await until(`document.body.classList.contains('subs-reading')`, 8000);
await until(`sourceData && sourceData.sources.length >= 1`, 12000);
check("feeds 数据照常读进来", await evalJs(`sourceKind`) === "rss");
check("刷新记住所选视图", await evalJs(`localStorage.getItem('kb-subs-kind')`) === "rss");

// 3. 老深链 #social 落到订阅页对应视图
await send("Page.navigate", { url: base + "/#social" });
await until(`typeof activeTab !== 'undefined' && activeTab === 'subs'`, 20000);
await until(`subsKind === 'social' && !document.getElementById('subs-feeds').hidden`);
check("老深链 #social 落到订阅页的大V观点视图", await evalJs(`subsKind === 'social'`));
await evalJs(`document.getElementById('subs-switch-wechat').click()`);
await until(`!document.getElementById('subs-wechat').hidden`);
check("切回公众号视图撤掉视口锁(它是自然流的长列表)", await evalJs(`!document.body.classList.contains('subs-reading')`));

// 4. 同步中心:跑完失败的那轮在总览里统一列出 + 重试按钮
await evalJs(`switchTab('pipe')`);
await sleep(300);
check("离开订阅页撤掉视口锁", await evalJs(`!document.body.classList.contains('subs-reading')`));
check("同步中心没有连接卡和定时设置", await evalJs(`!document.querySelector('#pipepanel .pipe-source')
      && !document.querySelector('#pipepanel #sched-fields, #pipepanel .sched-modal')`));
// 沙箱里有未提取的存量积压:问题块如实显示待提取(和旧版重试按钮同一口径)
check("空闲时问题块只报真实积压", await evalJs(
  `document.getElementById('pipe-issues').hidden
   || document.getElementById('pipe-issues-title').textContent.includes('待提取')`),
  await evalJs(`document.getElementById('pipe-issues-title').textContent`));
// 喂一份「跑完、导出失败」的快照
await evalJs(`renderPipeState({ running:false, stages:{}, log:[], last_run: {
  run_kind:'sync', planned_stages:['crawl','export','index'], finished_at:'2026-10-08 09:00:00',
  started_at:'2026-10-08 08:00:00', crawl_filter:{channel:'wechat_local',accounts:[]},
  stages:{ crawl:{status:'ok',detail:'完成'},
           export:{status:'error',detail:'4 条译文暂缺:模型服务 503'},
           index:{status:'ok',detail:'完成'} }, log:[] } }); true`);
check("失败步骤统一列在总览问题块里",
      await evalJs(`!document.getElementById('pipe-issues').hidden
        && document.getElementById('pipe-issues-title').textContent === '1 个步骤需处理'
        && document.getElementById('pipe-issues-list').textContent.includes('导出文章')
        && document.getElementById('pipe-issues-list').textContent.includes('503')`),
      await evalJs(`document.getElementById('pipe-issues-title').textContent`));
check("问题块里有重试按钮", await evalJs(`!document.getElementById('pipe-retry').hidden`));
// 运行中不弹问题块(失败不阻止本轮继续,收尾统一说)
await evalJs(`renderPipeState({ running:true, current_stage:'index', stop_supported:true,
  planned_stages:['crawl','export','index'], started_at:'2026-10-08 08:00:00',
  crawl_filter:{channel:'wechat_local',accounts:[]},
  stages:{ crawl:{status:'ok',detail:'完成'},
           export:{status:'error',detail:'失败 4'},
           index:{status:'running',detail:'建立索引'} }, log:[] }); true`);
check("运行中问题块不出现(最后统一处理)", await evalJs(`document.getElementById('pipe-issues').hidden`));

check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300));
const out = [...oks.map(n => `  ok ${n}`), ...fails.map(n => `  FAIL ${n}`)].join("\n");
process.stdout.write(`${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`);
ws.close();
proc.ref();
await new Promise(resolve => { proc.once('exit', resolve); proc.kill(); });
rmSync(profile, { recursive: true, force: true });
process.exitCode = fails.length ? 1 : 0;
