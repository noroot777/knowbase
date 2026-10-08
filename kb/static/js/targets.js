/* 采集目标(订阅了哪些公众号…)的管理,就地渲染在「我的订阅」页的公众号视图里。
 *
 * 只认 /api/sources/targets 这套通用接口,文案里的"公众号"来自后端给的 label
 * ——换来源时这个文件不用改。
 */

let tgtList = [];        // 已订阅
let tgtCands = [];       // 上次搜索的候选
let tgtLabel = '目标';
let tgtBusy = false;     // 有请求在飞:防连点造成重复添加/删除
// 归属没解析出来那几条候选,按钮上挂的解释。行里那句状态文案(signature)
// 已经说了是哪一档(没核验 / 读不动 / 确实没有图文),这里只说下一步怎么办。
const TGT_BLIND_HINT = '这条会话还没解析出归属,无法确认是哪个号;'
  + '按上面那行提示缩小关键词再搜一次';

/** 打开公众号订阅视图时调用:读订阅列表并就地渲染 */
async function loadTargets() {
  tgtCands = [];
  renderTgtCands();
  $('tgt-list').innerHTML = '<div class="empty">正在读取订阅列表…</div>';
  try { await loadPipeChannels(); } catch (e) {
    $('tgt-list').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  await reloadTargets();
}

async function reloadTargets() {
  const r = await fetch(channelUrl('/api/sources/targets')).catch(() => null);
  if (!r || !r.ok) {
    const reason = r ? await apiFail(r, '读取失败') : '连接失败';
    $('tgt-list').innerHTML = `<div class="empty">读取${esc(tgtLabel)}列表失败:${esc(reason)}<br><button class="ghost" style="margin-top:8px" onclick="loadTargets()">重新读取</button></div>`;
    return;
  }
  const d = await r.json();
  tgtList = d.targets || [];
  tgtLabel = d.label || tgtLabel;
  renderTargets();
}

/* 显示名 vs 档案键:来源那边改了名,列表要显示新的那个,否则你在绿泡泡里
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
  $('tgt-count').textContent = `共 ${tgtList.length} 个,${on} 个参与采集`;

  // 按名字排,不按启用状态:后端是"启用的在前",那样一点停用行就沉底、
  // 下一行顶上来接住第二次点击——连着停用几个时会误伤。位置稳定才点得准。
  // 排序用看得见的那个名字,否则改过名的行会排在一个没显示出来的键上。
  let rows = [...tgtList].sort(
    (a, b) => tgtShown(a).localeCompare(tgtShown(b), 'zh'));
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
    : `<div class="empty">还没有订阅任何${esc(tgtLabel)}</div>`;
  box.scrollTop = keepScroll;
}

function renderTgtCands() {
  const box = $('tgt-cands');
  if (!tgtCands.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
  /* 「已订阅」**只按身份(公众号是 fakeid/__biz)认,名字一律不作数**。
   * 按 id 认是为了改过名的号:只比名字的话它在这里永远显示成「添加」,而用户来搜
   * 新名字,正是因为列表上那个旧名字他已经认不出来了,点下去就多出一条同 id 的订阅。
   * 名字兜底怎么收都不对——它回答的是"这个名字订过没有",要显示的却是"这一条候选
   * 订过没有",而同名的两个会话是两个号,名字相同什么也证明不了。第 1 轮按「同屏
   * 只出现一次」放行,于是拿第二条下面那个编号单独搜一次(结果只剩它一条)就又按
   * 名字认成「已订阅」:候选 id 为空的、候选 id 明确是另一个 __biz 的,两种写法都
   * 这么显示,而真正订着的是同名的另一个号(第 2 轮审查两张截图)。
   * 名字被另一个号占着这件事不在这里判——后端那条闸门(AccountNameTaken)才是
   * 唯一口径,照它的说明拒掉比在浏览器里再写一份判据可靠。 */
  const byId = new Map(tgtList.filter(t => t.id).map(t => [t.id, t]));
  box.style.display = '';
  box.innerHTML = tgtCands.map(c => {
    const own = c.id ? byId.get(c.id) : null;
    // 认出是同一个号、但库里存的是另一个名字:说清楚,否则"已订阅"看着像认错了
    const renamed = own && own.name !== c.name;
    /* 归属(__biz)没解析出来的候选不能点添加:这里能发上去的只有名字和一个空 id,
     * 后端拿名字再搜一遍 —— 同名的另一个号就会被当成这一条入库,还报「已添加」
     * (第 1 轮审查复现)。后端那边同样按歧义拒掉,这里是别让它点得下去。 */
    const blind = !c.id;
    return `
      <div class="tgt-row">
        <div style="min-width:0">
          <div class="tgt-name">${esc(c.name)}</div>
          <div class="tgt-detail">${esc(c.detail || '')}</div>
          ${renamed ? `<div class="tgt-was">已订阅,库里存作「${esc(own.name)}」</div>` : ''}
        </div>
        <button class="ghost" data-tgt-add="${esc(c.name)}" data-tgt-id="${esc(c.id)}"
                style="padding:3px 12px;font-size:12px;flex:none"
                ${own || blind ? 'disabled' : ''}
                ${blind ? `title="${esc(TGT_BLIND_HINT)}"` : ''}>
          ${own ? '已订阅' : blind ? '不可添加' : '添加'}
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
  if (e.key === 'Enter' && e.target.id === 'tgt-search' && !e.isComposing) searchTargets();
});
