import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const root = new URL('../kb/static/js/', import.meta.url);
const core = readFileSync(new URL('core.js', root), 'utf8');
const linkHelpers = core.slice(core.indexOf('function obsLink'), core.indexOf('// Markdown'))
  + core.match(/function docAHtml[\s\S]*?\n\}/)[0];
const source = ['icons.js', 'fav.js', 'fav_view.js']
  .map(name => readFileSync(new URL(name, root), 'utf8')).join('\n');
const row = (id, extra = {}) => ({ id, path: `article-${id}.md`, title: `收藏 ${id}`, source: 'rss',
  account: 'Example', quote: '', note: '', summary: '', status: 'inbox', vault: 'test',
  created_at: `2026-10-${String(id).padStart(2, '0')}T12:00:00`, updated_at: '2026-10-01', ...extra });
const payload = favorites => ({ favorites, counts: { inbox: favorites.length, done: 0, archived: 0 },
  doc_paths: favorites.filter(item => !item.quote).map(item => item.path) });
const response = value => ({ ok: true, status: 200, json: async () => value });

function rig() {
  const elements = new Map();
  const timers = [];
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, { id, value: '', textContent: '', innerHTML: '', hidden: true,
        style: { display: '' }, checked: false, indeterminate: false, disabled: false,
        classList: { toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
          contains: name => classes.has(name) },
        setAttribute() {}, querySelector: () => null, querySelectorAll: () => [], focus() {} });
    }
    return elements.get(id);
  };
  const state = vm.createContext({ console, $, Date, URLSearchParams, CSS: { escape: value => value },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener() {} },
    window: { addEventListener() {} }, localStorage: { getItem: () => null, setItem() {} },
    matchMedia: () => ({ matches: false }), modelLabel: value => value,
    navigator: { clipboard: { writeText: async () => {} } }, toast() {},
    confirmBox: async () => false, apiFail: async reply => reply.message || '请求失败',
    fetch: async url => response(url.includes('/triage') ? { fresh: false } : payload([])),
    setTimeout: callback => { timers.push(callback); return timers.length; }, clearTimeout() {} });
  function $(id) { return element(id); }
  vm.runInContext(linkHelpers + '\n' + source, state);
  element('fav-sort').value = 'recent';
  return { state, element, timers, run: code => vm.runInContext(code, state),
    seed: rows => vm.runInContext(`favItems = ${JSON.stringify(rows)}`, state) };
}

test('default is all favorites and layout storage failure is harmless', () => {
  const app = rig();
  assert.equal(app.run('favView'), 'all');
  app.state.localStorage.setItem = () => { throw new Error('storage disabled'); };
  app.run("setFavLayout('list')");
  assert.equal(app.run('favLayout'), 'list');
});

test('classification uses actual estimated minutes, not a fabricated time', () => {
  const app = rig();
  const cases = [
    [{ eta: '~5 分钟' }, 'quick'], [{ eta: '阅读约8分钟', kind: 'read' }, 'quick'],
    [{ eta: '~15 分钟' }, 'deep'], [{ eta: '~1 小时' }, 'deep'], [{ eta: '1小时5分钟' }, 'deep'],
    [{ eta: '半天+' }, 'deep'], [{ name: '未知耗时' }, 'fresh'],
    [{ kind: 'reference' }, 'quote'],
  ];
  for (const [action, category] of cases) {
    assert.equal(app.run(`favCategory(${JSON.stringify(row(1, { action }))})`), category);
  }
});

test('snippet classification wins even when its action says try', () => {
  const app = rig();
  assert.equal(app.run(`favCategory(${JSON.stringify(row(1, { quote: '记下来', action: { eta: '3分钟' } }))})`), 'quote');
});

test('stale AI classifications are ignored', () => {
  const app = rig();
  app.run('favTri = {fresh: false, data: {quick: [{id: 1}]}}');
  assert.equal(app.run(`favCategory(${JSON.stringify(row(1))})`), 'fresh');
  app.run('favTri.fresh = true');
  assert.equal(app.run(`favCategory(${JSON.stringify(row(1))})`), 'quick');
});

test('search covers original title, quote, saved note and command case-insensitively', () => {
  const app = rig();
  app.seed([row(1, { title: '原文标题' }), row(2, { quote: '特殊摘录' }),
    row(3, { note: '每周总结' }), row(4, { action: { name: 'ToolKit', cmd: 'npx MagicCLI' } })]);
  for (const [query, id] of [['原文', 1], ['摘录', 2], ['每周', 3], ['magiccli', 4], ['toolkit', 4]]) {
    app.element('fav-search').value = query;
    assert.equal(app.run('visibleFavs()[0].id'), id);
    assert.equal(app.run('visibleFavs().length'), 1);
  }
});

test('source, category and search filters compose', () => {
  const app = rig();
  app.seed([row(1, { source: 'rss', action: { eta: '5分钟' } }),
    row(2, { source: 'video', action: { eta: '5分钟' } }),
    row(3, { source: 'rss', action: { eta: '2小时' } })]);
  app.element('fav-source').value = 'rss';
  app.run("favFilter = 'quick'");
  assert.equal(app.run('visibleFavs().length'), 1);
  assert.equal(app.run('visibleFavs()[0].id'), 1);
});

test('sorting is deterministic and does not mutate API order', () => {
  const app = rig();
  app.seed([row(1, { title: 'Zed' }), row(2, { title: 'Alpha' })]);
  assert.equal(app.run('visibleFavs()[0].id'), 2);
  app.element('fav-sort').value = 'oldest';
  assert.equal(app.run('visibleFavs()[0].id'), 1);
  app.element('fav-sort').value = 'name';
  assert.equal(app.run('visibleFavs()[0].id'), 2);
  assert.equal(app.run('favItems[0].id'), 1);
});

test('select all only includes currently visible results and filtering clears selection', () => {
  const app = rig();
  app.seed([row(1), row(2, { quote: '引用' })]);
  app.run("favFilter = 'quote'; selectVisibleFavs(true)");
  assert.equal(app.run('favSelected.size'), 1);
  assert.equal(app.run('favSelected.has(2)'), true);
  app.run("filterFavs('all')");
  assert.equal(app.run('favSelected.size'), 0);
});

test('empty search has a clear-filters action, not an empty archive message', () => {
  const app = rig();
  app.element('fav-search').value = '不存在';
  assert.match(app.run('favEmptyHtml()'), /清除筛选/);
  app.run('resetFavFilters()');
  assert.match(app.run('favEmptyHtml()'), /给下一个好点子/);
});

test('all external text is escaped in cards, notes and commands', () => {
  const app = rig();
  const hostile = '<img src=x onerror="alert(1)">';
  app.seed([row(1, { title: hostile, note: '</textarea>' + hostile,
    action: { name: hostile, what: hostile, cmd: 'echo "<script>"' } })]);
  const card = app.run('favCardHtml(favItems[0])');
  const detail = app.run('favDetailHtml(favItems[0])');
  assert.equal(card.includes('<img'), false);
  assert.equal(detail.includes('<img'), false);
  assert.equal(detail.includes('<script>'), false);
  assert.match(detail, /&lt;\/textarea(?:>|&gt;)/);
});

test('snippet read link retains quote, prefix and suffix in its anchor', () => {
  const app = rig();
  const item = row(1, { quote: '摘录 & 内容', prefix: '之前', suffix: '之后' });
  const anchor = app.run(`favDocA(${JSON.stringify(item)}, '阅读')`);
  const encoded = anchor.match(/#kbq=([^"\s]+)/)[1];
  assert.deepEqual(JSON.parse(decodeURIComponent(encoded)), { q: item.quote, p: item.prefix, s: item.suffix });
});

test('recommendations exclude completed, archived and reference-only entries', () => {
  const app = rig();
  app.seed([row(1, { status: 'done' }), row(2, { status: 'archived' }),
    row(3, { quote: '摘录' }), row(4)]);
  assert.equal(app.run('favRecommendations().length'), 1);
  assert.equal(app.run('favRecommendations()[0].id'), 4);
});

test('duplicate suggestions discard missing ids and require a valid keep candidate', () => {
  const app = rig();
  app.seed([row(1), row(2)]);
  app.run("favTri = {fresh: true, data: {dup: [{ids: [1,2,999], keep: 999}]}}");
  const invalid = app.run('favTriageHtml()');
  assert.match(invalid, /2 条收藏可能重复/);
  assert.equal(invalid.includes('保留推荐，其余暂存'), false);
  app.run('favTri.data.dup[0].keep = 1');
  assert.match(app.run('favTriageHtml()'), /archiveFavDuplicates\(\[1,2\],1\)/);
});

test('AI reconsideration and age-based suggestions do not repeat the same favorite', () => {
  const app = rig();
  app.seed([row(1, { updated_at: '2020-01-01' })]);
  app.run("favTri = {fresh:true,data:{cold:[{id:1,why:'方向过时'}]}}");
  const html = app.run('favTriageHtml()');
  assert.match(html, /1 条收藏可以重新考虑/);
  assert.equal(html.includes('1 条收藏很久没动过'), false);
});

test('draft survives switching and editing back to saved note clears dirty state', () => {
  const app = rig();
  app.seed([row(1, { note: '原来的笔记' }), row(2)]);
  app.run("favActive = 1; draftFavNote('尚未保存')");
  app.run('favActive = 2; renderFavInspector(); favActive = 1; renderFavInspector()');
  assert.match(app.element('fav-inspector').innerHTML, /尚未保存/);
  app.run("draftFavNote('原来的笔记')");
  assert.equal(app.run('favDrafts.size'), 0);
});

test('failed note save keeps the draft and reports failure', async () => {
  const app = rig();
  app.seed([row(1)]);
  app.run("favActive = 1; draftFavNote('有价值的笔记')");
  app.state.fetch = async () => ({ ok: false, message: '磁盘不可写' });
  await app.run('saveFavNote(1)');
  assert.equal(app.run('favDrafts.get(1)'), '有价值的笔记');
  assert.match(app.element('fav-note-state').textContent, /保存失败/);
  assert.equal(app.run('favBusy'), false);
});

test('successful note save updates local search and clears only its own draft', async () => {
  const app = rig();
  app.seed([row(1), row(2)]);
  app.run("favActive = 1; draftFavNote('  每周整理  '); favDrafts.set(2, '另一条草稿')");
  app.state.fetch = async () => response({ ok: true });
  await app.run('saveFavNote(1)');
  assert.equal(app.run('favItems[0].note'), '每周整理');
  assert.equal(app.run('favDrafts.has(1)'), false);
  assert.equal(app.run('favDrafts.get(2)'), '另一条草稿');
});

test('typing during note save keeps the newer unsaved draft', async () => {
  const app = rig();
  app.seed([row(1)]);
  let resolveSave;
  app.state.fetch = () => new Promise(resolve => { resolveSave = resolve; });
  app.run("favActive = 1; draftFavNote('先保存这一版')");
  const saving = app.run('saveFavNote(1)');
  app.run("draftFavNote('这是后来输入的内容')");
  resolveSave(response({ ok: true }));
  await saving;
  assert.equal(app.run('favItems[0].note'), '先保存这一版');
  assert.equal(app.run('favDrafts.get(1)'), '这是后来输入的内容');
  assert.match(app.element('fav-note-state').textContent, /还有未保存/);
});

test('global quick-search stays in favorites instead of navigating to full-text search', () => {
  const app = rig();
  const workspace = readFileSync(new URL('workspace.js', root), 'utf8');
  const helper = workspace.match(/function focusWorkspaceSearch\(\)[\s\S]*?\n\}/)[0];
  let focused = false;
  app.element('fav-search').focus = () => { focused = true; };
  app.element('fav-search').select = () => {};
  app.run("let activeTab = 'fav'; function toggleNavigation() {} function switchTab() { throw new Error('不应切换页面'); }");
  app.run(helper + '\nfocusWorkspaceSearch()');
  assert.equal(focused, true);
});

test('mobile quick-search leaves the detail view before focusing the hidden collection search', () => {
  const app = rig();
  app.seed([row(1)]);
  app.run('favActive = 1');
  app.state.matchMedia = () => ({ matches: true });
  app.element('fav-search').select = () => {};
  const workspace = readFileSync(new URL('workspace.js', root), 'utf8');
  const helper = workspace.match(/function focusWorkspaceSearch\(\)[\s\S]*?\n\}/)[0];
  app.run("let activeTab = 'fav'; function toggleNavigation() {} function switchTab() { throw new Error('不应切换页面'); }");
  app.run(helper + '\nfocusWorkspaceSearch()');
  assert.equal(app.run('favActive'), null);
});

test('slow AI never blocks loading the collection', async () => {
  const app = rig();
  app.state.fetch = async url => url.includes('/triage') ? new Promise(() => {}) : response(payload([row(1)]));
  await app.run('loadFavPanel()');
  assert.equal(app.run('favItems.length'), 1);
  assert.match(app.element('fav-list').innerHTML, /收藏 1/);
});

test('late responses cannot overwrite a newer status selection', async () => {
  const app = rig();
  let resolveFirst;
  const firstReply = new Promise(resolve => { resolveFirst = resolve; });
  app.state.fetch = async url => url.includes('/triage') ? response({ fresh: false })
    : url.includes('status=all') ? firstReply : response(payload([row(2, { status: 'done' })]));
  const first = app.run('loadFavPanel()');
  await app.run("favView = 'done'; loadFavPanel()");
  resolveFirst(response(payload([row(1)])));
  await first;
  assert.equal(app.run('favItems[0].id'), 2);
  assert.equal(app.run('favItems[0].status'), 'done');
});

test('partial batch failures are reported and undo only includes successes', async () => {
  const app = rig();
  const rows = [row(1), row(2)];
  app.seed(rows);
  app.run('favSelected = new Set([1, 2])');
  app.state.fetch = async (url, options) => {
    if (options?.method === 'PATCH') return url.endsWith('/2')
      ? { ok: false, message: '写入失败' } : response({ ok: true });
    return response(url.includes('/triage') ? { fresh: false } : payload(rows));
  };
  await app.run("favMarkMany([1, 2], 'done')");
  assert.equal(app.run('favUndo.length'), 1);
  assert.equal(app.run('favUndo[0].id'), 1);
  assert.equal(app.run('favSelected.has(1)'), false);
  assert.equal(app.run('favSelected.has(2)'), true);
  assert.match(app.element('fav-error').innerHTML, /1 条未能更新/);
  assert.equal(app.run('favBusy'), false);
});

test('a completely failed batch keeps selection and never claims anything was saved', async () => {
  const app = rig();
  app.seed([row(1), row(2)]);
  app.run('favSelected = new Set([1, 2])');
  app.state.fetch = async () => ({ ok: false, message: '数据库不可写' });
  await app.run("favMarkMany([1, 2], 'done')");
  assert.equal(app.run('favSelected.size'), 2);
  assert.equal(app.run('favUndo.length'), 0);
  assert.match(app.element('fav-error').innerHTML, /状态未改变/);
  assert.equal(app.element('fav-error').innerHTML.includes('其余已保存'), false);
});

test('AI startup HTTP failure never produces a fake running state', async () => {
  const app = rig();
  app.state.fetch = async () => ({ ok: false, message: '模型未配置' });
  await app.run('runTriage()');
  assert.equal(app.run('favTri'), null);
  assert.match(app.element('fav-error').innerHTML, /未能启动/);
  assert.equal(app.timers.length, 0);
});

test('cancelling deletion never sends a delete request', async () => {
  const app = rig();
  let requests = 0;
  app.state.fetch = async () => { requests++; return response({}); };
  await app.run('favDel(1)');
  assert.equal(requests, 0);
  assert.equal(app.run('favBusy'), false);
});

test('copy command reports clipboard permission failure instead of claiming success', async () => {
  const app = rig();
  const messages = [];
  app.state.toast = text => messages.push(text);
  app.state.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
  await app.run("copyFavCmd({dataset:{cmd:'npx fixture'}})");
  assert.match(messages[0], /复制失败/);
});

test('copy command copies the exact text and does not execute it', async () => {
  const app = rig();
  let copied;
  app.state.navigator.clipboard.writeText = async text => { copied = text; };
  await app.run("copyFavCmd({dataset:{cmd:'npx fixture --help'}})");
  assert.equal(copied, 'npx fixture --help');
});

test('poll stops after three failed reads with actionable feedback', async () => {
  const app = rig();
  app.run('favTri = {running: true}');
  app.state.fetch = async () => { throw new Error('连接中断'); };
  app.run('pollTriage()');
  for (let index = 0; index < 3; index++) await app.timers[index]();
  assert.equal(app.timers.length, 3);
  assert.equal(app.run('favTri.running'), false);
  assert.match(app.element('fav-error').innerHTML, /仍可正常使用/);
});
