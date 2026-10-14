// 保存定时之后,同一页右侧那句「什么时候自动同步」要跟着变 —— **真鼠标操作**,
// 而且保存之后不许替它调任何产品刷新函数:原来那句说明自己缓存了一份配置,保存
// 只更新页头按钮,于是页头写着「定时同步 · 每天 07:30」、右侧写着「目前不会自动
// 同步」,反过来关掉再保存右侧继续承诺每天同步,只有整页刷新才对得上。
//
// 这一组钉的是**接线**:`saveSched` 有没有真的把新配置交到共享定时层
// (`schedules.js` 的 `schedulesSaved`)。不变量本身和乱序那几条在
// `tests/test_source_schedule_note.py` 里。
export async function checkScheduleSaveNote({ evaluate, wait, check, click, screenshot, viewport }) {
  const note = () => evaluate(`$('source-settings-status').textContent`);
  const entry = () => evaluate(`$('subs-sched-label').textContent`);
  const keptKind = await evaluate('sourceKind');
  // 视口定到一个确定值:前一组检查改过它,而弹窗里那几个控件要点得到
  // (点不到的话鼠标事件会落在遮罩上,弹窗当场被关掉 —— 那是一种假红)。
  await viewport(1440, 1000);
  // 点之前先确认这个控件真的在最上层接得住鼠标,别赌它恰好在视口里 —— 点不中的
  // 那一下会落到遮罩上把弹窗关掉,后面每一条都跟着红,而原因看不出来。
  // **命中算到子元素**:按钮中心点上多半是里面那枚图标或那段文字,事件照样冒上来。
  const hit = async selector => {
    await evaluate(`document.querySelector(${JSON.stringify(selector)})
      .scrollIntoView({ block: 'nearest' }); true`);
    await wait(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      const box = el.getBoundingClientRect();
      const on = document.elementFromPoint(box.left + box.width / 2,
                                           box.top + box.height / 2);
      return !!on && el.contains(on); })()`);
    await click(selector);
  };
  // 弹窗里那几个控件在读取/保存期间整片禁用(fieldset),这时候的点击一律不算 ——
  // 不等它开放就点,控件压根不会变,红出来的是 `sched-on` 没勾上,看不出真因。
  const ready = () => wait(`schedLoaded && !schedLoading && !schedSaving
    && !$('sched-fields').disabled`);
  const toggleOn = async want => {
    await ready();
    if (await evaluate(`$('sched-on').checked`) !== want) {
      await hit('#sched-on');
      await wait(`$('sched-on').checked === ${want}`);
    }
  };
  const save = async () => {
    await wait(`!$('sched-save').disabled`);
    await hit('#sched-save');
    await wait(`!schedSaving && !schedDirty`);
  };

  // 摆局:把右侧那句说明按 rss 画出来(读一次两条定时是打开设置本来就会做的事),
  // 再把页头那条定时**确定地**调到关着 —— 不赌它初始是什么。
  await evaluate(`(() => { sourceKind = 'rss'; return true; })()`);
  await evaluate(`loadSchedules()`);
  await hit('#subs-sched-open');
  await wait(`$('sched-modal').style.display === ''`);
  if (await evaluate(`$('sched-on').checked`)) {
    await toggleOn(false);
    await save();
  }
  await wait(`/不会自动同步/.test($('source-settings-status').textContent)`);
  const staged = await note();
  check('摆局:页头那条定时关着,右侧说这一类不会自动同步',
        /不会自动同步/.test(staged) && !/跟着页头的定时同步那一轮同步/.test(staged), staged);

  // ── 开启并保存 ──────────────────────────────────────────────────────────────
  await toggleOn(true);
  // 页头那条定时的默认范围是采集器那一条,得把 rss 也勾上才轮得到这一类来源
  if (!await evaluate(`$('sched-channels').querySelector('[data-sched-channel="rss"]').checked`)) {
    await hit('#sched-channels [data-sched-channel="rss"]');
    await wait(`$('sched-channels').querySelector('[data-sched-channel="rss"]').checked`);
  }
  await save();
  await wait(`/已保存/.test($('sched-msg').textContent)`);
  // **到此为止不再调任何刷新函数**:要验的就是保存自己有没有把配置交出去。
  const savedOn = { note: await note(), entry: await entry() };
  check('开启并保存之后,右侧说明当场改口说会跟着页头那一轮同步',
        /跟着页头的定时同步那一轮同步/.test(savedOn.note)
        && !/目前不会自动同步/.test(savedOn.note), JSON.stringify(savedOn));
  // 时刻要真从页头按钮上取下来再比 —— 取不到就算红,别拿一个空串去 includes
  // (那个永远为真,这一条会变成一句不会红的话)。
  const when = (savedOn.entry.match(/每天 \d\d:\d\d/) || [])[0];
  check('页头按钮和右侧说明说的是同一个时刻',
        !!when && savedOn.note.includes(when), JSON.stringify({ ...savedOn, when }));
  await screenshot('schedule-saved-note-follows');

  // ── 关停并保存:反过来也要改口 ───────────────────────────────────────────────
  await toggleOn(false);
  await save();
  await wait(`/未启用/.test($('sched-msg').textContent)`);
  const savedOff = { note: await note(), entry: await entry() };
  check('关停并保存之后,右侧说明不再承诺每天同步',
        !/跟着页头的定时同步那一轮同步/.test(savedOff.note)
        && /不会自动同步/.test(savedOff.note), JSON.stringify(savedOff));

  // ── 页头那颗按钮也是派生展示:别处读到的新配置要改到它 ──────────────────────
  // 它原来只在弹窗自己读/存的时候刷。先把弹窗关掉,再用直接 POST 冒充"另一份
  // 页面保存了"(这一页的共享层对那次保存毫不知情),然后只调一次共享层的读取 ——
  // 页头按钮和右侧说明都得跟着变,而且说的是同一个时刻。
  await evaluate(`closeSchedModal(); true`);
  await evaluate(`pipePost('/api/pipeline/schedule',
    {enabled: true, time: '05:05', channels: ['rss']})`);
  await evaluate(`loadSchedules()`);
  await wait(`/每天 05:05/.test($('subs-sched-label').textContent)`);
  const elsewhere = { note: await note(), entry: await entry() };
  check('别处改了定时,页头按钮跟着共享层变(没打开过弹窗)',
        /每天 05:05/.test(elsewhere.entry)
        && /每天 05:05 跟着页头的定时同步那一轮同步/.test(elsewhere.note),
        JSON.stringify(elsewhere));
  // 还回去:别给这一轮的后续检查留一条开着的定时
  await evaluate(`pipePost('/api/pipeline/schedule',
    {enabled: false, time: '07:30', channels: ['wechat_local']})`);
  await evaluate(`loadSchedules()`);
  await evaluate(`(() => { sourceKind = ${JSON.stringify(keptKind)}; return true; })()`);
}

// 一次读取必须有终态:**过期的那趟失败不许把弹窗留在「正在读取」**。
// 第 3 轮真实鼠标实测的形态:打开弹窗时那次 GET 还在路上,关掉再打开(这一次
// loadSched 被 schedLoading 挡住),然后那个旧 GET 以网络错误结束 —— 它被读取资格门
// 挡住(对的:别拿它的失败盖掉别处刚确认的好值),但弹窗就停在那里:整片字段禁用、
// 没有重读按钮、消息还是「正在读取定时设置…」,点启用开关毫无反应,**再关一次重开**
// 才恢复。判据落在用户看得见、点得动的地方:字段能点、点了真的变、说的是当前事实。
export async function checkScheduleFormSettles({ evaluate, wait, check, click, screenshot, viewport }) {
  await viewport(1440, 1000);
  const hit = async selector => {
    await evaluate(`document.querySelector(${JSON.stringify(selector)})
      .scrollIntoView({ block: 'nearest' }); true`);
    await wait(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      const box = el.getBoundingClientRect();
      const on = document.elementFromPoint(box.left + box.width / 2,
                                           box.top + box.height / 2);
      return !!on && el.contains(on); })()`);
    await click(selector);
  };
  const form = () => evaluate(`JSON.stringify({
    msg: $('sched-msg').textContent, fields: $('sched-fields').disabled,
    reload: $('sched-reload').hidden, on: $('sched-on').checked,
    time: $('sched-time').value, loading: schedLoading, loaded: schedLoaded })`);

  // 摆局:后端那条定时先关着,弹窗关着
  await evaluate(`closeSchedModal(); true`);
  await evaluate(`pipePost('/api/pipeline/schedule',
    {enabled: false, time: '07:30', channels: ['rss']})`);
  await evaluate(`loadSchedules()`);
  await wait(`/未启用/.test($('subs-sched-label').textContent)`);

  // ① 按住这次真实的 GET,打开弹窗 —— 它进了加载态
  await evaluate(`(() => {
    window.__realPipeJson = pipeJson;
    window.__failHeld = null;
    let held = false;
    pipeJson = async (path, ...rest) => {
      if (path !== '/api/pipeline/schedule' || rest.length || held) return __realPipeJson(path, ...rest);
      held = true;                                   // 只按住第一趟
      return new Promise((_, reject) => { window.__failHeld = () => reject(new Error('连接已断开')); });
    };
    return true; })()`);
  await hit('#subs-sched-open');
  await wait(`$('sched-modal').style.display === '' && schedLoading`);
  const reading = JSON.parse(await form());
  check('摆局:弹窗在加载态(字段禁用、没有重读、写着正在读取)',
        reading.fields && reading.reload && /正在读取/.test(reading.msg), JSON.stringify(reading));
  await screenshot('schedule-form-reading');

  // ② 真鼠标关掉再打开(这一次的 loadSched 会被 schedLoading 挡住)
  await hit('.sched-modal-head .icon-button');
  await wait(`$('sched-modal').style.display === 'none'`);
  await hit('#subs-sched-open');
  await wait(`$('sched-modal').style.display === ''`);

  // ③ 别处把定时开到 06:05 并确认成共享事实(这一页那趟还按着)
  // 走 pipePost:它带着 options 调 pipeJson,上面那个桩只按住"不带 options 的那一趟"
  // (也就是 GET),所以这次保存照常落到后端。
  await evaluate(`pipePost('/api/pipeline/schedule',
    {enabled: true, time: '06:05', channels: ['rss']})`);
  await evaluate(`loadSchedules()`);
  await wait(`/每天 06:05/.test($('subs-sched-label').textContent)`);

  // ④ 现在才让那个旧 GET 以网络错误结束
  await evaluate(`(() => { __failHeld(); pipeJson = __realPipeJson; return true; })()`);
  await wait(`!schedLoading`);
  await evaluate(`new Promise(r => setTimeout(r, 200))`);
  const settled = JSON.parse(await form());
  check('过期的失败落地之后,弹窗收尾成当前事实(不许留在正在读取)',
        !settled.fields && !/正在读取/.test(settled.msg) && settled.reload
        && settled.on && settled.time === '06:05' && /每天 06:05/.test(settled.msg),
        JSON.stringify(settled));
  await screenshot('schedule-form-settled');
  // 判据不能只看标志位和那句话:**真鼠标点得动才算收尾**(实测那一版点了毫无反应)。
  await hit('#sched-on');
  const clicked = JSON.parse(await form());
  check('收尾之后真鼠标点启用开关真的改得动',
        !clicked.on && /未保存/.test(clicked.msg), JSON.stringify(clicked));
  await screenshot('schedule-form-click-after-settle');

  // 上面那一下点出了草稿(这是对的:草稿优先,谁都不许盖掉它)—— 存掉它,下面那条
  // 检查要的是**干净表单**;顺带再把后端调到开启,免得"补完"和当前显示的值一样、
  // 什么都验不到。
  await hit('#sched-save');
  await wait(`!schedSaving && !schedDirty`);
  await evaluate(`pipePost('/api/pipeline/schedule',
    {enabled: true, time: '06:05', channels: ['rss']})`);

  // ⑤ 自己这趟真失败了:给原因和重读入口;**晚到的共享事实要能把这张干净表单补完**
  //    (原来 watchSchedules 只有页头按钮和来源说明两个订阅者,没有干净表单那个 ——
  //    自己那趟失败之后它就只能靠用户再关一次重开)。
  await evaluate(`(() => {
    pipeJson = async (path, ...rest) => {
      if (path === '/api/pipeline/schedule' && !rest.length) throw new Error('这趟真失败了');
      return __realPipeJson(path, ...rest);
    };
    return true; })()`);
  await evaluate(`loadSched()`);
  await wait(`!schedLoading`);
  const failed = JSON.parse(await form());
  check('自己这趟读失败:给原因、放出重读,不留在加载态',
        !/正在读取/.test(failed.msg) && /这趟真失败了/.test(failed.msg)
        && !failed.reload, JSON.stringify(failed));
  await evaluate(`(() => { pipeJson = __realPipeJson; return true; })()`);
  await evaluate(`loadSchedules()`);                   // 别处那次读取确认了事实
  await evaluate(`new Promise(r => setTimeout(r, 200))`);
  const completed = JSON.parse(await form());
  check('晚到的共享事实把这张干净表单补完(没再碰过弹窗自己的读取)',
        completed.on && completed.time === '06:05' && !completed.fields
        && /每天 06:05/.test(completed.msg) && completed.reload, JSON.stringify(completed));

  // 还回去:别给后面的检查留一条开着的定时
  await evaluate(`pipePost('/api/pipeline/schedule',
    {enabled: false, time: '07:30', channels: ['wechat_local']})`);
  await evaluate(`loadSchedules()`);
  await evaluate(`closeSchedModal(); true`);
}
