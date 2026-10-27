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
  //    末态四句:已写入 / 这一篇还没进知识库 / 没核到 / 稍后点一次。
  check("写入那一轮结束后提示不再停在进行中",
        await until(`/已写入知识库|稍后|还没进知识库|没核到/.test($('grab-hint').textContent)`, 90000),
        await evalJs(`$('grab-hint').textContent`));
  // 7b. **「已写入」这句话要由这一篇自己的事实支撑。** 只验「出现了成功字样」的
  //     话,把提示写死成那句也是绿的;而按那一轮 export/index 双 ok 收尾同样会
  //     谎报 —— 阶段全绿而这一篇不在库里是实测形态(下面第 8 条就是它)。所以
  //     这里去问**条目事实**(`/api/grab/state`),两个方向各钉一边。
  const agree = JSON.parse(await evalJs(`(async () => {
    const r = await fetch('/api/grab/state?url=' + encodeURIComponent(grabUrl));
    const s = r.ok ? await r.json() : {};
    return JSON.stringify({ said: $('grab-hint').textContent, done: !!s.done,
                            owing: s.owing || '', url: grabUrl });
  })()`));
  check("说「已写入」时这一篇真查得到", !/已写入知识库/.test(agree.said) || agree.done,
        JSON.stringify(agree));
  check("这一篇真进了知识库时就说已写入", !agree.done || /已写入知识库/.test(agree.said),
        JSON.stringify(agree));

  /** 注入一张快照 + 一条地址,走一次收尾,等它落定。
   *  沙箱里的管道是真的、不会按需失败,所以这几条用定点注入。收尾是**异步**的
   *  (要问一次条目状态),等待和注入必须在**同一次** eval 里 —— 分两次的话中间
   *  那次轮询会把快照换掉,断言就落在另一张快照上了。 */
  const converge = async (js) => JSON.parse(await evalJs(`(async () => {
    grabBusy = false; grabStage = 'writing'; grabNote = ''; grabWhy = '';
    grabRun = ''; grabRunAt = 'r-before';
    grabResume = '，稍后在「手动收录」里点一次「写入知识库」';
    ${js}
    updateGrabControls();
    const mid = $('grab-hint').textContent;
    for (let i = 0; i < 200 && grabStage === 'checking'; i++)
      await new Promise(r => setTimeout(r, 25));
    return JSON.stringify({ mid, hint: $('grab-hint').textContent,
                            stage: grabStage, note: grabNote });
  })()`));
  // 用一条**没收录过**的地址当素材:它在库里的事实是确定的(不在),于是「阶段
  // 结果」和「条目事实」在这几条上永远对不上 —— 正是要分清的那个岔口。
  const ghostUrl = `grabUrl = 'https://example.org/posts/never-grabbed';`;

  // 8. **整轮 export/index 双 ok 不等于这一篇进了知识库。** 实测形态:vault 里那
  //    一篇被删掉而两个 hash 还相等时,export 一个字没写、index 正确地把它那行
  //    旧索引收掉,阶段全绿而它搜不到。按阶段结果收尾的写法在这一条上说「已写入
  //    知识库」,而用户搜不到、再投一次只会得到「已经收过了」。
  const ghost = await converge(`${ghostUrl}
    grabRun = 'r-ok';
    pipeSnapshot = { run_id: 'r-ok', running: false,
      stages: { export: { status: 'ok' }, index: { status: 'ok' } } };`);
  check("整轮双 ok 而这一篇不在库里:不说已写入,并说出欠哪一步",
        !/已写入知识库/.test(ghost.hint) && ghost.stage === 'pending'
          && /还没收进来/.test(ghost.hint) && /点一次「写入知识库」/.test(ghost.hint),
        JSON.stringify(ghost));
  check("核对期间说的是正在核对,不是停在正在写入",
        /正在核对/.test(ghost.mid), JSON.stringify(ghost));
  // 8b. **那一轮红了**:阶段的红不当结论用,但要用来给失败那句话补上原因 ——
  //     只说「还没进知识库」而不说为什么,用户只能反复点那颗按钮。
  const red = await converge(`${ghostUrl}
    grabRun = 'r-red';
    pipeSnapshot = { run_id: 'r-red', running: false, stages: {
      export: { status: 'error', detail: '导出失败：盘满了' }, index: { status: 'ok' } } };`);
  check("那一轮红了:不说已写入,落可恢复的终态并带上原因",
        !/已写入知识库/.test(red.hint) && /还没进知识库/.test(red.hint)
          && /盘满了/.test(red.hint) && red.stage === 'pending', JSON.stringify(red));
  // 9. 快照已经是**别的一轮**:它的绿和红都不算自己的 —— 不许借它的绿说已写入,
  //    也不许把它的红当成这一篇的原因(那会指着一个毫不相干的毛病让用户去修)。
  const other = await converge(`${ghostUrl}
    grabRun = 'r-mine';
    pipeSnapshot = { run_id: 'r-other', running: false, stages: {
      export: { status: 'error', detail: '别人那一轮的毛病' }, index: { status: 'ok' } } };`);
  check("别人的一轮:绿不借、红也不借",
        !/已写入知识库/.test(other.hint) && !/别人那一轮的毛病/.test(other.hint)
          && /还没收进来/.test(other.hint) && other.stage === 'pending',
        JSON.stringify(other));
  // 10b. **「写进去了」不等于「写进去的是一篇文章」。** 库里那一份看不出是正文时,
  //      不许说「已写入知识库,搜得到了」—— 那句话会让用户以为检索里有一篇文章,
  //      而他搜到的是一页登录提示。出路要一起说出来(再投一次会重抓替换)。
  const bad = await converge(`${ghostUrl}
    grabRun = 'r-ok';
    pipeSnapshot = { run_id: 'r-ok', running: false,
      stages: { export: { status: 'ok' }, index: { status: 'ok' } } };
    const real = window.fetch;
    window.fetch = (u, o) => {
      if (!String(u).includes('/api/grab/state')) return real(u, o);
      window.fetch = real;
      return Promise.resolve(new Response(JSON.stringify({ ok: true, known: true,
        done: true, content_ok: false, owing: '收下的那一份看不出是正文' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };`);
  check("收下的内容看不出是正文:不说已写入,并指向重抓这条出路",
        !/已写入知识库/.test(bad.hint) && /看不出是正文/.test(bad.hint)
          && /重新抓一次/.test(bad.hint) && bad.stage === 'pending', JSON.stringify(bad));

  // 9b. **反过来钉一边**:快照还停在起跑之前那一轮时只是轮询还没回来,不许提前
  //     收尾 —— 提交之后面板立刻刷一次,那一刻看到的正是这张旧快照。
  const early = await converge(`${ghostUrl}
    grabRun = 'r-mine';
    pipeSnapshot = { run_id: 'r-before', running: false, stages: {} };`);
  check("快照还没轮询到那一轮时继续等,不提前收尾",
        early.stage === 'writing' && early.note === '', JSON.stringify(early));
  // 10. **问不到就说问不到**(元规则:判不准不许返回「确定没有」的结论)。只拦
  //     那一问、拦一次,其余请求照走 —— 整个 fetch 拦掉会把列表轮询也打断。
  const lost = await converge(`${ghostUrl}
    grabRun = 'r-ok';
    pipeSnapshot = { run_id: 'r-ok', running: false,
      stages: { export: { status: 'ok' }, index: { status: 'ok' } } };
    const real = window.fetch;
    window.fetch = (u, o) => String(u).includes('/api/grab/state')
      ? (window.fetch = real, Promise.reject(new Error('断网')))
      : real(u, o);`);
  check("这一篇的状态问不到:如实说没核到,不猜一个结论",
        /没核到/.test(lost.hint) && lost.stage === 'pending', JSON.stringify(lost));
  // 11. **回包没送达 ≠ 后端没存下来。** 保存排在起管道之前,断线可能发生在它
  //     之后 —— 说「这条没存下来」是在断言一件我们不知道的事。定点把 POST 掐断,
  //     面板该去问一次这一篇的状态,再按事实说话。
  const cut = async (state) => JSON.parse(await evalJs(`(async () => {
    $('grab-url').value = 'https://example.org/posts/cut-wire';
    const real = window.fetch;
    window.fetch = (u, o) => String(u).includes('/api/grab/state')
      ? Promise.resolve(new Response(JSON.stringify(${state}),
          { status: 200, headers: { 'Content-Type': 'application/json' } }))
      : (String(u).includes('/api/grab') ? Promise.reject(new Error('断线')) : real(u, o));
    await submitGrab();
    for (let i = 0; i < 200 && grabStage === 'checking'; i++)
      await new Promise(r => setTimeout(r, 25));
    window.fetch = real;
    return JSON.stringify({ hint: $('grab-hint').textContent, stage: grabStage,
                            left: $('grab-url').value });
  })()`));
  const landed = await cut(`{ ok: true, known: true, done: true, content_ok: true, owing: '' }`);
  check("提交断在半路、而它其实存下了:按事实说,不说没存下来",
        !/没存下来/.test(landed.hint) && /断在半路/.test(landed.hint)
          && /已写入知识库/.test(landed.hint) && landed.stage === 'done',
        JSON.stringify(landed));
  // 11b. **反过来钉一边**:库里压根没有这一行,这时才说得出「没存下来」。
  const lostIt = await cut(`{ ok: true, known: false, done: false, content_ok: false,
                              owing: '这条还没收进来' }`);
  check("提交断在半路、库里也确实没有:如实说没存下来并留着那条链接",
        /没存下来/.test(lostIt.hint) && lostIt.stage === 'failed'
          && lostIt.left === 'https://example.org/posts/cut-wire', JSON.stringify(lostIt));

  // 12. **错存的唯一出路是那颗「重新抓一次」。** 质量那一道只认得出我们认得出的
  //     坏内容(通篇复读那一类),「五段各不相同的会员说明」过得了关、从此
  //     `content_ok` 恒真 —— 恢复出口要是也架在它上面,这种错存一辈子换不回来。
  //     所以只要库里有这一行,按钮就得在;换不换得成由后端的八道准入和 CAS 决定。
  await evalJs(`$('grab-url').value = 'https://example.org/posts/manual-one'; true`);
  if (!await until(`!$('grab-go').disabled`, 10000) || !await clickAt("#grab-go"))
    throw new Error("点不到「存入知识库」");
  if (!await until(`!grabBusy && grabKnown === true`, 60000))
    throw new Error("重复投递没落地");
  check("库里有这一行时「重新抓一次」才露面",
        await evalJs(`$('grab-refetch').hidden === false`),
        await evalJs(`$('grab-hint').textContent`));
  // 真点它:沙箱那一页内容没变,后端如实说「和它一样」而**不谎报完成** ——
  // 这句话本身证明 refetch 传到了后端(不带它的重复投递回的是「已经在知识库里了」)。
  if (!await clickAt("#grab-refetch")) throw new Error("点不到「重新抓一次」");
  if (!await until(`!grabBusy && grabStage !== ''`, 60000)) throw new Error("重抓没回来");
  const again = JSON.parse(await evalJs(
    `JSON.stringify({ hint: $('grab-hint').textContent, stage: grabStage })`));
  check("重抓到的和库里那一份一样:如实说没换成,不说已完成",
        again.stage === 'pending' && /一样|已经有人换过/.test(again.hint)
          && !/已写入知识库/.test(again.hint), JSON.stringify(again));
  check("没换成之后那颗按钮还在(这不是终态)",
        await evalJs(`$('grab-refetch').hidden === false`));
  // 12b. 换成了的那一档:面板要说清库里那一份被换掉了,并接着等写入知识库。
  const swapped = JSON.parse(await evalJs(`(async () => {
    const real = window.fetch;
    window.fetch = (u, o) => (String(u).includes('/api/grab') && o && o.method === 'POST')
      ? (window.fetch = real, Promise.resolve(new Response(JSON.stringify({ ok: true,
          duplicate: true, replaced: true, done: false, content_ok: true,
          pipeline: 'started', run: 'r-swap', url: 'https://example.org/posts/manual-one',
          title: '真正的文章', detail: '你要求重新抓一次，已经按这次重抓到的内容换掉，正在写入知识库' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } })))
      : real(u, o);
    await refetchGrab();
    window.fetch = real;
    return JSON.stringify({ hint: $('grab-hint').textContent, stage: grabStage, run: grabRun });
  })()`));
  check("换成了:说清那一份被换掉,并继续等写进知识库",
        swapped.stage === 'writing' && /换掉/.test(swapped.hint)
          && swapped.run === 'r-swap', JSON.stringify(swapped));

  // 12c. **「重新抓一次」只对「`url` 指向它自己」的来源露面。** `known()` 按
  //      **url** 查全表,命中的可能是任何一类来源的条目:简报那条简讯的 `url` 指向
  //      的是别人家那篇文章,后端会拒(`grab_api._recover`)—— 亮着就只是一颗必定
  //      失败的按钮,和后端静默覆盖是同一处口径分家的两半(来源卡片那一侧曾经就是
  //      这么留下一颗必定 409 的入口,见 tests/source_post_probe.mjs)。
  //      **问的不是「这一类进不进补抓队列」**:推文在那一问上不许可、在这一问上
  //      许可(它的链接就是它本人的永久链接),两者只在这一类上分叉 —— 按前者判的
  //      后果是那几百条残片唯一的出路连按钮都没了,所以下面三档里有一档专钉它。
  //      走的是真实赋值路径(打桩 `/api/grab/state` 的回包再调 `grabCheckItem`),
  //      不是直接设那个变量。
  const refetchShown = payload => evalJs(`(async () => {
    const real = window.fetch;
    window.fetch = (u, o) => String(u).includes('/api/grab/state')
      ? Promise.resolve(new Response(JSON.stringify(${payload}), { status: 200 }))
      : real(u, o);
    try { await grabCheckItem(); } finally { window.fetch = real; }
    return !$('grab-refetch').hidden;
  })()`);
  const KNOWN = "ok: true, known: true, done: false, content_ok: true, "
                + "owing: '还没写成 vault 里那一篇'";
  check("链接指向别处的来源:「重新抓一次」不露面",
        await refetchShown(`{ ${KNOWN}, kind: 'digest', url_is_origin: false }`) === false);
  // **反过来钉一边**:链接指向它自己的来源照旧露面 —— 这是错存唯一的出路
  // (`put_items` 是 DO NOTHING),一律收走等于把它整条打死。
  check("链接指向它自己的来源:「重新抓一次」照旧露面",
        await refetchShown(`{ ${KNOWN}, kind: 'inbox', url_is_origin: true }`) === true);
  // **分叉那一档**:大V观点进不了补抓队列,但它的链接是它本人的永久链接 —— 按
  // 「进不进补抓队列」判的话这一颗会消失,而那是那几百条推文残片唯一的人工出路。
  check("大V观点:进不了补抓队列,这颗按钮照旧露面",
        await refetchShown(`{ ${KNOWN}, kind: 'social', url_is_origin: true }`) === true);
  // **字段缺失不算「不许可」**:浏览器里缓存着旧脚本、回包没走到那几档,按原来那样
  // 亮,不凭「读不到」就把那条出路收走(前端只拿它和 `false` 比;后端那几条回包路径
  // 也因此必须把这个键填齐,见 `test_grab_endpoint.py` 撞车那一条)。
  check("回包没给这一列时照旧露面",
        await refetchShown(`{ ${KNOWN}, kind: 'inbox' }`) === true);

  // ── 13. 刷新之后立刻点侧栏:首次定位不许把人点的那一下扔掉 ────────────────
  // 这一局是**摆出来的**,不靠抢时机:把页面还回「地址还没被读走」那一刻
  // (`workspaceLocated = false`),再按真实顺序走一遍 —— 人先点,定位后到。
  // 真机上那一刻就是「刷新后立刻点侧栏」,机器忙的时候几乎必中(满载实测第 1 次
  // 就红),空载约 40 次红 1 次 —— 所以靠抢时机的写法会变成一条看运气的用例。
  //
  // 判据是**人眼看得见的那件事**:点了「存链接」,眼前就该是「我的订阅」那一屏、
  // 投递栏露着,而不是被弹回图鉴。真机上跟着来的还有一截:来源面板的装载有一道
  // `sourceActive()`(判据含 `activeTab === 'subs'`),被切走之后它直接 return,
  // 面板的数据请求一次都没发、那颗按钮停在「同步全部」。
  //
  // **那两样在这个摆局里都当不了判据**(注入实测,如实记着):`sourceData` 在 8 秒
  // 窗口里被别的路重填;按钮那行字被页面上那条还在跑的轮询重画了 —— 它只看
  // `sourceKind`,不看面板装没装完。真机上它们之所以卡着,是因为页面刚打开、
  // 那两条路一次都还没跑过。所以这里钉的是「哪一屏在眼前」,它由 `switchTab`
  // 直接写,谁也盖不掉。
  await evalJs(`history.replaceState(null, '', '#gallery');
    switchTab('gallery'); workspaceLocated = false; workspaceNavigated = false; true`);
  await evalJs(`grabFromNav(); true`);              // 人点的那一下(此刻还写不进地址栏)
  await evalJs(`restoreWorkspaceLocation(); true`); // 页面装完才跑的那一步
  check("刷新后立刻点侧栏:那一下没被首次定位扔掉,眼前就是手动收录那一屏",
        await until(`activeTab === 'subs' && sourceKind === 'inbox'
                     && $('subspanel').style.display !== 'none'
                     && $('gallerypanel').style.display === 'none'
                     && !$('grab-bar').hidden`, 8000),
        await evalJs(`JSON.stringify({activeTab, sourceKind, 地址: location.hash,
          订阅屏: $('subspanel').style.display, 图鉴屏: $('gallerypanel').style.display,
          投递栏隐藏: $('grab-bar').hidden})`));
  check("人点的那一下也落进了地址栏(定位期间写被挡着,定位完要补上)",
        await evalJs(`location.hash === '#subs'`), await evalJs(`location.hash`));
  // **反过来钉一边**:没人点过的时候,地址栏照旧说话 —— 否则深链就全失效了。
  // 用「全文检索」而不是日报:这个沙箱摆得出它要的那两个接口,不会带出别的红。
  await evalJs(`history.replaceState(null, '', '#search');
    workspaceLocated = false; workspaceNavigated = false;
    restoreWorkspaceLocation(); true`);
  check("没人点过的时候照旧按地址栏定位",
        await evalJs(`activeTab === 'search' && location.hash === '#search'`),
        await evalJs(`JSON.stringify([activeTab, location.hash])`));

} catch (err) {
  check("探针跑完", false, err.message);
} finally {
  check("页面没有未捕获异常", pageErrors.length === 0, pageErrors.join(" | "));
  console.log(JSON.stringify(checks));
  ws.close();
  proc.kill();
}
