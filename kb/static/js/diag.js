/* 「问题与待办」面板的外壳:状态、页头、分组页签、筛选、取数。
   列表在 diag_list.js,详情在 diag_detail.js,各个出口在 diag_actions.js
   —— 拆开是因为单个代码文件不得超过 700 行(CLAUDE.md)。

   **这一屏答的问题和同步中心不是同一个**:这里数的是全库还有多少**条内容**
   没走完收录,同步中心那个「需处理」数的是这一轮/今天欠了哪几**步**。页头下面
   那句话由后端给(`/api/intake-care` 的 `note`),**前端不另写一份** —— 两屏
   文案互相矛盾是这块面板最容易犯的错。

   **不按 `activeTab` 拦住装载。** `sources.js` 那道 `sourceActive()` 式的闸门
   踩过一次:首次定位和人点的那一下抢跑时,面板的数据请求一次都没发出去,页面
   零报错却像卡死(`docs/decisions/link-grab.md`)。这里改用请求序号丢弃过期
   回包 —— 写进一个隐藏面板没有任何代价,少发一次请求才有。 */

const CARE_GROUP_LABELS = {
  // **刻意不叫「需处理」**:那个词在同步中心已经有主,而两边的数字天生不一样。
  attention: '待决定', queued: '待执行', running: '执行中',
  resolved: '已完成', archived: '已搁置',
};
const CARE_SOURCE_SECTION = 'source_problems';

const CARE = {
  kind: '', group: 'attention', stage: '', q: '',
  overview: null, sections: [],
  picked: new Map(), selected: '',
  seq: 0, timer: 0, qTimer: 0,
};

/* 面板骨架。**住在这里而不是 index.html** —— 那个文件连上这一块就到了 720 行,
   越过了单文件 700 行的上限(CLAUDE.md),而骨架和渲染它的代码本来就该在一起。
   页头**只有一行**:标题 + 同一行上的事实数字(界面底线)。那行数字和同步中心的
   「需处理」**不是同一个问题**,所以下面那句话由后端给(`/api/intake-care` 的
   `note`),两屏的措辞只有一份。 */
const CARE_SKELETON = `
<div class="pipe-page-heading care-heading">
  <div class="pipe-title-row"><h1>问题与待办</h1>
    <span id="care-head-facts" class="pipe-head-facts" role="status">正在读取积压</span></div>
  <div class="pipe-actions">
    <select id="care-kind" onchange="setCareKind(this.value)" aria-label="按来源筛选"><option value="">全部来源</option></select>
    <button class="ghost" onclick="openCareLog()">处理记录</button>
    <button id="care-run-all" onclick="runCareKind()" disabled>立即执行本类</button>
  </div>
</div>
<p id="care-note" class="care-note"></p>
<div id="care-running" class="care-running" hidden role="status"></div>
<nav id="care-tabs" class="care-tabs" role="tablist" aria-label="处理状态"></nav>
<section class="care-bench">
  <div class="care-list-pane">
    <div class="care-filters">
      <label class="care-search"><i data-icon="search"></i>
        <input id="care-q" type="search" placeholder="搜索标题、链接或失败原因" aria-label="搜索问题"></label>
      <select id="care-stage" onchange="setCareStage(this.value)" aria-label="按环节筛选"><option value="">全部环节</option></select>
      <span id="care-visible" class="care-visible"></span>
    </div>
    <div id="care-batch" class="care-batch" hidden></div>
    <div id="care-list" class="care-list" aria-label="问题列表"></div>
  </div>
  <aside id="care-detail" class="care-detail" aria-label="所选问题的详情与操作"></aside>
</section>`;

/** 时间一律按**项目时区**显示(CLAUDE.md:Asia/Shanghai,不按宿主机)。

    库里存的是 `reg.now()` 的 UTC ISO 带偏移。上一版直接把 `T` 换成空格再截断,
    于是偏移被切掉、数字还是 UTC —— 屏幕上那个「收进来的时间」比上海时间少 8
    小时,而它长得像本地时间(审查第 1 轮 B02)。四处显示共用这一份。 */
function careTime(value, { seconds = false } = {}) {
  if (!value) return '';
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return value;
  return at.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
    ...(seconds ? { second: '2-digit' } : {}),
  }).replace(/\//g, '-');
}

function careApiBase() { return '/api/intake-care'; }

async function careGet(path, params = {}) {
  const url = new URL(careApiBase() + path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v !== '' && v != null) url.searchParams.set(k, v);
  const r = await fetch(url);
  if (!r.ok) throw new Error(await apiFail(r, '读取失败'));
  return r.json();
}

async function carePost(path, body) {
  const r = await fetch(careApiBase() + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await apiFail(r, '操作失败'));
  return r.json();
}

function loadCarePanel() {
  const host = $('carepanel');
  if (!host.dataset.built) {
    host.innerHTML = CARE_SKELETON;
    host.dataset.built = '1';
    renderIcons(host);          // 骨架里那枚放大镜是 `<i data-icon>`
  }
  refreshCare(true);
}

/** 整屏刷新。`full` 时连筛选下拉一起重建(第一次进来、或换了来源)。 */
async function refreshCare(full = false) {
  const mine = ++CARE.seq;
  try {
    const data = await careGet('', { kind: CARE.kind });
    if (mine !== CARE.seq) return;
    CARE.overview = data;
    if (full) careFillFilters(data);
    careRenderHead();
    careRenderTabs();
    careRenderRunning();
    await careLoadSections();
  } catch (e) {
    if (mine !== CARE.seq) return;
    $('care-head-facts').textContent = '读取积压失败';
    toast('读不到积压情况', { detail: e.message });
  }
  careSchedule();
}

/** 在跑的时候才轮询,而且只在这一屏上。空闲时一次请求都不发。 */
function careSchedule() {
  clearTimeout(CARE.timer);
  const live = CARE.overview?.running?.running;
  if (!live || typeof activeTab === 'undefined' || activeTab !== 'care') return;
  CARE.timer = setTimeout(() => refreshCare(false), 10000);
}

function careFillFilters(data) {
  const kinds = $('care-kind');
  while (kinds.options.length > 1) kinds.remove(1);
  for (const one of data.kinds) {
    const o = document.createElement('option');
    o.value = one.id;
    o.textContent = one.label;
    kinds.appendChild(o);
  }
  kinds.value = CARE.kind;
  const stages = $('care-stage');
  while (stages.options.length > 1) stages.remove(1);
  for (const name of data.stages) {
    const o = document.createElement('option');
    o.value = name;
    o.textContent = name;
    stages.appendChild(o);
  }
  stages.value = CARE.stage;
  $('care-note').innerHTML = careNoteHtml(data.note);
  const input = $('care-q');
  if (!input.dataset.wired) {
    input.dataset.wired = '1';
    input.oninput = () => {
      clearTimeout(CARE.qTimer);
      CARE.qTimer = setTimeout(() => { CARE.q = input.value.trim(); careLoadSections(); }, 250);
    };
  }
}

/** 后端那句话里用 `**…**` 标了两个量词,这里只认这一种标记,其余一律转义。 */
function careNoteHtml(note) {
  const safe = (note || '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  return safe.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
}

function careRenderHead() {
  const data = CARE.overview;
  const waiting = data.groups.attention + data.groups.queued;
  const problems = data.source_problems || 0;
  // **来源问题不混进「条内容」那个数**(审查第 1 轮 B08):一条来源问题不是一篇
  // 文章,同一条订阅还可能同时有「失败」和「没读完」两条。它单独说一句。
  // **每一组数字连着它的量词是一个不可拆的单元**(`<span>` + nowrap),换行按组
  // 换。不这么写的话 320 / 390px 下整行按字换行,屏幕上是「条内容在/等」「待决/
  // 定」这样的碎字 —— 没有横向溢出,但读不出数(审查第 2 轮 B6,四宽截图)。
  const bits = [
    `<span><b>${waiting}</b> 条内容在等</span>`,
    `<span><i>·</i> 待决定 <b>${data.groups.attention}</b></span>`,
    `<span><i>·</i> 待执行 <b>${data.groups.queued}</b></span>`,
    `<span><i>·</i> 已搁置 <b>${data.groups.archived}</b></span>`,
  ];
  if (problems) bits.push(`<span><i>·</i> 另有 <b>${problems}</b> 条来源连接问题</span>`);
  $('care-head-facts').innerHTML = bits.join(' ');
  const badge = $('nav-care-count');
  const pending = waiting + problems;
  if (badge) badge.textContent = pending ? String(pending) : '';
  // 「立即执行本类」的射程是**一整类来源**(export + index),所以没选来源时
  // 它压根没有一个说得出口的范围 —— 禁掉并说明,而不是让它偷偷按某个默认值跑。
  const run = $('care-run-all');
  run.disabled = !CARE.kind;
  run.title = CARE.kind ? '起跑这一类来源的导出与索引' : '先在左边选一类来源';
}

function careRenderTabs() {
  const data = CARE.overview;
  const host = $('care-tabs');
  host.innerHTML = '';
  for (const [id, label] of Object.entries(CARE_GROUP_LABELS)) {
    // 「待决定」那一组的页签数**含来源问题**(它们就列在那一组的最上面一节),
    // 而页头把两者分开说 —— 页签回答「点进去有几行」,页头回答「有多少条内容」。
    const n = id === 'running' ? (data.running.running ? 1 : 0)
      : (data.groups[id] || 0) + (id === 'attention' ? (data.source_problems || 0) : 0);
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', String(CARE.group === id));
    b.innerHTML = `${label}<span>${n}</span>`;
    b.onclick = () => careSetGroup(id);
    host.appendChild(b);
  }
}

/** 执行中那一组:**这一轮的事实**,不是条目级的「正在处理这一条」。
    管道只以阶段为粒度发布进度,猜一条出来就会有一条停在「执行中」几十分钟
    不动、而它这一轮压根没被碰到(理由在 `kb/intake_diag/running.py`)。 */
function careRenderRunning() {
  const live = CARE.overview.running;
  const host = $('care-running');
  if (!live.running) { host.hidden = true; host.innerHTML = ''; return; }
  const where = live.whole_library ? '全库那几步（跟来源无关）'
    : live.channel_names.join('、');
  const stage = live.stages.find(s => s.key === live.stage);
  host.hidden = false;
  host.innerHTML = '';
  const b = document.createElement('b');
  b.textContent = `正在跑：${live.stage_name || '准备中'}`;
  const span = document.createElement('span');
  span.textContent = `范围 ${where}${stage?.detail ? ' · ' + stage.detail : ''}`
    + (live.stopping ? ' · 已请求在本阶段结束后停止' : '');
  const em = document.createElement('em');
  em.textContent = careTime(live.started_at, { seconds: true });
  host.append(b, span, em);
}

function careSetGroup(id) {
  if (CARE.group === id) return;
  CARE.group = id;
  // 切分组清空勾选(操作契约):勾选只作用于当前可见并明确勾过的那几条。
  CARE.picked.clear();
  CARE.selected = '';
  careRenderTabs();
  careLoadSections();
}

function setCareKind(value) {
  CARE.kind = value;
  CARE.picked.clear();
  CARE.selected = '';
  refreshCare(false);
}

function setCareStage(value) {
  CARE.stage = value;
  CARE.picked.clear();
  careLoadSections();
}

function carePage() { return CARE.overview?.page || 40; }

/** 「展开更多」:**按 `offset` 往后取一页,追加在已有那几行后面**。

    上一版把 `limit` 当成「累计展开数」,而服务端把 `limit` 夹在 `lanes.PAGE`
    封顶、`offset` 又没传 —— 一个总数 41 的档位反复点这颗按钮拿回来的永远是最前
    40 条,第 41 条点不出来(审查第 1 轮 F09)。分页协议只有服务端那一份。 */
async function careMore(laneId) {
  const section = CARE.sections.find(one => one.lane.id === laneId);
  if (!section || section.source) return;
  const mine = CARE.seq;
  try {
    const params = { kind: CARE.kind, q: CARE.q,
      offset: section.items.length, limit: carePage() };
    const data = await careGet('/items',
      laneId === 'archived' ? { group: 'archived', ...params } : { lane: laneId, ...params });
    if (mine !== CARE.seq) return;
    const got = data.sections[0];
    if (!got) return;
    // 按 key 去重:两次请求之间队列可能动过,同一条可能在两页里都露一次。
    const seen = new Set(section.items.map(row => row.key));
    section.items = [...section.items, ...got.items.filter(row => !seen.has(row.key))];
    section.total = got.total;
    careRenderList();
  } catch (e) {
    toast('没有读到更多', { detail: e.message });
  }
}

/** 取这一组里每一档的条目。**每一档自带它自己的总数**(后端按同一个 WHERE 查)。 */
async function careLoadSections() {
  const mine = ++CARE.seq;
  const group = CARE.group;
  if (group === 'running') {
    CARE.sections = [];
    careRenderList();
    return;
  }
  try {
    const wanted = (CARE.overview?.lanes || [])
      .filter(l => l.group === group && (!CARE.stage || l.stage === CARE.stage));
    const picked = [];
    if (group === 'archived') {
      const data = await careGet('/items', { group, kind: CARE.kind, q: CARE.q,
        offset: 0, limit: carePage() });
      if (mine !== CARE.seq) return;
      picked.push(...data.sections);
    } else {
      const got = await Promise.all(wanted.map(lane =>
        careGet('/items', { lane: lane.id, kind: CARE.kind, q: CARE.q,
          offset: 0, limit: carePage() })));
      if (mine !== CARE.seq) return;
      for (const data of got) picked.push(...data.sections);
    }
    CARE.sections = picked.filter(s => s.total > 0);
    if (group === 'attention' && (!CARE.stage || CARE.stage === careSourceStage())) {
      const rows = careFilterSources(CARE.overview.sources || []);
      if (rows.length) CARE.sections.unshift({ lane: careSourceLane(), total: rows.length,
        items: rows, source: true });
    }
    careRenderList();
  } catch (e) {
    if (mine !== CARE.seq) return;
    $('care-list').innerHTML = '';
    toast('读不到这一组的内容', { detail: e.message });
  }
}

function careSourceStage() { return (CARE.overview?.stages || []).slice(-1)[0] || '来源连接'; }

function careSourceLane() {
  return { id: CARE_SOURCE_SECTION, group: 'attention', stage: careSourceStage(),
    state: '来源受限', summary: '这几条订阅自己没读成，跟单篇文章无关', step: 0,
    exits: ['run'] };
}

/** 来源级问题的搜索在前端过:它们不在 `feed_items` 里,后端那条 `LIKE` 管不到。 */
function careFilterSources(rows) {
  const q = CARE.q.toLowerCase();
  if (!q) return rows;
  return rows.filter(one => [one.name, one.url, one.label, one.evidence]
    .some(text => (text || '').toLowerCase().includes(q)));
}
