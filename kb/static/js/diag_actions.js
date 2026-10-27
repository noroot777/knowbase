/* 「问题与待办」的处理出口。外壳在 diag.js,列表在 diag_list.js,详情在 diag_detail.js。

   **确认框里那段话一律用服务端给的那一份**(`POST /scope` 的 `detail`),这里一个
   字都不自己编:阶段清单改了之后没人会想起来改前端那句话,而那句话正是人据以决定
   要不要花这笔钱的全部信息(先例:`kb/web/browser_capture_api.py` 的 `CONTINUE_STAGES`
   旁边那句 `detail`)。

   **不用原生 alert / confirm / prompt**(CLAUDE.md):确认走 `ui.js` 的 `confirmBox`,
   需要填字的走下面这个 `careFormBox`,字段级校验就地红字(`.field-err`)不弹 toast,
   错误文案一律过 `apiFail`(未捕获异常时 Starlette 回的是纯文本 500,`r.json()` 会炸)。 */

/** 带输入框的确认框。resolve 出一份 {name: value},取消时 resolve null。

    `check(values, fail)` 做本地校验;`submit(values, fail)` 做**真正那一下**
    (发请求),返回假值或抛异常时**弹窗留在原地**、错误就地红字。

    **非得这样不可**:服务端那几句拒绝(PDF 不算网页、这一条的正文已经定下来了、
    它不属于这个来源)比前端能判的更准,而它们是在提交之后才回来的。先关掉弹窗
    再去发请求的话,那句话只能弹成一条 toast —— 人刚填的内容连同焦点一起没了,
    要重新点开、重新填一遍(实页实测:字段级红字压根没地方显示,因为宿主节点
    已经被移出文档了)。 */
function careFormBox({ title, body = '', fields = [], ok = '保存', cancel = '取消',
                       danger = false, check = null, submit: onSubmit = null }) {
  return new Promise(resolve => {
    const prevFocus = document.activeElement;
    const mask = document.createElement('div');
    mask.className = 'modal-mask confirm-mask';
    const box = document.createElement('div');
    box.className = 'modal confirm confirm-wide care-form';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    const h = document.createElement('h2');
    h.textContent = title;
    box.appendChild(h);
    if (body) {
      const p = document.createElement('div');
      p.className = 'hint confirm-body';
      p.textContent = body;
      box.appendChild(p);
    }
    const inputs = new Map();
    for (const field of fields) {
      const wrap = document.createElement('div');
      wrap.className = 'row';
      const label = document.createElement('label');
      label.textContent = field.label;
      label.htmlFor = 'care-field-' + field.name;
      if (field.count) {
        const count = document.createElement('span');
        count.className = 'care-form-count';
        wrap.appendChild(label);
        label.appendChild(count);
        field.countNode = count;
      } else wrap.appendChild(label);
      const el = document.createElement(field.type === 'textarea' ? 'textarea' : 'input');
      el.id = label.htmlFor;
      if (field.type !== 'textarea') el.type = field.type || 'text';
      if (field.placeholder) el.placeholder = field.placeholder;
      if (field.value) el.value = field.value;
      const err = document.createElement('p');
      err.className = 'field-err';
      err.hidden = true;
      el.oninput = () => {
        err.hidden = true;
        if (field.countNode) field.countNode.textContent = careCountText(el.value, field);
      };
      if (field.countNode) field.countNode.textContent = careCountText(el.value, field);
      wrap.append(el, err);
      box.appendChild(wrap);
      inputs.set(field.name, { el, err });
    }
    const acts = document.createElement('div');
    acts.className = 'confirm-acts';
    const no = document.createElement('button');
    no.className = 'ghost';
    no.textContent = cancel;
    const yes = document.createElement('button');
    if (danger) yes.className = 'danger';
    yes.textContent = ok;
    acts.append(no, yes);
    box.appendChild(acts);
    mask.appendChild(box);

    const done = value => {
      document.removeEventListener('keydown', onKey, true);
      mask.remove();
      if (prevFocus && prevFocus.focus) prevFocus.focus();
      resolve(value);
    };
    const fail = (name, text) => {
      const slot = inputs.get(name);
      if (!slot) return;
      slot.err.textContent = text;
      slot.err.hidden = false;
      slot.el.focus();
    };
    let busy = false;
    const submit = async () => {
      if (busy) return;
      const values = {};
      for (const [name, slot] of inputs) values[name] = slot.el.value;
      if (check && !check(values, fail)) return;
      if (!onSubmit) { done(values); return; }
      busy = true;
      yes.disabled = true;
      const was = yes.textContent;
      yes.textContent = '处理中…';
      try {
        if (await onSubmit(values, fail)) { done(values); return; }
      } finally {
        busy = false;
        yes.disabled = false;
        yes.textContent = was;
      }
    };
    const onKey = e => {
      if (e.key === 'Escape') { e.stopPropagation(); done(null); }
      // 多行输入里 Enter 是换行,不是提交。
      else if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA') {
        e.stopPropagation();
        e.preventDefault();
        submit();
      }
    };
    no.onclick = () => done(null);
    yes.onclick = submit;
    mask.onclick = e => { if (e.target === mask) done(null); };
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(mask);
    (inputs.values().next().value?.el || yes).focus();
  });
}

function careCountText(value, field) {
  const n = value.trim().length;
  return field.countHint ? `${n} 字 · ${field.countHint}` : `${n} 字`;
}

function careRunExit(name, row, lane) {
  if (name === 'run') return careRun(row.kind, [row.key]);
  if (name === 'requeue') return careRequeue(row.kind, [row.key], careRowLives(row));
  if (name === 'body') return carePasteBody(row);
  if (name === 'relink') return careRelink(row);
  if (name === 'release') {
    return careRelease(row.kind, [row.key], [row.shelf_decision_id || '']);
  }
  return careShelf(row.kind, [row.key], name, careRowLives(row));
}

async function careAfter(detail) {
  toast(detail, { type: 'ok', ms: 6000 });
  await refreshCare(false);
}

/** 立即执行。**射程声明由服务端给**,前端只负责显示和回传指纹。 */
async function careRun(kind, keys, extra = {}) {
  let plan;
  try {
    plan = await carePost('/scope', { kind, keys, ...extra });
  } catch (e) {
    toast('这一下现在执行不了', { detail: e.message });
    return;
  }
  const ok = await confirmBox({ title: plan.title, body: plan.detail,
    ok: plan.confirm, cancel: '先不执行', wide: true });
  if (!ok) return;
  try {
    // **`plan` 必须原样回传**:服务端按现态再签一张比一比,不等就整批拒绝 ——
    // 人确认的是一份计划,而等待期间那几条可能换了档、换了世(确认框里写着
    // 「条目化提取不在这一步」,而另一条路带着它)。
    const done = await carePost('/run',
      { kind, keys, plan: plan.plan, ...extra });
    await careAfter(done.detail || '已开始，结果以同步中心为准');
  } catch (e) {
    toast('没有开始', { detail: e.message });
  }
}

function runCareKind() {
  if (!CARE.kind) return;
  careRun(CARE.kind, [], { whole_kind: true });
}

function careRunSource(row) {
  careRun(row.kind, [], { problem_id: row.id });
}

/** 单条那几颗按钮的那一份:形状和 `careLivesOf` 相同,取的是这一行自己那一世。 */
function careRowLives(row) {
  return row.life ? { [row.key]: row.life } : {};
}

async function careRequeue(kind, keys, lives = {}) {
  const ok = await confirmBox({
    title: `把 ${keys.length} 条放回队列`,
    body: '这一下只把处理机会还回去：不联网、不改正文，也不宣称成功。\n'
      + '下一轮同步（或者你接着点「立即执行」）才会真去处理它们。\n'
      + '实际放回几条按库里此刻的状态算——有的可能刚被别的轮次处理掉了。',
    ok: '放回队列', cancel: '取消', wide: true });
  if (!ok) return;
  try {
    const done = await carePost('/requeue', { kind, keys, lives });
    await careAfter(done.detail);
  } catch (e) {
    toast('没有放回', { detail: e.message });
  }
}

async function careShelf(kind, keys, decision, lives = {}) {
  const exclude = decision === 'exclude';
  await careFormBox({
    title: exclude ? `不再收录这 ${keys.length} 条` : `暂缓这 ${keys.length} 条`,
    body: exclude
      ? '只影响这几条内容：不会关掉整条订阅，也不会删掉已经写进知识库的文档。\n'
        + '它们会离开处理队列，随时可以在「已搁置」里恢复。'
      : '它们离开处理队列，不会自己到点恢复——要恢复请到「已搁置」里手动恢复。\n'
        + '正文、文件和已经入库的内容一个字都不动。',
    fields: [{ name: 'reason', type: 'textarea', label: '原因（可不填，会记进处理记录）',
      placeholder: exclude ? '比如：这个站点的内容跟我无关' : '比如：等作者补完再说' }],
    ok: exclude ? '不再收录' : '暂缓', danger: exclude,
    submit: async (vals, fail) => {
      try {
        const done = await carePost('/shelf',
          { kind, keys, decision, reason: vals.reason.trim(), lives });
        await careAfter(done.detail);
        return true;
      } catch (e) {
        fail('reason', e.message);
        return false;
      }
    } });
}

/** 恢复。**`decisions` 和 `keys` 一一对应**,说的是「我要取消的是那一次决定」——
    不带的话「先暂缓 → 恢复 → 再不再收录」之后,第一条记录上那颗按钮会把后来那次
    决定一起取消(服务端会拒,判据在 `shelf.release` 自己的 WHERE 里)。 */
async function careRelease(kind, keys, decisions) {
  try {
    const done = await carePost('/shelf/release', { kind, keys, decisions });
    await careAfter(done.detail);
    return done.released > 0;
  } catch (e) {
    toast('没有恢复', { detail: e.message });
    return false;
  }
}

/** 手动补正文。**长度判据在服务端**(按行去重之后的字数),这里只拦空输入,
    并把服务端那句话原样显示在字段下面 —— 前端自己数一遍就是第二份口径。 */
async function carePasteBody(row) {
  const floor = CARE.overview?.min_body_chars || 400;
  await careFormBox({
    title: '手动补正文',
    body: '把原文正文粘进来。保存之后这一条的正文换成这一份，来源记为「人工粘贴」。\n'
      + '注意：正文补入 ≠ 已入库——还要等一轮导出才真的写进知识库。\n'
      + '它也会退出自动补抓队列，所以不会被下一轮补抓盖掉；粘错了再粘一次就好。',
    fields: [{ name: 'body', type: 'textarea', label: '正文', count: true,
      countHint: `门槛 ${floor} 个不重复字符（按行去重后算）`,
      placeholder: '粘贴纯文本正文…' }],
    ok: '保存正文',
    check: (vals, fail) => {
      if (vals.body.trim()) return true;
      fail('body', '先把正文粘进来');
      return false;
    },
    // 真正那一道门槛在服务端(按行去重之后的字数),它那句话就地显示在字段下面
    // —— 人刚粘进去的那一大段不该因为一句拒绝就没了。
    submit: async (vals, fail) => {
      try {
        const done = await carePost('/body',
          { key: row.key, body: vals.body, life: row.life || '' });
        await careAfter(done.detail);
        return true;
      } catch (e) {
        fail('body', e.message);
        return false;
      }
    } });
}

/** 换链接。身份那一列不动,只换「这一次去哪儿抓」。 */
async function careRelink(row) {
  await careFormBox({
    title: '换一个取正文的地址',
    body: '原文链接和这一条的身份都不动，只换「补抓时去哪儿取正文」。\n'
      + '保存之后它回到补抓队列——这一下不去抓，下一轮同步或者「立即执行」才取。',
    fields: [{ name: 'url', type: 'url', label: '新的网页地址',
      value: row.fulltext_url || '', placeholder: 'https://…' }],
    ok: '保存地址',
    check: (vals, fail) => {
      const url = vals.url.trim();
      if (!url) { fail('url', '填一个 http(s) 网页地址'); return false; }
      if (!/^https?:\/\//i.test(url)) { fail('url', '只接受 http:// 或 https:// 的网页地址'); return false; }
      return true;
    },
    // 服务端那几句(PDF 不算网页、这一条的正文已经定下来了、它不属于这个来源)
    // 比前端能判的更准,原样显示在字段下面。
    submit: async (vals, fail) => {
      try {
        const done = await carePost('/link',
          { kind: row.kind, key: row.key, url: vals.url.trim(), life: row.life || '' });
        await careAfter(done.detail);
        return true;
      } catch (e) {
        fail('url', e.message);
        return false;
      }
    } });
}

/* ── 批量 ─────────────────────────────────────────────────────────────────
   **按来源分组后才发请求**:每一类来源的执行射程不一样。立即执行那一颗在混选
   时已经被禁掉(diag_list.js),放回/搁置这几颗逐类发一次就行。 */

function carePickedByKind() {
  const groups = new Map();
  for (const one of CARE.picked.values()) {
    if (!groups.has(one.kind)) groups.set(one.kind, []);
    groups.get(one.kind).push(one.key);
  }
  return groups;
}

/** 勾着的这几条各在第几世(`{key: life}`),随每个写请求发回去。

    服务端把它 AND 进写语句自己的 WHERE —— 它防的不是并发写入方(那一侧在服务端
    现取),而是**这一屏停留期间**那条内容被清理删掉又从同一个链接收回来:key 一个
    字都没变,而人的决定说的是另一条(口径在 `kb/intake_diag/warrant.py`)。 */
function careLivesOf(keys) {
  const out = {};
  for (const key of keys) {
    const life = CARE.picked.get(key)?.life || '';
    if (life) out[key] = life;
  }
  return out;
}

function careRunPicked() {
  const groups = carePickedByKind();
  if (groups.size !== 1) return;
  const [[kind, keys]] = [...groups];
  careRun(kind, keys);
}

/** 逐类来源发请求,**只把成功那几类的勾选去掉**。

    上一版不管成败一律 `CARE.picked.clear()`,而填原因那个弹窗的 submit 又无条件
    返回 true 关掉自己 —— 一类超时或 500 时,失败那几条的勾选和人刚写的那段原因
    一起没了,旁边还弹着一句别的类成功的提示(审查第 1 轮 B04)。
    返回「有没有全部成功」,给调用方决定要不要关掉输入框。 */
async function careEachKind(path, label, build) {
  let touched = 0;
  const notes = [];
  const failed = [];
  for (const [kind, keys] of carePickedByKind()) {
    try {
      const done = await carePost(path, build(kind, keys));
      touched += done.requeued ?? done.shelved ?? done.released ?? 0;
      if (done.detail) notes.push(done.detail);
      for (const key of keys) CARE.picked.delete(key);
    } catch (e) {
      failed.push(e.message);
    }
  }
  if (failed.length) {
    toast(`${label}没有全部完成，没成功的那几条还勾着`,
      { detail: failed.join('；') });
  }
  if (touched || notes.length) await careAfter(notes.join('；'));
  else await refreshCare(false);
  return !failed.length;
}

async function careRequeuePicked() {
  const total = CARE.picked.size;
  const ok = await confirmBox({
    title: `把 ${total} 条放回队列`,
    body: '这一下只把处理机会还回去：不联网、不改正文，也不宣称成功。\n'
      + '实际放回几条按库里此刻的状态算。',
    ok: '放回队列', cancel: '取消', wide: true });
  if (!ok) return;
  await careEachKind('/requeue', '放回队列',
    (kind, keys) => ({ kind, keys, lives: careLivesOf(keys) }));
}

async function careShelfPicked(decision) {
  const exclude = decision === 'exclude';
  await careFormBox({
    title: exclude ? `不再收录这 ${CARE.picked.size} 条` : `暂缓这 ${CARE.picked.size} 条`,
    body: exclude
      ? '只影响这几条内容：不会关掉任何订阅，也不会删掉已经写进知识库的文档。'
      : '它们离开处理队列，不会自己到点恢复；正文和文件一个字都不动。',
    fields: [{ name: 'reason', type: 'textarea', label: '原因（可不填，会记进处理记录）' }],
    ok: exclude ? '不再收录' : '暂缓', danger: exclude,
    // 返回假值 = 弹窗留在原地(人刚写的那段原因还在),理由见 `careEachKind`。
    submit: vals => careEachKind('/shelf', exclude ? '不再收录' : '暂缓',
      (kind, keys) => ({ kind, keys, decision, reason: vals.reason.trim(),
        lives: careLivesOf(keys) })) });
}

async function careReleasePicked() {
  const ids = new Map([...CARE.picked.values()].map(one => [one.key, one.decision || '']));
  await careEachKind('/shelf/release', '恢复',
    (kind, keys) => ({ kind, keys, decisions: keys.map(key => ids.get(key) || '') }));
}

/* ── 处理记录 ─────────────────────────────────────────────────────────────
   **不做快照回滚。** 演示页上每个出口都挂着「撤销」,那是内存里一份种子数据才
   给得起的东西;落到库上,把重试次数按快照写回上限是在伪造一次没发生过的失败。
   所以这里只对「暂缓 / 不再收录」给一颗「恢复」—— 它是那件事真正的逆操作
   (删掉搁置那一行),而且可重复、按实际删掉几行报数。 */

async function openCareLog() {
  let data;
  try {
    data = await careGet('/log', { kind: CARE.kind });
  } catch (e) {
    toast('读不到处理记录', { detail: e.message });
    return;
  }
  const mask = document.createElement('div');
  mask.className = 'modal-mask confirm-mask';
  const box = document.createElement('div');
  box.className = 'modal confirm confirm-wide';
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  const h = document.createElement('h2');
  h.textContent = '处理记录';
  const hint = document.createElement('div');
  hint.className = 'hint confirm-body';
  hint.textContent = `只留最近 ${data.keep_days} 天。重试次数一类的改动没有「撤销」——`
    + '按快照写回等于伪造一次没发生过的失败；暂缓和不再收录可以在这里直接恢复。';
  box.append(h, hint);
  const rows = document.createElement('div');
  rows.className = 'care-log-rows';
  if (!data.entries.length) {
    const empty = document.createElement('p');
    empty.className = 'care-detail-hint';
    empty.textContent = '还没有处理过任何内容。';
    rows.appendChild(empty);
  }
  for (const entry of data.entries) {
    const row = document.createElement('div');
    row.className = 'care-log-row';
    const b = document.createElement('b');
    b.textContent = entry.summary;
    const em = document.createElement('em');
    em.textContent = `${careTime(entry.at)} · ${entry.kind || '—'}`
      + ` · 实际影响 ${entry.affected} 条`;
    row.append(b, em);
    if (entry.detail) {
      const p = document.createElement('p');
      p.textContent = entry.detail;
      row.appendChild(p);
    }
    if (entry.undo === 'shelf_release' && entry.item_keys.length) {
      const undo = document.createElement('button');
      undo.type = 'button';
      undo.className = 'ghost';
      const ready = entry.undo_ready ?? entry.item_keys.length;
      undo.textContent = `恢复这 ${entry.item_keys.length} 条`;
      if (!ready) {
        // **后来又改过决定的,这颗按钮明确失效**(原型的操作契约,审查第 1 轮
        // F08):它绑的是**那一次**决定,而那次决定已经被后面的操作换掉了。
        undo.disabled = true;
        undo.title = '这几条的搁置决定在那之后变过了（或者已经恢复过），'
          + '这颗按钮不再有效；要恢复请到「已搁置」里操作';
      } else {
        undo.onclick = async () => {
          undo.disabled = true;
          if (await careRelease(entry.kind, entry.item_keys, entry.decisions || [])) {
            shut();
          } else undo.disabled = false;
        };
      }
      row.appendChild(undo);
    }
    rows.appendChild(row);
  }
  box.appendChild(rows);
  const acts = document.createElement('div');
  acts.className = 'confirm-acts';
  const close = document.createElement('button');
  close.className = 'ghost';
  close.textContent = '关闭';
  acts.appendChild(close);
  box.appendChild(acts);
  mask.appendChild(box);
  const onKey = e => {
    if (e.key !== 'Escape') return;
    e.stopPropagation();
    shut();
  };
  // **所有关闭路径只有这一个出口。** 恢复成功那一支原来直接 `mask.remove()`,
  // 于是那次的 keydown 监听留在 document 上(它捕获着已经消失的弹窗和那份日志)
  // —— 反复用几次就攒下几个(审查第 2 轮 B9)。口径同「判据只有一份」:关闭这件
  // 事也只许有一份实现。
  function shut() { document.removeEventListener('keydown', onKey, true); mask.remove(); }
  close.onclick = shut;
  mask.onclick = e => { if (e.target === mask) shut(); };
  document.addEventListener('keydown', onKey, true);
  document.body.appendChild(mask);
  close.focus();
}
