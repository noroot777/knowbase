// 生成按钮上那颗药丸:**今天第 N 次抓取抓了多少**,以及它的几种说法。
//
// 这一组钉的是「别的数不许冒充它」和「不知道不许说成没有」:
// ① 取材结果、队列总长都不许写到按钮上(三个数答的是三个问题);
// ② 后端答 null(账本读不出来)说「读不出来」,不说「今天还没有抓取」;
// ③ 后端答 partial(某个来源的成果说不出来)说「至少 +N 条」,不冒充确定值;
// ④ 抓取事实是从**外面**进来的,页面自己的空闲轮询和整块重载都得接得住,而
//    用户填过的那两个时刻一个字都不许被覆盖;
// ⑤ 那个数由什么构成要拆得开:这一次新抓到的 / 之前抓到这一次才补导的 /
//    说不清的,三档各一句话,而「说不清」不许并进「补导」。
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
  // ── 那个数由什么构成 ──────────────────────────────────────────────────────
  // 药丸报的是「这次同步之后日报能多拿到多少」,而它和「远端这次新发现多少」在
  // 补导场景下必然不等:正文抓取当天失败、隔两天补上才导出的那几篇是前几天抓到
  // 的。远端今天一条新内容都没发而按钮写「+3 条」时,用户得能看出这 3 条值不值
  // 一次模型钱 —— 所以 tooltip 里要把差额拆开说。
  const tip = `document.getElementById('daily-gen').title`;
  await evalJs(winStub(
    `{ n: 1, added: 3, fresh: 0, carried: 3, unsure: 0, at: '09:30' }`));
  await evalJs(`refreshDailyPending()`);
  check("全是补导进来的时候,tooltip 说清这 3 篇不是这一次抓到的", await evalJs(
    `(() => { const t = ${tip};
              return /新加了 3 篇/.test(t) && /3 篇是之前抓到、这一次才补导进来的/.test(t)
                     && !/新抓到/.test(t); })()`), await evalJs(tip));
  // 反过来钉一边:正常情形(全是这一次新抓到的)**不加这句** —— 把同一个数说
  // 两遍只是噪音。
  await evalJs(winStub(
    `{ n: 1, added: 3, fresh: 3, carried: 0, unsure: 0, at: '09:30' }`));
  await evalJs(`refreshDailyPending()`);
  check("全是这一次新抓到的时候不多说一句", await evalJs(
    `(() => { const t = ${tip};
              return /新加了 3 篇/.test(t) && !/其中/.test(t); })()`), await evalJs(tip));
  await evalJs(winStub(
    `{ n: 2, added: 5, fresh: 2, carried: 3, unsure: 0, at: '09:40' }`));
  await evalJs(`refreshDailyPending()`);
  check("两档都有时各报各的,加起来就是药丸上那个数", await evalJs(
    `(() => { const t = ${tip};
              return /新加了 5 篇/.test(t) && /2 篇是这一次新抓到的/.test(t)
                     && /3 篇是之前抓到、这一次才补导进来的/.test(t); })()`),
        await evalJs(tip));
  // **「说不清」自己一句话,不许并进「补导」**:抓取身份是空串时我们只知道
  // 「没记过」(存量内容、手工导入的链接),不知道「不是这一次」。
  await evalJs(winStub(
    `{ n: 2, added: 4, fresh: 1, carried: 0, unsure: 3, at: '09:50' }`));
  await evalJs(`refreshDailyPending()`);
  check("说不清是哪一次抓到的那几篇单独说,不算成补导", await evalJs(
    `(() => { const t = ${tip};
              return /3 篇说不清是哪一次抓到的/.test(t)
                     && !/补导/.test(t); })()`), await evalJs(tip));
  // 后端答不出这三个字段(`kb/static/` 每次请求从磁盘读、而 server.py 要重启才
  // 生效,所以「新前端 + 旧后端」天天走到)时整句不说。
  await evalJs(winStub(`{ n: 2, added: 4, at: '09:55' }`));
  await evalJs(`refreshDailyPending()`);
  check("后端答不出分解时整句不说", await evalJs(
    `(() => { const t = ${tip};
              return /新加了 4 篇/.test(t) && !/其中/.test(t)
                     && !/NaN|undefined/.test(t); })()`), await evalJs(tip));
  // **半份答案更要整句不说。** 上面那一档(三个全缺)被「有没有不是本次抓到的」
  // 那道闸顺手挡住了,所以它证不了按形状判这件事;真正只有形状那道闸挡得住的是
  // 这一档 —— 答了 carried 没答 fresh,写出去就是「新加了 4 篇。其中 3 篇是之前
  // 抓到的」,一个加不起来的分解,而第 4 篇怎么来的页面上一个字都没有。
  await evalJs(winStub(`{ n: 2, added: 4, carried: 3, at: '09:58' }`));
  await evalJs(`refreshDailyPending()`);
  check("只答了一半的分解整句不说,不写出一个加不起来的拆分", await evalJs(
    `(() => { const t = ${tip};
              return /新加了 4 篇/.test(t) && !/其中/.test(t)
                     && !/NaN|undefined/.test(t); })()`), await evalJs(tip));

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
  // 收尾:恢复真回包、清掉草稿,后面几组看到的是真实状态
  await evalJs(`(() => { if (window.__winReal) { pipeJson = window.__winReal;
                         delete window.__winReal; } return true; })()`);
  await evalJs(`resetDailyWindow()`);
  check("摆盘收干净:回到真实的抓取事实和默认区间",
        (await evalJs(`document.getElementById('daily-gen-count').textContent`))
          === "今天还没有抓取" && !(await evalJs(`dailyWindowDirty`)),
        await evalJs(chipNow));
}
