// 生成按钮上那颗药丸:**今天第 N 次抓取抓了多少**,以及它的几种说法。
//
// 这一组钉的是「别的数不许冒充它」和「不知道不许说成没有」:
// ① 取材结果、队列总长都不许写到按钮上(三个数答的是三个问题);
// ② 后端答 null(账本读不出来)说「读不出来」,不说「今天还没有抓取」;
// ③ 后端答 partial(某个来源的成果说不出来)说「至少 +N 条」,不冒充确定值;
// ④ 抓取事实是从**外面**进来的,页面自己的空闲轮询和整块重载都得接得住,而
//    用户填过的那两个时刻一个字都不许被覆盖。
//
// **从 tests/daily_probe.mjs 拆出来只因为行数**(那边到了 700 行上限),摆法和
// 判据原样搬过来、共用同一次页面会话。后端那一侧的不变量在
// tests/test_fetch_rounds.py 与 tests/test_fetch_receipt.py。
export async function checkFetchCountChip({ evalJs, check }) {
  // 沙箱从没跑过管道,所以此刻确实"今天还没有抓取" —— 这是一句事实,不是零。
  check("今天没抓过时按钮上这么说",
        (await evalJs(`document.getElementById('daily-gen-count').textContent`)) === "今天还没有抓取"
        && !(await evalJs(`document.getElementById('daily-gen-count').hidden`)),
        await evalJs(`document.getElementById('daily-gen').textContent`));
  check("没抓到新内容时退成灰字、不占品牌色",
        await evalJs(`document.getElementById('daily-gen-count').classList.contains('is-none')`));
  // 抓过之后那颗药丸长什么样。摆盘自检:探针跑到这里时沙箱一轮管道都没跑过,
  // 真实状态必然是"今天还没有抓取",所以这一档只能把 fetch 摆进去再渲染一次。
  check("抓过之后按钮上报出第几次和多少条", /^第 2 次抓取 · \+7 条$/.test(await evalJs(
    `(() => { const keep = dailyWindow.fetch;
              dailyWindow.fetch = { n: 2, added: 7, at: '15:30' };
              renderDailyGenButton();
              const t = document.getElementById('daily-gen-count').textContent;
              dailyWindow.fetch = keep; renderDailyGenButton(); return t; })()`)));
  // **按钮上报的是抓取事实,不是取材结果,也不是队列总长。** 三个数答的是三个
  // 问题:抓回来 9 条、这一次只选得进 3 条、队列里还积着 1097 条。把后两个写在
  // 按钮上,用户就看不出"这次同步到底抓回来了什么"——而他正是靠这个决定要不要
  // 再花一次模型钱(生产库实测队列 1097、点下去只多出 11)。
  check("队列再大也不写到按钮上", await evalJs(
    `(() => { const kf = dailyWindow.fetch, kp = dailyWindow.pending;
              dailyWindow.fetch = { n: 1, added: 9, at: '09:00' };
              dailyWindow.pending = { items: 79, late: 1018, total: 1097 };
              renderDailyGenButton();
              const t = document.getElementById('daily-gen-count').textContent;
              const tip = document.getElementById('daily-gen').title;
              dailyWindow.fetch = kf; dailyWindow.pending = kp; renderDailyGenButton();
              return t === '第 1 次抓取 · +9 条' && !/1097/.test(t)
                     && /1097/.test(tip) && !/陆续带走/.test(tip); })()`));
  // 后端答「不可确认」(账本读不出来、或者有成果没落下去)。**不许说成「今天还
  // 没有抓取」** —— 那是把"不知道"说成"没有",而一次写失败之后它会一直这么说。
  check("查不出来时说「读不出来」,不谎称今天没抓过", await evalJs(
    `(() => { const keep = dailyWindow.fetch;
              dailyWindow.fetch = null; renderDailyGenButton();
              const t = document.getElementById('daily-gen-count').textContent;
              const tip = document.getElementById('daily-gen').title;
              dailyWindow.fetch = keep; renderDailyGenButton();
              return t === '抓取次数读不出来' && !/还没有抓取/.test(tip)
                     && !document.getElementById('daily-gen-count').hidden; })()`));
  check("后端把抓取事实和队列数一起给了出来",
        await evalJs(`(async () => {
          const w = await (await fetch('/api/daily/window')).json();
          return w.fetch && typeof w.fetch.n === 'number'
                 && typeof w.fetch.added === 'number'
                 && w.pending && typeof w.pending.total === 'number'; })()`));
  // 上面那几档摆的是**页面状态**,它们证明不了"抓取事实能从外面进来"—— 而这正是
  // 第 2 轮审查实测到的那一条:同步在另一个窗口跑完,药丸一直停在「今天还没有
  // 抓取」,而同一个页面请求的 API 已经返回 n=1、added=3。原因是接收层只接了
  // `pending`、把新返回的 `fetch` 丢了,改过覆盖区间时更是两处刷新入口一起提前
  // 返回。所以这一组伪造的是**后端回包**,走页面自己的刷新入口。
  const winStub = (val) => `(() => {
    window.__winReal = window.__winReal || pipeJson;
    pipeJson = async (path, ...rest) => {
      const r = await window.__winReal(path, ...rest);
      if (String(path) === '/api/daily/window') r.fetch = ${val};
      return r;
    };
    return true; })()`;
  const chipNow = `(() => JSON.stringify({
    chip: document.getElementById('daily-gen-count').textContent,
    since: document.getElementById('daily-since').value,
    until: document.getElementById('daily-until').value }))()`;
  await evalJs(winStub(`{ n: 1, added: 3, at: '09:12' }`));
  await evalJs(`refreshDailyPending()`);
  check("同步在外面跑完,空闲那一跳就把抓取事实接下来",
        /^第 1 次抓取 · \+3 条$/.test(JSON.parse(await evalJs(chipNow)).chip),
        await evalJs(chipNow));
  // **「至少」和「确定」是两句话。** 勾了几个来源而其中一个炸在半路、采集器给不出
  // 清单时,已知的那几条只是下界(后端给 partial)。写成确定值的后果是用户照着一个
  // 偏小的数决定不重新生成日报,而真落进库的比它多。
  await evalJs(winStub(`{ n: 2, added: 2, partial: true, at: '10:05' }`));
  await evalJs(`refreshDailyPending()`);
  check("有一段成果说不出来时报「至少」,不冒充确定值", await evalJs(
    `(() => { const t = document.getElementById('daily-gen-count').textContent;
              const tip = document.getElementById('daily-gen').title;
              return t === '第 2 次抓取 · 至少 +2 条' && !/· \\+2 条/.test(t)
                     && /可能更多/.test(tip); })()`), await evalJs(chipNow));
  // 一条都说不出来时**不许写「0 条」** —— 那和"确实一篇都没抓到"是两回事。
  await evalJs(winStub(`{ n: 2, added: 0, partial: true, at: '10:05' }`));
  await evalJs(`refreshDailyPending()`);
  check("一条都说不出来时说「条数读不出来」,不说 0 条", await evalJs(
    `(() => { const t = document.getElementById('daily-gen-count').textContent;
              return t === '第 2 次抓取 · 条数读不出来'
                     && !/0 条/.test(t) && !/还没有抓取/.test(t); })()`),
        await evalJs(chipNow));
  // 反过来钉一边:不是 partial 的时候照旧是确定的说法。
  await evalJs(winStub(`{ n: 2, added: 4, partial: false, at: '10:05' }`));
  await evalJs(`refreshDailyPending()`);
  check("没有缺口时照旧报确定值,不无故写「至少」", await evalJs(
    `(() => { const t = document.getElementById('daily-gen-count').textContent;
              return t === '第 2 次抓取 · +4 条' && !/至少/.test(t); })()`),
        await evalJs(chipNow));
  // 改过覆盖区间:草稿只该挡住"改写用户填的那两个时刻",挡不到抓取事实 ——
  // 抓取是从外面进来的,跟用户在这里填了什么没有关系。
  await evalJs(`(() => { document.getElementById('daily-since').value = '2026-01-02T03:04';
                         document.getElementById('daily-until').value = '2026-01-05T06:07';
                         markDailyWindow(); return true; })()`);
  await evalJs(winStub(`{ n: 2, added: 1, at: '10:20' }`));
  await evalJs(`refreshDailyPending()`);
  const dirtyPoll = await evalJs(chipNow);
  check("改过覆盖区间,空闲那一跳照样接,而两个时刻还是用户填的",
        /"chip":"第 2 次抓取 · \+1 条"/.test(dirtyPoll)
        && /"since":"2026-01-02T03:04"/.test(dirtyPoll)
        && /"until":"2026-01-05T06:07"/.test(dirtyPoll), dirtyPoll);
  await evalJs(winStub(`{ n: 3, added: 5, at: '11:30' }`));
  await evalJs(`loadDailyWindow()`);
  const dirtyLoad = await evalJs(chipNow);
  check("整块重载时也一样:只挡输入框,不挡抓取事实",
        /"chip":"第 3 次抓取 · \+5 条"/.test(dirtyLoad)
        && /"since":"2026-01-02T03:04"/.test(dirtyLoad), dirtyLoad);
  // **在途的那趟回包不许把已经显示出来的新事实退回旧事实**(审查 N2):空闲轮询
  // 和整块重载共用同一个读取口,慢的那趟晚到时无条件覆盖,药丸会从「第 2 次」
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
  check("在途的旧回包晚到,不许把已经显示的新事实退回去",
        /^第 2 次抓取 · \+2 条$/.test(JSON.parse(await evalJs(chipNow)).chip),
        await evalJs(chipNow));
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
  const held = await evalJs(chipNow);
  check("等回包期间刚填的覆盖时间,旧回包落地也不许盖掉",
        /"since":"2026-03-04T05:06"/.test(held)
        && /"until":"2026-03-07T08:09"/.test(held), held);
  await evalJs(`(() => { if (window.__pollReal) { refreshDailyStatus = window.__pollReal;
                         delete window.__pollReal; } return true; })()`);
  // 收尾:恢复真回包、清掉草稿,后面几组看到的是真实状态
  await evalJs(`(() => { if (window.__winReal) { pipeJson = window.__winReal;
                         delete window.__winReal; } return true; })()`);
  await evalJs(`resetDailyWindow()`);
  check("摆盘收干净:回到真实的抓取事实和默认区间",
        (await evalJs(`document.getElementById('daily-gen-count').textContent`))
          === "今天还没有抓取" && !(await evalJs(`dailyWindowDirty`)),
        await evalJs(chipNow));
}
