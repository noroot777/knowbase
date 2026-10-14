// ── 附属结果的新鲜度:凭据是回包自己的身份,不是此刻的页面快照 ──────────────────
// 状态轮询回答的是「现在是哪一轮」,而历史列表和待处理数各是**另一次**请求取回来
// 的。这里记两件事:**desired**(状态轮询最近说的那一轮)和 **applied**(已经应用的
// 那份数据**自己**的身份)。两者不等就是落后,落后就按退避补读。
//
// 踩过两次,都记在这里:
//
// ① 只记「见过哪一轮」(`pipeDoneRun` 无条件前移)的话,那一次附属请求失败之后后续
//    轮询只知道「这一轮见过了」、不知道「屏幕上还没对上」——列表长期停在旧那份清
//    单,等多久都不补,页面上还一个字都不说。
// ② applied 取**此刻的页面快照**身份的话,一份属于上一轮的回包会被盖成「已追平最新
//    那一轮」:实测里旧 pending 回包(`extract=1`)迟到、新一轮已经把它清成 0,而面板
//    永久写着「还有 1 项内容待提取」—— applied 和 desired 看上去相等,缺口不存在、
//    提示不出现、补读也不会发生。**所以 applied 必须由回包自己证明**(pending 回包
//    带 run_id;历史回包按它最新那一轮的身份,见 `pipeFreshDesired` 的用处)。
//
// **落后就补,不管上一次是「请求失败」还是「拿到的是旧数据」** —— 后者原来根本不算
// 缺口(请求是成功的),而它恰恰是 ② 那条路。
//
// **退避挂在状态轮询上,不另起计时器**:轮询本身 6 秒一次(跑的时候 1.5 秒),再叠一
// 层就有两套时钟,离线时还会变成无间隔重试。到上限只停自动重试,提示和那颗「重新
// 读取」留着。**历史和待处理数走同一套**,不在两处各判一遍。
const PIPE_FRESH = {
  history: { label: '历史运行' },
  pending: { label: '待处理数' },
};
const PIPE_FRESH_TRIES = 5;
let pipeFreshTick = 0;
// 后端那边最近一次留档失败的异常类型(`/api/pipeline/runs` 的 `error`)。它和上面
// 那两个缺口是**两件不同的事**:缺口是「这一屏取到的是旧的」,这个是「那一轮根本
// 没留下」—— 后者补读多少次也追不平,所以不能当缺口。
let pipeFreshNote = '';

function pipeFreshSlot(key) {
  const s = PIPE_FRESH[key];
  if (s.applied === undefined) Object.assign(s, { applied: '', desired: '', tries: 0, due: 0, reason: '' });
  return s;
}

// 「现在要对齐哪一轮」。历史那边用它:留档失败时后端能给的最新就是手上这一份,
// 那不是缺口(面板另有一句如实说了),所以按 desired 盖章。
function pipeFreshDesired(key) { return pipeFreshSlot(key).desired; }

// 应用了一份数据,**`run` 是这份数据自己的身份**
function pipeFreshOk(key, run) {
  const s = pipeFreshSlot(key);
  s.applied = run || '';
  if (s.applied === s.desired) Object.assign(s, { tries: 0, due: 0, reason: '' });
  renderPipeFresh();
}

// 这一次请求就没成:记下原因(退避和次数由 `pipeFreshCheck` 统一管)。
// reason 非空 = 请求失败,这一档**立刻**在面板上说;拿到旧数据那一档先补读再说。
function pipeFreshMiss(key, reason) {
  pipeFreshSlot(key).reason = reason || '未知原因';
  renderPipeFresh();
}

// 状态轮询每读到一次都调:记下要对齐哪一轮,落后的按退避补一次。
function pipeFreshCheck(run, loaders) {
  pipeFreshTick++;
  for (const key of Object.keys(loaders)) {
    const s = pipeFreshSlot(key);
    s.desired = run || '';
    if (s.applied === s.desired) { Object.assign(s, { tries: 0, due: 0, reason: '' }); continue; }
    if (pipeFreshTick < s.due || s.tries >= PIPE_FRESH_TRIES) continue;
    s.due = pipeFreshTick + (1 << Math.min(s.tries, 4));   // 1、2、4、8、16 轮
    s.tries++;
    loaders[key]();
  }
  renderPipeFresh();
}

// 「还有哪几块是旧的」。拿到旧数据那一档要**连试两次仍没追平**才说:一次就说的话
// 正常的时序追赶(补读多半立刻成功)会在面板上闪一下。请求失败那一档立刻说。
function pipeFreshGaps() {
  return Object.keys(PIPE_FRESH).map(pipeFreshSlot)
    .filter(s => s.applied !== s.desired && (s.reason || s.tries >= 2));
}

function renderPipeFresh() {
  const box = $('pipe-fresh');
  if (!box) return;
  const gaps = pipeFreshGaps();
  const lines = gaps.map(s => s.reason
    ? `${s.label}还是上一次取到的那份（读取失败：${s.reason}）`
    : `${s.label}还是上一轮的，没能对上这一轮`);
  // 后端留档失败单独一句:那一轮的结论是对的,只是没留下记录
  if (pipeFreshNote) lines.push(`上一轮的结果没能留档（${pipeFreshNote}），同步结论不受影响`);
  box.hidden = !lines.length;
  if (!lines.length) return;
  const stuck = gaps.some(s => s.tries >= PIPE_FRESH_TRIES);
  $('pipe-fresh-text').textContent = lines.join('；') + '。'
    + (gaps.length ? (stuck ? '已停止自动重试。' : '正在自动重试。') : '');
}

// 「重新读取」:退避归零重新开始,并立刻各补一次
function pipeFreshRetry() {
  for (const key of Object.keys(PIPE_FRESH)) Object.assign(pipeFreshSlot(key), { tries: 0, due: 0, reason: '' });
  pipeFreshNote = '';
  renderPipeFresh();
  loadPipeHistory();
  refreshPipePending();
}
