// 类型状态归约那一层的不变量:**已经发生的失败在类型摘要里一律看得见,「余下的跳过」
// 只是跟着的一枚旁注**(`pipe_flow.js` 的 `pipeFlowState`,硬约束见
// docs/decisions/sync-flow.md 第一节)。
//
// 为什么钉在这一层而不是钉某个标签:这一份 state 被四处共用 —— 轨道上那个点与副标题、
// 详情头上那枚标签、历史表里那一格、以及「连着几轮卡在同一处」那个归约。把失败让给
// 「本次跳过」的话四处会**一致地**念跳过,红一个字都看不见(实测形态:存量提取 503
// 之后又跳过了入库处理余下几步)。所以这份回归一次把四个消费方都问一遍,并且反过来
// 钉一边:没有失败的跳过照旧念「本次跳过」。
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

/** 收了尾的一轮:抓取都成了,存量提取红了一条,入库处理余下几步按请求跳过。 */
function archived(runId, extract) {
  return { run_id: runId, running: false, stopped: false, is_current: false,
    planned_stages: ['crawl', 'export', 'index', 'extract'], scopes: [{ channel: 'rss' }],
    stages: { crawl: { status: 'ok' }, export: { status: 'ok' }, index: { status: 'ok' }, extract },
    results: {},
    flow: { run: runId, owner: runId, kind: 'manual', nodes: [SRC, INGEST], skips: ['ingest'],
      done: ['src:rss', 'ingest'], cuts: {}, results: {},
      times: { 'src:rss': { started_at: '2026-10-28 09:00:00', finished_at: '2026-10-28 09:05:00' } } } };
}

const nodeOf = (model, key) => model.nodes.find(n => n.key === key);

test('跳过余下几步,盖不住已经发生的失败', () => {
  const { ctx, element } = rig();
  const run = archived('r-1', FAIL);
  const model = ctx.pipeFlowModel({ running: false, last_run: run }, run);
  const node = nodeOf(model, 'ingest');
  assert.deepEqual({ state: node.state.state, issues: node.state.issues, skipped: node.state.skipped },
    { state: 'issue', issues: 1, skipped: true });
  assert.equal(ctx.pipeFlowSub(model, node), '1 步有问题 · 其余跳过');
  assert.equal(ctx.pipeFlowDot(node, 1), '1');
  ctx.__cols(COLS);
  assert.equal(ctx.pipeMatrixTip(model, node), '1 步有问题 · 其余跳过');
  assert.match(ctx.pipeMatrixCells(model), /data-s="issue"/);
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

test('失败之后还没轮到它:红照说,「恢复执行」照旧给', () => {
  const { ctx, element } = rig();
  const s = { running: true, run_id: 'r-3', planned_stages: ['crawl', 'export', 'index', 'extract'],
    scopes: [{ channel: 'rss' }], results: {},
    stages: { crawl: { status: 'running' }, export: { status: 'pending' }, index: { status: 'pending' }, extract: FAIL },
    flow_live: { owner: 'r-3', kind: 'manual', nodes: [SRC, INGEST, DAILY], step: 'crawl',
      running: 'src:rss', skips: ['ingest'], done: [], cuts: {}, results: {},
      times: { 'src:rss': { started_at: '2026-10-28 09:00:00', finished_at: null } } } };
  const model = ctx.pipeFlowModel(s, null);
  const node = nodeOf(model, 'ingest');
  assert.equal(node.state.state, 'issue');
  assert.equal(node.state.skipped, true);
  ctx.renderPipeFlowHead(model, node);
  const head = element('pipe-flow-dhead').innerHTML;
  assert.match(head, /1 步有问题 · 其余本次跳过/);
  assert.match(head, /data-flow="unskip"/);
  // 「排在谁后面」问的是前面还有谁真要跑:带红的那一类余下几步跳过了,不算
  ctx.renderPipeFlowHead(model, nodeOf(model, 'daily'));
  assert.doesNotMatch(element('pipe-flow-dhead').innerHTML, /入库处理/);
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
