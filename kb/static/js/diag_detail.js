/* 「问题与待办」的详情:这一条卡在哪一步、为什么、现在能做什么。外壳在 diag.js。

   两种形状,**四步进度各走自己那一套**(状态契约):文章按「发现链接 → 补齐正文
   → 翻译处理 → 写入知识库」,来源连接问题按「访问来源 → 读取时间线 → 检查覆盖
   → 结束检查」。把来源问题套进文章那四步,屏幕上就会出现「这个账号的正文没补齐」
   这种不存在的事。

   **出口按后端给的那一份给**(`lane.exits` / 来源问题的 `actionable`),前端不
   自己判:一条内容此刻在哪一档只有后端答得准,而出口是按档分的。

   **这一屏不加次级标题,也不描述按钮。** 原先每一块都顶着一行小标题(「收录进度」
   「最后一次失败的原话」「接下来会发生什么」「这一条的身份」),而下面那块东西自己
   就长着那个样子:四段进度条、灰底的证词、一段话、一组事实。同理,建议里不再写
   「用『从浏览器采集』最省事,否则试『换链接』」—— 那几颗按钮就排在这段话底下,
   顺序还是后端按主次给的。留下来的只有**屏幕上看不出来的那些**:再试一轮会不会
   还是同一个结果、这一下要不要花模型的钱、它的壳入没入库。 */

const CARE_EXIT_LABELS = {
  run: '立即执行', requeue: '仅重新排队', capture: '从浏览器采集', body: '手动补正文',
  relink: '换链接', snooze: '暂缓', exclude: '不再收录', release: '恢复',
};

/** 「从浏览器采集」的前提,**写在按钮旁边,不等人点进去才发现**:它需要那篇文章
    此刻在浏览器里开着,还需要装好并配对过采集扩展(怎么装写在采集抽屉里,这里不
    重复一遍)。`capture` 和 `body` 共用同一道闸(后端的 `can_paste`),所以这句话
    只说前提,不说许可。 */
const CARE_CAPTURE_HINT = '「从浏览器采集」要这篇文章此刻在你的浏览器里开着，'
  + '并装好、配对过采集扩展；否则用「手动补正文」。';

function careRenderDetail() {
  const host = $('care-detail');
  const id = CARE.selected;
  if (!id) {
    host.innerHTML = '';
    const hint = document.createElement('p');
    hint.className = 'care-detail-hint';
    hint.textContent = '点左边任意一条';
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
  const steps = careBlock(host, '', []);
  steps.appendChild(careStepsNode(CARE.overview.item_steps, lane.step,
    lane.id.endsWith('_recent') ? CARE.overview.item_steps.length : lane.step));
  const stateLine = document.createElement('p');
  stateLine.textContent = `${lane.stage} · ${lane.state}：${lane.summary}`;
  steps.appendChild(stateLine);
  // 那句原话**贴着它解释的那一行**放,不另起一块、也不再标「最后一次失败的原话」:
  // 紧跟在「重试已停止:…」下面的那块灰底等宽字,本来就只能是它自己的原话。
  const why = careWhyOf(row, lane);
  if (why) {
    const quote = document.createElement('p');
    quote.className = 'care-evidence';
    quote.textContent = why;
    steps.appendChild(quote);
  }
  if (row.fulltext_url) {
    careBlock(host, '换过的取正文地址', row.fulltext_url, { mono: true });
  }
  careBlock(host, '', careItemAdvice(row, lane));
  // 「收进来的时间 + 正文版本」这两样**就是浏览器采集那条路认「这一世」的判据**
  // (`kb.browser_capture._same_life`),而这块面板自己按 `feed_items.life` 认
  // (`kb.intake_diag.warrant`)。两份口径各有主人,**这里不再判第三遍** —— 屏幕上
  // 把那两个值如实摆出来就够:它们一变,采集抽屉里那张回执就认不出来了(按设计当
  // 「没有回执」,不是「正文被改过」)。
  //
  // **一块灰框,不是四块。** 一行一个「内容编号 / 收进来的时间 / 正文版本」的标签,
  // 等于拿三个词去说明三样本来就认得出的东西:时间戳、版本号、32 位十六进制的 key。
  careBlock(host, '', [[
    `${careTime(row.fetched_at, { seconds: true })} 收进来 · 正文版本 ${row.content_rev}`
      + (row.body_provenance ? ' · 人工补入' : ''),
    // 「某一环完成了」不等于「已交付」:入没入库按这一条自己的导出回执说。
    !row.exported_hash ? '还没有写进知识库'
      : row.exported_hash === row.content_hash ? '知识库里就是这一份内容'
        : '知识库里躺着的是它早先那一版，这一份还没写进去',
    row.key,
  ].join('\n')], { mono: true });
  careExits(host, lane.exits, row, lane);
  careLoadItemLog(host, row.key);
}

function careItemAdvice(row, lane) {
  if (lane.id === 'body_held') {
    return ['拿到了页面，但认不出哪一段是正文，所以挂起等人看一眼，一次都没扣重试预算。'
      + '原样重试多半还是同一个结果。'];
  }
  if (lane.id === 'body_exhausted') {
    return ['重试预算已经用完，自动同步不会再碰它；原文站点本来就读不到'
      + '（要登录、脚本渲染、HTTP 403）的话，再给一轮也是同一个结果。',
      '它的壳照旧按原样入库，不会因为补不到正文就丢掉。'];
  }
  if (lane.id === 'translate_exhausted') {
    // **入没入库按这一条自己的导出回执说**,不一概说「已经按原文入库」:还没导出
    // 过的那几条也在这一档里(审查第 1 轮 F06)。
    return [(row.exported_hash
      ? '译不动，知识库里躺的是原文那一份，检索到的是英文。'
      : '译不动，它还没写进知识库——导出会按原文写进去，检索到的是英文。')
      + '「立即执行」会把这个范围里等着译的一起跑一轮翻译，要花模型的钱。'];
  }
  if (lane.id === 'body_queued') {
    return ['排在这一类来源的补抓队列里，下一轮同步会去取。'
      + '「立即执行」的范围是整类来源，不只这一条。'];
  }
  if (lane.id === 'translate_queued') return ['排在翻译队列里，下一轮同步会译它。'];
  if (lane.id === 'ingest_queued') {
    return ['正文已经定下来了，但要等一轮导出才真的写进知识库。'];
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
  const steps = careBlock(host, '', []);
  steps.appendChild(careStepsNode(CARE.overview.source_steps, row.step, row.step));
  const stateLine = document.createElement('p');
  stateLine.textContent = `${row.state}：${row.label}`;
  steps.appendChild(stateLine);
  // 和文章那一侧同一个摆法:原话贴着它解释的那一行,后面才是「这意味着什么、
  // 建议怎么办」。标题撤了 —— 灰底等宽字是原话、下面两段是人话,看一眼就分得开。
  if (row.evidence) {
    const quote = document.createElement('p');
    quote.className = 'care-evidence';
    quote.textContent = row.evidence;
    steps.appendChild(quote);
  }
  careBlock(host, '', [row.explanation, row.advice]);
  careBlock(host, '', [
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
  // 这一句解释的是**屏幕上没有的那颗按钮**,所以非说不可:人找不到「不再收录」时,
  // 下一步多半是去「我的订阅」之外的地方乱点。
  careBlock(host, '', ['「不再收录」只影响单篇内容，不该被用来停用一整条订阅——'
    + '要停用请到「我的订阅」。'
    + (row.blocked ? '另外，' + row.blocked + '——那跟它这一次为什么失败是两件事。' : '')]);
}

/** 画出口。**顺序和主次都按后端那一份**(`lane.exits`),这里不挑不排:
    「从浏览器采集」排在「手动补正文」前面、而且是那两者里的主项,口径写在
    `kb.intake_diag.lanes.Lane.exits` 上。

    每颗按钮带 `data-exit`,**禁用那一道按它取**(`careApplyPasteability`)——
    上一版按 `textContent` 认按钮,改一个字就静默认不出来了。 */
function careExits(host, names, row, lane) {
  const box = document.createElement('div');
  box.className = 'care-exits';
  for (const name of names) {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.exit = name;
    b.textContent = CARE_EXIT_LABELS[name] || name;
    if (name === 'exclude') b.className = 'ghost care-exit-danger';
    // 「从浏览器采集」是这两条人工出口里的主项(`body` 退成兜底的 ghost),
    // 但它不跟「立即执行」抢那块实心底 —— 软底 + 品牌描边比它低一档。
    else if (name === 'capture') b.className = 'care-exit-main';
    else if (name !== 'run') b.className = 'ghost';
    b.onclick = () => careRunExit(name, row, lane);
    box.appendChild(b);
  }
  host.appendChild(box);
  if (names.includes('capture')) {
    const note = document.createElement('p');
    note.className = 'care-exit-note';
    note.textContent = CARE_CAPTURE_HINT;
    host.appendChild(note);
  }
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

// 一道闸管的就是这三颗,见下面那段说明。
const CARE_BODY_GATED = ['capture', 'body', 'relink'];

/** 写正文那几条人工出口对某些来源压根不开放(X 观点、YouTube、简报的正文不许被
    原文链接后面那一页替换)。

    **一道闸管三颗**:「从浏览器采集」「手动补正文」「换链接」写的都是同一列、过的
    都是 `browser_capture._admit_body` 那一份准入,所以**不许一颗开一颗关** ——
    开着的那一颗点下去只会在服务端被拒,而屏幕上看不出是为什么。
    判据和那句理由都**问后端要**(`can_paste` / `paste_block`,
    `source_types.link_body_refusal`),前端一个字都不自己编。

    理由**既进 `title` 也就地显示一行**:`title` 在触屏上压根出不来,而「按钮灰着、
    旁边什么都不说」正是这颗按钮最容易被反复点的形态。 */
function careApplyPasteability(host, detail) {
  if (detail.can_paste) return;
  const why = detail.paste_block || '这一类来源的正文不允许被原文链接后面那一页替换';
  let gated = 0;
  for (const name of CARE_BODY_GATED) {
    for (const b of host.querySelectorAll(`.care-exits button[data-exit="${name}"]`)) {
      b.disabled = true;
      b.title = why;
      gated += 1;
    }
  }
  if (!gated) return;
  // 前提那句话这时候没有意义了(压根点不了),换成「为什么点不了」。
  const note = host.querySelector('.care-exit-note') || (() => {
    const made = document.createElement('p');
    made.className = 'care-exit-note';
    host.querySelector('.care-exits').after(made);
    return made;
  })();
  note.classList.add('care-exit-blocked');
  note.textContent = why;
}
