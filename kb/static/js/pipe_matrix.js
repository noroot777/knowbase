// ── 历史运行那张表:一行一轮,一列一类 ────────────────────────────────────────────
// 列就是同步中心那条线上的类型节点(`pipe_flow.js` 的 `pipeFlowModel`,口径只有那一份),
// 同一列往下看就知道这一类是不是天天出问题。行本身(`.pipe-history-row`、选中、
// 「最新」那一枚、点击核验)仍归 `pipe_history.js`,这里只管一行里面画什么。
//
// 列的集合按**这一屏所有的行**现算(正在跑那一轮 + 留档那几轮):表头、活的那一行、
// 留档那几行必须是同一组列,否则格子对不上表头。
let pipeMatrixColsNow = [], pipeMatrixSig = '';
const PIPE_MATRIX_ORDER = ['ingest', 'daily', 'tidy'];

function pipeMatrixModel(run, live) {
  try {
    return live ? pipeFlowModel(pipeSnapshot || { running: false }, null)
      : pipeFlowModel({ running: false, last_run: run }, run);
  } catch { return { nodes: [] }; }
}

/** 留档那一行画哪一份:**它就是此刻屏幕上那一轮(`pipeIsCurrent`)时画活的那一份**,同概况
 *  那张卡 —— 留档那份是收尾那一刻抄下的(日报那一批还要接着生成),点它回到的也是活的那份。 */
function pipeMatrixModelOf(run) {
  return pipeMatrixModel(run, pipeIsCurrent(run));
}

/** 这一屏的行(新的在上):活的那一轮(在跑时)+ 留档那几轮。 */
function pipeMatrixRows() {
  const rows = pipeRunning() ? [{ live: true, model: pipeMatrixModel(null, true) }] : [];
  for (const run of pipeHistory) {
    if (run && !run.invalid) rows.push({ run, model: pipeMatrixModelOf(run) });
  }
  return rows;
}

function pipeMatrixCols(rows) {
  const src = [], groups = new Map();
  for (const { model } of rows) {
    for (const n of model.nodes) {
      if (n.kind === 'src') { if (!src.some(c => c.key === n.key)) src.push({ key: n.key, label: n.label, src: true }); }
      else if (!groups.has(n.key)) groups.set(n.key, { key: n.key, label: n.label });
    }
  }
  return [...src, ...PIPE_MATRIX_ORDER.filter(k => groups.has(k)).map(k => groups.get(k))];
}

function pipeMatrixTip(model, n) {
  const t = model.plan?.times?.[n.key], took = t ? pipeFlowSpan(t.started_at, t.finished_at) : '';
  const st = n.state;
  if (st.state === 'done') return took ? `完成 · ${took}` : '完成';
  if (st.state === 'issue') return `${st.issues} 步有问题${took ? ' · ' + took : ''}`;
  if (st.state === 'cut') return st.stopped ? '停在这里' : '提前结束';
  if (st.state === 'unknown') return '结果未知';
  if (st.state === 'run') return '正在跑';
  if (st.state === 'wait') return '等待';
  return st.halt ? '没跑' : '本次跳过';
}

function pipeMatrixCells(model) {
  return pipeMatrixColsNow.map(col => {
    const i = model.nodes.findIndex(x => x.key === col.key), n = model.nodes[i];
    if (!n) return `<span class="pipe-matrix-c" title="这一轮没选${esc(col.label)}"><span class="pipe-matrix-dot none"></span></span>`;
    const glyph = ['run', 'wait'].includes(n.state.state) ? '' : pipeFlowDot(n, i);
    return `<span class="pipe-matrix-c" data-node="${esc(n.key)}" title="${esc(col.label)} · ${esc(pipeMatrixTip(model, n))}">`
      + `<span class="pipe-matrix-dot" data-s="${n.state.state}">${glyph}</span></span>`;
  }).join('');
}

/** 一行里面的那几格。`when` / `result` / `where` 几个类名照旧(探针按它们读字)。 */
function pipeMatrixRowBody({ when, kind, where, model, took, result, chosen }) {
  return `<span class="pipe-history-when">${esc(when)}</span>`
    + `<span class="pipe-history-kind">${esc(kind)}<span class="pipe-history-where">${esc(where)}</span></span>`
    + pipeMatrixCells(model)
    + `<span class="pipe-history-dur">${esc(took)}</span>`
    + `<span class="pipe-history-result">${esc(result)}</span>`
    + `<span class="pipe-history-go">${chosen ? '正在看' : arrow('right')}</span>`;
}

function pipeMatrixRowFor(run, chosen) {
  return pipeMatrixRowBody({ when: pipeRunWhen(run), kind: pipeRunLabel(run), where: pipeRunWhere(run),
    model: pipeMatrixModelOf(run), took: pipeDuration(run.started_at, run.finished_at),
    result: pipeRunSummary(run).text, chosen });
}

function pipeMatrixLiveFor(run, chosen) {
  // 日报那一批里正在跑的是它的一轮子管道(那一轮自己的 run_kind 是同步),身份看计划
  const model = pipeMatrixModel(null, true);
  return pipeMatrixRowBody({ when: pipeRunWhen({ started_at: run.started_at }),
    kind: model.plan?.kind === 'daily' ? '日报' : pipeRunLabel(run), where: pipeRunWhere(run), model,
    took: run.started_at ? '已跑 ' + pipeDuration(run.started_at, '') : '', result: '正在运行', chosen });
}

/** 列变了没有(活的那一轮多出一类时表头要跟着重画)。 */
function pipeMatrixStale() {
  const cols = pipeMatrixCols(pipeMatrixRows());
  return cols.map(c => c.key).join('|') !== pipeMatrixSig;
}

function renderPipeMatrixFrame() {
  const rows = pipeMatrixRows();
  pipeMatrixColsNow = pipeMatrixCols(rows);
  pipeMatrixSig = pipeMatrixColsNow.map(c => c.key).join('|');
  const panel = document.querySelector('.pipe-matrix');
  panel?.style.setProperty('--cols', `136px 56px repeat(${pipeMatrixColsNow.length}, minmax(44px, 1fr)) 84px 108px 48px`);
  const reps = pipeMatrixRepeats(rows), hot = new Set(reps.map(r => r.key));
  const nSrc = pipeMatrixColsNow.filter(c => c.src).length, nLib = pipeMatrixColsNow.length - nSrc;
  const head = $('pipe-history-head');
  head.innerHTML = !rows.length ? '' : (pipeMatrixColsNow.length > 2
    ? '<div class="pipe-matrix-g"><span></span><span></span>'
      + (nSrc ? `<span style="grid-column:span ${nSrc}">来源</span>` : '')
      + (nLib ? `<span style="grid-column:span ${nLib}">全库</span>` : '') + '</div>' : '')
    + '<div class="pipe-matrix-h"><span>时间</span><span>类型</span>'
    + pipeMatrixColsNow.map(c => `<span class="c${hot.has(c.key) ? ' rep' : ''}">${esc(c.label)}</span>`).join('')
    + '<span>耗时</span><span>结果</span><span></span></div>';
  $('pipe-history-repeats').innerHTML = reps.map(r =>
    `<p class="pipe-matrix-note"><span><b>${esc(r.label)}</b> 连着 ${r.k} 轮`
    + (r.title ? `都是「${esc(r.title)}」，不像偶发，重试多半不顶用` : '都有问题') + '</span></p>').join('');
}

// 同一类连着几轮卡在同一处:不像偶发,在表底下点出来。从最新那一轮往下数,碰到一轮
// 这一类好好跑完就断;没选它、跳过、提前结束、还没轮到的那几轮不算数,也不算断。
function pipeMatrixRepeats(rows) {
  return pipeMatrixColsNow.flatMap(col => {
    const hit = [];
    for (const { model } of rows) {
      const n = model.nodes.find(x => x.key === col.key);
      if (!n || !['done', 'issue'].includes(n.state.state)) continue;
      if (n.state.state === 'done') break;
      hit.push(pipeMatrixWhy(model, n));
    }
    if (hit.length < 2) return [];
    const same = hit.every(h => h.sig === hit[0].sig);
    return [{ key: col.key, label: col.label, k: hit.length, title: same ? hit[0].text : '' }];
  });
}

/** 这一类这一轮卡在哪:第一步出问题的那一步 + 它第一条原因。比的时候把数字抹掉
 *  (「2 个账号」和「3 个账号」是同一种问题),念的是最新那一轮的原话。 */
function pipeMatrixWhy(model, n) {
  for (const k of n.steps) {
    const st = pipeFlowStep(model, n, k);
    if (!pipeHasFailure(st)) continue;
    const reason = String((st.failures || [])[0]?.reason || st.detail || '').slice(0, 40);
    const text = `${STAGE_META[k]?.name || PIPE_FLOW_GENERATE.name}${reason ? '：' + reason : ''}`;
    return { text, sig: text.replace(/\d+/g, 'N') };
  }
  return { text: '', sig: '' };
}

/** 点在某一格上:先按那一行换轮次(行自己的点击核验照旧),再把选中落到那一类。
 *
 *  **行的语义一点不改**:同一行再点一次照旧是「回到最新」(`viewPipeRun`)。只有点在
 *  这一行已经摊开、而选中的不是这一类的那一格上,才只换选中 —— 再点同一格就退回行
 *  的语义,不然这一行就再也点不回最新了。
 */
function pipeMatrixPick(e, row) {
  const node = e.target.closest('[data-node]')?.dataset.node;
  const already = row.getAttribute('aria-current') === 'true';
  if (node && already && node !== pipeFlowSel) return pipeMatrixSelect(node);
  if (row.dataset.run) pickPipeRun(row.dataset.run); else exitPipeHistory();
  if (node && !already) pipeMatrixSelect(node);
}

function pipeMatrixSelect(node) {
  pipeFlowSel = node; pipeFlowPinned = true;
  renderPipeState(pipeSnapshot || { running: false });
  const card = $('pipe-overview');
  if (card && card.getBoundingClientRect().top < 0) card.scrollIntoView({ block: 'start' });
}
