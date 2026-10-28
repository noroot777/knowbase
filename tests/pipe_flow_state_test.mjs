// 类型状态归约那一层的不变量:**`state` 只答「走到哪了」,`issues` 答「有没有出问题」,
// 两样正交、谁也不许代表谁**(`pipe_flow.js` 的 `pipeFlowState`,硬约束见
// docs/decisions/sync-flow.md 第一节)。两个方向都会坏,所以两边都要钉:
//   · 生命周期盖住失败 → 跳过余下几步把已经发生的红抹掉,四处一致念「本次跳过」;
//   · 失败冒充生命周期 → 还没轮到的一类因为并行那一步红了就念「已完成」,「本次跳过」
//     那颗按钮当场消失,后面那一类也不再把它算成前面要跑的。
// 这一份 state 被四处共用(轨道点与副标题、详情头那枚标签、历史表那一格、「连着几轮卡在
// 同一处」那个归约),所以这份回归一次把四个消费方都问一遍,并且反过来钉一边:没有失败
// 的跳过照旧念「本次跳过」。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const root = new URL('../kb/static/js/', import.meta.url);
const read = name => readFileSync(new URL(name, root), 'utf8');
// 失败判据和「这一轮计划了哪几步」只认实现里那一份,不在这里另写一遍
const view = read('pipe_view.js');
const borrowed = [/const STAGE_META = \{[\s\S]*?\n\};/.exec(read('pipe.js'))[0],
  /function pipeHasFailure[\s\S]*?\n\}/.exec(view)[0],
  /function pipeResult[^\n]*\n/.exec(view)[0],
  /function pipePlannedStages[\s\S]*?\n\}/.exec(view)[0]].join('\n');

function rig() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      elements.set(id, { id, innerHTML: '', textContent: '', hidden: false, dataset: {},
        addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], focus() {},
        getBoundingClientRect: () => ({ top: 0 }), style: { setProperty() {} } });
    }
    return elements.get(id);
  };
  const ctx = vm.createContext({ console, Date, CSS: { escape: v => v }, $: element,
    document: { activeElement: null, querySelector: () => null, querySelectorAll: () => [] },
    addEventListener() {}, esc: v => String(v ?? ''), icon: k => `<i>${k}</i>`, arrow: k => `<i>${k}</i>`,
    channelLabel: id => id.toUpperCase(), PIPE_STATUS: {}, pipeConnected: true, pipeAction: '',
    pipeSnapshot: null, pipeHistory: [], pipeLibraryOnly: () => false,
    pipeRunScopes: r => r.scopes || [], pipeIsCurrent: () => false, pipeRunning: () => false,
    renderPipeState() {}, renderPipeButtons() {}, toast() {}, confirmBox: async () => false });
  // 两份脚本必须同一次 runInContext:顶层 const/let 只活在那一次调用的词法作用域里。
  // `__cols` 是这份回归的桥 —— `pipeMatrixColsNow` 是 let,外头赋不进去。
  vm.runInContext([borrowed, read('pipe_flow.js'), read('pipe_matrix.js'),
    'function __cols(v) { pipeMatrixColsNow = v; }'].join('\n'), ctx);
  return { ctx, element };
}

const SRC = { key: 'src:rss', kind: 'src', channel: 'rss', label: 'RSS', steps: ['crawl', 'export'] };
const INGEST = { key: 'ingest', kind: 'ingest', label: '入库处理', steps: ['index', 'extract'] };
const DAILY = { key: 'daily', kind: 'daily', label: '生成日报', steps: ['generate'] };
const FAIL = { status: 'error', detail: '隔离冒烟：提取服务 503' };
const COLS = [{ key: 'src:rss', label: 'RSS', src: true }, { key: 'ingest', label: '入库处理' }];

const SPAN = { started_at: '2026-10-28 09:00:00', finished_at: '2026-10-28 09:05:00' };

/** 收了尾的一轮:抓取都成了,存量提取留下 `extract` 那一格;
 *  `skips` 默认把入库处理余下几步按请求跳过,传 `[]` 就是它好好跑完的那一轮。 */
function archived(runId, extract, skips = ['ingest']) {
  const times = { 'src:rss': SPAN, ...(skips.length ? {} : { ingest: SPAN }) };
  return { run_id: runId, running: false, stopped: false, is_current: false,
    planned_stages: ['crawl', 'export', 'index', 'extract'], scopes: [{ channel: 'rss' }],
    stages: { crawl: { status: 'ok' }, export: { status: 'ok' }, index: { status: 'ok' }, extract },
    results: {},
    flow: { run: runId, owner: runId, kind: 'manual', nodes: [SRC, INGEST], skips,
      done: ['src:rss', 'ingest'], cuts: {}, results: {}, times } };
}

const nodeOf = (model, key) => model.nodes.find(n => n.key === key);

/** 还在跑的那一轮:抓取挂在 rss 上,抓取期间并行跑的存量提取已经红了。 */
function live(flowExtra = {}) {
  return { running: true, run_id: 'r-live', planned_stages: ['crawl', 'export', 'index', 'extract'],
    scopes: [{ channel: 'rss' }], results: {},
    stages: { crawl: { status: 'running' }, export: { status: 'pending' }, index: { status: 'pending' }, extract: FAIL },
    flow_live: { owner: 'r-live', kind: 'sync', nodes: [SRC, INGEST, DAILY], step: 'crawl', running: 'src:rss',
      skips: [], done: [], cuts: {}, results: {},
      times: { 'src:rss': { started_at: '2026-10-28 09:00:00', finished_at: null } }, ...flowExtra } };
}

test('跳过余下几步,盖不住已经发生的失败', () => {
  const { ctx, element } = rig();
  const run = archived('r-1', FAIL);
  const model = ctx.pipeFlowModel({ running: false, last_run: run }, run);
  const node = nodeOf(model, 'ingest');
  // 生命周期照实说「跳过」,红是跟着的另一件事
  assert.deepEqual({ state: node.state.state, issues: node.state.issues }, { state: 'skip', issues: 1 });
  assert.equal(ctx.pipeFlowSub(model, node), '1 步有问题 · 其余跳过');
  assert.equal(ctx.pipeFlowDot(node, 1), '1');
  ctx.__cols(COLS);
  assert.equal(ctx.pipeMatrixTip(model, node), '1 步有问题 · 其余跳过');
  assert.match(ctx.pipeMatrixCells(model), /data-s="skip" data-issues/);
  ctx.renderPipeFlowHead(model, node);
  const head = element('pipe-flow-dhead').innerHTML;
  assert.match(head, /pipe-flow-tag err/);
  assert.match(head, /1 步有问题 · 其余本次跳过/);
});

test('没有失败的跳过照旧念「本次跳过」', () => {
  const { ctx, element } = rig();
  const run = archived('r-2', { status: 'skipped', detail: '本次跳过' });
  const model = ctx.pipeFlowModel({ running: false, last_run: run }, run);
  const node = nodeOf(model, 'ingest');
  assert.equal(node.state.state, 'skip');
  assert.equal(node.state.issues, 0);
  assert.equal(ctx.pipeFlowSub(model, node), '本次跳过');
  ctx.__cols(COLS);
  assert.equal(ctx.pipeMatrixTip(model, node), '本次跳过');
  ctx.renderPipeFlowHead(model, node);
  assert.match(element('pipe-flow-dhead').innerHTML, /pipe-flow-tag mut">本次跳过/);
});

test('跳过之后还没轮到它:红照说,「恢复执行」照旧给', () => {
  const { ctx, element } = rig();
  const model = ctx.pipeFlowModel(live({ skips: ['ingest'] }), null);
  const node = nodeOf(model, 'ingest');
  assert.deepEqual({ state: node.state.state, issues: node.state.issues }, { state: 'skip', issues: 1 });
  ctx.renderPipeFlowHead(model, node);
  const head = element('pipe-flow-dhead').innerHTML;
  assert.match(head, /1 步有问题 · 其余本次跳过/);
  assert.match(head, /data-flow="unskip"/);
});

test('没跳过、还没轮到它:红不许冒充「已完成」,跳过那颗按钮照旧在', () => {
  const { ctx, element } = rig();
  const model = ctx.pipeFlowModel(live(), null);
  const node = nodeOf(model, 'ingest');
  // 它仍在计划里、仍要执行:生命周期是「等待」,红只是跟着的那一枚
  assert.deepEqual({ state: node.state.state, issues: node.state.issues }, { state: 'wait', issues: 1 });
  assert.equal(ctx.pipeFlowSub(model, node), '1 步有问题 · 等待');
  ctx.renderPipeFlowHead(model, node);
  const head = element('pipe-flow-dhead').innerHTML;
  assert.match(head, /1 步有问题 · 等待/);
  assert.doesNotMatch(head, /已完成/);
  assert.match(head, /data-flow="skip"/);
  // 后面那一类还排在它后面,正在跑的那一类也要说「结束 RSS，开始入库处理」
  ctx.renderPipeFlowHead(model, nodeOf(model, 'daily'));
  assert.match(element('pipe-flow-dhead').innerHTML, /排在 入库处理 后面/);
  ctx.renderPipeFlowHead(model, nodeOf(model, 'src:rss'));
  assert.match(element('pipe-flow-dhead').innerHTML, /结束RSS，开始入库处理/);
});

test('连着几轮红在同一处,跳过了余下几步也照样数得上', () => {
  const { ctx } = rig();
  const rows = ['r-4', 'r-5'].map(id => {
    const run = archived(id, FAIL);
    return { run, model: ctx.pipeFlowModel({ running: false, last_run: run }, run) };
  });
  ctx.__cols(COLS);
  const reps = ctx.pipeMatrixRepeats(rows);
  assert.deepEqual(reps.map(r => [r.key, r.k, r.title]), [['ingest', 2, '提取知识：隔离冒烟：提取服务 503']]);
});

test('中间一轮好好跑完:连续失败在那里断开', () => {
  const { ctx } = rig();
  const ok = { status: 'ok', detail: '成功 3' };
  const rows = [archived('r-6', FAIL), archived('r-7', ok, []), archived('r-8', FAIL)].map(run =>
    ({ run, model: ctx.pipeFlowModel({ running: false, last_run: run }, run) }));
  ctx.__cols(COLS);
  assert.deepEqual(ctx.pipeMatrixRepeats(rows), []);
});
