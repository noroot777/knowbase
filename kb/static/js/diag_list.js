/* 「问题与待办」的列表:分节、行、勾选、批量栏。外壳在 diag.js。

   **文本一律 textContent**:标题、失败原因、来源原话都来自站外,拼进 innerHTML
   就是把别人的页面内容当标记执行。只有图标和箭头走 innerHTML(它们是本地常量,
   而且 CLAUDE.md 要求箭头必须走 `icons.js` 的 `arrow()`,页面里不许出现 ↗ ↑ ↓ → 字符)。

   **来源级问题不进批量勾选。** 它的出口是「点名这一条订阅同步一次」,而管道一次
   只跑得了一轮 —— 勾三条来源批量执行,第一条占住管道、后面两条当场 409。所以它
   只在详情里一条一条处理,列表上不给复选框(也就谈不上「批量里混进来源问题」)。 */

function careRowId(row) { return row.id || ('item:' + row.key); }

function careTitleOf(row) {
  if (row.id) return row.name || row.url;
  return row.title_zh || row.title || row.url || '(没有标题)';
}

function careWhyOf(row, lane) {
  if (row.id) return row.evidence || '';
  if (row.fulltext_hold) return row.fulltext_hold;
  if (lane.stage === '翻译') return row.translate_error || '';
  return row.fulltext_error || '';
}

function careRenderList() {
  const host = $('care-list');
  host.innerHTML = '';
  if (CARE.group === 'running') { careRenderRunningPane(host); careRenderBatch(); return; }
  if (!CARE.sections.length) {
    const empty = document.createElement('div');
    empty.className = 'care-empty';
    empty.textContent = CARE.q ? '这个搜索词在这一组里没有命中。'
      : CARE.group === 'archived' ? '还没有搁置过任何内容。'
        : '这一组现在是空的。';
    host.appendChild(empty);
    careRenderBatch();
    return;
  }
  for (const section of CARE.sections) host.appendChild(careSectionNode(section));
  careRenderBatch();
  careRenderDetail();
}

function careSectionNode(section) {
  const lane = section.lane;
  const box = document.createElement('section');
  box.className = 'care-section';
  const head = document.createElement('div');
  head.className = 'care-section-head';
  const title = document.createElement('h3');
  title.textContent = lane.stage ? `${lane.stage} · ${lane.state}` : lane.state;
  const n = document.createElement('span');
  n.className = 'care-section-n';
  n.textContent = `${section.total} 条`;
  head.append(title, n);
  if (lane.summary) {
    const hint = document.createElement('p');
    hint.textContent = lane.summary;
    head.appendChild(hint);
  }
  box.appendChild(head);
  for (const row of section.items) box.appendChild(careRowNode(row, lane));
  if (section.items.length < section.total) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'care-more';
    more.textContent = `还有 ${section.total - section.items.length} 条，展开更多`;
    more.onclick = () => careMore(lane.id);
    box.appendChild(more);
  }
  return box;
}

function careRowNode(row, lane) {
  const id = careRowId(row);
  const node = document.createElement('div');
  node.className = 'care-row' + (CARE.selected === id ? ' on' : '');
  const pickCell = document.createElement('div');
  // **只有真有出口的那一档才给复选框**(审查第 1 轮 B03):「已完成」那一组的
  // `exits` 是空的,给了复选框就会在批量栏上冒出「立即执行已选 / 仅重新排队」——
  // 后端确实会拒,可按钮本身就是一句谎话。来源级问题照旧不进批量(见文件头)。
  if (!row.id && (lane.exits || []).length) {
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = CARE.picked.has(row.key);
    box.setAttribute('aria-label', '选择「' + careTitleOf(row) + '」');
    box.onclick = e => {
      e.stopPropagation();
      carePick(row, lane, box.checked);
    };
    pickCell.appendChild(box);
  }
  const main = document.createElement('button');
  main.type = 'button';
  main.className = 'care-row-main';
  main.onclick = () => careSelect(id);
  const title = document.createElement('span');
  title.className = 'care-row-title';
  title.textContent = careTitleOf(row);
  main.appendChild(title);
  const meta = document.createElement('div');
  meta.className = 'care-row-meta';
  for (const text of careMetaBits(row)) {
    const bit = document.createElement('em');
    bit.textContent = text;
    meta.appendChild(bit);
  }
  main.appendChild(meta);
  const why = careWhyOf(row, lane);
  if (why) {
    const line = document.createElement('div');
    line.className = 'care-row-why' + (careIsWarn(row, lane) ? ' warn' : '');
    line.textContent = why;
    main.appendChild(line);
  }
  const side = document.createElement('div');
  side.className = 'care-row-side';
  const pill = document.createElement('span');
  pill.className = 'care-pill ' + carePillTone(row, lane);
  pill.textContent = row.id ? row.state : lane.state;
  side.appendChild(pill);
  if (!row.id && lane.id !== 'archived') {
    const tries = document.createElement('span');
    tries.textContent = careTriesText(row, lane);
    if (tries.textContent) side.appendChild(tries);
  }
  if (row.shelf_decision) {
    const mark = document.createElement('span');
    mark.className = 'care-pill warn';
    mark.textContent = row.shelf_decision === 'exclude' ? '不再收录' : '暂缓';
    side.appendChild(mark);
  }
  node.append(pickCell, main, side);
  return node;
}

function careMetaBits(row) {
  if (row.id) {
    return [row.failures ? `连续失败 ${row.failures} 次` : '',
      row.last_attempt ? '上次尝试 ' + careTime(row.last_attempt) : '',
      row.enabled ? '' : '这条订阅已停用'].filter(Boolean);
  }
  return [row.subscriptions || '(不在任何订阅名单里)',
    careTime(row.published),
    row.lang && row.lang !== 'zh' ? '外文' : ''].filter(Boolean);
}

function careIsWarn(row, lane) {
  return row.id ? row.state === '需人工确认' : lane.id === 'body_held';
}

function carePillTone(row, lane) {
  const state = row.id ? row.state : lane.state;
  if (state === '已完成') return 'ok';
  if (state === '待执行' || state === '已搁置') return '';
  return careIsWarn(row, lane) ? 'warn' : 'bad';
}

function careTriesText(row, lane) {
  if (lane.stage === '翻译') return row.translate_tries ? `译过 ${row.translate_tries} 次` : '';
  return row.fulltext_tries ? `抓过 ${row.fulltext_tries} 次` : '';
}

function carePick(row, lane, on) {
  // `exits` 决定批量栏上给哪几颗按钮;`decision` 是「已搁置」那一组恢复时要取消
  // 的**那一次决定**(没有它后端会拒,理由见 `shelf.release`)。
  // `life` 是**勾这一下时它在第几世**:随请求发回去,由服务端 AND 进写语句 ——
  // 列表开着的时候这条内容被清理删掉又从同一个链接收回来时,这个决定不许落在
  // 新一世上(口径在 `kb/intake_diag/warrant.py`)。
  if (on) {
    CARE.picked.set(row.key, { key: row.key, kind: row.kind, lane: lane.id,
      exits: [...(lane.exits || [])], decision: row.shelf_decision_id || '',
      life: row.life || '' });
  } else CARE.picked.delete(row.key);
  careRenderBatch();
}

function careSelect(id) {
  CARE.selected = CARE.selected === id ? '' : id;
  careRenderList();
}

/** 批量栏。**只作用于勾过的那几条**(操作契约),而且按来源分组后才发请求 ——
    每一类来源的执行射程不一样,混在一条请求里写不出一句不说谎的确认文案。 */
function careRenderBatch() {
  const host = $('care-batch');
  const picked = [...CARE.picked.values()];
  const visible = careVisibleKeys();
  // 勾过但已经不在这一屏上的(换了筛选、刷新之后它走了)一律丢掉:批量栏承诺的
  // 是「当前可见并明确勾选的记录」。
  for (const key of CARE.picked.keys()) if (!visible.has(key)) CARE.picked.delete(key);
  careRenderVisibleCount();
  if (!CARE.picked.size) { host.hidden = true; host.innerHTML = ''; return; }
  host.hidden = false;
  host.innerHTML = '';
  const label = document.createElement('strong');
  label.textContent = `已选 ${CARE.picked.size} 条`;
  const kinds = new Set(picked.map(one => one.kind));
  const acts = document.createElement('div');
  acts.className = 'care-batch-acts';
  const add = (text, fn, cls = 'ghost', why = '') => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = cls;
    b.textContent = text;
    if (why) { b.disabled = true; b.title = why; } else b.onclick = fn;
    acts.appendChild(b);
  };
  const mixed = kinds.size > 1 ? '不同来源的执行范围不一样，请先在上面选一类来源' : '';
  // **按真实出口给按钮**(审查第 1 轮 B03):出口是后端按档给的
  // (`lane.exits`),这里取勾中那几档的**交集** —— 一半能放回队列、一半不能时
  // 给出一颗「仅重新排队」,点下去只会部分生效,而按钮上看不出是哪一半。
  const common = picked.reduce(
    (keep, one) => keep.filter(name => (one.exits || []).includes(name)),
    [...(picked[0]?.exits || [])]);
  const has = name => common.includes(name);
  if (has('run')) add('立即执行已选', () => careRunPicked(), '', mixed);
  if (has('requeue')) add('仅重新排队', () => careRequeuePicked(), 'ghost');
  if (has('snooze')) add('暂缓', () => careShelfPicked('snooze'), 'ghost');
  if (has('exclude')) add('不再收录', () => careShelfPicked('exclude'), 'ghost care-exit-danger');
  if (has('release')) add('恢复', () => careReleasePicked(), '');
  if (!common.length) {
    const none = document.createElement('em');
    none.className = 'care-batch-none';
    none.textContent = '勾中的这几条没有共同的处理出口——请分开处理';
    acts.appendChild(none);
  }
  const clear = document.createElement('button');
  clear.type = 'button';
  clear.className = 'ghost';
  clear.textContent = '清除选择';
  clear.onclick = () => { CARE.picked.clear(); careRenderList(); };
  acts.appendChild(clear);
  host.append(label, acts);
}

function careVisibleKeys() {
  const out = new Set();
  for (const section of CARE.sections) {
    for (const row of section.items) if (row.key && !row.id) out.add(row.key);
  }
  return out;
}

function careRenderVisibleCount() {
  const total = CARE.sections.reduce((n, s) => n + s.total, 0);
  const shown = CARE.sections.reduce((n, s) => n + s.items.length, 0);
  $('care-visible').textContent = CARE.q
    ? `搜索命中 ${total} 条，列出 ${shown} 条`
    : total ? `共 ${total} 条，列出 ${shown} 条` : '';
}

/** 「执行中」那一组的内容。**不列条目** —— 理由在 diag.js 的 careRenderRunning。 */
function careRenderRunningPane(host) {
  const live = CARE.overview.running;
  const box = document.createElement('section');
  box.className = 'care-section';
  const head = document.createElement('div');
  head.className = 'care-section-head';
  const title = document.createElement('h3');
  title.textContent = live.running ? '这一轮正在跑' : '现在没有同步在跑';
  head.appendChild(title);
  const hint = document.createElement('p');
  hint.textContent = live.running
    ? '进度只以阶段为粒度公布，所以这里不猜「正在处理哪一条」：条目仍然留在它自己那一档里。'
    // 留的是口径(「待执行」≠「在跑」),去掉的是「要开始请用立即执行」—— 那颗按钮就在这一屏上。
    : '「待执行」只表示这几条已经排上了，不表示有东西在运行。';
  head.appendChild(hint);
  box.appendChild(head);
  for (const stage of (live.stages || [])) {
    const row = document.createElement('div');
    row.className = 'care-row';
    const pad = document.createElement('div');
    const main = document.createElement('div');
    main.className = 'care-row-main';
    const name = document.createElement('span');
    name.className = 'care-row-title';
    name.textContent = stage.name;
    main.appendChild(name);
    if (stage.detail) {
      const detail = document.createElement('div');
      detail.className = 'care-row-meta';
      const em = document.createElement('em');
      em.textContent = stage.detail;
      detail.appendChild(em);
      main.appendChild(detail);
    }
    const side = document.createElement('div');
    side.className = 'care-row-side';
    const pill = document.createElement('span');
    pill.className = 'care-pill ' + (stage.status === 'done' ? 'ok'
      : stage.status === 'error' ? 'bad' : '');
    pill.textContent = stage.status || '等待';
    side.appendChild(pill);
    row.append(pad, main, side);
    box.appendChild(row);
  }
  host.appendChild(box);
}
