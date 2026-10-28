/* 「问题与待办」的详情:这一条卡在哪一步、为什么、现在能做什么。外壳在 diag.js。

   两种形状,**四步进度各走自己那一套**(状态契约):文章按「发现链接 → 补齐正文
   → 翻译处理 → 写入知识库」,来源连接问题按「访问来源 → 读取时间线 → 检查覆盖
   → 结束检查」。把来源问题套进文章那四步,屏幕上就会出现「这个账号的正文没补齐」
   这种不存在的事。

   **出口按后端给的那一份给**(`lane.exits` / 来源问题的 `actionable`),前端不
   自己判:一条内容此刻在哪一档只有后端答得准,而出口是按档分的。 */

const CARE_EXIT_LABELS = {
  run: '立即执行', requeue: '仅重新排队', body: '手动补正文',
  relink: '换链接', snooze: '暂缓', exclude: '不再收录', release: '恢复',
};

function careRenderDetail() {
  const host = $('care-detail');
  const id = CARE.selected;
  if (!id) {
    host.innerHTML = '';
    const hint = document.createElement('p');
    hint.className = 'care-detail-hint';
    hint.textContent = '点左边任意一条，这里说清它卡在哪一步、为什么，以及现在能做什么。';
    host.appendChild(hint);
    return;
  }
  const found = careFindRow(id);
  if (!found) { host.innerHTML = ''; return; }
  if (found.row.id) careRenderSourceDetail(host, found.row);
  else careRenderItemDetail(host, found.row, found.lane);
}

function careFindRow(id) {
  for (const section of CARE.sections) {
    for (const row of section.items) {
      if (careRowId(row) === id) return { row, lane: section.lane };
    }
  }
  return null;
}

function careBlock(host, heading, lines, { mono = false } = {}) {
  const box = document.createElement('div');
  box.className = 'care-block';
  if (heading) {
    const h = document.createElement('h4');
    h.textContent = heading;
    box.appendChild(h);
  }
  for (const text of [].concat(lines).filter(Boolean)) {
    const p = document.createElement('p');
    if (mono) p.className = 'care-evidence';
    p.textContent = text;
    box.appendChild(p);
  }
  host.appendChild(box);
  return box;
}

function careStepsNode(names, stuckAt, doneThrough) {
  const box = document.createElement('div');
  box.className = 'care-steps';
  names.forEach((name, i) => {
    const step = document.createElement('div');
    step.className = 'care-step' + (i === stuckAt ? ' stuck' : i < doneThrough ? ' done' : '');
    const bar = document.createElement('span');
    const label = document.createElement('b');
    label.textContent = name;
    step.append(bar, label);
    box.appendChild(step);
  });
  return box;
}

function careRenderItemDetail(host, row, lane) {
  host.innerHTML = '';
  const title = document.createElement('h2');
  title.textContent = careTitleOf(row);
  host.appendChild(title);
  if (row.url) {
    const link = document.createElement('a');
    link.className = 'care-detail-link';
    link.href = row.url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = row.url;
    link.insertAdjacentHTML('beforeend', arrow('ext'));
    host.appendChild(link);
  }
  const steps = careBlock(host, '收录进度', []);
  steps.appendChild(careStepsNode(CARE.overview.item_steps, lane.step,
    lane.id.endsWith('_recent') ? CARE.overview.item_steps.length : lane.step));
  const stateLine = document.createElement('p');
  stateLine.textContent = `${lane.stage} · ${lane.state}：${lane.summary}`;
  steps.appendChild(stateLine);
  if (row.fulltext_url) {
    careBlock(host, '换过的取正文地址', row.fulltext_url, { mono: true });
  }
  const why = careWhyOf(row, lane);
  if (why) careBlock(host, row.fulltext_hold ? '为什么要人看一眼' : '最后一次失败的原话',
    why, { mono: true });
  careBlock(host, '接下来会发生什么', careItemAdvice(row, lane));
  careBlock(host, '这一条的身份', [
    `内容编号 ${row.key}`,
    `收进来的时间 ${careTime(row.fetched_at, { seconds: true })}`,
    `正文版本 ${row.content_rev}${row.body_provenance ? ' · 正文来自人工补入' : ''}`,
    // 三句话,三件不同的事(审查第 1 轮 F06:别拿「某一环完成了」当「已交付」)。
    !row.exported_hash ? '这一条还没有写进知识库'
      : row.exported_hash === row.content_hash ? '知识库里就是这一份内容'
        : '知识库里躺着的是它早先那一版，这一份还没写进去',
  ], { mono: true });
  careExits(host, lane.exits, row, lane);
  careLoadItemLog(host, row.key);
}

function careItemAdvice(row, lane) {
  if (lane.id === 'body_held') {
    return ['补抓拿到了页面，但没能确认哪一段是这一篇的正文，所以它一次都不扣重试预算、'
      + '就挂起等人看一眼。原样重试很可能还是同一个结果——优先「手动补正文」或者「换链接」。'];
  }
  if (lane.id === 'body_exhausted') {
    return ['重试预算已经用完，自动同步不会再碰它。「仅重新排队」等于再给一轮机会；'
      + '要是原文站点本来就读不到（要登录、脚本渲染），换链接或手动补正文更省事。',
      '它的壳照旧会按原样入库，不会因为补不到正文就丢掉。'];
  }
  if (lane.id === 'translate_exhausted') {
    // **入没入库按这一条自己的导出回执说**,不一概说「已经按原文入库」:还没导出
    // 过的那几条也在这一档里(审查第 1 轮 F06)。
    return [(row.exported_hash
      ? '译不动，知识库里躺的是原文（外文）那一份——检索到的是英文。'
      : '译不动。它还没有写进知识库：导出那一步会按原文把它写进去，'
        + '所以检索到的会是英文那一份。')
      + '「立即执行」会把这个范围里等着译的一起放回队列并立刻跑一轮翻译，要花模型的钱。'];
  }
  if (lane.id === 'body_queued') {
    return ['它排在这一类来源的补抓队列里，下一轮同步会去取。现在就想取的话用「立即执行」，'
      + '那一轮的范围是整类来源、不只这一条。'];
  }
  if (lane.id === 'translate_queued') return ['排在翻译队列里，下一轮同步会译它。'];
  if (lane.id === 'ingest_queued') {
    return ['正文已经定下来了，等着写进知识库。注意：正文补入 ≠ 已入库，'
      + '要等一轮导出才真的写进去。'];
  }
  return ['这一环已经完成了。'];
}

function careRenderSourceDetail(host, row) {
  host.innerHTML = '';
  const title = document.createElement('h2');
  title.textContent = row.name;
  host.appendChild(title);
  if (row.url) {
    const link = document.createElement('a');
    link.className = 'care-detail-link';
    link.href = row.url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = row.url;
    link.insertAdjacentHTML('beforeend', arrow('ext'));
    host.appendChild(link);
  }
  const steps = careBlock(host, '这一次检查走到哪一步', []);
  steps.appendChild(careStepsNode(CARE.overview.source_steps, row.step, row.step));
  const stateLine = document.createElement('p');
  stateLine.textContent = `${row.state}：${row.label}`;
  steps.appendChild(stateLine);
  careBlock(host, '实际含义', row.explanation);
  careBlock(host, '来源原话', row.evidence, { mono: true });
  careBlock(host, '建议', row.advice);
  careBlock(host, '保留下来的东西', [
    row.since ? `同步下界 ${careTime(row.since) || row.since}` : '这条订阅还没有水位',
    row.retry_at ? '退避已经打上，下一轮自动同步会按它再试一次' : '',
    '这一次复查成功不代表发现了新内容，也不会被当成新文章入库。',
  ]);
  const exits = document.createElement('div');
  exits.className = 'care-exits';
  const run = document.createElement('button');
  run.type = 'button';
  run.textContent = '重试这一个来源';
  if (row.actionable) run.onclick = () => careRunSource(row);
  else {
    run.disabled = true;
    run.title = row.blocked || '这条订阅已停用，单独重试它不会跑';
  }
  exits.appendChild(run);
  host.appendChild(exits);
  careBlock(host, '', ['来源连接问题不是一篇文章，所以这里没有「不再收录」：'
    + '那颗按钮只影响单篇内容，不该被用来停用一整条订阅（要停用请到「我的订阅」）。'
    + (row.blocked ? '另外，' + row.blocked + '——那跟它这一次为什么失败是两件事。' : '')]);
}

function careExits(host, names, row, lane) {
  const box = document.createElement('div');
  box.className = 'care-exits';
  for (const name of names) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = CARE_EXIT_LABELS[name] || name;
    if (name === 'exclude') b.className = 'ghost care-exit-danger';
    else if (name !== 'run') b.className = 'ghost';
    b.onclick = () => careRunExit(name, row, lane);
    box.appendChild(b);
  }
  host.appendChild(box);
}

/** 这一条自己的处理记录。**异步补上**:它只是旁证,拿不到也不该拦住整个详情。 */
async function careLoadItemLog(host, key) {
  let detail;
  try {
    detail = await careGet('/items/' + encodeURIComponent(key));
  } catch (e) {
    careBlock(host, '处理记录', '读不到这一条的处理记录：' + e.message);
    return;
  }
  if (CARE.selected !== 'item:' + key) return;
  CARE.detail = detail;
  careApplyPasteability(host, detail);
  if (!detail.log.length) return;
  const box = careBlock(host, '处理记录', []);
  const rows = document.createElement('div');
  rows.className = 'care-log-rows';
  for (const entry of detail.log) {
    const row = document.createElement('div');
    row.className = 'care-log-row';
    const b = document.createElement('b');
    b.textContent = entry.summary;
    const em = document.createElement('em');
    em.textContent = careTime(entry.at);
    row.append(b, em);
    if (entry.detail) {
      const p = document.createElement('p');
      p.textContent = entry.detail;
      row.appendChild(p);
    }
    rows.appendChild(row);
  }
  box.appendChild(rows);
}

/** 「手动补正文」对某些来源压根不开放(X 观点、YouTube、简报的正文不许被原文
    链接里的页面替换)。后端如实给了 `can_paste`,这里把按钮禁掉并说清原因 ——
    不说清的话人会一直点那颗没反应的按钮。 */
function careApplyPasteability(host, detail) {
  if (detail.can_paste) return;
  for (const b of host.querySelectorAll('.care-exits button')) {
    if (b.textContent === CARE_EXIT_LABELS.body || b.textContent === CARE_EXIT_LABELS.relink) {
      b.disabled = true;
      b.title = '这一类来源的正文不允许被原文链接里的页面替换';
    }
  }
}
