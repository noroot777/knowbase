// ── 手动收录的界面贯通探针 ───────────────────────────────────────────────────
// 钉的是**后果**,不是「按钮点下去了」:粘一条链接 → 列表里出现那一篇 →
// vault 里真有那份 md(这一条由 test_grab_probe.py 在 node 之外查盘)。
//
// 不连外网:沙箱把 `grab.request` 打成了固定那一页(serve_sources.py 的
// --grab-fixture),正文提取、入库、导出、索引全是真的。
import { attachCdp, sleep } from "./pipe_history_cdp.mjs";

const base = process.argv[2];
const { proc, ws, send, evalJs, until, clickAt, pageErrors } = await attachCdp();

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride",
           { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: base + "/" });
if (!await until(`typeof switchTab === 'function' && typeof submitGrab === 'function'`, 20000))
  throw new Error("页面没起来");

const checks = [];
const check = (name, ok, detail = "") => checks.push({ name, ok: !!ok, detail: String(detail) });

try {
  // 1. 侧栏那个「存链接」入口把人送到手动收录,并把光标放进输入框
  if (!await clickAt("#nav-grab")) throw new Error("点不到侧栏的「存链接」");
  if (!await until(`sourceKind === 'inbox' && !$('grab-bar').hidden`, 30000))
    throw new Error("没切到手动收录");
  // 面板是**异步**装的:openSourceModule 先摆壳(投递栏、焦点),loadSourceModule
  // 回来之后才刷按钮和列表。所以先等它装完,再断言那几件事 —— 不等的话这几条
  // 断的是摆壳那一刻的页面,机器忙一点就红一片(连跑三轮实测过)。
  // 等满 60 秒:冷启动那一次(建库、补种默认订阅)实测把 30 秒用光过,而那是
  // 环境慢不是产品坏 —— 这一条等的是「面板装完了」,放宽不弱化任何断言。
  if (!await until(`$('source-sync').textContent === '写入知识库'`, 60000))
    throw new Error("面板没装完:「同步全部」还没换成「写入知识库」");
  check("「同步全部」在这一类里叫「写入知识库」", true);
  check("侧栏入口直达手动收录并聚焦输入框",
        await until(`document.activeElement === $('grab-url')`, 8000),
        await evalJs(`document.activeElement && document.activeElement.id`));
  // 这一类来源不该出现「添加订阅」:收件桶是按站点自动建的
  check("这一类没有「添加订阅」入口", await evalJs(`$('source-add').hidden === true`));

  // 2. 粘一条链接、回车存入
  await evalJs(`$('grab-url').value = 'https://example.org/posts/manual-one'; true`);
  if (!await until(`!$('grab-go').disabled`, 10000) || !await clickAt("#grab-go"))
    throw new Error("点不到「存入知识库」");
  check("在途期间按钮禁用、文案变成「正在抓取…」",
        await until(`$('grab-go').disabled && $('grab-go').textContent === '正在抓取…'`, 4000));
  if (!await until(`!$('grab-go').disabled`, 60000)) throw new Error("请求没回来");
  check("成功之后输入框清空、按钮恢复",
        await evalJs(`$('grab-url').value === '' && $('grab-go').textContent === '存入知识库'`),
        await evalJs(`JSON.stringify([$('grab-url').value, $('grab-go').textContent])`));
  check("提示里写明正在写入知识库",
        /写入知识库|其他同步/.test(await evalJs(`$('grab-hint').textContent`)),
        await evalJs(`$('grab-hint').textContent`));
  check("弹的是本项目的 toast、不是原生弹窗",
        await evalJs(`!!document.querySelector('#toast-host .toast.ok')`),
        await evalJs(`document.querySelector('#toast-host .toast')?.textContent || '(没有 toast)'`));

  // 3. 列表里马上看得见它(published 为空的条目按收录时刻排最前)
  if (!await until(`[...document.querySelectorAll('.source-entry')]
        .some(e => e.textContent.includes('把链接存进知识库'))`, 20000))
    throw new Error("列表里没出现刚存的那一篇");
  check("刚存的那一篇出现在内容列表里", true);
  check("收件桶按站点建好并出现在筛选里",
        await evalJs(`[...$('source-filter').options].some(o => o.textContent.includes('example.org'))`),
        await evalJs(`[...$('source-filter').options].map(o => o.textContent).join(' / ')`));

  // 4. 同一条再投一次:如实说「已经收过了」,不再多出一条
  await evalJs(`$('grab-url').value = 'https://example.org/posts/manual-one'; true`);
  await clickAt("#grab-go");
  if (!await until(`!$('grab-go').disabled`, 60000)) throw new Error("第二次请求没回来");
  check("重复投递说「已经收过了」",
        await evalJs(`[...document.querySelectorAll('#toast-host .toast')]
          .some(t => t.textContent.includes('已经收过了'))`),
        await evalJs(`[...document.querySelectorAll('#toast-host .toast')].map(t=>t.textContent).join(' | ')`));
  await sleep(400);
  const copies = await evalJs(`[...document.querySelectorAll('.source-entry')]
          .filter(e => e.textContent.includes('把链接存进知识库')).length`);
  check("列表里仍然只有一条那篇内容", copies === 1, `${copies} 条`);

  // 5. 抓不到的那一档:错误原样说给用户,而且不入库
  await evalJs(`$('grab-url').value = 'https://blocked.example.net/a'; true`);
  await clickAt("#grab-go");
  if (!await until(`!$('grab-go').disabled`, 60000)) throw new Error("第三次请求没回来");
  check("抓不到时把原因说出来",
        await evalJs(`[...document.querySelectorAll('#toast-host .toast.err')]
          .some(t => t.textContent.includes('没收下'))`),
        await evalJs(`[...document.querySelectorAll('#toast-host .toast.err')].map(t=>t.textContent).join(' | ')`));
  check("失败不清输入框（不用重新粘一次）",
        await evalJs(`$('grab-url').value === 'https://blocked.example.net/a'`));
  check("失败也落在提示上（不是停在「正在抓正文」）",
        /没收下/.test(await evalJs(`$('grab-hint').textContent`)),
        await evalJs(`$('grab-hint').textContent`));

  // 6. 慢回包跨过一次列表刷新:**正常成功不许被当成过期丢掉**。
  //    `loadSourceModule` 就是那条自动刷新走的路(闲时 15 秒、管道跑时 4 秒各一
  //    次),这里手动触发一次 = 把那个交错钉成确定的。借列表代次做归属的写法
  //    在这一条上红:输入框不清、提示停在「正在抓这一篇的正文…」。
  await evalJs(`$('grab-url').value = 'https://example.org/posts/slow-one'; true`);
  await clickAt("#grab-go");
  if (!await until(`$('grab-go').disabled`, 8000)) throw new Error("慢请求没起来");
  await evalJs(`loadSourceModule(false); true`);        // 列表刷一次(代次 ++)
  await sleep(300);
  await evalJs(`loadSourceModule(false); true`);
  if (!await until(`!$('grab-go').disabled`, 60000)) throw new Error("慢请求没回来");
  check("慢回包跨过列表刷新仍算自己的：输入框清空",
        await evalJs(`$('grab-url').value === ''`),
        await evalJs(`$('grab-url').value`));
  check("慢回包跨过列表刷新仍算自己的：提示收敛到写入知识库",
        /写入知识库/.test(await evalJs(`$('grab-hint').textContent`)),
        await evalJs(`$('grab-hint').textContent`));
  // 7. 那一轮跑完了,提示要**自己收敛** —— 不能永远停在「正在写入知识库」。
  //    (管道占不到时的末态是「稍后点一次」,同样是一句终态,所以两样都算。)
  check("写入那一轮结束后提示不再停在进行中",
        await until(`/已写入知识库|稍后|没写进知识库|没核到/.test($('grab-hint').textContent)`, 90000),
        await evalJs(`$('grab-hint').textContent`));
  // 7b. **「已写入」这句话要和那一轮的真实结果对得上。** 只验「出现了成功字样」
  //     的话,把提示写死成那句话也是绿的(第二轮审查指出的正是这一点)。所以
  //     这里拿**同一轮**的 run_id 去问后端:两个阶段真是 ok,这句话才算成立。
  const agree = JSON.parse(await evalJs(`(async () => {
    const s = await (await fetch('/api/pipeline/status?source=false')).json();
    const st = s.stages || {};
    return JSON.stringify({ said: $('grab-hint').textContent,
      sameRun: (s.run_id || '') === grabRun,
      ok: ['export', 'index'].every(k => (st[k] || {}).status === 'ok'),
      got: ['export', 'index'].map(k => k + '=' + ((st[k] || {}).status || '-')).join(' ') });
  })()`));
  check("说「已写入」时那一轮的 export/index 真是 ok",
        !/已写入知识库/.test(agree.said) || (agree.sameRun && agree.ok),
        JSON.stringify(agree));
  // 8. **那一轮红了**:面板不许写「已写入知识库」,要落可恢复的那一档并说出原因。
  //    定点注入:沙箱里的管道是真的、不会按需失败,所以这里直接把快照摆成
  //    「同一轮、export 红」再走一次收敛那条路(注入和断言在**同一次** eval 里,
  //    免得轮询在中间把快照换掉)。
  const red = JSON.parse(await evalJs(`(() => {
    grabStage = 'writing'; grabNote = ''; grabRun = 'r-red'; grabRunAt = 'r-before';
    pipeSnapshot = { run_id: 'r-red', running: false, stages: {
      export: { status: 'error', detail: '导出失败：盘满了' }, index: { status: 'ok' } } };
    updateGrabControls();
    return JSON.stringify({ hint: $('grab-hint').textContent, stage: grabStage });
  })()`));
  check("那一轮红了:不说已写入,落可恢复的终态并带上原因",
        !/已写入知识库/.test(red.hint) && /没写进知识库/.test(red.hint)
          && /盘满了/.test(red.hint) && red.stage === 'pending',
        JSON.stringify(red));
  // 9. 快照已经是**别的一轮**:那一轮的结果再也问不到了 —— 如实说没核到,
  //    而不是按「换了一轮就算跑完」谎报已完成。
  const other = JSON.parse(await evalJs(`(() => {
    grabStage = 'writing'; grabNote = ''; grabRun = 'r-mine'; grabRunAt = 'r-before';
    pipeSnapshot = { run_id: 'r-other', running: false,
                     stages: { export: { status: 'ok' }, index: { status: 'ok' } } };
    updateGrabControls();
    return JSON.stringify({ hint: $('grab-hint').textContent, stage: grabStage });
  })()`));
  check("核不到那一轮的结果:如实说没核到(别人的一轮不算自己的)",
        /没核到/.test(other.hint) && other.stage === 'pending', JSON.stringify(other));
  // 9b. **反过来钉一边**:快照还停在起跑之前那一轮时只是轮询还没回来,不许提前
  //     说「核不上」—— 提交之后面板立刻刷一次,那一刻看到的正是这张旧快照。
  const early = JSON.parse(await evalJs(`(() => {
    grabStage = 'writing'; grabNote = ''; grabRun = 'r-mine'; grabRunAt = 'r-before';
    pipeSnapshot = { run_id: 'r-before', running: false, stages: {} };
    updateGrabControls();
    return JSON.stringify({ hint: $('grab-hint').textContent, stage: grabStage, note: grabNote });
  })()`));
  check("快照还没轮询到那一轮时继续等,不提前说核不上",
        early.stage === 'writing' && early.note === '', JSON.stringify(early));
} catch (err) {
  check("探针跑完", false, err.message);
} finally {
  check("页面没有未捕获异常", pageErrors.length === 0, pageErrors.join(" | "));
  console.log(JSON.stringify(checks));
  ws.close();
  proc.kill();
}
