// ── 同步中心那张卡上「按类型排的那几行」:名字 | 一根细条 | 此刻那句 | 耗时 | 动作 ──────
// 状态归约在 `pipe_flow.js`,本文件只画;硬约束在 docs/decisions/sync-flow.md 第一节。
//
// 来源那几类是**同时跑**的,所以细条全轮共用一个比例尺:条的位置是它什么时候开始、长短是
// 跑了多久,末端在闪的还在跑。点一行在它下面展开那几步 —— 阶段行 `#pipe-stages` 挂在展开
// 那一块里(失败明细、「不再抓取」都还在那几行上),那一块始终在文档里,只挪位置。

// 展开那一块和来源/全库之间那条分隔线:始终是同一个节点,只挪位置(重建会丢掉里面的焦点、
// 明细的展开与滚动位置)
const PIPE_FLOW_DETAIL = $('pipe-flow-detail');
const PIPE_FLOW_SEP = Object.assign(document.createElement?.('div') || {}, { className: 'pipe-flow-sep' });
const pipeFlowMs = stamp => new Date(String(stamp || '').replace(' ', 'T')).getTime();

/** 全轮共用的那根尺:{t0, len}(毫秒)。没有时刻(老留档推出来的那几类)就没有尺。 */
function pipeFlowScale(model) {
  const spans = Object.values(model.plan?.times || {});
  const starts = spans.map(t => pipeFlowMs(t.started_at)).filter(Number.isFinite);
  if (!starts.length) return null;
  const now = Date.now();
  const ends = spans.map(t => t.finished_at ? pipeFlowMs(t.finished_at) : model.live ? now : NaN)
    .filter(Number.isFinite);
  const t0 = Math.min(...starts), t1 = Math.max(t0 + 60000, ...ends);
  // 还在跑的那一轮右边留一截,尺子不必每一跳都顶到头
  return { t0, len: (t1 - t0) * (model.live ? 1.2 : 1) };
}

function pipeFlowBar(model, node, scale) {
  const t = model.plan?.times?.[node.key];
  const from = pipeFlowMs(t?.started_at);
  if (!scale || !Number.isFinite(from)) return '';
  const to = t.finished_at ? pipeFlowMs(t.finished_at) : Date.now();
  const left = (from - scale.t0) / scale.len * 100, width = Math.max((to - from) / scale.len * 100, 0.6);
  return `<i style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%"></i>`;
}

/** 这一行此刻那句话。门在等的那一行写在等谁、等了多久。 */
function pipeFlowSay(model, node) {
  const f = model.plan, st = node.state, bad = pipeFlowBad(st);
  if (pipeFlowGateOn(model, node)) {
    const names = pipeFlowWaitingOn(model).map(n => n.label).join('、');
    return pipeFlowJoin(bad, `等 ${names || '来源'} 跑完`, f.gate_at ? `已等 ${pipeFlowSpan(f.gate_at)}` : '');
  }
  if (st.state === 'run') {
    if (model.stopping) return pipeFlowJoin(bad, '收尾中');
    const step = pipeFlowStepOf(f, node.key);
    // 没等它的那一路:抓取做完了,导出挪到全库那几步之后(`pipeline_sources.Lanes.hold`)
    if (node.kind === 'src' && f && !step) return pipeFlowJoin(bad, '抓取做完了，导出排在入库处理之后');
    if (!step) return pipeFlowJoin(bad, '运行中');
    const one = pipeFlowStep(model, node, step);
    return pipeFlowJoin(bad, STAGE_META[step]?.name || PIPE_FLOW_GENERATE.name, one.detail || '运行中');
  }
  if (st.state === 'done') {
    const said = node.steps.map(k => pipeFlowStep(model, node, k)).filter(x => !bad || pipeHasFailure(x))
      .map(x => x.detail).filter(Boolean);
    return pipeFlowJoin(bad, ...said) || '已完成';
  }
  return pipeFlowSub(model, node);
}

/** 这一行的耗时(还在跑的算到此刻)。 */
function pipeFlowTook(model, node) {
  const t = model.plan?.times?.[node.key];
  return t?.started_at ? pipeFlowSpan(t.started_at, t.finished_at) : '';
}

const pipeFlowCan = model => model.live && !model.stopping && pipeConnected && !pipeAction;

/** 行上那一颗(主要的那个动作)。展开之后的余下几颗在 `pipeFlowHead` 里。 */
function pipeFlowRowAct(model, node) {
  if (!pipeFlowCan(model)) return '';
  const st = node.state.state, btn = (act, text) =>
    `<button type="button" class="ghost" data-flow="${act}" data-node="${esc(node.key)}">${text}</button>`;
  if (pipeFlowGateOn(model, node)) return btn('go', '不等了，继续');
  if (st === 'run') return pipeFlowCuttable(model, node) ? btn('cut', node.kind === 'src' ? '结束本类型' : '提前结束') : '';
  if (st === 'wait') return btn('skip', '本次跳过');
  if (st === 'skip' && !(model.plan?.done || []).includes(node.key)) return btn('unskip', '恢复执行');
  return '';
}

/** 展开那一块顶上那一行:补充说明 + 行上没放下的动作 + 只看这一类的日志。 */
function pipeFlowHead(model, node) {
  const st = node.state, t = model.plan?.times?.[node.key];
  const at = model.nodes.indexOf(node);
  const before = model.nodes.slice(0, at).reverse().find(n => n.state.state !== 'skip');
  const bad = pipeFlowBad(st);
  let facts = '', act = '', note = '';
  if (st.state === 'run') {
    facts = t ? `${esc(pipeFlowHm(t.started_at))} 开始` : '';
    if (model.live && !model.stopping && node.kind === 'daily') note = '日报已经开始生成，没法中途结束，整轮也停不下来（模型已经在出稿）；做完会接着往下走。';
  } else if (st.state === 'wait') {
    const gate = pipeFlowGateOn(model, node);
    facts = model.stopping ? '整轮停止之后不会轮到它'
      : gate ? '来源都跑完才开始；不等的话，没跑完的那几类照常跑完、照常记账，新内容进下一轮入库'
        : node.kind === 'src' ? '和别的来源同时开始'
          : before?.kind === 'src' ? '来源都跑完之后自动开始'
            : before ? `排在 ${esc(before.label)} 后面，轮到它会自动开始` : '轮到它会自动开始';
    // 门在等的那一行,行上那颗是「不等了」,跳过放在这里
    if (gate && pipeFlowCan(model)) act = `<button type="button" class="ghost" data-flow="skip" data-node="${esc(node.key)}">本次跳过</button>`;
    if (!model.stopping) note = PIPE_FLOW_SKIP_WHY[node.kind] || '';
  } else if (st.state === 'skip') {
    const undo = model.live && !(model.plan.done || []).includes(node.key);
    facts = undo ? (bad ? '余下这几步按你的选择跳过，轮到它之前都可以改回来' : '轮到它之前都可以改回来')
      : st.halt ? (model.record.stopped ? '整轮停止了，没轮到它' : '这一轮没轮到它')
        : bad ? '余下这几步按你的选择跳过了，已经发生的结果照实留着' : '这一轮按你的选择跳过了';
    if (undo) note = PIPE_FLOW_SKIP_WHY[node.kind] || '';
  } else {
    facts = t ? `${esc(pipeFlowHm(t.started_at))} – ${esc(pipeFlowHm(t.finished_at))}` : '';
    const cut = model.plan?.cuts?.[node.key];
    if (cut) note = `你在「${STAGE_META[cut.step]?.name || cut.step}」时结束了${node.label}。已经抓到的内容留在库里，没做完的下次同步会接着做。`;
    else if (st.state === 'unknown') note = '这一轮没留下这一类自己的结果，成没成说不准；下面那几步和运行日志里有当时的记录。';
  }
  if ((model.plan?.late || []).includes(node.key)) facts = pipeFlowJoin(facts, '这一轮的入库和日报没等它，它的新内容进下一轮入库');
  if (model.nodes.length === 1 && node.kind === 'src') facts = pipeFlowJoin(facts, '本次只同步这一类');
  const who = node.kind === 'src' ? node.channel : '';
  const log = `<button type="button" class="text-button" data-log="${esc(who)}">只看这一类的日志</button>`;
  return { html: `<p>${facts}</p><div class="pipe-flow-act">${act}${log}</div>`, note };
}

function pipeFlowRowHtml(model, node, scale) {
  const late = (model.plan?.late || []).includes(node.key) ? '<span class="pipe-flow-late">本轮不等 · </span>' : '';
  return `<span class="pipe-flow-nm">${esc(node.label)}</span><span class="pipe-flow-bar">${pipeFlowBar(model, node, scale)}</span>`
    + `<span class="pipe-flow-say">${late}${esc(pipeFlowSay(model, node))}</span>`
    + `<span class="pipe-flow-t">${esc(pipeFlowTook(model, node))}</span><span class="pipe-flow-rowact">${pipeFlowRowAct(model, node)}</span>`;
}

/** 画那几行和展开的那一块;返回「哪几步露出来、每一步念什么」交给阶段行那一圈。 */
function renderPipeFlow(s, viewing) {
  const model = pipeFlowModel(s, viewing);
  pipeFlowModelNow = model;
  // 「这张卡讲的是哪一轮」要带上那一轮的身份:只写 live 的话,上一轮手点展开的那一类
  // 会一直钉到下一轮
  const viewKey = viewing ? 'v:' + viewing.key : 'live:' + (model.plan?.owner || s.run_id || '');
  if (viewKey !== pipeFlowViewKey) { pipeFlowViewKey = viewKey; pipeFlowSel = ''; }
  if (pipeFlowSel && !model.nodes.some(n => n.key === pipeFlowSel)) pipeFlowSel = '';
  $('pipe-flow').hidden = !model.nodes.length;
  const node = model.nodes.find(n => n.key === pipeFlowSel);
  renderPipeFlowRows(model);
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

function renderPipeFlowRows(model) {
  const rail = $('pipe-flow-rail'), scale = pipeFlowScale(model);
  const have = new Map([...rail.children].filter(el => el.dataset.node).map(el => [el.dataset.node, el]));
  const want = [];
  model.nodes.forEach((n, i) => {
    if (i && n.kind !== 'src' && model.nodes[i - 1].kind === 'src') want.push(PIPE_FLOW_SEP);
    let row = have.get(n.key);
    if (!row) {
      row = document.createElement('div');
      row.className = 'pipe-flow-row'; row.dataset.node = n.key; row.tabIndex = 0; row.setAttribute('role', 'button');
    }
    const open = n.key === pipeFlowSel;
    row.dataset.s = pipeFlowGateOn(model, n) ? 'gate' : n.state.state;
    row.toggleAttribute('data-issues', !!n.state.issues);
    row.setAttribute('aria-expanded', String(open));
    const html = pipeFlowRowHtml(model, n, scale);
    // 内容没变就不碰 DOM:轮询每 1.5 秒一跳,重建会把键盘焦点从按钮上拿走
    if (row._html !== html) {
      const focused = document.activeElement?.closest?.('[data-flow]');
      const again = focused && row.contains(focused) ? focused.dataset.flow : '';
      row.innerHTML = html; row._html = html;
      if (again) row.querySelector(`[data-flow="${CSS.escape(again)}"]`)?.focus();
    }
    want.push(row);
    if (open) want.push(PIPE_FLOW_DETAIL);
  });
  PIPE_FLOW_DETAIL.hidden = !want.includes(PIPE_FLOW_DETAIL);
  if (PIPE_FLOW_DETAIL.hidden) want.push(PIPE_FLOW_DETAIL);
  // 按顺序摆,只挪位置不对的那几个
  let at = rail.firstElementChild;
  for (const el of want) {
    if (el === at) at = at.nextElementSibling;
    else rail.insertBefore(el, at);
  }
  while (at) { const next = at.nextElementSibling; at.remove(); at = next; }
}

function renderPipeFlowHead(model, node) {
  const head = $('pipe-flow-dhead'), why = $('pipe-flow-why'), extra = $('pipe-flow-extra');
  if (!node) { head.innerHTML = ''; why.hidden = true; extra.innerHTML = ''; return; }
  const { html, note } = pipeFlowHead(model, node);
  if (head.innerHTML !== html) head.innerHTML = html;
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
    + `<div><h3>${PIPE_FLOW_GENERATE.name}</h3>`
    + `<p class="pipe-stage-detail">${esc(st.detail || '')}</p></div>`
    + `<span class="pipe-stage-status">${esc(PIPE_STATUS[status] || '状态未知')}</span></div>`;
}

async function pipeFlowAct(action, key) {
  const model = pipeFlowModelNow, node = model?.nodes.find(n => n.key === key);
  if (!model?.live || !node || pipeAction) return;
  if (action === 'cut') {
    const step = STAGE_META[pipeFlowStepOf(model.plan, node.key)]?.name || '手上这一步';
    if (!await confirmBox({
      title: `提前结束 ${node.label}？`,
      body: `正在做的「${step}」停在下一个能停的地方（有的来源要等这一整步做完），${node.label}后面的步骤这次不跑。已经抓到的内容留在库里，没做完的下次同步接着做。这一轮其余类型照常往下跑。`,
      ok: `结束 ${node.label}`,
    })) return;
  }
  pipeAction = 'flow'; renderPipeButtons();
  try {
    await pipePost('/api/pipeline/flow', { owner: model.plan.owner, node: node.key, action });
    const said = { skip: `${node.label} 这次跳过`, unskip: `${node.label} 恢复执行`,
      cut: `已请求结束 ${node.label}`, go: `不等了，接着${node.label}` }[action];
    toast(said, { key: 'pipeline', type: 'ok' });
  } catch (e) {
    toast('没有受理', { key: 'pipeline', detail: e.message });
  } finally {
    pipeAction = ''; invalidatePipeStatus(); await refreshPipe(); renderPipeButtons();
  }
}

function pipeFlowToggle(key) {
  pipeFlowSel = pipeFlowSel === key ? '' : key;
  renderPipeState(pipeSnapshot || { running: false });
}

$('pipe-flow-rail').addEventListener('click', e => {
  const act = e.target.closest('[data-flow]');
  if (act) { if (!act.disabled) pipeFlowAct(act.dataset.flow, act.dataset.node || pipeFlowSel); return; }
  const log = e.target.closest('[data-log]');
  if (log) { showPipeLogOf(log.dataset.log); return; }
  // 展开那一块里的点击(明细、链接、「不再抓取」)不收起它
  if (e.target.closest('#pipe-flow-detail')) return;
  const row = e.target.closest('.pipe-flow-row');
  if (row) pipeFlowToggle(row.dataset.node);
});
$('pipe-flow-rail').addEventListener('keydown', e => {
  const row = e.target.closest?.('.pipe-flow-row');
  if (!row || e.target !== row || !['Enter', ' '].includes(e.key)) return;
  e.preventDefault();
  pipeFlowToggle(row.dataset.node);
  $('pipe-flow-rail').querySelector(`.pipe-flow-row[data-node="${CSS.escape(row.dataset.node)}"]`)?.focus();
});
