// ── 同步中心那条「按类型排的线」:这一轮有哪几类、走到哪一类、每一类的那几步 ──────
// 硬约束在 docs/decisions/sync-flow.md 第一节。
//
// **类型是给人看的那一层,阶段是给机器跑的那一层。** 节点上的成败一律从阶段卡片和
// 结果账(`results`,逐来源逐步那一份)里读,本文件不另记一份;后端那份计划
// (`flow_live` / 记录里的 `flow`)只答「要求了什么、走到了哪、各跑了多久」。
//
// **计划只认身份对得上的那一份**:正在跑的那一轮认 `flow_live`(owner 等于这一轮的
// run id 或它所在那一批),留档那一轮认它自己记着的 `flow`(`flow.run === run_id`)。
// 两样都对不上就按计划阶段和来源推出一排只读节点 —— 宁可点不了,不许把别人那份
// 计划的跳过名单念到这一轮头上。
const PIPE_FLOW_GROUPS = [['ingest', '入库处理', ['index', 'extract']],
  ['daily', '生成日报', ['generate']], ['tidy', '整理工具与 Wiki', ['projects', 'wiki']]];
const PIPE_FLOW_GENERATE = { name: '生成日报' };
const PIPE_FLOW_SKIP_WHY = {
  src: '跳过之后这一类这次不抓，下次同步接着抓，多半只是晚一轮进库；只给最近若干条的订阅源，隔得太久可能补不全。',
  ingest: '跳过之后，这一轮新抓的内容搜不到，日报也取不到它们（日报从入了库的内容里取材）。抓取期间已经在做的存量提取会照常收尾，做完的结果保留。',
  daily: '跳过之后，这一轮只同步、不出日报。今天那份日报保持原样。',
  tidy: '跳过之后，图鉴和三张图谱这次不更新。条目化只回看最近 3 天，连着跳几天会留下补不回来的空白。',
};
const PIPE_FLOW_SQUARE = '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><rect x="2" y="2" width="6" height="6" rx="1" fill="currentColor"/></svg>';
const PIPE_FLOW_DASH = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 6h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

// 展开的是哪一类(空串 = 一行都没展开)。只有人点才会展开,默认一行都不展开 —— 出问题的
// 那几类行上已经是红的、带着条数,不必替人点开。`view` 记的是「这张卡讲的是哪一轮」,换了
// 一轮就全收起。画那几行在 `pipe_flow_rows.js`。
let pipeFlowSel = '', pipeFlowViewKey = null, pipeFlowModelNow = null;

/** 这一类此刻在跑。来源那几类是同时跑的,各自记在 `lanes` 里(手上那一步做完、导出排在
 *  全库那几步之后的那一路记成空串,照样算在跑);全库那几组一次只跑一组,记在 `running`。
 *  老留档没有 `lanes`,那时来源也记在 `running` 上。 */
function pipeFlowRunning(plan, key) {
  return !!plan && (plan.running === key || Object.prototype.hasOwnProperty.call(plan.lanes || {}, key));
}

/** 这一类手上是哪一步(口径同后端 `pipeline_flow._step_of`)。 */
function pipeFlowStepOf(plan, key) {
  if (!plan) return '';
  return Object.prototype.hasOwnProperty.call(plan.lanes || {}, key) ? plan.lanes[key] || '' : plan.step || '';
}

function pipeFlowTrusted(record) {
  const f = record?.flow;
  return f && typeof f === 'object' && f.run && f.run === record.run_id && Array.isArray(f.nodes) ? f : null;
}

// 几轮的结果账合成一份,同一个(步骤, 来源)后面那份说了算(同 `pipeline_results.merge`)。
function pipeFlowMergeResults(...parts) {
  const out = {};
  for (const part of parts) {
    for (const [stage, owned] of Object.entries(part || {})) {
      if (owned && typeof owned === 'object') out[stage] = { ...(out[stage] || {}), ...owned };
    }
  }
  return out;
}

function pipeFlowDerivedNodes(record) {
  const planned = pipePlannedStages(record);
  const steps = ['crawl', 'export'].filter(k => planned.includes(k));
  const nodes = steps.length && !pipeLibraryOnly(record)
    ? pipeRunScopes(record).filter(s => s.channel).map(s => ({
      key: 'src:' + s.channel, kind: 'src', channel: s.channel, label: channelLabel(s.channel), steps }))
    : [];
  for (const [key, label, group] of PIPE_FLOW_GROUPS) {
    const own = group.filter(k => planned.includes(k));
    if (own.length) nodes.push({ key, kind: key, label, steps: own });
  }
  return nodes;
}

/** 此刻后端手里那份计划**是不是这一屏这一轮的**;是就返回它,不是返回 null。
 *  口径只有这一份:那条线认它,「停止整轮」也认它(停哪一轮、走哪个接口)。 */
function pipeFlowLive(s) {
  const fl = s?.flow_live;
  if (!fl || typeof fl !== 'object' || !fl.owner) return null;
  if (s.running) return fl.owner === s.run_id || (s.batch && fl.owner === s.batch) ? fl : null;
  // 日报那一批在两轮管道之间(正在生成日报):管道空着,计划还是它的
  return fl.kind === 'daily' && s.last_run?.flow?.owner === fl.owner ? fl : null;
}

/** 这一屏要画的那条线。`live` = 计划还在后端手里,按钮才点得动。 */
function pipeFlowModel(s, viewing) {
  const record = viewing || pipeResult(s);
  const fl = viewing ? null : pipeFlowLive(s);
  let plan = fl, live = !!fl, batch = null;
  if (live && fl.kind === 'daily' && s.last_run?.flow?.owner === fl.owner) batch = s.last_run;
  if (!plan) plan = pipeFlowTrusted(record);
  const running = live && s.running;
  const results = live
    ? pipeFlowMergeResults(batch?.results, running ? s.results : null)
    : pipeFlowMergeResults(record.results);
  const stage = k => (running && (s.planned_stages || []).includes(k) ? s.stages?.[k]
    : (batch || record).stages?.[k]) || null;
  // 拷一份再挂状态:节点对象来自快照,原地写会把这一屏的推断混进下一跳的回包里
  const nodes = (plan ? plan.nodes : pipeFlowDerivedNodes(record)).map(n => ({ ...n, steps: [...(n.steps || [])] }));
  const model = { record, plan, live, stopping: live && !!(s.stop_requested || fl.stopping), results, stage, nodes,
    srcCount: nodes.filter(n => n.kind === 'src').length };
  for (const n of nodes) n.state = pipeFlowState(model, n);
  return model;
}

/** 「生成日报」那一步此刻的结论。它不是管道阶段、卡片里没有它;这份计划里没有它就是 null。 */
function pipeFlowGenerate(plan, live) {
  if (!plan || !(plan.nodes || []).some(n => n.key === 'daily')) return null;
  if (plan.running === 'daily') return { status: 'running', detail: '正在生成日报' };
  const r = plan.results?.daily;
  // 跳过那一档日报那边按「停下」收尾(交进来的是 stopped),这里按人点的那一下说
  if ((plan.skips || []).includes('daily') && (plan.done || []).includes('daily')) return { status: 'skipped', detail: r?.detail || '本次跳过' };
  // **计划已经不在后端手里了还没有结论,就是不知道,不是「还没轮到」。** 日报那边不管
  // 停在哪一步都会交一份结论(`daily_run` 收尾那一笔),所以一份收了尾的记录里缺它,
  // 只能是这份记录拍在结论之前、或者那一笔没落下 —— 两种都不许推成「已完成」
  if (!r) return live ? { status: '', detail: '' } : { status: 'unknown', detail: '这一轮没留下日报生成的结论' };
  return { status: { done: 'ok', failed: 'error', stopped: 'stopped' }[r.status] || r.status || '', detail: r.detail || '' };
}

/** 这一轮**整个业务上**计划了哪几步、各是什么结论:管道那几个阶段 + 「生成日报」。
 *  概况那几个数、页头、历史那一行都按它数 —— 只数管道阶段的话,日报还在生成、或者
 *  生成失败时,这一轮照样写「已完成」(管道那会儿早就空了)。`generate` 结论还没落下
 *  (空串)就既不算成也不算败;计划已经收了尾还没有结论,念「结果未知」(`live` 那一档)。 */
function pipeRoundSteps(record, plan, live) {
  const planned = pipePlannedStages(record), stages = { ...(record.stages || {}) };
  const gen = pipeFlowGenerate(plan, live);
  if (gen) { planned.push('generate'); stages.generate = gen; }
  return { planned, stages, generating: gen?.status === 'running' };
}

/** 一条留档该配哪份计划:是此刻屏幕上那一轮(`pipeIsCurrent`)就认活的那一份,否则认它自己记着的。 */
function pipeRoundPlan(run) {
  const live = pipeIsCurrent(run) && pipeFlowLive(pipeSnapshot);
  return { plan: live || pipeFlowTrusted(run), live: !!live };
}

/** 这一类的第 `key` 步该念什么(状态 / 说明 / 失败明细)。没有证据就是空的。 */
function pipeFlowStep(model, node, key) {
  const st = pipeFlowStepOwn(model, node, key);
  // 这一类按请求跳过了、这一步又没留下自己的结论(日报在外层直接略过了它):照实念「跳过」,
  // 不念「未开始」—— 只在没有证据时补这一句,有结论的照旧按结论念
  const f = model.plan, skipped = f && (f.skips || []).includes(node.key) && (f.done || []).includes(node.key);
  return skipped && ['', 'pending', 'unknown'].includes(st.status || '') ? { status: 'skipped', detail: '本次跳过' } : st;
}

function pipeFlowStepOwn(model, node, key) {
  if (key === 'generate') return pipeFlowGenerate(model.plan, model.live) || { status: '', detail: '' };
  if (node.kind === 'src') {
    const cell = model.results?.[key]?.[node.channel];
    if (cell && typeof cell === 'object') return cell;
    // 只有一个来源时那张阶段卡片就是它的;几个来源合在一张卡上时不许拿来念某一个
    if (model.srcCount === 1) return model.stage(key) || { status: '', detail: '' };
    const span = model.plan?.times?.[node.key], running = pipeFlowRunning(model.plan, node.key);
    const t = span?.steps?.[key];
    if (t && !t[1] && running) return { status: 'running', detail: '' };
    // **没有它自己那一格 ≠ 没问题,也 ≠ 没跑。** 这个来源进过门、已经走完了(计划里有它
    // 的时刻),或者这一步整体确实跑过(老留档没有计划),却没留下它自己的结论:如实说
    // 不知道。推成「已完成」就是把别的来源的绿念到它头上,推成「没跑」同样是编的
    const ran = model.plan ? !!span && !running : ['ok', 'error', 'stopped'].includes(model.stage(key)?.status);
    return ran ? { status: 'unknown', detail: '这一轮没留下这个来源这一步自己的结果' } : { status: '', detail: '' };
  }
  return model.stage(key) || { status: '', detail: '' };
}

function pipeFlowStepState(st) {
  if (pipeHasFailure(st)) return 'issue';
  return { ok: 'ok', running: 'run', skipped: 'skip', stopped: 'skip', pending: 'wait', unknown: 'unknown' }[st?.status] || 'wait';
}

function pipeFlowState(model, node) {
  const f = model.plan, steps = node.steps.map(k => pipeFlowStepState(pipeFlowStep(model, node, k)));
  const issues = steps.filter(x => x === 'issue').length;
  const out = (state, extra = {}) => ({ state, issues, ...extra });
  // **`state` 只答「走到哪了」,`issues` 答「有没有出问题」,两样正交、谁也不许代表谁。**
  // 生命周期一律按计划推(running / times / done / skips),失败只是跟着的一枚标记:
  //   · 拿失败当生命周期 → 还没轮到的一类因为并行那一步红了就念「已完成」,「本次跳过」
  //     那颗按钮当场消失、后面那一类也找不着它(它仍要执行);
  //   · 拿生命周期盖失败 → 跳过余下几步把已经发生的红抹掉,轨道、详情、历史格一致念
  //     「本次跳过」,红一个字都看不见。
  // 归约只有这一层,轨道点/副标题、详情头、历史格、连续失败归约都读它:四处都要把两件事
  // 各说一遍,**不许在消费方给某个按钮补特例**(硬约束见 docs/decisions/sync-flow.md)。
  if (f) {
    const times = f.times?.[node.key], done = (f.done || []).includes(node.key);
    if (pipeFlowRunning(f, node.key)) return out('run');
    if ((f.skips || []).includes(node.key) && (done || !times)) return out('skip');
    // 没进过门的也可能是「不知道」:日报那一类不管停在哪都会交结论,收了尾的记录里缺它
    // (`pipeFlowGenerate` 念成 unknown)就不是「没跑」—— 这一句要先于下面那句「没跑」
    if (!times && !done && !steps.includes('unknown')) return model.live ? out('wait') : out('skip', { halt: true });
    if (steps.includes('unknown')) return out('unknown');
    if (f.cuts?.[node.key] || steps.includes('skip')) return out('cut', { stopped: !f.cuts?.[node.key] });
    // 走完了、却有一步一个字都没留下(既不是成也不是跳过):不知道,不是「完成」
    if (steps.includes('wait')) return out('unknown');
    return out('done');
  }
  if (steps.includes('run')) return out('run');
  if (steps.includes('unknown')) return out('unknown');
  if (steps.every(x => x === 'wait')) return model.record.running ? out('wait') : out('skip', { halt: true });
  if (steps.every(x => x === 'skip')) return out('skip', { halt: !!model.record.stopped });
  if (steps.includes('skip') || steps.includes('wait')) return out('cut', { stopped: true });
  return out('done');
}

function pipeFlowSpan(start, end) {
  const from = new Date(String(start || '').replace(' ', 'T')).getTime();
  const to = end ? new Date(String(end).replace(' ', 'T')).getTime() : Date.now();
  if (!Number.isFinite(from) || !Number.isFinite(to)) return '';
  const sec = Math.max(0, Math.round((to - from) / 1000));
  if (sec < 60) return `${sec} 秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${String(sec % 60).padStart(2, '0')} 秒`;
  return `${Math.floor(sec / 3600)} 小时 ${Math.floor(sec % 3600 / 60)} 分`;
}
const pipeFlowHm = stamp => String(stamp || '').slice(11, 16);

/** 「N 步有问题」那半句:有红就念,跟走到哪一步无关。 */
function pipeFlowBad(st) { return st.issues ? `${st.issues} 步有问题` : ''; }
const pipeFlowJoin = (...xs) => xs.filter(Boolean).join(' · ');

function pipeFlowSub(model, node) {
  const t = model.plan?.times?.[node.key], st = node.state, bad = pipeFlowBad(st);
  if (st.state === 'run') return pipeFlowJoin(bad, model.stopping ? '收尾中' : t ? `运行中 ${pipeFlowSpan(t.started_at)}` : '运行中');
  if (st.state === 'done') return bad || (t ? pipeFlowSpan(t.started_at, t.finished_at) || '已完成' : '已完成');
  if (st.state === 'cut') return pipeFlowJoin(bad, st.stopped ? '停在这里' : '提前结束');
  // 带着红的跳过念「其余跳过」:跳掉的只是还没做的那几步
  if (st.state === 'skip') return pipeFlowJoin(bad, st.halt ? '没跑' : bad ? '其余跳过' : '本次跳过');
  if (st.state === 'unknown') return pipeFlowJoin(bad, '结果未知');
  return pipeFlowJoin(bad, model.stopping ? '停止后不跑' : '等待');
}

function pipeFlowDot(node, i) {
  const st = node.state.state;
  // 正在跑的那个点是脉冲,里面不填字;其余只要有红就念红的条数
  if (st === 'run') return '';
  if (node.state.issues) return String(node.state.issues);
  if (st === 'done') return icon('check');
  if (st === 'cut') return PIPE_FLOW_SQUARE;
  if (st === 'skip') return PIPE_FLOW_DASH;
  if (st === 'unknown') return '?';
  return String(i + 1);
}

/** 「看第一个」那颗按钮:把选中挪到这一步所在的那一类(问题最先出现的那一类)。 */
function pipeFlowSelectStage(stage) {
  const model = pipeFlowModelNow;
  const hit = model?.nodes.find(n => n.steps.includes(stage) && n.state.issues)
    || model?.nodes.find(n => n.steps.includes(stage));
  if (!hit) return;
  pipeFlowSel = hit.key;
  renderPipeState(pipeSnapshot || { running: false });
}

function pipeFlowCuttable(model, node) {
  if (node.kind === 'daily') return false;
  // 来源那一路手上没有步骤 = 抓取做完了、导出排在全库那几步之后(没等它的那一路):
  // 没有「手上这一步」可以停,提前结束它只剩丢掉导出这一个后果
  const parked = Object.prototype.hasOwnProperty.call(model.plan?.lanes || {}, node.key) && !model.plan.lanes[node.key];
  const current = pipeFlowStepOf(model.plan, node.key) || node.steps[0];
  if (node.kind === 'src') return !parked && current === 'crawl' && node.steps.length > 1;
  return node.steps.indexOf(current) < node.steps.length - 1;
}

/** 全库那几步前面那道门此刻在等,而且等的是 `node` 这一类开跑(计划里第一个没跳过的全库那一类)。 */
function pipeFlowGateOn(model, node) {
  const f = model.plan;
  if (!model.live || f?.gate !== 'wait' || node.kind === 'src' || node.state.state !== 'wait') return false;
  return model.nodes.find(n => n.kind !== 'src' && n.state.state === 'wait') === node;
}

/** 门在等的那几类:还在跑、而且不是「本轮不等」的来源。 */
function pipeFlowWaitingOn(model) {
  const late = model.plan?.late || [];
  return model.nodes.filter(n => n.kind === 'src' && n.state.state === 'run' && !late.includes(n.key));
}
