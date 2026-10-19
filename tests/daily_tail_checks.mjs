// 出报之后那一段(整理图鉴与 Wiki)跑着的时候,页面说什么、刷不刷阅读区。
//
// **从 tests/daily_probe.mjs 拆出来只因为行数**(那边贴着 700 行上限)。
//
// 背景:日报出完报之后还挂着一段几十分钟的整理,那段时间里 `running` 仍是 true。
// 页面原来凡是「这一轮有结果了吗」都读 `running` —— 于是报已经出来了、阅读区却要
// 再等一个钟头才刷新,页头那行字一直写着「正在生成」(审查 R1)。后端把日报的
// **结论**和整理的**进度**分开记(`phase` 落 done/failed/stopped 就是终态,整理走
// `library` 那一格),这一份钉的就是消费端:
//
// ①**结论一落地就刷新阅读区、就弹完成提示**,不等整理跑完;
// ②那段时间里页头那行字说的是整理的进度,**不是「正在生成」**,按钮也不写「生成中」;
// ③**反过来钉一边**:整理跑完的那一跳不许再刷一遍、也不许再弹一次(同一轮,结果
//   一个字没变 —— 多刷一次就是把用户正在读的那一份从底下换掉)。
//
// 判据落在**刷新次数**和**页面上那行字**上,不验"某个标志位有没有打上"。
export async function checkDailyTail({ evalJs, check }) {
  const settled = {
    phase: 'done', detail: '日报已生成：2 条素材', run_id: 'daily-tail-1',
    settled_at: '2026-10-19 09:40:00', channels: [],
  };
  await evalJs(`(() => {
    clearTimeout(dailyTimer); dailyTimer = null;
    document.getElementById('toast-host').innerHTML = '';
    activeTab = 'daily';
    window.__realPipeJson = pipeJson;
    window.__realLoad = loadDailyReports; window.__realWin = loadDailyWindow;
    window.__loads = 0;
    loadDailyReports = async () => { window.__loads++; };
    loadDailyWindow = async () => {};
    dailyState = null; dailyDoneRun = null;
    window.__fed = [
      { running: true, phase: 'generate', detail: '正在生成日报', channels: [] },
      // 报出来了,后面那段整理还要跑几十分钟 —— running 仍然是 true
      { running: true, ...${JSON.stringify(settled)},
        library: { phase: 'sync', detail: '整理工具与 Wiki：同步中' } },
      // 整理也跑完了:同一轮、同一个结论,什么都没变
      { running: false, ...${JSON.stringify(settled)}, finished_at: '2026-10-19 10:30:00',
        library: { phase: 'done', detail: '整理工具与 Wiki：完成' } }];
    pipeJson = async (path, ...rest) => path === '/api/daily/status' && window.__fed.length
      ? window.__fed.shift() : window.__realPipeJson(path, ...rest);
    return true; })()`);

  await evalJs(`refreshDailyStatus()`);
  check("还在出稿那一跳:不刷阅读区、也不弹完成提示",
        (await evalJs(`window.__loads`)) === 0
        && !(await evalJs(`document.getElementById('toast-host').textContent`)).trim(),
        await evalJs(`JSON.stringify([window.__loads, dailyPhaseText(dailyState)])`));

  await evalJs(`refreshDailyStatus()`);
  const mid = await evalJs(`JSON.stringify({
    loads: window.__loads,
    toast: document.getElementById('toast-host').textContent,
    text: document.getElementById('daily-progress-text').textContent,
    shown: !document.getElementById('daily-progress').hidden,
    label: document.getElementById('daily-gen-label').textContent,
    dot: document.getElementById('nav-daily-dot').getAttribute('aria-label') })`);
  const m = JSON.parse(mid);
  check("报一落地就刷新阅读区,不等后面那段整理", m.loads === 1, mid);
  check("报一落地就弹完成提示", /日报已生成/.test(m.toast), mid);
  check("整理跑着时那行字说的是整理的进度,不写「正在生成」",
        m.shown && /整理工具与 Wiki/.test(m.text) && !/正在生成/.test(m.text), mid);
  check("整理跑着时按钮写「整理中」,不写「生成中」",
        /整理中/.test(m.label) && !/生成中/.test(m.label), mid);
  // 侧栏那颗点只有读屏软件念得到,所以它最容易被漏掉 —— 而漏掉的后果是:正文写着
  // 「上次生成失败」、按钮写着「整理中…」,念出来的却是「日报生成中」(审查 N2)。
  check("整理跑着时侧栏那颗点念的也是整理,不是「日报生成中」",
        /整理/.test(m.dot) && !/日报生成中/.test(m.dot), mid);

  await evalJs(`refreshDailyStatus()`);
  const end = await evalJs(`JSON.stringify({
    loads: window.__loads,
    toasts: document.querySelectorAll('#toast-host > *').length })`);
  check("整理跑完那一跳:同一轮、结果没变,不再刷一遍阅读区",
        JSON.parse(end).loads === 1, end);
  check("整理跑完那一跳:也不再弹一次完成提示",
        JSON.parse(end).toasts <= 1, end);

  await evalJs(`(() => { pipeJson = window.__realPipeJson;
    loadDailyReports = window.__realLoad; loadDailyWindow = window.__realWin;
    clearTimeout(dailyTimer); dailyTimer = null;
    document.getElementById('toast-host').innerHTML = '';
    return true; })()`);
}
