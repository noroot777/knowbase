// Edit a complete schedule, then save once; do not write on every field change.
let schedLoaded = false, schedDirty = false, schedSaving = false, schedLoading = false;
function scheduleMessage(s) {
  if (!s.enabled) return '定时同步未启用';
  const next = s.next_run ? ` · 下次 ${new Date(s.next_run).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}` : '';
  return `已保存：每天 ${s.time} 自动同步${next}`;
}
// 设置搬进弹窗之后,页头那颗按钮就是它在页面上唯一的露头 —— 当前状态写在按钮
// 上,不打开也看得见「几点跑/有没有开」。读不到时保持裸标签(别写死一个未启用,
// 那是在报一个没查到的结论)。
function renderSchedEntry(s) {
  const label = $('subs-sched-label');
  if (!label) return;
  label.textContent = s ? (s.enabled ? `定时同步 · 每天 ${s.time}` : '定时同步 · 未启用') : '定时同步';
  $('subs-sched-open').classList.toggle('is-on', !!s?.enabled);
}
// 页头这颗按钮也是**派生展示**,跟着共享事实走(见 `schedules.js`)。原来它只在
// 弹窗自己读/存的时候刷 —— 别处读到的新配置(来源页那次读取、另一份页面保存后
// 这边回来重读)改不到它,于是按钮上的时刻和同一页右侧那句说明能长期说两套话。
// 顺带把**开着的那张干净表单**补完:别处(来源页那次读取、另一份页面保存之后这边
// 回来重读)确认的新事实到了,而这张表单自己那一趟早就结束了(甚至就是被顶掉的那
// 一趟)—— 没有草稿、不在读也不在存的话,它就该跟着这份事实收尾,而不是停在上一次
// 的样子、更不是停在「正在读取」。只认**非 null**:null 是"当前不知道",而共享层
// 分不出"从没读过"和"读了没读到"(两者都是 null),拿它去报"读不到"会在根本没读过
// 的时候冤枉一条好定时;本次读取自己失败那一档由 settleSched 自己报。
watchSchedules(config => {
  renderSchedEntry(config.pipe);
  if (!config.pipe || schedLoading) return;              // 自己那趟会收尾
  if ($('sched-modal').style.display === 'none') return; // 没开着就只刷按钮
  if (!pipeChannels.length) return;                      // 来源目录还没到,画不出勾选框
  settleSched(config.pipe, '');
});
function openSchedModal() {
  $('sched-modal').style.display = '';
  loadSched();
}
function closeSchedModal() { $('sched-modal').style.display = 'none'; }
// 定时同步也可以一次跑几个来源，所以这里是一组勾选框而不是一个下拉。
function renderSchedChannels(selected) {
  $('sched-channels').innerHTML = pipeChannels.map(c => `<label class="pipe-channel">
    <input type="checkbox" data-sched-channel="${esc(c.id)}" onchange="markSchedDirty()"${selected.includes(c.id) ? ' checked' : ''}>
    <span><b>${esc(c.label)}</b></span></label>`).join('');
}
function schedChannels() {
  return [...$('sched-channels').querySelectorAll('input:checked')].map(i => i.dataset.schedChannel);
}
// **一次读取必须有终态。** 读取一开始禁用整片字段、隐藏重读入口、把那句话换成
// 「正在读取定时设置…」—— 那是个加载态,所以成功、失败、被更晚的读取顶掉,哪条分支
// 走完都得回到这里收尾。终态只有三种:**采用当前已确认的事实**、**说读不到并把重读
// 放出来**、**保留用户的草稿**。
//
// 原来成功和失败两条分支各有一处早退(拿到的事实为空、或者这趟的失败被顶掉),早退
// 之后 finally 只清 `schedLoading`:弹窗就停在「正在读取定时设置…」、整片字段禁用、
// 连重读按钮都藏着,真鼠标点启用开关毫无反应,**再关一次重开**才恢复(第 3 轮实测)。
// 共享事实当时是好的 —— 所以"共享事实没有倒退"证明不了"界面已经消费完"。
function settleSched(fact, failure) {
  if (schedSaving) return;        // 保存那条路自己会收尾(saveSched 的 finally)
  if (schedDirty) {               // 草稿优先:一个字都不许覆盖,但得让他改得动、存得下
    $('sched-fields').disabled = false; $('sched-save').disabled = false;
    return;
  }
  if (fact) {
    $('sched-on').checked = !!fact.enabled; $('sched-time').value = fact.time || '07:30';
    renderSchedChannels(fact.channels?.length ? fact.channels : [fact.channel || pipeChannel]);
    schedLoaded = true; $('sched-fields').disabled = false;
    $('sched-reload').hidden = true;
    $('sched-msg').textContent = scheduleMessage(fact);
    return;
  }
  // 当前"不知道":这趟自己失败了,或者更晚那一趟失败把事实抹成了未知。一律给原因
  // 和重读入口,**不许留在加载态**。
  $('sched-msg').textContent = failure ? '读取失败：' + failure : '读不到定时设置';
  $('sched-reload').hidden = false;
  // 重读失败时屏幕上那份仍是上一次读到的真实设置,照旧可编辑可保存;
  // 从没读成功过才留在禁用态 —— 那时根本没有值可编辑。
  $('sched-fields').disabled = !schedLoaded;
  $('sched-save').disabled = !schedDirty;
}
async function loadSched() {
  if (schedLoading || schedSaving || schedDirty) return;
  // 占代次要在发请求**之前**(见 schedules.js 的 schedulesRead):这一趟还没回来
  // 时设置被保存了,这个回包就该丢掉,不许把刚保存的那一份倒退回去。
  const accept = schedulesRead('pipe');
  schedLoading = true; $('sched-reload').hidden = true;
  $('sched-fields').disabled = true;
  $('sched-save').disabled = true;
  $('sched-msg').textContent = '正在读取定时设置…';
  let fact = null, failure = '';
  try {
    await loadPipeChannels();
    // **资格先判,再动任何一格;画的是共享层确认的那一份,不是我这趟的回包。**
    // 别处(另一份页面、日报面板那次读取)可能在这中间写进了更新的事实,照自己
    // 的回包画就会把它退回旧值 —— 而共享事实拒绝退回,同一页两处说法当场打架。
    fact = accept({ pipe: await pipeJson('/api/pipeline/schedule') }).pipe;
  } catch (e) {
    // 读失败同样过这道门:这趟已经被顶掉就别用它的失败盖掉别处刚拿到的好值 ——
    // 但"别盖掉"不等于"不用收尾",拿回来的那份事实照样要画上去。
    fact = accept({ pipe: null }).pipe;
    if (!fact) failure = e.message;
  }
  finally { schedLoading = false; settleSched(fact, failure); }
}
function markSchedDirty() {
  if (!schedLoaded || schedSaving || schedLoading) return;
  schedDirty = true; $('sched-save').disabled = false; $('sched-msg').textContent = '有未保存的修改';
}
async function saveSched() {
  if (!schedLoaded || schedSaving || !schedDirty) return;
  if (!$('sched-time').reportValidity()) return;
  const channels = schedChannels();
  if (!channels.length) { $('sched-msg').textContent = '请至少选择一个同步来源'; return; }
  const body = { enabled: $('sched-on').checked, time: $('sched-time').value, channels };
  schedSaving = true; $('sched-fields').disabled = true; $('sched-save').disabled = true;
  $('sched-save').textContent = '正在保存…'; $('sched-msg').textContent = '';
  try {
    const saved = await pipePost('/api/pipeline/schedule', body); schedDirty = false;
    $('sched-msg').textContent = scheduleMessage(saved);
    // **保存成功必须过共享层**:只更新页头按钮的那一版里,同一页右侧的来源说明
    // 会长期说反话(开了说不会自动同步、关了还承诺每天同步),刷新才对得上。
    // 页头按钮由上面那条订阅跟着画,这里不用再单独刷一次。
    schedulesSaved('pipe', saved);
  } catch (e) { $('sched-msg').textContent = '未保存：' + e.message + '。修改已保留，可重试。'; }
  finally { schedSaving = false; $('sched-fields').disabled = false; $('sched-save').disabled = !schedDirty; $('sched-save').textContent = '保存设置'; }
}
