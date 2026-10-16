// 每日日报的**定时设置**:每天几点自动跑一轮、跑之前要不要先同步、同步哪些范围。
// 它在页头上是「改定时」那扇抽屉(开合在 daily.js 的 `setDailyDrawer`),抽屉收起
// 时页头那句话仍然报着当前的定时事实。
//
// **从 daily.js 拆出来只因为行数**:那边再加上两扇抽屉的开合就压着 700 行了。
// 搬过来的部分逐符号照原样,只在末尾添了抽屉自己那几件事(页头那句事实、收起、
// 放弃草稿)。
let dailySettings = null, dailyChannels = [];
let dailyLoaded = false, dailyDirty = false, dailySaving = false;
// 这一片**有几趟读取还在路上**。它不是一道闸(面板重开就该再读一次设置,在途的那
// 趟由共享层的代次去作废 —— 拿它当闸会把重开那次读取整个吞掉,于是被按住的旧回包
// 落地时写进去的是过时的值,实测把开关退回了未启用):它只回答「自己这趟读完了
// 没有」,共享层的新事实到了要不要替这张表单收尾,得先知道它还在不在读。
let dailySchedReads = 0;

// ── 定时设置 ────────────────────────────────────────────────────────────────

function dailyScheduleMessage(s) {
  if (!s.enabled) return '定时生成未启用';
  const next = s.next_run ? ` · 下次 ${dailyTime(s.next_run)}` : '';
  return `已保存：每天 ${s.time} ${s.sync ? '先同步再生成' : '只用已入库的内容生成'}${next}`;
}

// **一次读取必须有终态**(事故形态在 `docs/decisions/sync-schedule.md`,那一节原来
// 写的是两张表单,现在只剩这一张)。这一片底下那句话一开始就是「正在读取定时
// 设置…」、整片字段禁用着,所以
// 成功、失败、被更晚的读取顶掉,哪条分支走完都得回到这里收尾。终态只有三种:采用
// 当前已确认的事实、说读不到并把重读放出来、保留用户的草稿。
//
// 原来成功和失败两条分支各有一处早退,早退之后这一片就永远停在「正在读取定时
// 设置…」:字段禁用、没有重读入口,而后端和共享事实都是好的。
// 终态里有**三处**要跟着动:表单自己、页头那句定时事实、抽屉里那颗退出按钮
// (它的字在「收起」和「取消修改」之间换)。后两处挂在 finally 上,不跟着表单那
// 几条分支各写一遍 —— 早退分支漏掉一处,页头就会一直写着「正在读取定时设置…」
// 而抽屉里早就是好的了(这一片原来的事故正是分支各写一遍漏了早退那一条)。
function settleDaily(fact, failure) {
  try { settleDailyFields(fact, failure); }
  finally { renderDailySchedLabel(); renderDailySchedClose(); }
}

function settleDailyFields(fact, failure) {
  // **就绪判据只有一份**(`schedules.js` 的 `scheduleSettle`):配置**和**可选来源
  // 目录都到位才算"已加载" —— 目录没拿到就画不出同步范围,硬开放字段只会让他存出
  // 一份空范围(保存那边会当场拒绝)。这一档照"不知道"走,重读把目录一起补上。
  const { state, missing } = scheduleSettle({
    fact, saving: dailySaving, dirty: dailyDirty,
    deps: [{ ready: dailyChannels.length, what: '同步范围' }],
  });
  if (state === 'saving') return;  // 保存那条路自己会收尾
  if (state === 'draft') {         // 草稿优先:一个字都不许覆盖
    $('daily-sched-fields').disabled = false; $('daily-sched-save').disabled = false;
    return;
  }
  if (state === 'ready') {
    fillDailySched(fact);
    dailyLoaded = true;
    $('daily-sched-fields').disabled = false;
    $('daily-sched-reload').hidden = true;
    $('daily-sched-msg').textContent = dailyScheduleMessage(fact);
    return;
  }
  // 三句话分得清三件事:**这趟真失败了**、**定时读到了但依赖没读到**、**当前不知道**
  // (更晚那趟失败把事实抹成了未知)。都给重读入口,一句含糊的「失败」救不了用户。
  $('daily-sched-msg').textContent =
    failure ? '读取定时设置失败：' + failure
    : missing ? missing + '还没读到，点「重新读取」再试一次'
    : '读不到定时设置';
  $('daily-sched-reload').hidden = false;
  // 重读失败时屏幕上那份仍是上一次读到的真实设置,照旧可编辑可保存;
  // 从没读成功过才留在禁用态 —— 那时根本没有值可编辑。
  $('daily-sched-fields').disabled = !dailyLoaded;
  $('daily-sched-save').disabled = !dailyDirty;
}

async function loadDailySettings() {
  if (dailySaving || dailyDirty) return;
  // 占代次要在发请求之前,见 schedules.js 的 schedulesRead(面板重开会再读一次,
  // 这一趟还没回来时保存了的话,这个回包不许把刚保存的那一份倒退回去)。
  const accept = schedulesRead('daily');
  dailySchedReads++;
  let fact = null, failure = '';
  try {
    const data = await pipeJson('/api/daily/settings');
    // **资格先判,再动任何一格;而且画的是共享层确认的那一份,不是我这趟的回包。**
    // 照自己的回包画的后果(第 1、2 轮各实测一次):这趟被别处的保存顶掉了,
    // 开关却被它改回未勾选、提示写成「定时生成未启用」,而同一页读共享事实的
    // 来源说明还承诺每天同步 —— 同一份配置两处打架。
    fact = accept({ daily: data.settings }).daily;
    // 外层那个 channels 是**可选来源目录**(不是设置),共享层不管它,照这趟取。
    dailyChannels = data.channels;
  } catch (e) {
    // 读失败也要过同一道门:**这趟要是已经被顶掉,就不许用它的失败盖掉别处
    // 刚读到/刚保存的好值**(那会把一句「读取失败」挂在一份正常的配置上)。
    // 但"别盖掉"不等于"不用收尾" —— 拿回来的那份事实照样要画上去。
    fact = accept({ daily: null }).daily;
    if (!fact) failure = e.message;
  }
  finally { dailySchedReads--; settleDaily(fact, failure); }
}

// 这一片也跟着共享事实走:别处(「我的订阅」页那次读取;定时只剩这一张表单了,但
// 读的地方不止一处)确认的新事实到了,而这一片自己那趟早就结束了的话,它该跟着
// 完成,而不是停在上一次的样子。只认
// **非 null**:null 是"当前不知道",而共享层分不出"从没读过"和"读了没读到",拿它
// 报"读不到"会冤枉一条好定时;自己这趟失败那一档由 settleDaily 自己报。
watchSchedules(config => {
  if (config.daily && !dailySchedReads) settleDaily(config.daily, '');
});

function renderDailyChannels() {
  const picked = new Set(dailySettings.channels);
  $('daily-channels').innerHTML = dailyChannels.map(c =>
    `<label class="daily-channel"><input type="checkbox" data-channel="${esc(c.id)}"
      ${picked.has(c.id) ? 'checked' : ''} onchange="markDailyDirty()"> ${esc(c.label)}</label>`).join('');
}

// 把整张表单摆成 `fact` 的样子。两个消费方:一次读取落地、「取消修改」把草稿
// 丢回已保存的那一份。`dailySettings` 要先换成 fact —— `renderDailyChannels()`
// 勾哪几个范围读的是它。
function fillDailySched(fact) {
  dailySettings = fact;
  $('daily-on').checked = !!fact.enabled;
  $('daily-time').value = fact.time;
  $('daily-sync').checked = !!fact.sync;
  $('daily-firstdays').value = fact.first_days;
  renderDailyChannels();
}

function pickedDailyChannels() {
  return [...$('daily-channels').querySelectorAll('input:checked')].map(i => i.dataset.channel);
}
function markDailyDirty() {
  if (!dailyLoaded || dailySaving) return;
  dailyDirty = true;
  $('daily-sched-save').disabled = false;
  $('daily-sched-msg').textContent = '有未保存的修改';
  renderDailySchedLabel(); renderDailySchedClose();
}

async function saveDailySettings() {
  if (!dailyLoaded || dailySaving || !dailyDirty) return;
  if (!$('daily-time').reportValidity() || !$('daily-firstdays').reportValidity()) return;
  const channels = pickedDailyChannels();
  if (!channels.length) {
    // 同页头那张:**"一个都没勾"和"一个都没有可勾的"是两件事**,后者该说重读。
    if (!dailyChannels.length) {
      $('daily-sched-msg').textContent = '同步范围还没读到，点「重新读取」再试一次';
      $('daily-sched-reload').hidden = false;
    } else $('daily-sched-msg').textContent = '至少勾选一个同步范围';
    return;
  }
  // **保存期间把整片字段一起禁掉。** 只禁保存按钮的话:
  // 这期间改的开关/时刻/范围进不了已经发出去的那个请求,`markDailyDirty` 又因为
  // 正在保存直接返回 —— 保存成功后 dirty 被清掉、按钮继续禁着,那几下修改**既没
  // 保存也存不了**,而面板写着「已保存」。
  dailySaving = true; $('daily-sched-fields').disabled = true;
  $('daily-sched-save').disabled = true;
  $('daily-sched-save').textContent = '正在保存…';
  renderDailySchedClose();   // 保存期间那颗退出按钮不许写着「取消修改」
  try {
    dailySettings = await pipePost('/api/daily/settings', {
      enabled: $('daily-on').checked, time: $('daily-time').value,
      sync: $('daily-sync').checked, first_days: Number($('daily-firstdays').value) || 1, channels,
    });
    dailyDirty = false;
    $('daily-sched-msg').textContent = dailyScheduleMessage(dailySettings);
    // **保存成功必须过共享层**:订阅页那句「什么时候自动同步」是这份配置的派生
    // 展示,不交出去它就一直停在旧说法上(开了说不会自动同步、关了还承诺每天
    // 同步),刷新才对得上。POST 回的是扁的设置对象,GET 那边才套着一层 settings。
    schedulesSaved('daily', dailySettings);
  } catch (e) {
    $('daily-sched-msg').textContent = '未保存：' + e.message + '。修改已保留，可重试。';
  } finally {
    dailySaving = false; $('daily-sched-save').textContent = '保存设置';
    $('daily-sched-fields').disabled = false;
    $('daily-sched-save').disabled = !dailyDirty;
    renderDailySchedLabel(); renderDailySchedClose();
  }
}

// ── 页头那一句、和抽屉的退路 ────────────────────────────────────────────────

// 页头上那句定时事实。四种说法分得清四件事:**有草稿没存**、开着(每天几点)、
// 关着、读不出来。草稿排在最前面 —— 抽屉一收,这是屏幕上唯一还提得起"你改了
// 东西但没保存"的地方(抽屉里那句「有未保存的修改」跟着抽屉一起看不见了)。
function renderDailySchedLabel() {
  const fact = dailyLoaded && dailySettings;
  $('daily-sched-label').textContent =
    dailyDirty ? '定时有未保存的修改'
    : !fact ? '定时设置读不出来'
    : fact.enabled ? `每天 ${fact.time} 自动生成`
    : '没开定时生成';
}

// 没有草稿时它只是「收起」;有草稿时它是「取消修改」,按下去把草稿丢掉。
// 正在保存的那几百毫秒里草稿正要变成事实,这时按它只收起、不动表单。
function renderDailySchedClose() {
  $('daily-sched-close').textContent = dailyDirty && !dailySaving ? '取消修改' : '收起';
}

function toggleDailySched() {
  setDailyDrawer('sched', !dailyDrawerOpen('sched'));
}

// 收起这扇抽屉。**有草稿时连草稿一起丢掉**:草稿会把这张表单后续的刷新整个挡住
// (`loadDailySettings` 开头那道 `dailyDirty` 早退),抽屉一关人就看不见它了 ——
// 不丢的话,这张表单从此停在旧值上,而屏幕上没有任何地方说得清为什么。丢掉之后
// 立刻补读一次:被草稿挡掉的那些刷新,得有人把它还回来。
function cancelDailySched() {
  const discard = dailyDirty && !dailySaving;
  setDailyDrawer('sched', false);
  if (!discard) return;
  dailyDirty = false;
  if (dailySettings) {
    fillDailySched(dailySettings);
    $('daily-sched-msg').textContent = dailyScheduleMessage(dailySettings);
  }
  $('daily-sched-save').disabled = true;
  renderDailySchedLabel(); renderDailySchedClose();
  loadDailySettings();
}
