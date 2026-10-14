// ── 附属结果的新鲜度:成功应用了才算追平 ──────────────────────────────────────
// 状态轮询回答的是「现在是哪一轮」,而历史列表和待处理数各是**另一次**请求取回来
// 的。原来只记「见过哪一轮」(`pipeDoneRun` 无条件前移),于是那一次附属请求失败之后,
// 后续轮询只知道「这一轮已经见过」、不知道「屏幕上还没对上」—— 列表长期停在旧那份
// 清单,等多久都不补,页面上还一个字都不说。实测形态:后端 6 轮、页面 5 行、顶上已经
// 是新那一轮,切走再回来才补齐。待处理数那条 catch 是同一个形状(保留旧值、清掉欠账),
// 所以**两者走同一套**,不在两处各判一遍。
//
// 判据分成两件事:**desired**(状态轮询最近说的那一轮)和 **applied**(成功渲染过的
// 那一轮)。只有渲染成功才确认;失败就留着缺口,由后续状态轮询按退避补读,并在面板上
// 如实说一句「这部分还是旧的」—— 「见过结束事件」不等于「屏幕上对上了」。
//
// **退避挂在状态轮询上,不另起计时器**:轮询本身就是 6 秒一次(跑的时候 1.5 秒),
// 再叠一层自己的节奏就有两套时钟,离线时还会变成无间隔重试。第 1、2、4、8、16 轮各
// 补一次,到上限只停自动重试 —— 提示不撤,旁边那颗「重新读取」永远能点。
const PIPE_FRESH = {
  history: { label: '历史运行' },
  pending: { label: '待处理数' },
};
const PIPE_FRESH_TRIES = 5;
let pipeFreshTick = 0;
// 后端那边最近一次留档失败的异常类型(`/api/pipeline/runs` 的 `error`)。它和上面
// 那几个缺口是**两件不同的事**:缺口是「这一屏没取到」,这个是「那一轮根本没留下」。
let pipeFreshNote = '';

function pipeFreshSlot(key) {
  const s = PIPE_FRESH[key];
  if (s.applied === undefined) Object.assign(s, { applied: '', desired: '', misses: 0, due: 0, reason: '' });
  return s;
}

// 成功渲染了 `run` 这一轮的那份结果
function pipeFreshOk(key, run) {
  Object.assign(pipeFreshSlot(key), { applied: run || '', misses: 0, due: 0, reason: '' });
  renderPipeFresh();
}

// 这一次没取到:留着缺口,按轮询次数退避补读
function pipeFreshMiss(key, reason) {
  const s = pipeFreshSlot(key);
  s.misses++;
  s.reason = reason || '';
  s.due = s.misses > PIPE_FRESH_TRIES ? 0 : pipeFreshTick + (1 << (s.misses - 1));
  renderPipeFresh();
}

// 状态轮询每读到一次都调:记下要对齐哪一轮,到点的缺口补一次。
//
// **没失败过就不补**(`due` 只在 miss 时才设)—— 一轮新结果本来就有自己的事件分支去
// 调,这一道只管「那一次失败之后谁来接」。两边在「之前失败过、这一轮又有新结果」时
// 会重叠,代价是多读一次小 JSON(在途那次会记一笔欠账),比漏掉一次补齐划得来。
function pipeFreshCheck(run, loaders) {
  pipeFreshTick++;
  for (const key of Object.keys(loaders)) {
    const s = pipeFreshSlot(key);
    s.desired = run || '';
    if (s.due && pipeFreshTick >= s.due) { s.due = 0; loaders[key](); }
  }
  renderPipeFresh();
}

function pipeFreshGaps() {
  return Object.keys(PIPE_FRESH).map(pipeFreshSlot)
    .filter(s => s.misses > 0 && s.applied !== s.desired);
}

function renderPipeFresh() {
  const box = $('pipe-fresh');
  if (!box) return;
  const gaps = pipeFreshGaps();
  const lines = gaps.map(s =>
    `${s.label}还是上一次取到的那份${s.reason ? `（读取失败：${s.reason}）` : ''}`);
  // 后端留档失败单独一句:那一轮的结论是对的,只是没留下记录
  if (pipeFreshNote) lines.push(`上一轮的结果没能留档（${pipeFreshNote}），同步结论不受影响`);
  box.hidden = !lines.length;
  if (!lines.length) return;
  const stuck = gaps.some(s => s.misses > PIPE_FRESH_TRIES);
  $('pipe-fresh-text').textContent = lines.join('；') + '。'
    + (gaps.length ? (stuck ? '已停止自动重试。' : '正在自动重试。') : '');
}

// 「重新读取」:把缺口归零重新开始退避,并立刻各补一次
function pipeFreshRetry() {
  for (const key of Object.keys(PIPE_FRESH)) Object.assign(pipeFreshSlot(key), { misses: 0, due: 0, reason: '' });
  pipeFreshNote = '';
  renderPipeFresh();
  loadPipeHistory();
  refreshPipePending();
}
