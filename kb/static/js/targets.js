/* 采集目标(订阅了哪些公众号 / RSS 源…)的管理弹窗。
 *
 * 只认 /api/sources/targets 这套通用接口,文案里的"公众号"来自后端给的 label
 * ——换来源时这个文件不用改。后端说 manageable=false 就整个入口不显示。
 */

let tgtList = [];        // 已订阅
let tgtCands = [];       // 上次搜索的候选
let tgtLabel = '目标';
let tgtBusy = false;     // 有请求在飞:防连点造成重复添加/删除
let tgtReadiness = null; // {ready,total};来源没有"逐个目标就绪"这回事则为 null
let tgtOnlyPending = false;
let tgtTimer = null;     // 自动刷新:边在微信里点文章边看着它变绿

/** 面板加载时调用:决定要不要显示"管理订阅"入口 */
async function initTargets() {
  try { await loadPipeChannels(); } catch { return; }
  const requestedChannel = pipeChannel;
  const btn = $('src-manage');
  if (!btn) return;
  const d = await fetch(channelUrl('/api/sources/targets')).then(r => r.ok ? r.json() : null)
    .catch(() => null);
  if (requestedChannel !== pipeChannel) return;
  if (!d || !d.manageable) { btn.style.display = 'none'; return; }
  tgtLabel = d.label || '目标';
  tgtList = d.targets || [];
  tgtReadiness = d.readiness || null;
  btn.textContent = tgtReadiness
    ? `管理${tgtLabel} · ${tgtReadiness.ready}/${tgtReadiness.total} 就绪`
    : `管理${tgtLabel}`;
  btn.style.display = '';
}

async function openTargets() {
  $('tgt-modal').style.display = 'flex';
  $('tgt-search').value = '';
  tgtCands = [];
  renderTgtCands();
  await reloadTargets();
  startTgtAutoRefresh();
  $('tgt-search').focus();
}

function closeTargets() {
  $('tgt-modal').style.display = 'none';
  stopTgtAutoRefresh();
  initTargets();          // 入口按钮上的就绪计数也跟着更新
}

/* 自动刷新只在"还有号没就绪"时跑:你在微信里逐篇点,这边每 5 秒把 wxdown
 * 新抓到的凭证并进来。全部就绪就自动停——没什么可等的时候不该一直起子进程。*/
function startTgtAutoRefresh() {
  stopTgtAutoRefresh();
  if (!tgtReadiness) return;
  if (!$('tgt-auto') || !$('tgt-auto').checked) return;
  if (tgtReadiness && tgtReadiness.ready >= tgtReadiness.total) return;
  tgtTimer = setInterval(() => {
    if ($('tgt-modal').style.display === 'none') return stopTgtAutoRefresh();
    refreshReadiness(true);
  }, 5000);
}

function stopTgtAutoRefresh() {
  if (tgtTimer) { clearInterval(tgtTimer); tgtTimer = null; }
}

/** 重新探测就绪状态。silent=自动刷新时不弹 toast,只更新计数 */
async function refreshReadiness(silent = false) {
  if (tgtBusy) return;
  tgtBusy = true;
  const btn = $('tgt-refresh');
  if (btn && !silent) btn.textContent = '刷新中…';
  try {
    const r = await fetch('/api/sources/targets/refresh', { method: 'POST' });
    if (!r.ok) {
      stopTgtAutoRefresh();     // 失败就别一直重试,否则 5 秒一次刷屏
      if (!silent) toast('刷新失败', { detail: await apiFail(r) });
      else toast('自动刷新失败,已停止', { detail: await apiFail(r) });
      return;
    }
    if (!silent) toast((await r.json()).detail || '已刷新', { type: 'ok' });
    tgtBusy = false;            // reloadTargets 自己不占锁,这里先放开
    await reloadTargets();
    if (tgtReadiness && tgtReadiness.ready >= tgtReadiness.total) {
      stopTgtAutoRefresh();
      if (silent) toast(`全部 ${tgtReadiness.total} 个${tgtLabel}凭证就绪`, { type: 'ok' });
    }
  } finally {
    tgtBusy = false;
    if (btn && !silent) btn.textContent = '刷新凭证';
  }
}

async function reloadTargets() {
  const r = await fetch(channelUrl('/api/sources/targets'));
  if (await toastIfFail(r, `读取${tgtLabel}列表失败`)) return;
  const d = await r.json();
  tgtList = d.targets || [];
  tgtLabel = d.label || tgtLabel;
  tgtReadiness = d.readiness || null;
  renderTargets();
}

/* 显示名 vs 档案键:来源那边改了名,列表要显示新的那个,否则你在微信里
 * 按看到的名字去搜会搜不着。但增删改一律还得用 name——它是后端的键。 */
function tgtShown(t) { return t.display_name || t.name; }
function tgtRenamed(t) { return !!t.display_name && t.display_name !== t.name; }

/* 小圆点的颜色。后端给 tone 就照它来(ok 绿 / warn 黄 / bad 红),
 * 没给就退回"能采=绿,不能采=红"——tone 是后加的,别让老来源渲染成没颜色。*/
function tgtTone(t) {
  if (t.tone) return t.tone;
  return t.ready ? 'ok' : 'bad';
}

function renderTargets() {
  const on = tgtList.filter(t => t.enabled).length;
  $('tgt-title').textContent = `订阅的${tgtLabel}`;
  $('tgt-count').textContent = `共 ${tgtList.length} 个,${on} 个参与采集`
    + (tgtReadiness ? ` · ${tgtReadiness.ready}/${tgtReadiness.total} 个凭证就绪` : '');

  // 就绪那一栏只在来源确实逐个授权时才出现
  const bar = $('tgt-ready-bar');
  bar.style.display = tgtReadiness ? '' : 'none';
  if (tgtReadiness) {
    const left = tgtReadiness.total - tgtReadiness.ready;
    $('tgt-ready-text').textContent = left
      ? `还有 ${left} 个凭证不新鲜(黄=放太久,红=没有或已失效)`
        + '——去 Mac 绿泡泡里各点开一篇文章'
      : '全部就绪,可以同步了';
    $('tgt-ready-text').style.color = left ? 'var(--muted)' : 'var(--ok)';
  }

  // 按名字排,不按启用状态:后端是"启用的在前",那样一点停用行就沉底、
  // 下一行顶上来接住第二次点击——连着停用几个时会误伤。位置稳定才点得准。
  // 排序用看得见的那个名字,否则改过名的行会排在一个没显示出来的键上。
  let rows = [...tgtList].sort(
    (a, b) => tgtShown(a).localeCompare(tgtShown(b), 'zh'));
  // 「只看未就绪」是给"还差谁要去微信里点"用的,停用的号不用点,别混进来
  if (tgtOnlyPending) rows = rows.filter(t => t.ready === false && t.enabled);
  const box = $('tgt-list');
  const keepScroll = box.scrollTop;      // 整块重渲染会把长列表弹回顶部
  box.innerHTML = rows.length
    ? rows.map(t => `
      <div class="tgt-row">
        <div style="min-width:0">
          <div class="tgt-name${t.enabled ? '' : ' off'}">
            ${t.ready === null || t.ready === undefined ? ''
              : `<span class="tgt-dot ${tgtTone(t)}"></span>`}${esc(tgtShown(t))}
          </div>
          <div class="tgt-detail">${esc([t.state, t.detail].filter(Boolean).join(' · '))}</div>
          ${tgtRenamed(t)
            ? `<div class="tgt-was">已改名,库里存作「${esc(t.name)}」</div>` : ''}
        </div>
        <div style="display:flex;gap:6px;flex:none">
          <button class="ghost" data-tgt-toggle="${esc(t.name)}"
                  style="padding:3px 10px;font-size:12px">
            ${t.enabled ? '停用' : '启用'}
          </button>
          <button class="ghost" data-tgt-remove="${esc(t.name)}"
                  style="padding:3px 10px;font-size:12px;color:#c33">移除</button>
        </div>
      </div>`).join('')
    : `<div class="empty">${tgtOnlyPending
        ? '没有待处理的' : `还没有订阅任何${esc(tgtLabel)}`}</div>`;
  box.scrollTop = keepScroll;
}

function toggleOnlyPending(el) {
  tgtOnlyPending = el.checked;
  renderTargets();
}

function renderTgtCands() {
  const box = $('tgt-cands');
  if (!tgtCands.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
  /* 「已订阅」优先按 id(公众号是 fakeid)判,名字只是兜底。
   * 只比名字的话,改过名的号在这里永远显示成「添加」——而用户来搜新名字,
   * 正是因为列表上那个旧名字他已经认不出来了。点下去就会多出一条同 id 的订阅。 */
  const byId = new Map(tgtList.filter(t => t.id).map(t => [t.id, t]));
  const byName = new Map(tgtList.map(t => [t.name, t]));
  box.style.display = '';
  box.innerHTML = tgtCands.map(c => {
    const own = (c.id && byId.get(c.id)) || byName.get(c.name);
    // 认出是同一个号、但库里存的是另一个名字:说清楚,否则"已订阅"看着像认错了
    const renamed = own && own.name !== c.name;
    return `
      <div class="tgt-row">
        <div style="min-width:0">
          <div class="tgt-name">${esc(c.name)}</div>
          <div class="tgt-detail">${esc(c.detail || '')}</div>
          ${renamed ? `<div class="tgt-was">已订阅,库里存作「${esc(own.name)}」</div>` : ''}
        </div>
        <button class="ghost" data-tgt-add="${esc(c.name)}" data-tgt-id="${esc(c.id)}"
                style="padding:3px 12px;font-size:12px;flex:none" ${own ? 'disabled' : ''}>
          ${own ? '已订阅' : '添加'}
        </button>
      </div>`;
  }).join('');
}

async function searchTargets() {
  const q = $('tgt-search').value.trim();
  if (!q || tgtBusy) return;
  tgtBusy = true;
  $('tgt-search-btn').textContent = '搜索中…';
  try {
    const r = await fetch('/api/sources/targets/search?q=' + encodeURIComponent(q));
    if (await toastIfFail(r, '搜索失败')) return;
    tgtCands = (await r.json()).candidates || [];
    renderTgtCands();
    if (!tgtCands.length) toast(`没搜到「${q}」`, { type: 'info' });
  } finally {
    tgtBusy = false;
    $('tgt-search-btn').textContent = '搜索';
  }
}

/** 增删改共用:发请求 → 成功就刷新列表并 toast 后端给的文案 */
async function tgtAction(path, body, fallback) {
  if (tgtBusy) return;
  tgtBusy = true;
  try {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (await toastIfFail(r, fallback)) return;
    toast((await r.json()).detail || '已完成', { type: 'ok' });
    await reloadTargets();
    renderTgtCands();          // 候选里的"添加"要变成"已订阅"
    if (typeof refreshPipe === 'function') refreshPipe(false);
  } finally {
    tgtBusy = false;
  }
}

document.addEventListener('click', async e => {
  const add = e.target.closest('[data-tgt-add]');
  if (add) {
    return tgtAction('/api/sources/targets/add',
      { name: add.dataset.tgtAdd, id: add.dataset.tgtId || '' }, '添加失败');
  }
  const toggle = e.target.closest('[data-tgt-toggle]');
  if (toggle) {
    const name = toggle.dataset.tgtToggle;
    const cur = tgtList.find(t => t.name === name);
    return tgtAction('/api/sources/targets/toggle',
      { name, enabled: !(cur && cur.enabled) }, '切换失败');
  }
  const rm = e.target.closest('[data-tgt-remove]');
  if (rm) {
    const name = rm.dataset.tgtRemove;
    const ok = await confirmBox({
      title: `移除「${name}」?`,
      body: '之后不再采集它的新文章。已经采集过的文章不受影响,仍留在知识库里。',
      ok: '移除', danger: true,
    });
    if (ok) await tgtAction('/api/sources/targets/remove', { name }, '移除失败');
  }
});

document.addEventListener('keydown', e => {
  if ($('tgt-modal') && $('tgt-modal').style.display !== 'none') {
    if (e.key === 'Escape') closeTargets();
    if (e.key === 'Enter' && e.target.id === 'tgt-search') searchTargets();
  }
});
