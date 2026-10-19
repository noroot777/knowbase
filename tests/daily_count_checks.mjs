// 生成按钮前面那句**「有 N 条新内容」**,以及它背后那三个各答各的数。
//
// 这一组钉的是:
// ① **0 条时这颗按钮整个不显示** —— 再生成一次会原样挑回今天那份已经带走的那批,
//    点下去只会花一次模型钱、拿回同一份日报;留一句灰字交代它为什么不在;
// ② **「不知道」不许当成「没有」** —— 后端答 `pending: null`(算不出来)说「读不
//    出来」,旧后端没给这个字段就整段不说,**两者都照常显示按钮**;
// ③ **三个数不许互相冒充**:这一次会多带进来几条(`gain`,写在按钮边上)、队列里
//    一共积着多少(`pending.total`,只进 tooltip)、今天抓回来多少(`fetch`,抓取
//    自己的事实,也只进 tooltip);
// ④ tooltip 里抓取事实的六种说法 + 那个数由什么构成的三档分解,一句都不许并进别人;
// ⑤ 这些事实是从**外面**进来的,页面自己的空闲轮询和整块重载都得接得住,而
//    用户填过的那两个时刻一个字都不许被覆盖。
//
// **从 tests/daily_probe.mjs 拆出来只因为行数**(那边到了 700 行上限),摆法和
// 判据原样搬过来、共用同一次页面会话。后端那一侧的不变量在
// tests/test_daily_carried.py(`gain` 那四条)、tests/test_fetch_rounds.py、
// tests/test_fetch_receipt.py 与 tests/test_fetch_identity.py。
export async function checkNewContentCount({ evalJs, waitFor, check }) {
  const line = `document.getElementById('daily-new-count')`;
  const tip = `document.getElementById('daily-gen').title`;
  // 可见那一句、它藏没藏、按钮**点不点得了**、tooltip,外加两个时刻输入框 ——
  // 下面每一档都按这一份快照判,免得各写各的 selector。
  //
  // **`genOff` 读的是 `disabled` 而不是 `hidden`。** 页头两颗按钮并成一颗之后,
  // 「这一次生成一个字都不会多」那一档从「整颗藏掉」改成了「置灰 + 把出路写在
  // 它上面」—— 藏掉会把旁边「先同步一轮」那条路一起藏了。语义仍是「这颗按钮现在
  // 用不了」,所以下面每一档的判据原样成立。
  const now = `(() => JSON.stringify({
    line: ${line}.textContent, lineOff: ${line}.hidden,
    genOff: document.getElementById('daily-gen').disabled,
    label: document.getElementById('daily-gen-label').textContent,
    tip: ${tip},
    since: document.getElementById('daily-since').value,
    until: document.getElementById('daily-until').value }))()`;
  const snap = async () => JSON.parse(await evalJs(now));

  // 真实状态:探针刚生成过今天那一份,沙箱里那三篇素材全被它带走了。
  let s = await snap();
  check("素材都进了日报时,「重新生成」这颗按钮点不动",
        s.genOff && s.line === "没有新内容" && !s.lineOff, JSON.stringify(s));
  check("点不动的时候要说清为什么、以及出路在哪 —— 不说的话它和坏了没区别",
        await evalJs(`${line}.classList.contains('is-none')
          && ${line}.offsetParent !== null
          && document.getElementById('daily-gen').offsetParent !== null
          && /不会多带进任何内容/.test(document.getElementById('daily-gen').title)
          && /先同步一轮/.test(document.getElementById('daily-gen').title)`),
        JSON.stringify(s));
  // **勾上「先同步一轮」按钮就该恢复可点。** 这是「没有新内容」时唯一的出路:
  // 那个判据(后端按**现有库存**算的 `gain`)对「先去取一遍新的」压根不成立。
  // 一直置灰等于把这条路堵死,而藏掉连这个勾都成了没有去处的勾。
  s = JSON.parse(await evalJs(`(() => {
    const chk = document.getElementById('daily-presync');
    chk.checked = true; renderDailyGenButton();
    const got = ${now};
    chk.checked = false; renderDailyGenButton(); return got; })()`));
  check("没有新内容时勾上「先同步一轮」,按钮恢复可点、名字也改了",
        !s.genOff && /^同步后/.test(s.label), JSON.stringify(s));
  s = await snap();
  check("取消那个勾又回到点不动(摆盘自检:上面那一下复位了)", s.genOff, JSON.stringify(s));
  // 沙箱从没跑过管道,所以此刻确实"今天还没有同步过" —— 这是一句事实,不是零。
  check("没抓过时 tooltip 这么说", /今天还没有同步过/.test(s.tip), s.tip);

  // ── 按钮回不回得来 ──────────────────────────────────────────────────────
  // 有新内容进来时它得自己回来(摆盘自检:上面那一档证明此刻它确实是藏着的)。
  // (「此刻它确实点不动」由上面那两档证明)
  const render = (patch) => `(() => { const keep = JSON.stringify(dailyWindow);
    Object.assign(dailyWindow, ${patch}); renderDailyGenButton();
    const got = ${now};
    dailyWindow = JSON.parse(keep); renderDailyGenButton(); return got; })()`;
  s = JSON.parse(await evalJs(render(`{ pending: { items: 7, late: 2, total: 9, gain: 9 } }`)));
  check("有新内容时按钮恢复可点,并在它前面写有几条",
        !s.genOff && s.line === "有 9 条新内容" && !s.lineOff, JSON.stringify(s));
  // **按钮边上那个数是「这一次会多带进来几条」,不是队列总长,也不是抓回来多少。**
  // 三个数答的是三个问题:抓回来 860 篇、队列里还积着 1097 条、而这一次生成只多带
  // 得进 11 条(生产库实测)。把队列总长写在按钮边上,用户会照着它花一次模型钱,
  // 拿回一份只多出 11 条的日报。
  s = JSON.parse(await evalJs(render(
    `{ fetch: { n: 1, added: 860, at: '09:00' },
       pending: { items: 79, late: 1018, total: 1097, gain: 11 } }`)));
  check("队列再大、抓得再多,按钮边上写的还是「这一次会多带进来几条」",
        s.line === "有 11 条新内容" && !/1097|860/.test(s.line), JSON.stringify(s));
  check("那两个数都在 tooltip 里,各说各的",
        /1097 条还没进过任何日报/.test(s.tip) && /新加了 860 篇/.test(s.tip)
        && !/陆续带走/.test(s.tip), s.tip);
  // **「不知道」不许当成「没有」。** 后端算不出来(pending: null)时按钮照显示 ——
  // 藏掉等于拿一次读取故障把出报这条路堵死,而页面上一个字都不说。
  s = JSON.parse(await evalJs(render(`{ pending: null }`)));
  check("算不出来时说「读不出来」,按钮照旧可点",
        !s.genOff && s.line === "新内容读不出来" && !s.lineOff, JSON.stringify(s));
  // 旧后端没给这个字段(kb/static/ 每次请求从磁盘读、server.py 要重启才生效,
  // 「新前端 + 旧后端」天天走到):整段不说,按钮同样照显示。
  s = JSON.parse(await evalJs(render(`{ pending: { items: 3, late: 0, total: 3 } }`)));
  check("旧后端没给这个数时整段不说,按钮照旧可点",
        !s.genOff && s.lineOff, JSON.stringify(s));
  // **一次读取失败也是「不知道」,而且必须当场改口。** 后端答 `pending: null` 那
  // 一路覆盖不到 HTTP 错误、断连、解析失败和 `pipeJson` 的超时 —— 留着上一次那个
  // 0 就是拿一次历史成功当无限期证据:页面继续写「没有新内容」、继续藏着按钮,而
  // 库里明明又进了新文章(审查实测:只对这一个接口注入网络失败,服务端确有新增,
  // 面板照旧说 0 并藏着按钮)。五个入口共用一个接收点,所以注在那里、从空闲轮询
  // 这一个入口驱动。
  await evalJs(`(() => { window.__winFail = pipeJson;
    pipeJson = async (path, ...rest) => {
      if (path === '/api/daily/window') throw new Error('摆局:断连');
      return window.__winFail(path, ...rest); };
    return true; })()`);
  await evalJs(`refreshDailyPending()`);
  s = await snap();
  check("空闲那一跳读失败:当场改口说读不出来,按钮跟着恢复可点",
        !s.genOff && s.line === "新内容读不出来" && !s.lineOff, JSON.stringify(s));
  await evalJs(`(() => { pipeJson = window.__winFail; delete window.__winFail;
                         return true; })()`);
  await evalJs(`loadDailyWindow()`);
  s = await snap();
  check("读回来之后那个数自己恢复", s.genOff && s.line === "没有新内容", JSON.stringify(s));
  // 反过来钉一边:**迟到的失败不许盖掉更晚那趟刚读到的好事实**。摆法是让这一趟
  // 在失败之前把代次顶掉(等价于"它还在路上时又发了一趟"),它的失败就该整趟作废。
  await evalJs(`(() => { window.__winFail = pipeJson;
    pipeJson = async (path, ...rest) => {
      if (path === '/api/daily/window') { dailyWindowSeq++; throw new Error('摆局:迟到的失败'); }
      return window.__winFail(path, ...rest); };
    return true; })()`);
  await evalJs(`refreshDailyPending()`);
  s = await snap();
  check("被顶掉的那一趟失败了,也不许把刚读到的数抹成不知道",
        s.genOff && s.line === "没有新内容", JSON.stringify(s));
  await evalJs(`(() => { pipeJson = window.__winFail; delete window.__winFail;
                         return true; })()`);

  // **「有没有新内容」不许跟着别的请求一起失效。** 这一跳先读运行状态、再读这个
  // 数,而读状态失败时整轮早退过 —— 于是 `/status` 一直失败时,上一次那个 0 无限
  // 期有效:页面始终写「没有新内容」、按钮始终不在,而素材是从外面(同步中心、
  // CLI、定时)进来的,跟读不读得到"这一轮跑到哪了"没有关系(审查实测:只让
  // `/status` 失败,连驱动两轮,页面一个字都没变)。
  await evalJs(`(() => { window.__statFail = pipeJson;
    pipeJson = async (path, ...rest) => {
      if (String(path) === '/api/daily/status') throw new Error('摆局:状态读不到');
      const r = await window.__statFail(path, ...rest);
      if (String(path) === '/api/daily/window')
        r.pending = { items: 4, late: 0, total: 4, gain: 4 };
      return r; };
    return true; })()`);
  await evalJs(`refreshDailyStatus()`);
  s = await snap();
  check("状态读不到的那一跳,新内容这件事照样刷新",
        !s.genOff && s.line === "有 4 条新内容", JSON.stringify(s));
  // 反过来钉一边:两个都读不到时**不许编一个数出来**,说读不出来、按钮留着。
  await evalJs(`(() => { pipeJson = async (path, ...rest) => {
      if (String(path) === '/api/daily/status'
          || String(path) === '/api/daily/window') throw new Error('摆局:两头都断');
      return window.__statFail(path, ...rest); };
    return true; })()`);
  await evalJs(`refreshDailyStatus()`);
  s = await snap();
  check("两头都读不到时说读不出来,不编一个数",
        !s.genOff && s.line === "新内容读不出来", JSON.stringify(s));
  await evalJs(`(() => { pipeJson = window.__statFail; delete window.__statFail;
                         clearTimeout(dailyTimer); return true; })()`);
  await evalJs(`loadDailyWindow()`);
  s = await snap();
  check("状态那一侧恢复之后,这个数也回到真实值",
        s.genOff && s.line === "没有新内容", JSON.stringify(s));

  // **账本的章和日报文件是两头东西。** 文件在 Obsidian 里被移走、或者手工删掉
  // (没走界面那条删除)时,章还盖着、`gain` 照样是 0 —— 这时再把按钮藏掉,等于
  // 把"重建今天那一份"的唯一入口也一起藏了。
  //
  // **这件事只认后端那一趟带回来的 `has_report`**(和 `gain` 同一次读):页面手里
  // 那份日报列表缓存只在列表重读时更新,而空闲轮询只刷新 window —— 下面第一档
  // 摆的正是这个形状,**缓存还说"今天那份在"**,照它判的话按钮一直藏着,只有整页
  // 刷新才回来(审查实测)。
  s = JSON.parse(await evalJs(render(`{ has_report: false }`)));
  check("今天那份在外面被移走:列表缓存还说在,也照显示按钮(名字退回「生成日报」)",
        !s.genOff && s.line === "没有新内容" && s.label === "生成日报", JSON.stringify(s));
  // 反过来钉一边:**核实过"文件就在"的那一档照旧藏**(缓存空不空都一样)——
  // 不然"0 条就藏"这件事整个失效,按钮再也藏不掉。
  await evalJs(`(() => { window.__reports = dailyReports; dailyReports = [];
                         renderDailyGenButton(); return true; })()`);
  s = await snap();
  check("后端核实过那一份就在:列表缓存空着也照旧藏按钮", s.genOff, JSON.stringify(s));
  // 旧后端不给 `has_report`:列表缓存说"有"只是**猜**,猜不算核实 —— 不许据此
  // 藏掉按钮(名字照缓存说「重新生成日报」,说错顶多多问一句确认)。
  await evalJs(`(() => { dailyReports = window.__reports; delete window.__reports;
                         window.__has = dailyWindow.has_report; delete dailyWindow.has_report;
                         renderDailyGenButton(); return true; })()`);
  s = await snap();
  check("旧后端不给这件事时,列表缓存那一份只是猜,不许据此藏按钮",
        !s.genOff && s.label === "重新生成日报", JSON.stringify(s));
  await evalJs(`(() => { dailyWindow.has_report = window.__has; delete window.__has;
                         renderDailyGenButton(); return true; })()`);

  // **版本答不上来也是「不知道」。** 账本里没记下今天那份带走的是哪一版时(这一列
  // 是后加的、`seed_locked` 重算出来的那批也没有),后端把整个 `gain` 报成 null ——
  // 页面要说「读不出来」并把按钮留着,不许当成 0 藏掉(旧判据拿 `indexed_at` 推,
  // 而真正换掉素材的提取那条路压根不碰它,审查实测 12 组全错)。
  s = JSON.parse(await evalJs(render(
    `{ pending: { items: 0, late: 0, total: 0, gain: null } }`)));
  check("账本没记下带走的是哪一版:说读不出来,按钮留着",
        !s.genOff && s.line === "新内容读不出来" && !s.lineOff, JSON.stringify(s));

  // **一句过期的"今天还没有日报"不许继续当"可以不问一声就覆盖"的凭据。**
  // 形状:今天本来没有日报(那一趟读到 `has_report=false`)→ 生成**成功**了(列表
  // 里已经是新出的那一份)→ 紧接着这个接口开始读失败。页面要是还攥着生成**之前**
  // 那句"没有",再点生成就**不弹覆盖确认**,静悄悄盖掉刚出的日报(审查实测:两颗
  // 按钮都发出了 POST /api/daily/run)。**两颗按钮一起验** —— 它们共用
  // `runDaily(sync)`,只验一颗等于没验那条共用判据。
  //
  // POST 先拦下来:真发出去会在沙箱里再生成一次,把后面几组的摆盘冲掉;拦住之后
  // 「一次都没发」本身也是判据(光看"没见到确认框"证不了它真的发了)。
  await evalJs(`(() => { window.__runs = []; window.__postReal = pipePost;
    pipePost = async (path, body) => {
      if (String(path) === '/api/daily/run') { window.__runs.push(body); throw new Error('摆局:拦下'); }
      return window.__postReal(path, body); };
    window.__picked = [...document.getElementById('daily-channels')
      .querySelectorAll('input')].map(i => i.checked);
    const one = document.getElementById('daily-channels').querySelector('input');
    if (one) one.checked = true;              // 同步那颗先过"勾了没有"那道闸
    dailyWindow.has_report = false;           // 生成之前读到的那一句
    renderDailyGenButton(); return true; })()`);
  s = await snap();
  check("摆局:此刻按的还是生成之前那句「今天还没有日报」",
        s.label === "生成日报", JSON.stringify(s));
  await evalJs(`(() => { window.__winFail = pipeJson;
    pipeJson = async (path, ...rest) => {
      if (String(path) === '/api/daily/window') throw new Error('摆局:断连');
      return window.__winFail(path, ...rest); };
    return true; })()`);
  await evalJs(`refreshDailyPending()`);
  s = await snap();
  check("读失败之后那句过期的「没有」跟着作废(列表里明明已经有今天那一份)",
        s.label === "重新生成日报"
        && await evalJs(`dailyReports.some(r => r.date === dailyTargetDate())`),
        JSON.stringify(s));
  // 并成一颗之后,**两种意图都要照旧先问一声会不会覆盖** —— 勾不勾那个勾只决定
  // 要不要先同步,不改变「这一天已经有一份日报」这件事。
  for (const [checked, name] of [['false', '不勾先同步'], ['true', '勾了先同步']]) {
    await evalJs(`(() => { document.getElementById('daily-presync').checked = ${checked};
                           renderDailyButtons(); return true; })()`);
    await evalJs(`document.getElementById('daily-gen').click()`);
    const asked = await waitFor(`!!document.querySelector('.confirm-mask [data-act=ok]')`, 5000);
    check(`${name}:照旧先问一声会不会覆盖今天那一份`, asked);
    if (asked) await evalJs(
      `document.querySelector('.confirm-mask [data-act=cancel]').click()`);
  }
  // **确认之后,批准要跟着请求下去**:点击这一刻的浏览器日期不是写文件时的目标
  // 身份(勾了先同步的那条路要先跑几小时),所以"问过了"必须带着它问的是哪一天、
  // 哪一版,由后端在落盘之前核对(审查 R4)。只验"弹了确认框"的实现照样全绿,而
  // 跨零点那一档照旧盖掉没人问过的那一份。
  await evalJs(`document.getElementById('daily-gen').click()`);
  if (await waitFor(`!!document.querySelector('.confirm-mask [data-act=ok]')`, 5000)) {
    await evalJs(`document.querySelector('.confirm-mask [data-act=ok]').click()`);
    await waitFor(`(window.__runs || []).length === 1`, 5000);
  }
  const sent = JSON.parse(await evalJs(`JSON.stringify(window.__runs[0] || null)`));
  check("确认之后那份批准跟着请求下去:哪一天 + 哪一版",
        !!sent && sent.overwrite_day === (await evalJs(`dailyTargetDate()`))
        && typeof sent.overwrite_version === 'string'
        && sent.overwrite_version !== '', JSON.stringify(sent));
  await evalJs(`(() => { window.__runs.length = 0; return true; })()`);
  await evalJs(`(() => { document.getElementById('daily-presync').checked = false;
                         renderDailyButtons(); return true; })()`);
  check("问之前一次都没真发出去",
        (await evalJs(`JSON.stringify(window.__runs)`)) === "[]",
        await evalJs(`JSON.stringify(window.__runs)`));
  await evalJs(`(() => { pipeJson = window.__winFail; pipePost = window.__postReal;
    delete window.__winFail; delete window.__postReal; delete window.__runs;
    [...document.getElementById('daily-channels').querySelectorAll('input')]
      .forEach((i, n) => { i.checked = window.__picked[n]; });
    delete window.__picked; return true; })()`);
  await evalJs(`loadDailyWindow()`);
  s = await snap();
  check("摆盘收干净:读回来之后又回到「重新生成」那一档",
        s.genOff && s.line === "没有新内容", JSON.stringify(s));

  check("后端把三个数一起给了出来",
        await evalJs(`(async () => {
          const w = await (await fetch('/api/daily/window')).json();
          return w.fetch && typeof w.fetch.n === 'number'
                 && typeof w.fetch.added === 'number'
                 && w.pending && typeof w.pending.total === 'number'
                 && typeof w.pending.gain === 'number'
                 && typeof w.has_report === 'boolean' && typeof w.day === 'string'; })()`));

  // ── tooltip 里的抓取事实 ────────────────────────────────────────────────
  // 上面那几档摆的是**页面状态**,它们证明不了"事实能从外面进来"—— 而这正是
  // 第 2 轮审查实测到的那一条:同步在另一个窗口跑完,面板一直停在「今天还没有
  // 抓取」,而同一个页面请求的 API 已经返回 n=1、added=3。原因是接收层只接了
  // `pending`、把新返回的 `fetch` 丢了,改过覆盖区间时更是两处刷新入口一起提前
  // 返回。所以这一组伪造的是**后端回包**,走页面自己的刷新入口。
  const winStub = (val, pending = 'r.pending') => `(() => {
    window.__winReal = window.__winReal || pipeJson;
    pipeJson = async (path, ...rest) => {
      const r = await window.__winReal(path, ...rest);
      if (String(path) === '/api/daily/window') { r.fetch = ${val}; r.pending = ${pending}; }
      return r;
    };
    return true; })()`;
  const poll = async (val, pending) => {
    await evalJs(winStub(val, pending));
    await evalJs(`refreshDailyPending()`);
    return snap();
  };
  s = await poll(`{ n: 1, added: 3, at: '09:12' }`);
  check("同步在外面跑完,空闲那一跳就把抓取事实接下来",
        /今天第 1 次抓取（09:12）往库里新加了 3 篇/.test(s.tip), s.tip);
  // **「至少」和「确定」是两句话。** 勾了几个来源而其中一个炸在半路、采集器给不出
  // 清单时,已知的那几条只是下界(后端给 partial)。写成确定值的后果是用户照着一个
  // 偏小的数决定不重新生成日报,而真落进库的比它多。
  s = await poll(`{ n: 2, added: 2, partial: true, at: '10:05' }`);
  check("有一段成果说不出来时报「至少」,不冒充确定值",
        /已知新加了 2 篇/.test(s.tip) && /可能更多/.test(s.tip)
        && !/往库里新加了 2 篇/.test(s.tip), s.tip);
  // 反过来钉一边:不是 partial 的时候照旧是确定的说法。
  s = await poll(`{ n: 2, added: 4, partial: false, at: '10:05' }`);
  check("没有缺口时照旧报确定值,不无故写「至少」",
        /往库里新加了 4 篇/.test(s.tip) && !/可能更多|已知/.test(s.tip), s.tip);
  // 后端答「不可确认」(账本读不出来、或者有成果没落下去)。**不许说成「今天还
  // 没有抓取」** —— 那是把"不知道"说成"没有",而一次写失败之后它会一直这么说。
  s = await poll(`null`);
  check("抓取账本查不出来时说「不可确认」,不谎称今天没抓过",
        /不可确认/.test(s.tip) && !/还没有同步过/.test(s.tip), s.tip);
  // **什么都没抓到的那一次也是一次抓取**:`0 篇` 和「今天还没有同步过」是两句话。
  s = await poll(`{ n: 1, added: 0, at: '08:00' }`);
  check("抓过一次但一篇都没多,和「今天还没抓过」是两句话",
        /第 1 次抓取/.test(s.tip) && /新加了 0 篇/.test(s.tip)
        && !/还没有同步过/.test(s.tip), s.tip);
  // ── 那个数由什么构成 ──────────────────────────────────────────────────────
  // 抓取事实报的是「这次同步之后日报能多拿到多少」,而它和「远端这次新发现多少」在
  // 补导场景下必然不等:正文抓取当天失败、隔两天补上才导出的那几篇是前几天抓到
  // 的。远端今天一条新内容都没发而它写「+3 条」时,用户得能看出这 3 条值不值
  // 一次模型钱 —— 所以要把差额拆开说。
  s = await poll(`{ n: 1, added: 3, fresh: 0, carried: 3, unsure: 0, at: '09:30' }`);
  check("全是补导进来的时候,说清这 3 篇不是这一次抓到的",
        /新加了 3 篇/.test(s.tip) && /3 篇是之前抓到、这一次才补导进来的/.test(s.tip)
        && !/新抓到/.test(s.tip), s.tip);
  // 反过来钉一边:正常情形(全是这一次新抓到的)**不加这句** —— 把同一个数说
  // 两遍只是噪音。
  s = await poll(`{ n: 1, added: 3, fresh: 3, carried: 0, unsure: 0, at: '09:30' }`);
  check("全是这一次新抓到的时候不多说一句",
        /新加了 3 篇/.test(s.tip) && !/其中/.test(s.tip), s.tip);
  s = await poll(`{ n: 2, added: 5, fresh: 2, carried: 3, unsure: 0, at: '09:40' }`);
  check("两档都有时各报各的,加起来就是前面那个数",
        /新加了 5 篇/.test(s.tip) && /2 篇是这一次新抓到的/.test(s.tip)
        && /3 篇是之前抓到、这一次才补导进来的/.test(s.tip), s.tip);
  // **「说不清」自己一句话,不许并进「补导」**:抓取身份是空串时我们只知道
  // 「没记过」(存量内容、手工导入的链接),不知道「不是这一次」。
  s = await poll(`{ n: 2, added: 4, fresh: 1, carried: 0, unsure: 3, at: '09:50' }`);
  check("说不清是哪一次抓到的那几篇单独说,不算成补导",
        /3 篇说不清是哪一次抓到的/.test(s.tip) && !/补导/.test(s.tip), s.tip);
  // 后端答不出这三个字段时整句不说。
  s = await poll(`{ n: 2, added: 4, at: '09:55' }`);
  check("后端答不出分解时整句不说",
        /新加了 4 篇/.test(s.tip) && !/其中/.test(s.tip)
        && !/NaN|undefined/.test(s.tip), s.tip);
  // **半份答案更要整句不说。** 上面那一档(三个全缺)被「有没有不是本次抓到的」
  // 那道闸顺手挡住了,所以它证不了按形状判这件事;真正只有形状那道闸挡得住的是
  // 这一档 —— 答了 carried 没答 fresh,写出去就是「新加了 4 篇。其中 3 篇是之前
  // 抓到的」,一个加不起来的分解,而第 4 篇怎么来的页面上一个字都没有。
  s = await poll(`{ n: 2, added: 4, carried: 3, at: '09:58' }`);
  check("只答了一半的分解整句不说,不写出一个加不起来的拆分",
        /新加了 4 篇/.test(s.tip) && !/其中/.test(s.tip)
        && !/NaN|undefined/.test(s.tip), s.tip);

  // 改过覆盖区间:草稿只该挡住"改写用户填的那两个时刻",挡不到抓取事实 ——
  // 抓取是从外面进来的,跟用户在这里填了什么没有关系。**跟着藏掉的是按钮边上
  // 那句「有 N 条新内容」和 tooltip 里的队列**:后端算的是默认区间那一段。
  await evalJs(`(() => { document.getElementById('daily-since').value = '2026-01-02T03:04';
                         document.getElementById('daily-until').value = '2026-01-05T06:07';
                         markDailyWindow(); return true; })()`);
  s = await poll(`{ n: 2, added: 1, at: '10:20' }`,
                 `{ items: 5, late: 0, total: 5, gain: 5 }`);
  check("改过覆盖区间,空闲那一跳照样接,而两个时刻还是用户填的",
        /往库里新加了 1 篇/.test(s.tip) && s.since === "2026-01-02T03:04"
        && s.until === "2026-01-05T06:07", JSON.stringify(s));
  check("自定义区间不报新内容条数,按钮照显示",
        s.lineOff && !s.genOff && !/队列里有/.test(s.tip), JSON.stringify(s));
  await evalJs(winStub(`{ n: 3, added: 5, at: '11:30' }`));
  await evalJs(`loadDailyWindow()`);
  s = await snap();
  check("整块重载时也一样:只挡输入框,不挡抓取事实",
        /往库里新加了 5 篇/.test(s.tip) && s.since === "2026-01-02T03:04",
        JSON.stringify(s));
  // **在途的那趟回包不许把已经显示出来的新事实退回旧事实**(审查 N2):空闲轮询
  // 和整块重载共用同一个读取口,慢的那趟晚到时无条件覆盖,数字会从「第 2 次」
  // 退回「第 1 次」。
  //
  // 两件事让这一组是**确定的**,不靠网络抖动碰运气:
  // ① 桩按回包序号攥人 —— 第 1 趟被攥在手里,第 2 趟照常落地,然后手动放行第 1 趟;
  // ② **先把页面自己的空闲轮询停掉**。它也走同一个读取口,不停的话被攥住的那个
  //    很可能是轮询发的那一趟,而我们等的 promise 永远不会 resolve(实测:探针
  //    在这里挂住十几分钟,一行输出都没有)。
  const gateStub = `(() => {
    window.__winReal = window.__winReal || pipeJson;
    window.__n = 0; window.__holdAt = 0; window.__hold = null;
    pipeJson = async (path, ...rest) => {
      const r = await window.__winReal(path, ...rest);
      if (String(path) !== '/api/daily/window') return r;
      const n = ++window.__n;
      r.fetch = { n, added: n, at: '09:0' + n };
      r.since = '2026-09-01T00:00'; r.until = '2026-09-02T00:00';
      if (n === window.__holdAt) await new Promise(res => { window.__hold = res; });
      return r;
    };
    window.__pollReal = refreshDailyStatus;
    refreshDailyStatus = async () => {};      // 这一组期间不许有第三趟插进来
    clearTimeout(dailyTimer);
    return true; })()`;
  // 攥住的那一趟最多等 3 秒:桩要是没攥到人,这里要红,不许把探针挂死。
  const settle = (name) => `(async () => {
    await Promise.race([window.${name}, new Promise(r => setTimeout(r, 3000))]);
    return true; })()`;
  // **等它真的停在半路再往下走**:发请求到"停住"之间隔着一次真实往返,拍一个
  // 固定时刻去问的话,机器稍慢就什么都没攥到(实测探针在这里当场抛错)。
  const waitHold = `(async () => {
    for (let i = 0; i < 60 && !window.__hold; i++) await new Promise(r => setTimeout(r, 50));
    return !!window.__hold; })()`;
  await evalJs(gateStub);
  await evalJs(`(() => { window.__holdAt = window.__n + 1;
                         window.__p1 = refreshDailyPending(); return true; })()`);
  check("攥住的那一趟确实停在半路", await evalJs(waitHold));
  await evalJs(`refreshDailyPending()`);          // 第 2 趟照常落地
  await evalJs(`(() => { window.__hold(); return true; })()`);   // 放行第 1 趟
  await evalJs(settle('__p1'));
  s = await snap();
  check("在途的旧回包晚到,不许把已经显示的新事实退回去",
        /今天第 2 次抓取/.test(s.tip) && !/第 1 次/.test(s.tip), s.tip);
  // 同一道门的草稿那一侧:**等回包的那段时间里用户刚开始填**,旧回包落地时仍按
  // 发请求时算出的"可以填"把他填的两个时刻盖掉。判据要在落地前再认一次。
  await evalJs(`(() => { window.__hold = null; dailyWindowDirty = false;
                         window.__holdAt = window.__n + 1;
                         window.__p2 = loadDailyWindow(); return true; })()`);
  check("草稿那一趟也确实停在半路", await evalJs(waitHold));
  await evalJs(`(() => { document.getElementById('daily-since').value = '2026-03-04T05:06';
                         document.getElementById('daily-until').value = '2026-03-07T08:09';
                         markDailyWindow(); return true; })()`);
  await evalJs(`(() => { window.__hold(); return true; })()`);
  await evalJs(settle('__p2'));
  s = await snap();
  check("等回包期间刚填的覆盖时间,旧回包落地也不许盖掉",
        s.since === "2026-03-04T05:06" && s.until === "2026-03-07T08:09",
        JSON.stringify(s));
  // **「两个时刻还没填过」这个职责不许随整趟作废**(第 2 轮审查 N4 实测):刷新
  // 日报页后,初始化那趟(要填表)被只画按钮的空闲轮询顶掉,于是两个时刻框永久
  // 空着、区间那行字停在「正在算覆盖区间…」—— 连续多轮轮询都不自愈,要点一次
  // 「接上一份日报」才回来。职责挂在页面上,由真正落地的那一趟接手。
  await evalJs(`(() => { window.__hold = null; dailyWindowDirty = false;
                         dailyWindowFillDue = false;
                         document.getElementById('daily-since').value = '';
                         document.getElementById('daily-until').value = '';
                         document.getElementById('daily-window-label').textContent
                           = '正在算覆盖区间…';
                         window.__holdAt = window.__n + 1;
                         window.__p3 = loadDailyWindow(); return true; })()`);
  check("初始化那一趟确实停在半路", await evalJs(waitHold));
  await evalJs(`refreshDailyPending()`);          // 只画按钮的那一趟先落地、顶掉它
  await evalJs(`(() => { window.__hold(); return true; })()`);
  await evalJs(settle('__p3'));
  s = await snap();
  const label = await evalJs(
    `document.getElementById('daily-window-label').textContent`);
  check("初始化被空闲轮询顶掉,填表的职责由落地的那一趟接手",
        s.since === "2026-09-01T00:00" && s.until === "2026-09-02T00:00"
        && !/正在算/.test(label), JSON.stringify(s) + " / " + label);
  await evalJs(`(() => { if (window.__pollReal) { refreshDailyStatus = window.__pollReal;
                         delete window.__pollReal; } return true; })()`);
  // 收尾:恢复真回包、清掉草稿,后面几组看到的是真实状态
  await evalJs(`(() => { if (window.__winReal) { pipeJson = window.__winReal;
                         delete window.__winReal; } return true; })()`);
  await evalJs(`resetDailyWindow()`);
  s = await snap();
  check("摆盘收干净:回到真实的事实和默认区间",
        s.line === "没有新内容" && s.genOff && /今天还没有同步过/.test(s.tip)
        && !(await evalJs(`dailyWindowDirty`)), JSON.stringify(s));
}
