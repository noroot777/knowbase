// 定时配置这一层在**日报面板**这一侧的接线,三条,全部真浏览器:
// ① 保存日报定时之后,同一页别处那句派生说明当场跟着变;
// ② 同一页按住真实那次 GET、期间保存、再放行 —— 旧回包不许把开关改回未启用;
// ③ 双页面那个形状:按住第一趟 GET,用直接 POST 冒充"另一份页面保存了",这一页
//    随后那次更新的读取先画成开启,再放行那个旧 GET。
//
// **从 tests/daily_probe.mjs 拆出来只因为行数**(那边到了 700 行上限),摆法和
// 判据原样搬过来。不变量本身在 tests/test_source_schedule_note.py;**那一层钉不住
// 这三条**(面板"先画完再问资格"/"照自己的回包画"注入进去它一条都不红)。
export async function checkScheduleRaces({ evalJs, waitFor, check }) {
  // ── 保存日报定时之后,别处那句派生说明也要跟着变 ──────────────────────────────
  // 「这一类来源什么时候自动同步」原来自己缓存一份配置,保存只更新自己那一块 ——
  // 于是同一页上两处说法打架,只有整页刷新才对得上。配置收到共享层之后,保存成功
  // 就会通知全部派生展示。**保存之后不再调任何刷新函数**:要验的就是保存自己有没有
  // 把配置交出去。订阅那一侧的真鼠标版本在 tests/subs_schedule_note_checks.mjs。
  await evalJs(`(() => { sourceKind = 'rss'; return true; })()`);
  await evalJs(`loadSchedules()`);
  check("摆局:来源那句说明先读到了当前(关着的)定时",
        await waitFor(`/不会自动同步/.test(
          document.getElementById('source-settings-status').textContent)`, 8000),
        await evalJs(`document.getElementById('source-settings-status').textContent`));
  await evalJs(`(() => {
    $('daily-on').checked = true; $('daily-sync').checked = true;
    $('daily-time').value = '06:05';
    [...$('daily-channels').querySelectorAll('input')].forEach(
      i => { i.checked = i.dataset.channel === 'rss'; });
    markDailyDirty(); return true; })()`);
  await evalJs(`saveDailySettings()`);
  await waitFor(`!dailySaving && /每天 06:05/.test($('daily-sched-msg').textContent)`);
  const crossNote = await evalJs(`document.getElementById('source-settings-status').textContent`);
  check("保存日报定时之后,来源那句说明当场跟着变",
        /每天 06:05 跟着日报那一轮同步/.test(crossNote)
        && !/不会自动同步/.test(crossNote), crossNote);
  // 还回去:别给沙箱留一个开着的定时
  await evalJs(`(() => { $('daily-on').checked = false; markDailyDirty(); return true; })()`);
  await evalJs(`saveDailySettings()`);
  await waitFor(`!dailySaving`);

  // ── 在途的旧读取不许把刚保存的那一份改回去 ────────────────────────────────────
  // 面板重开会再读一次设置。这一趟还没回来时用户开启并保存了 —— 旧回包落地时带的
  // 是保存**之前**的事实。第 1 轮实测:它把开关改回未勾选、`dailySettings.enabled`
  // 改回 false、提示改成「定时生成未启用」,而同一页读共享事实的那句来源说明还写着
  // 每天同步。**按住的是真实的那次 GET**(不伪造回包内容),保存之后不调任何刷新。
  await evalJs(`(() => {
    window.__realPipeJson = pipeJson;
    window.__release = null;
    pipeJson = async (path, ...rest) => {
      if (path !== '/api/daily/settings' || rest.length) return __realPipeJson(path, ...rest);
      const answer = __realPipeJson(path, ...rest);        // 真去读,只是按住不放
      return new Promise(resolve => { window.__release = () => resolve(answer); });
    };
    return true; })()`);
  // **不许 await 它**:回包被按住,这个 promise 要等放行才 settle,而 `evalJs` 是
  // awaitPromise —— await 下去整支探针就停在这一行(实测挂死)。
  await evalJs(`(() => { loadDailySettings(); return true; })()`);
  await waitFor(`typeof window.__release === 'function'`);
  await evalJs(`(() => {
    $('daily-on').checked = true; $('daily-sync').checked = true;
    $('daily-time').value = '06:05';
    [...$('daily-channels').querySelectorAll('input')].forEach(
      i => { i.checked = i.dataset.channel === 'rss'; });
    markDailyDirty(); pipeJson = __realPipeJson; return true; })()`);
  await evalJs(`saveDailySettings()`);
  await waitFor(`!dailySaving && /每天 06:05/.test($('daily-sched-msg').textContent)`);
  await evalJs(`window.__release(); true`);                // 现在才放行那个旧回包
  await evalJs(`new Promise(r => setTimeout(r, 200))`);
  const afterStale = await evalJs(`JSON.stringify({
    on: $('daily-on').checked, time: $('daily-time').value,
    fact: !!dailySettings.enabled, msg: $('daily-sched-msg').textContent,
    note: document.getElementById('source-settings-status').textContent })`);
  const kept = JSON.parse(afterStale);
  check("在途的旧读取落地之后,日报开关没被改回未启用",
        kept.on && kept.fact && kept.time === '06:05'
        && !/未启用/.test(kept.msg), afterStale);
  // 判据是**两处说法一致**,不是单看来源那一句:只看它的话,共享事实拦住了旧值
  // 就够它绿了,而面板自己被旧回包改回「未启用」照样红不出来(实测注入时如此)。
  const promises = /每天 06:05 跟着日报那一轮同步/.test(kept.note);
  const panelOn = kept.on && !/未启用/.test(kept.msg);
  check("面板和来源说明说的是同一件事(不许一处说未启用、另一处承诺每天同步)",
        promises === panelOn && promises, afterStale);
  // ── 别处(另一份页面/另一台机器)保存之后,这边那次在途的旧读取也不许倒退 ─────
  // 上面那一条是**同一页**保存;这一条是第 2 轮双页面实测那个形状:A 页按住一次
  // GET,B 页保存成功,A 页随后那次**更新的**读取已经把开关/提示/共享事实都画成
  // 开启,这时才放行 A 那个旧 GET —— 照自己回包画的那一版会把它们全退回「未启用」,
  // 而共享事实拒绝退回,同一页上来源说明还承诺每天同步。这里用直接 POST 冒充 B 页
  // (不走 saveDailySettings,所以这一页的共享层对那次保存毫不知情,跟真双页一样)。
  // 摆局:先让后端(和这一页)回到**关着**,这样待会儿被按住那一趟读到的和"另一份
  // 页面"存进去的确实不是同一个值 —— 两趟读到同一个值的话这条检查什么都验不到
  // (实测过:它当时真的红不出来)。
  await evalJs(`pipePost('/api/daily/settings', {enabled: false, time: '09:00',
    sync: true, first_days: 1, channels: ['rss']})`);
  await evalJs(`(() => { loadDailySettings(); return true; })()`);
  await waitFor(`dailySettings && !dailySettings.enabled`);
  await evalJs(`(() => {
    window.__realPipeJson = pipeJson;
    window.__release = null;
    let held = false;
    pipeJson = async (path, ...rest) => {
      if (path !== '/api/daily/settings' || rest.length || held) return __realPipeJson(path, ...rest);
      held = true;                                  // 只按住第一趟,后面的照常走
      const answer = __realPipeJson(path, ...rest);
      return new Promise(resolve => { window.__release = () => resolve(answer); });
    };
    return true; })()`);
  await evalJs(`(() => { loadDailySettings(); return true; })()`);   // 旧 GET 发出去并被按住
  await waitFor(`typeof window.__release === 'function'`);
  // 「另一份页面」在后端把定时开到 06:05(这一页收不到任何通知)
  await evalJs(`pipePost('/api/daily/settings', {enabled: true, time: '06:05',
    sync: true, first_days: 1, channels: ['rss']})`);
  await evalJs(`(() => { loadDailySettings(); return true; })()`);   // 这一页更新的那次读取
  await waitFor(`dailySettings && dailySettings.enabled && /每天 06:05/.test(
    $('daily-sched-msg').textContent)`);
  await evalJs(`window.__release(); true`);                          // 现在才放行旧 GET
  await evalJs(`new Promise(r => setTimeout(r, 200))`);
  const afterOther = await evalJs(`JSON.stringify({
    on: $('daily-on').checked, time: $('daily-time').value,
    fact: !!dailySettings.enabled, shared: !!(schedules().daily || {}).enabled,
    msg: $('daily-sched-msg').textContent,
    note: document.getElementById('source-settings-status').textContent })`);
  const bPage = JSON.parse(afterOther);
  check("别处保存之后,在途的旧读取落地也不许把开关退回未启用",
        bPage.on && bPage.fact && bPage.shared && bPage.time === '06:05'
        && !/未启用/.test(bPage.msg), afterOther);
  check("退不退都不许出现两处说法打架",
        /每天 06:05 跟着日报那一轮同步/.test(bPage.note)
        === (bPage.on && !/未启用/.test(bPage.msg)), afterOther);
  await evalJs(`(() => { pipeJson = __realPipeJson; return true; })()`);

  // 还回去:别给沙箱留一个开着的定时
  await evalJs(`(() => { $('daily-on').checked = false; markDailyDirty(); return true; })()`);
  await evalJs(`saveDailySettings()`);
  await waitFor(`!dailySaving`);
}

// 一次读取必须有终态,日报面板这一侧(页头那张表单的对称消费者;真鼠标那一版在
// tests/subs_schedule_note_checks.mjs,那边能点得到弹窗里的控件)。
// 两条:**过期的那趟失败也要收尾**、**晚到的共享事实能把干净表单补完**。原来成功和
// 失败两条分支各有一处早退 —— 早退之后这一片就停在「正在读取定时设置…」:字段禁用、
// 没有重读入口,而后端和共享事实都是好的。
export async function checkDailyFormSettles({ evalJs, waitFor, check }) {
  const form = () => evalJs(`JSON.stringify({
    msg: $('daily-sched-msg').textContent, fields: $('daily-sched-fields').disabled,
    reload: $('daily-sched-reload').hidden, on: $('daily-on').checked,
    time: $('daily-time').value, loaded: dailyLoaded })`);

  // 摆局:后端那条日报定时先关着,面板读到它
  await evalJs(`pipePost('/api/daily/settings', {enabled: false, time: '09:00',
    sync: true, first_days: 1, channels: ['rss']})`);
  await evalJs(`loadDailySettings()`);
  await waitFor(`dailySettings && !dailySettings.enabled`);
  // 再摆成「从没读成功过」那一刻的样子 —— 页面初次加载时就是这个状态(字段禁用、
  // 底下写着正在读取,见 index.html 那一片的初值)。这一页早就读过一次了,没法真的
  // 回到那一刻,所以按它的初值摆。
  await evalJs(`(() => {
    dailyLoaded = false;
    $('daily-sched-fields').disabled = true;
    $('daily-sched-reload').hidden = true;
    $('daily-sched-msg').textContent = '正在读取定时设置…';
    return true; })()`);
  // 按住这一趟**真实的** GET(不伪造回包),让它待会儿以网络错误结束
  await evalJs(`(() => {
    window.__realPipeJson = pipeJson;
    window.__failHeld = null;
    let held = false;
    pipeJson = async (path, ...rest) => {
      if (path !== '/api/daily/settings' || rest.length || held) return __realPipeJson(path, ...rest);
      held = true;                                  // 只按住第一趟
      return new Promise((_, reject) => { window.__failHeld = () => reject(new Error('连接已断开')); });
    };
    return true; })()`);
  await evalJs(`(() => { loadDailySettings(); return true; })()`);
  await waitFor(`typeof window.__failHeld === 'function'`);
  // 「别处」把定时开到 06:05 并经由共享层确认(这一页那一趟还按着,毫不知情)
  await evalJs(`pipePost('/api/daily/settings', {enabled: true, time: '06:05',
    sync: true, first_days: 1, channels: ['rss']})`);
  await evalJs(`loadSchedules()`);
  await waitFor(`(schedules().daily || {}).enabled`);
  const held = JSON.parse(await form());
  check('摆局:面板还在加载态,共享事实已经是开启的',
        held.fields && /正在读取/.test(held.msg) && !held.on, JSON.stringify(held));
  // 现在才让那个旧 GET 失败 —— 它被读取资格门挡住(对的),但必须照样收尾
  await evalJs(`(() => { __failHeld(); pipeJson = __realPipeJson; return true; })()`);
  await evalJs(`new Promise(r => setTimeout(r, 200))`);
  const settled = JSON.parse(await form());
  check('日报面板:过期的失败落地之后收尾成当前事实(不许留在正在读取)',
        !settled.fields && settled.loaded && settled.on && settled.time === '06:05'
        && settled.reload && /每天 06:05/.test(settled.msg), JSON.stringify(settled));

  // 自己这趟真失败那一档:给原因、放出重读;**晚到的共享事实再把它补完**
  // (先把后端换个值,免得"补完"跟当前显示的一样、什么都验不到)
  await evalJs(`pipePost('/api/daily/settings', {enabled: false, time: '09:00',
    sync: true, first_days: 1, channels: ['rss']})`);
  await evalJs(`(() => {
    pipeJson = async (path, ...rest) => {
      if (path === '/api/daily/settings' && !rest.length) throw new Error('这趟真失败了');
      return __realPipeJson(path, ...rest);
    };
    return true; })()`);
  await evalJs(`loadDailySettings()`);
  await waitFor(`/这趟真失败了/.test($('daily-sched-msg').textContent)`);
  const failed = JSON.parse(await form());
  check('日报面板:自己这趟读失败,给原因并放出重读',
        !failed.reload && !/正在读取/.test(failed.msg) && !failed.fields,
        JSON.stringify(failed));
  await evalJs(`(() => { pipeJson = __realPipeJson; return true; })()`);
  await evalJs(`loadSchedules()`);                    // 别处那次读取确认了当前事实
  await evalJs(`new Promise(r => setTimeout(r, 200))`);
  const completed = JSON.parse(await form());
  check('日报面板:晚到的共享事实把这张干净表单补完(没再碰过它自己的读取)',
        !completed.on && /未启用/.test(completed.msg) && completed.reload
        && !completed.fields, JSON.stringify(completed));
}
