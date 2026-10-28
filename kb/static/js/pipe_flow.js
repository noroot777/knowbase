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
const PIPE_FLOW_GENERATE = { name: '生成日报', desc: '汇总今天入库的内容，写成一份日报' };
const PIPE_FLOW_SKIP_WHY = {
  src: '跳过之后这一类这次不抓，下次同步接着抓，多半只是晚一轮进库；只给最近若干条的订阅源，隔得太久可能补不全。',
  ingest: '跳过之后，这一轮新抓的内容搜不到，日报也取不到它们（日报从入了库的内容里取材）。抓取期间已经在做的存量提取会照常收尾，做完的结果保留。',
  daily: '跳过之后，这一轮只同步、不出日报。今天那份日报保持原样。',
  tidy: '跳过之后，图鉴和三张图谱这次不更新。条目化只回看最近 3 天，连着跳几天会留下补不回来的空白。',
};
const PIPE_FLOW_SQUARE = '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><rect x="2" y="2" width="6" height="6" rx="1" fill="currentColor"/></svg>';
const PIPE_FLOW_DASH = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 6h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

// 选中的是哪一类。`pinned` = 人点过(之后不再自动跟着正在跑的那一类走);`view` 记的
// 是「这张卡讲的是哪一轮」,换了一轮就回到默认选择。
let pipeFlowSel = '', pipeFlowPinned = false, pipeFlowViewKey = null, pipeFlowModelNow = null;

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
    const span = model.plan?.times?.[node.key], running = model.plan?.running === node.key;
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
  if (f) {
    const times = f.times?.[node.key], done = (f.done || []).includes(node.key);
    if (f.running === node.key) return out('run');
    if ((f.skips || []).includes(node.key) && (done || !times)) return out('skip');
    // 没进过门的也可能是「不知道」:日报那一类不管停在哪都会交结论,收了尾的记录里缺它
    // (`pipeFlowGenerate` 念成 unknown)就不是「没跑」—— 这一句要先于下面那句「没跑」
    if (!times && !done && !steps.includes('unknown')) return model.live ? out('wait') : out('skip', { halt: true });
    if (issues) return out('issue', { cut: !!f.cuts?.[node.key] });
    if (steps.includes('unknown')) return out('unknown');
    if (f.cuts?.[node.key] || steps.includes('skip')) return out('cut', { stopped: !f.cuts?.[node.key] });
    // 走完了、却有一步一个字都没留下(既不是成也不是跳过):不知道,不是「完成」
    if (steps.includes('wait')) return out('unknown');
    return out('done');
  }
  if (steps.includes('run')) return out('run');
  if (issues) return out('issue');
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

function pipeFlowSub(model, node) {
  const t = model.plan?.times?.[node.key], st = node.state;
  if (st.state === 'run') return model.stopping ? '收尾中' : t ? `运行中 ${pipeFlowSpan(t.started_at)}` : '运行中';
  if (st.state === 'done') return t ? pipeFlowSpan(t.started_at, t.finished_at) || '已完成' : '已完成';
  if (st.state === 'issue') return `${st.issues} 步有问题`;
  if (st.state === 'cut') return st.stopped ? '停在这里' : '提前结束';
  if (st.state === 'skip') return st.halt ? '没跑' : '本次跳过';
  if (st.state === 'unknown') return '结果未知';
  return model.stopping ? '停止后不跑' : '等待';
}

function pipeFlowDot(node, i) {
  const st = node.state.state;
  if (st === 'done') return icon('check');
  if (st === 'issue') return String(node.state.issues);
  if (st === 'cut') return PIPE_FLOW_SQUARE;
  if (st === 'skip') return PIPE_FLOW_DASH;
  if (st === 'unknown') return '?';
  return st === 'run' ? '' : String(i + 1);
}

function pipeFlowDefault(model) {
  const pick = fn => model.nodes.find(fn)?.key;
  return pick(n => n.state.state === 'run')
    || pick(n => ['issue', 'unknown', 'cut'].includes(n.state.state)) || model.nodes[0]?.key || '';
}

/** 「看第一个」那颗按钮:把选中挪到这一步所在的那一类(问题最先出现的那一类)。 */
function pipeFlowSelectStage(stage) {
  const model = pipeFlowModelNow;
  const hit = model?.nodes.find(n => n.steps.includes(stage) && n.state.state === 'issue')
    || model?.nodes.find(n => n.steps.includes(stage));
  if (!hit) return;
  pipeFlowSel = hit.key; pipeFlowPinned = true;
  renderPipeState(pipeSnapshot || { running: false });
}

/** 画那条线和选中那一类的头;返回「哪几步露出来、每一步念什么」交给阶段行那一圈。 */
function renderPipeFlow(s, viewing) {
  const model = pipeFlowModel(s, viewing);
  pipeFlowModelNow = model;
  // 「这张卡讲的是哪一轮」要带上那一轮的身份:只写 live 的话,上一轮手点选中的那一类
  // 会一直钉到下一轮,新一轮不跟着正在跑的那一类走
  const viewKey = viewing ? 'v:' + viewing.key : 'live:' + (model.plan?.owner || s.run_id || '');
  if (viewKey !== pipeFlowViewKey) { pipeFlowViewKey = viewKey; pipeFlowPinned = false; }
  if (!pipeFlowPinned || !model.nodes.some(n => n.key === pipeFlowSel)) pipeFlowSel = pipeFlowDefault(model);
  const host = $('pipe-flow');
  host.hidden = !model.nodes.length;
  const node = model.nodes.find(n => n.key === pipeFlowSel);
  renderPipeFlowRail(model);
  renderPipeFlowHead(model, node);
  return {
    shows: key => !!node && node.steps.includes(key),
    step: key => node ? pipeFlowStep(model, node, key) : null,
    index: key => node ? node.steps.indexOf(key) : -1,
    time: key => {
      const t = node && model.plan?.times?.[node.key]?.steps?.[key];
      return t && t[1] ? pipeFlowSpan(t[0], t[1]) : '';
    },
  };
}

function renderPipeFlowRail(model) {
  const N = model.nodes.length, nSrc = model.srcCount;
  const rail = $('pipe-flow-rail'), groups = $('pipe-flow-groups');
  rail.style.setProperty('--n', N); groups.style.setProperty('--n', N);
  // 只有一两类时轨道不拉满,免得一个点孤零零挂在中间
  $('pipe-flow-track').style.maxWidth = N < 3 ? N * 180 + 'px' : '';
  const gHtml = N < 3 ? '' : (nSrc ? `<span style="grid-column:span ${nSrc}">来源 · <b>${nSrc}</b> 类，逐个跑</span>` : '')
    + (N > nSrc ? `<span style="grid-column:span ${N - nSrc}">全库 · <b>${N - nSrc}</b> 段${nSrc ? '，来源都跑完之后' : ''}</span>` : '');
  if (groups.innerHTML !== gHtml) groups.innerHTML = gHtml;
  const reached = ['done', 'issue', 'unknown', 'cut', 'run'];
  const front = model.nodes.reduce((m, n, i) => reached.includes(n.state.state) ? i : m, -1);
  const html = model.nodes.map((n, i) => {
    const cls = [i > 0 && i <= front ? 'pl' : '', i < front ? 'pr' : ''].join(' ').trim();
    const on = n.key === pipeFlowSel;
    return `<button type="button" class="pipe-flow-node${cls ? ' ' + cls : ''}" role="tab" data-node="${esc(n.key)}" data-s="${n.state.state}" aria-selected="${on}" tabindex="${on ? 0 : -1}">`
      + `<span class="pipe-flow-dot">${pipeFlowDot(n, i)}</span><span class="pipe-flow-nm">${esc(n.label)}</span>`
      + `<span class="pipe-flow-sub">${esc(pipeFlowSub(model, n))}</span></button>`;
  }).join('');
  // 内容没变就不碰 DOM:轮询每 1.5 秒一跳,重建会把键盘焦点从节点上拿走
  if (rail.innerHTML !== html) {
    const focused = document.activeElement?.closest?.('.pipe-flow-node')?.dataset.node;
    rail.innerHTML = html;
    if (focused) rail.querySelector(`[data-node="${CSS.escape(focused)}"]`)?.focus();
  }
  requestAnimationFrame(placePipeFlowNotch);
}

function placePipeFlowNotch() {
  const node = $('pipe-flow-rail').querySelector('[aria-selected="true"]'), detail = $('pipe-flow-detail');
  if (!node || !detail) return;
  const box = detail.getBoundingClientRect(), r = node.getBoundingClientRect();
  $('pipe-flow-notch').style.left = (r.left + r.width / 2 - box.left - 7) + 'px';
}

function pipeFlowCuttable(model, node) {
  if (node.kind === 'daily') return false;
  const current = model.plan?.step || node.steps[0];
  if (node.kind === 'src') return current === 'crawl' && node.steps.length > 1;
  return node.steps.indexOf(current) < node.steps.length - 1;
}

function renderPipeFlowHead(model, node) {
  const head = $('pipe-flow-dhead'), why = $('pipe-flow-why'), extra = $('pipe-flow-extra');
  if (!node) { head.innerHTML = ''; why.hidden = true; extra.innerHTML = ''; return; }
  const st = node.state, t = model.plan?.times?.[node.key];
  const at = model.nodes.indexOf(node);
  const next = model.nodes.slice(at + 1).find(n => n.state.state === 'wait');
  const before = model.nodes.slice(0, at).reverse().find(n => n.state.state !== 'skip');
  const can = model.live && !model.stopping && pipeConnected && !pipeAction;
  let tag, facts, act = '', note = '';
  if (st.state === 'run') {
    tag = `<span class="pipe-flow-tag run">${model.stopping ? '收尾中' : '执行中'}</span>`;
    facts = t ? `${esc(pipeFlowHm(t.started_at))} 开始 · 已运行 <b>${esc(pipeFlowSpan(t.started_at))}</b>` : '正在跑';
    if (can && pipeFlowCuttable(model, node)) act = `<button type="button" class="ghost" data-flow="cut">结束${esc(node.label)}${next ? `，开始${esc(next.label)}` : ''} ${arrow('right')}</button>`;
    else if (model.live && !model.stopping) note = node.kind === 'daily' ? '日报已经开始生成，没法中途结束，整轮也停不下来（模型已经在出稿）；做完会接着往下走。' : '';
  } else if (st.state === 'wait') {
    tag = `<span class="pipe-flow-tag mut">${model.stopping ? '停止后不跑' : '等待'}</span>`;
    facts = model.stopping ? '整轮停止之后不会轮到它' : before ? `排在 ${esc(before.label)} 后面，轮到它会自动开始` : '轮到它会自动开始';
    if (can) act = '<button type="button" class="ghost" data-flow="skip">本次跳过</button>';
    if (!model.stopping) note = PIPE_FLOW_SKIP_WHY[node.kind] || '';
  } else if (st.state === 'skip') {
    tag = `<span class="pipe-flow-tag mut">${st.halt ? '没跑' : '本次跳过'}</span>`;
    const undo = model.live && !(model.plan.done || []).includes(node.key);
    facts = undo ? '轮到它之前都可以改回来' : st.halt ? (model.record.stopped ? '整轮停止了，没轮到它' : '这一轮没轮到它') : '这一轮按你的选择跳过了';
    if (undo && can) act = '<button type="button" class="ghost" data-flow="unskip">恢复执行</button>';
    if (undo) note = PIPE_FLOW_SKIP_WHY[node.kind] || '';
  } else {
    tag = st.state === 'cut' ? `<span class="pipe-flow-tag warn">${st.stopped ? '停在这里' : '提前结束'}</span>`
      : st.issues ? `<span class="pipe-flow-tag err">已完成 · ${st.issues} 步有问题</span>`
      : st.state === 'unknown' ? '<span class="pipe-flow-tag warn">结果未知</span>'
      : '<span class="pipe-flow-tag ok">已完成 · 没有问题</span>';
    facts = t ? `${esc(pipeFlowHm(t.started_at))} – ${esc(pipeFlowHm(t.finished_at))} · 耗时 <b>${esc(pipeFlowSpan(t.started_at, t.finished_at))}</b>` : '';
    const cut = model.plan?.cuts?.[node.key];
    if (cut) note = `你在「${STAGE_META[cut.step]?.name || cut.step}」时结束了${node.label}。已经抓到的内容留在库里，没做完的下次同步会接着做。`;
    else if (st.state === 'unknown') note = '这一轮没留下这一类自己的结果，成没成说不准；下面那几步和运行日志里有当时的记录。';
  }
  const only = model.nodes.length === 1 && node.kind === 'src' ? `${facts ? ' · ' : ''}本次只同步这一类` : '';
  const html = `<div><h3>${esc(node.label)} ${tag}</h3><p>${facts}${only}</p></div><div class="pipe-flow-act">${act}</div>`;
  // 已运行的秒数每一跳都变,整段重建会把键盘焦点从按钮上拿走(同轨道那一处)
  if (head.innerHTML !== html) {
    const focused = document.activeElement?.closest?.('#pipe-flow-dhead [data-flow]')?.dataset.flow;
    head.innerHTML = html;
    if (focused) head.querySelector(`[data-flow="${CSS.escape(focused)}"]`)?.focus();
  }
  why.hidden = !note;
  if (why.textContent !== note) why.textContent = note;
  // 「生成日报」不是管道阶段,没有阶段行可挪:它那一步单独画在这里
  const gen = node.steps.includes('generate') ? pipeFlowGenerateRow(model, node) : '';
  if (extra.innerHTML !== gen) extra.innerHTML = gen;
}

function pipeFlowGenerateRow(model, node) {
  const st = pipeFlowStep(model, node, 'generate');
  const status = pipeHasFailure(st) ? 'error' : st.status || '';
  return `<div class="pipe-stage" data-status="${esc(status)}"><span class="pipe-stage-number">1</span>`
    + `<div><h3>${PIPE_FLOW_GENERATE.name}</h3><p class="pipe-stage-desc">${PIPE_FLOW_GENERATE.desc}</p>`
    + `<p class="pipe-stage-detail">${esc(st.detail || '')}</p></div>`
    + `<span class="pipe-stage-status">${esc(PIPE_STATUS[status] || '状态未知')}</span></div>`;
}

async function pipeFlowAct(action) {
  const model = pipeFlowModelNow, node = model?.nodes.find(n => n.key === pipeFlowSel);
  if (!model?.live || !node || pipeAction) return;
  if (action === 'cut') {
    const at = model.nodes.indexOf(node), next = model.nodes.slice(at + 1).find(n => n.state.state === 'wait');
    const step = STAGE_META[model.plan.step]?.name || '手上这一步';
    if (!await confirmBox({
      title: next ? `结束 ${node.label}，直接开始${next.label}？` : `提前结束 ${node.label}？`,
      body: `正在做的「${step}」停在下一个能停的地方（有的来源要等这一整步做完），${node.label}后面的步骤这次不跑。已经抓到的内容留在库里，没做完的下次同步接着做。这一轮其余类型照常往下跑。`,
      ok: `结束 ${node.label}`,
    })) return;
  }
  pipeAction = 'flow'; renderPipeButtons();
  try {
    await pipePost('/api/pipeline/flow', { owner: model.plan.owner, node: node.key, action });
    const said = { skip: [`${node.label} 这次跳过`, '轮到它之前都可以点「恢复执行」改回来。'],
      unskip: [`${node.label} 恢复执行`, ''], cut: [`已请求结束 ${node.label}`, '手上这一步停下后接着下一类。'] }[action];
    toast(said[0], { key: 'pipeline', type: 'ok', detail: said[1] });
  } catch (e) {
    toast('没有受理', { key: 'pipeline', detail: e.message });
  } finally {
    pipeAction = ''; invalidatePipeStatus(); await refreshPipe(); renderPipeButtons();
  }
}

$('pipe-flow-rail').addEventListener('click', e => {
  const btn = e.target.closest('.pipe-flow-node');
  if (!btn) return;
  pipeFlowSel = btn.dataset.node; pipeFlowPinned = true;
  renderPipeState(pipeSnapshot || { running: false });
});
$('pipe-flow-rail').addEventListener('keydown', e => {
  const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
  const nodes = pipeFlowModelNow?.nodes || [];
  if (!step || !nodes.length) return;
  const i = Math.max(0, Math.min(nodes.length - 1, nodes.findIndex(n => n.key === pipeFlowSel) + step));
  pipeFlowSel = nodes[i].key; pipeFlowPinned = true;
  renderPipeState(pipeSnapshot || { running: false });
  $('pipe-flow-rail').querySelector(`[data-node="${CSS.escape(pipeFlowSel)}"]`)?.focus();
});
$('pipe-flow-dhead').addEventListener('click', e => {
  const btn = e.target.closest('[data-flow]');
  if (btn && !btn.disabled) pipeFlowAct(btn.dataset.flow);
});
addEventListener('resize', placePipeFlowNotch);
