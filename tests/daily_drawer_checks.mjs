// 页头那两扇抽屉(「改区间」「改定时」)在真浏览器里的回归,九条。
//
// 仓库主人提的两件事:改区间那一行**没有退出口**(展开之后只能再点一次页头那颗
// 按钮,而它的字从头到尾都是「改区间」,看不出是开关);定时设置压在页面最下面,
// 要先滚过整片日报才摸得到。于是两件事收成同一套:页头一句事实 + 一颗开抽屉的
// 文字按钮,抽屉右下角一颗退出按钮。
//
// **从 tests/daily_probe.mjs 拆出来只因为行数**(那边贴着 700 行上限)。
//
// 判据全部落在后果上:抽屉开没开、**取消之后区间是哪一段**、草稿还在不在、页头
// 那句话说的是不是当前事实 —— 不验"某个标志位有没有打上"。
export async function checkDailyDrawers({ evalJs, waitFor, check }) {
  // 摆到同一个起点:两扇都收着,区间是默认的那一段。
  await evalJs(`(async () => { setDailyDrawer('window', false); await resetDailyWindow(); })()`);

  // ── 定时设置搬到页头 ──────────────────────────────────────────────────────
  check("定时设置长在页头抽屉里,不再压在页面最下面", await evalJs(`(() => {
    const sched = document.getElementById('daily-sched');
    const bench = document.querySelector('.daily-workbench');
    // 在工作区**之前**:DOCUMENT_POSITION_FOLLOWING 说的是 bench 在 sched 后面
    return sched.hidden && (sched.compareDocumentPosition(bench) & 4) !== 0
      && document.getElementById('daily-sched-toggle').getAttribute('aria-expanded') === 'false'
      && !document.querySelector('#dailypanel > section.daily-schedule'); })()`));
  await evalJs(`toggleDailySched()`);
  check("点「改定时」就地展开,勾选框都在里面", await evalJs(`(() => {
    const box = document.getElementById('daily-sched');
    return !box.hidden && box.querySelectorAll('#daily-channels input').length > 0
      && document.getElementById('daily-sched-toggle').getAttribute('aria-expanded') === 'true'; })()`));

  // ── 同一时刻只开一扇 ──────────────────────────────────────────────────────
  // 两扇叠起来小半屏高,下面的日报列表和正文会被推出视野。
  await evalJs(`toggleDailyWindow()`);
  check("开了改区间,定时那扇自己收起来", await evalJs(`(() => {
    const $$ = id => document.getElementById(id);
    return !$$('daily-window').hidden && $$('daily-sched').hidden
      && $$('daily-window-toggle').getAttribute('aria-expanded') === 'true'
      && $$('daily-sched-toggle').getAttribute('aria-expanded') === 'false'; })()`));

  // ── 改区间那一行的「取消」 ────────────────────────────────────────────────
  // 进来时是默认区间:取消 = 把这次改的那几下丢掉,回到默认。
  await evalJs(`(() => {
    document.getElementById('daily-since').value = '2026-02-03T04:05';
    document.getElementById('daily-until').value = '2026-02-04T04:05';
    markDailyWindow(); return true; })()`);
  check("摆局:页头跟着写成改过的那一段",
        /^覆盖 02-03 04:05 → 02-04 04:05$/.test(
          await evalJs(`document.getElementById('daily-window-label').textContent`)),
        await evalJs(`document.getElementById('daily-window-label').textContent`));
  await evalJs(`cancelDailyWindow()`);
  check("「取消」把这一行收起来", await evalJs(`(() => {
    return document.getElementById('daily-window').hidden
      && document.getElementById('daily-window-toggle').getAttribute('aria-expanded') === 'false'; })()`));
  check("进来时是默认区间,取消之后还是默认区间",
        await evalJs(`!dailyWindowDirty`)
        && /起到生成时刻/.test(
          await evalJs(`document.getElementById('daily-window-label').textContent`)),
        await evalJs(`document.getElementById('daily-window-label').textContent`));

  // 进来时就挂着一段自定义区间:取消只丢**这次**改的那几下,原来那段照旧挂着
  // (想把它也丢掉有「接上一份日报」那颗)。否则「取消」等于顺手撤销了上一次
  // 的决定,而那一次人是存心改的。
  await evalJs(`(() => { toggleDailyWindow();
    document.getElementById('daily-since').value = '2026-02-03T04:05';
    document.getElementById('daily-until').value = '2026-02-04T04:05';
    markDailyWindow(); toggleDailyWindow(); return true; })()`);
  await evalJs(`toggleDailyWindow()`);
  await evalJs(`(() => { document.getElementById('daily-until').value = '2026-09-09T09:09';
    markDailyWindow(); return true; })()`);
  await evalJs(`cancelDailyWindow()`);
  check("进来时挂着的那段自定义区间,取消不会一起丢掉", await evalJs(`(() => {
    return dailyWindowDirty
      && document.getElementById('daily-since').value === '2026-02-03T04:05'
      && document.getElementById('daily-until').value === '2026-02-04T04:05'; })()`),
        await evalJs(`document.getElementById('daily-window-label').textContent`));

  // 取消那一下必须是**一次完整的状态变更**:屏幕上那两个时刻、页头那句话、和真正
  // 会被提交的区间(`dailyWindowDirty`)是同一件事,不能先提交其中一项、把其余的
  // 回滚交给一次可能失败的网络读取。审查实测:重读失败时页头还写着刚被取消掉的
  // 「覆盖 01-01 → 01-02」,而此刻点生成走的是默认区间、写的是今天那一份。
  // 验的是**进来时是默认区间**那一支(上一条验的是另一支),所以先摆回默认。
  await evalJs(`resetDailyWindow()`);
  await evalJs(`(() => { window.__winFail = pipeJson;
    pipeJson = async (path, ...rest) => {
      if (path === '/api/daily/window') throw new Error('摆局:断连');
      return window.__winFail(path, ...rest); };
    return true; })()`);
  await evalJs(`toggleDailyWindow()`);
  await evalJs(`(() => {
    document.getElementById('daily-since').value = '2026-01-01T00:00';
    document.getElementById('daily-until').value = '2026-01-02T00:00';
    markDailyWindow(); return true; })()`);
  await evalJs(`cancelDailyWindow()`);
  const stale = JSON.parse(await evalJs(`JSON.stringify({
    dirty: dailyWindowDirty,
    label: document.getElementById('daily-window-label').textContent,
    since: document.getElementById('daily-since').value,
    until: document.getElementById('daily-until').value })`));
  check("取消时重读失败:页头、两个时刻、要提交的区间仍然是同一件事",
        !stale.dirty && !/01-01|01-02/.test(stale.label)
        && stale.since !== '2026-01-01T00:00' && stale.until !== '2026-01-02T00:00',
        JSON.stringify(stale));
  await evalJs(`(() => { pipeJson = window.__winFail; delete window.__winFail;
                         return true; })()`);
  await evalJs(`resetDailyWindow()`);

  // Esc 跟那颗「取消」是同一件事(确认框那边也是这个约定)。
  await evalJs(`resetDailyWindow()`);
  await evalJs(`toggleDailyWindow()`);
  await evalJs(`(() => {
    document.getElementById('daily-since').value = '2026-05-06T07:08'; markDailyWindow();
    document.getElementById('daily-window').dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); return true; })()`);
  // 等的是**落地之后**那句话:`dailyWindowDirty` 在取消那一刻就先落了 false,只等
  // 它的话,默认区间还没取回来就算过了(实测那一瞬页头还写着改过的那一段)。
  check("焦点在这一行里按 Esc,等于点了取消",
        await waitFor(`document.getElementById('daily-window').hidden && !dailyWindowDirty
          && /起到生成时刻/.test(document.getElementById('daily-window-label').textContent)`, 5000),
        await evalJs(`document.getElementById('daily-window-label').textContent`));

  // ── 页头那句定时事实 ──────────────────────────────────────────────────────
  // 抽屉一收,它是屏幕上唯一还说得清定时开没开的地方,所以三种说法都得钉住。
  await evalJs(`(() => {
    $('daily-on').checked = true; $('daily-time').value = '07:15';
    [...$('daily-channels').querySelectorAll('input')].forEach(
      i => { i.checked = i.dataset.channel === 'rss'; });
    markDailyDirty(); return true; })()`);
  check("有草稿时页头替收起来的抽屉喊一声",
        (await evalJs(`$('daily-sched-label').textContent`)) === '定时有未保存的修改'
        && (await evalJs(`$('daily-sched-close').textContent`)) === '取消修改',
        await evalJs(`$('daily-sched-label').textContent + ' / ' + $('daily-sched-close').textContent`));
  await evalJs(`saveDailySettings()`);
  await waitFor(`!dailySaving && dailySettings && dailySettings.enabled`);
  check("开着时页头写明每天几点",
        (await evalJs(`$('daily-sched-label').textContent`)) === '每天 07:15 自动生成'
        && (await evalJs(`$('daily-sched-close').textContent`)) === '收起',
        await evalJs(`$('daily-sched-label').textContent + ' / ' + $('daily-sched-close').textContent`));

  // 保存失败那一档:草稿还在,那颗退出按钮必须退回「取消修改」。它要是停在
  // 「收起」,人按下去就把刚才没存进去的那几下一起丢了,而屏幕上一个字没提。
  await evalJs(`(() => {
    window.__realPipePost = pipePost;
    pipePost = async (...a) => { pipePost = __realPipePost; throw new Error('摆局:存不进去'); };
    $('daily-time').value = '06:30'; markDailyDirty(); return true; })()`);
  await evalJs(`saveDailySettings()`);
  await waitFor(`!dailySaving`);
  check("保存失败之后那颗退出按钮退回「取消修改」", await evalJs(`(() => {
    return dailyDirty && $('daily-sched-close').textContent === '取消修改'
      && /未保存/.test($('daily-sched-msg').textContent); })()`),
        await evalJs(`$('daily-sched-close').textContent + ' / ' + $('daily-sched-msg').textContent`));
  await evalJs(`cancelDailySched()`);
  await waitFor(`!dailyDirty`);

  // ── 「取消修改」把草稿丢掉 ────────────────────────────────────────────────
  // 草稿会把这张表单后续的刷新整个挡住(`loadDailySettings` 开头那道早退),抽屉
  // 一关人就看不见它了 —— 所以收起时连草稿一起丢,并补读一次把挡掉的还回来。
  await evalJs(`toggleDailySched()`);
  await evalJs(`(() => { $('daily-on').checked = false; $('daily-time').value = '23:59';
    markDailyDirty(); return true; })()`);
  await evalJs(`cancelDailySched()`);
  await waitFor(`!dailyDirty && $('daily-time').value === '07:15'`);
  check("「取消修改」:草稿丢掉、表单回到已保存那一份、抽屉收起", await evalJs(`(() => {
    return document.getElementById('daily-sched').hidden && !dailyDirty
      && $('daily-time').value === dailySettings.time
      && $('daily-on').checked === !!dailySettings.enabled
      && $('daily-sched-close').textContent === '收起'
      && $('daily-sched-label').textContent === '每天 07:15 自动生成'; })()`),
        await evalJs(`$('daily-sched-msg').textContent + ' / ' + $('daily-time').value`));
  check("草稿丢掉之后,被它挡住的那趟刷新也回来了",
        !/有未保存的修改/.test(await evalJs(`$('daily-sched-msg').textContent`)),
        await evalJs(`$('daily-sched-msg').textContent`));

  // 还回去:别给沙箱留一个开着的定时(同 checkScheduleRaces 的收尾)
  await evalJs(`toggleDailySched()`);
  await evalJs(`(() => { $('daily-on').checked = false; markDailyDirty(); return true; })()`);
  await evalJs(`saveDailySettings()`);
  await waitFor(`!dailySaving && dailySettings && !dailySettings.enabled`);
  check("关着时页头写「没开定时生成」",
        (await evalJs(`$('daily-sched-label').textContent`)) === '没开定时生成',
        await evalJs(`$('daily-sched-label').textContent`));
  // 收尾口径管的不只是保存那条路:**一次读取落地**也得把页头那句话收完。把它
  // 涂掉再重读一次,它得自己回来 —— 读取那几条早退分支原来就漏过一次收尾。
  await evalJs(`(() => { $('daily-sched-label').textContent = '——'; return true; })()`);
  await evalJs(`loadDailySettings()`);
  check("一次读取落地,页头那句事实跟着收尾",
        await waitFor(`$('daily-sched-label').textContent === '没开定时生成'`, 5000),
        await evalJs(`$('daily-sched-label').textContent`));
  await evalJs(`setDailyDrawer('sched', false)`);
}
