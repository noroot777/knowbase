// 每日日报:定时设置、立即生成、历史日报的阅读。
// 生成过程借用同步管道(每个通道跑一趟),细节留在同步中心里看;这里只展示
// "现在跑到哪个来源了"和结果,同一件事不做两套进度展示。
let dailySettings = null, dailyChannels = [], dailyReports = [], dailyWindow = null;
let dailyCurrent = '', dailyState = null, dailyTimer = null;
// 上一次看到的那一轮**已结束**运行的身份(还没查过状态是 null,正在跑是空串)。
// 身份是 run_id 和完成时刻**两段拼起来的**,哪一段变了都算新一轮:承重的是
// run_id(`finished_at` 只精确到秒,同一秒里跑完的两轮拿它分不开),而完成时刻
// 是它撞号时的兜底 —— 多刷一次列表的代价,换"再不会静默停在旧正文上"。
let dailyDoneRun = null;
let dailyLoaded = false, dailyDirty = false, dailySaving = false, dailyBusy = false;
let dailyWindowDirty = false;

function dailyActive() { return activeTab === 'daily'; }

function dailyTime(value) {
  if (!value) return '';
  const d = new Date(value.replace(' ', 'T'));
  return Number.isNaN(d.getTime()) ? value
    : d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

// 横幅只有一条,却有两个来源:生成失败的原因、列表读不出来的原因。清空时必须
// 认一下是谁写的——列表刷新成功就顺手清掉的话,刚生成失败的那句原因一闪就没了
// (生成失败后紧接着就会刷一次列表),用户只看到按钮弹回去、什么也没发生。
let dailyErrorKind = '';

function dailyError(message = '', kind = '') {
  $('daily-error').hidden = !message;
  $('daily-error-text').textContent = message;
  dailyErrorKind = message ? kind : '';
}

function clearDailyError(kind) {
  if (dailyErrorKind === kind) dailyError();
}

async function loadDailyPanel() {
  await Promise.all([loadDailySettings(), loadDailyReports(), loadDailyWindow()]);
  refreshDailyStatus();
}

// ── 覆盖区间 ────────────────────────────────────────────────────────────────
// 日报覆盖的是「上一份日报的截止时刻 → 这一次的生成时刻」这段时间里**发布**的
// 内容,不是某个自然日、也不是这一轮抓到了什么。默认区间由后端按上一份日报算,
// 这里只负责显示和让人改。

function dailyClock(iso) {
  // '2026-09-29T08:00' → '09-29 08:00'。不过 Date:本地时区解析再格式化,
  // 中途换一次时区表示只会给自己找麻烦,后端给的本来就是本地时刻。
  return iso ? iso.slice(5, 16).replace('T', ' ') : '';
}

function renderDailyWindowLabel() {
  if (!dailyWindow) return;
  const since = dailyClock($('daily-since').value || dailyWindow.since);
  $('daily-window-label').textContent = dailyWindowDirty
    ? `覆盖 ${since} → ${dailyClock($('daily-until').value)}`
    : `覆盖 ${since} 起到生成时刻`;
  renderDailyGenButton();
}

function dailyTargetDate() {
  // 这一次会写到哪一天那份日报上。**只有这一份口径**:按钮叫"生成"还是"重新
  // 生成"、要不要先弹覆盖确认,问的是同一件事,两处各判一遍必然各漂各的。
  // 自定义区间落在截止时刻那天,默认区间落在今天(生成时刻)。
  return dailyWindowDirty && $('daily-until').value
    ? $('daily-until').value.slice(0, 10)
    : new Date().toLocaleDateString('sv');
}

function renderDailyGenButton() {
  // 这颗按钮的名字跟着"那一天有没有日报"走:已经有就是**重新生成**(点下去会
  // 覆盖它,所以还会弹一次确认),没有就是头一份。写死成"重新生成"的话,第一次
  // 用的人会以为自己漏掉了一步。
  const exists = dailyReports.some(r => r.date === dailyTargetDate());
  $('daily-gen-label').textContent = exists ? '重新生成日报' : '生成日报';

  // 「再出一份能多带进来多少」。日报是钉在某一天的产物,生成完不会自己跟着新
  // 内容变 —— 重试失败项、新加一个订阅之后,得有个地方让人看出攒下了东西,再
  // 决定要不要为这些内容再花一次模型钱。它长在按钮里:回答的是"点这颗会得到
  // 什么",贴在覆盖区间那行字后面就成了另一个问题的答案。
  //
  // 自定义区间时不报这个数——后端算的是默认区间(接上一份日报)那一段。
  // 后端查不出来(pending 给 null)时整颗藏掉,**不写成「没有新内容」**:
  // 那是在把"不知道"说成"没有"。
  const chip = $('daily-gen-count');
  const p = dailyWindowDirty ? null : dailyWindow?.pending;
  chip.hidden = !p;
  if (!p) { $('daily-gen').title = ''; return; }
  chip.textContent = p.total ? `+${p.total} 条新内容` : '没有新内容';
  chip.classList.toggle('is-none', !p.total);
  $('daily-gen').title = p.total
    ? `有 ${p.total} 条内容还没进过任何一份日报（${p.items} 条在本段窗口内发布、`
      + `${p.late} 条发布得更早）。再生成一次会把它们带进来。`
    : '上一份日报之后没有新内容进来，再生成一次得到的基本是同一份。';
}

async function loadDailyWindow() {
  // 改过区间就不覆盖用户填的那两个时刻,但按钮上的名字和计数照样要重算一次
  // ——「这一天有没有日报」是跟着列表变的,和区间改没改无关。
  if (dailyWindowDirty) { renderDailyGenButton(); return; }
  try {
    dailyWindow = await pipeJson('/api/daily/window');
  } catch { return; }
  $('daily-since').value = dailyWindow.since;
  $('daily-until').value = dailyWindow.until;
  renderDailyWindowLabel();
}

function toggleDailyWindow() {
  const box = $('daily-window');
  box.hidden = !box.hidden;
  $('daily-window-toggle').setAttribute('aria-expanded', String(!box.hidden));
}

function markDailyWindow() {
  dailyWindowDirty = true;
  renderDailyWindowLabel();
}

async function resetDailyWindow() {
  dailyWindowDirty = false;
  await loadDailyWindow();
}

// ── 定时设置 ────────────────────────────────────────────────────────────────

function dailyScheduleMessage(s) {
  if (!s.enabled) return '定时生成未启用';
  const next = s.next_run ? ` · 下次 ${dailyTime(s.next_run)}` : '';
  return `已保存：每天 ${s.time} ${s.sync ? '先同步再生成' : '只用已入库的内容生成'}${next}`;
}

async function loadDailySettings() {
  if (dailySaving || dailyDirty) return;
  try {
    const data = await pipeJson('/api/daily/settings');
    dailySettings = data.settings; dailyChannels = data.channels;
    $('daily-on').checked = !!dailySettings.enabled;
    $('daily-time').value = dailySettings.time;
    $('daily-sync').checked = !!dailySettings.sync;
    $('daily-firstdays').value = dailySettings.first_days;
    renderDailyChannels();
    dailyLoaded = true;
    $('daily-sched-fields').disabled = false;
    $('daily-sched-msg').textContent = dailyScheduleMessage(dailySettings);
  } catch (e) {
    $('daily-sched-msg').textContent = '读取定时设置失败：' + e.message;
  }
}

function renderDailyChannels() {
  const picked = new Set(dailySettings.channels);
  $('daily-channels').innerHTML = dailyChannels.map(c =>
    `<label class="daily-channel"><input type="checkbox" data-channel="${esc(c.id)}"
      ${picked.has(c.id) ? 'checked' : ''} onchange="markDailyDirty()"> ${esc(c.label)}</label>`).join('');
}

function pickedDailyChannels() {
  return [...$('daily-channels').querySelectorAll('input:checked')].map(i => i.dataset.channel);
}
function markDailyDirty() {
  if (!dailyLoaded || dailySaving) return;
  dailyDirty = true;
  $('daily-sched-save').disabled = false;
  $('daily-sched-msg').textContent = '有未保存的修改';
}

async function saveDailySettings() {
  if (!dailyLoaded || dailySaving || !dailyDirty) return;
  if (!$('daily-time').reportValidity() || !$('daily-firstdays').reportValidity()) return;
  const channels = pickedDailyChannels();
  if (!channels.length) { $('daily-sched-msg').textContent = '至少勾选一个同步范围'; return; }
  dailySaving = true; $('daily-sched-save').disabled = true;
  $('daily-sched-save').textContent = '正在保存…';
  try {
    dailySettings = await pipePost('/api/daily/settings', {
      enabled: $('daily-on').checked, time: $('daily-time').value,
      sync: $('daily-sync').checked, first_days: Number($('daily-firstdays').value) || 1, channels,
    });
    dailyDirty = false;
    $('daily-sched-msg').textContent = dailyScheduleMessage(dailySettings);
  } catch (e) {
    $('daily-sched-msg').textContent = '未保存：' + e.message + '。修改已保留，可重试。';
  } finally {
    dailySaving = false; $('daily-sched-save').textContent = '保存设置';
    $('daily-sched-save').disabled = !dailyDirty;
  }
}

// ── 历史日报 ────────────────────────────────────────────────────────────────

async function loadDailyReports(keepSelection = true) {
  try {
    const data = await pipeJson('/api/daily/reports');
    dailyReports = data.reports;
    clearDailyError('list');
  } catch (e) {
    dailyError('日报列表读取失败：' + e.message, 'list');
    return;
  }
  $('daily-count').textContent = dailyReports.length;
  $('daily-latest').textContent = dailyReports.length
    ? `最近一份 ${dailyReports[0].date}` : '还没有生成过日报';
  $('daily-list-note').textContent = dailyReports.length ? `共 ${dailyReports.length} 份` : '';
  renderDailyList();
  const wanted = keepSelection && dailyReports.some(r => r.date === dailyCurrent)
    ? dailyCurrent : dailyReports[0]?.date;
  // await:调用方("生成完了,刷新一下")等的是"新那一份已经在屏幕上",不是
  // "列表请求回来了"。不等的话阅读区还挂着上一份的正文和元信息,看起来像是
  // 生成没生效(2026-09-30 复审在全新沙箱上抓到,回归因此假红)。
  if (wanted) await openDailyReport(wanted);
  // 按钮叫"生成"还是"重新生成"取决于目标那天有没有日报,而那正是这份列表。
  renderDailyGenButton();
}

function renderDailyList() {
  $('daily-list').innerHTML = dailyReports.map(r => `<button class="daily-row ${r.date === dailyCurrent ? 'on' : ''}"
    data-report="${esc(r.date)}"><b>${esc(r.date)}</b>
    <span>${r.docs} 条素材${r.total > r.docs ? ` / 窗口内 ${r.total}` : ''}${r.late ? ` · 补录 ${r.late}` : ''}</span>
    <small>${esc(Object.entries(r.sources || {}).map(([k, n]) => `${k} ${n}`).join(' · ') || '—')}${
      r.sync_failed?.length ? ` · ${r.sync_failed.length} 个来源未同步` : ''}</small></button>`).join('')
    || '<div class="daily-empty"><h3>还没有日报</h3><p>选好日期，点右上角生成。</p></div>';
}

// 「这次正文读取是不是还算数」。**两道各管一段,缺一不可**:
// ①`dailyCurrent !== date` —— 选中的已经不是这一天了。删除那条路把 dailyCurrent 清成
//   空串却**不经过本函数**(所以序号不动),在途的那次读取只有这一道挡得住,否则刚删掉的
//   正文会被画回阅读区。
// ②序号 —— **同一天又读了一次**,这一次已经被替代。原来只有①:用户点开今天这一份(请求
//   慢),紧接着点「重新生成」,生成完那一跳自动重读同一天 —— 两次的 date 逐字相同,于是
//   先发后到的那份旧正文把新的盖回去,新文章闪一下就消失,而完成身份已经记下、后面多少轮
//   轮询都不会再补刷(审查实测:左边列表写着 5 条,正文只剩 4 条,只有手动再点一次才回来)。
let dailyReadSeq = 0;

async function openDailyReport(date) {
  dailyCurrent = date;
  renderDailyList();
  const seq = ++dailyReadSeq;
  const stale = () => dailyCurrent !== date || seq !== dailyReadSeq;
  try {
    const r = await pipeJson('/api/daily/reports/' + encodeURIComponent(date));
    if (stale()) return;
    $('daily-reader-title').textContent = '日报 · ' + r.date;
    $('daily-reader-meta').textContent =
      `覆盖 ${dailyClock(r.window_since.replace(' ', 'T'))} → ${dailyClock(r.window_until.replace(' ', 'T'))} 发布的内容 · ` +
      `${r.docs} 条素材${r.total > r.docs ? `（窗口内共 ${r.total} 条）` : ''}` +
      `${r.late ? ` · 补录 ${r.late} 条` : ''} · ${modelLabel(r.model)} · 生成于 ${r.generated}`;
    // 正文是模型写的 markdown(外部内容),mdHtml 里过 DOMPurify。开头那行
    // `# 日报 · 日期` 是给 Obsidian 看的,面板上标题栏已经写着同一句,渲染出来
    // 就是连着两行一模一样的标题;跟在它后面那句「> 覆盖 … 发布的内容」同理,
    // 元信息那一行已经写了。两处都只在面板上剥掉,vault 里的文件一个字不改。
    // 正文里那段「本轮有 N 个来源没有同步成功」跟着 md 一起渲染出来(剥掉的只有
    // 标题和覆盖区间这两行重复信息),所以历史日报翻回来也看得见当时的同步缺口。
    $('daily-reader-body').innerHTML =
      mdHtml(r.body.replace(/^\s*#\s+[^\n]*\n+(?:>\s*覆盖[^\n]*\n)?/, ''));
    enhanceDailyBody($('daily-reader-body'));
    $('daily-copy').hidden = $('daily-del').hidden = false;
    $('daily-copy').dataset.body = r.body;
  } catch (e) {
    // 失败这一侧走同一道判据:被替代的那次读取报出来的错也是陈旧的,
    // 让它覆盖新正文等于拿一句「读取失败」盖掉一份读到了的日报。
    if (stale()) return;
    $('daily-reader-body').innerHTML = `<div class="daily-empty" role="alert"><h3>读取失败</h3><p>${esc(e.message)}</p></div>`;
  }
}

// ── 正文里的出处链接 ────────────────────────────────────────────────────────────
// 序号→原文的对应关系写在每份日报自己的素材清单里,所以这里不问接口,只读这一篇。
// **读的是渲染之后的那个 <ol>,不是 markdown 原文**:清单那几行的目标写法有两种
// (老日报裸写 url、新日报套尖括号),而裸写的 url 自己可以带平衡圆括号 ——
// `https://…/paper-(clean).pdf` 用正则一路吃到第一个 `)` 就断,那一条整行匹配
// 不上、编号也就补不出链接(2026-09-30 审查实测,生产那份日报里真有这么一条)。
// marked 本来就按 CommonMark 把它解析对了,照抄解析结果比再写一遍解析器可靠。
function dailyRefMap(el) {
  const out = new Map();
  const head = [...el.querySelectorAll('h2')].find(h => /^素材清单/.test(h.textContent.trim()));
  let list = head && head.nextElementSibling;
  // 清单为空时别一路找到文末去认别的有序列表 —— 下一个小节标题就是边界。
  while (list && list.tagName !== 'OL' && list.tagName !== 'H2') list = list.nextElementSibling;
  if (!list || list.tagName !== 'OL') return { refs: out, list: null };
  // 清单是从 1 数起的一段有序列表;真被改过 start 就按它算。
  const base = Number(list.getAttribute('start')) || 1;
  [...list.children].forEach((li, i) => {
    const a = li.querySelector('a[href]');
    // 没有 url 的素材那一行没有链接,补不出来 —— 跳过就是对的。
    if (a) out.set(String(base + i), { url: a.getAttribute('href'), title: li.textContent.trim() });
  });
  return { refs: out, list };
}

// 正文里那些出处编号要能点。新日报落盘时就已经写成链接了(kb/daily.py 的
// `_link_refs`),这里补两件事:
//   ① 那之前生成的日报编号还是纯文本。它们是花过模型钱的产物,不该为了加链接
//      重新生成一份(重算窗口还会换掉内容),照素材清单补上就是。新日报上这一步
//      是空转——编号已经在 <a> 里,遍历时直接跳过。
//   ② 外链一律新标签打开。日报正文和素材清单指的都是站外地址,在当前标签里跳走
//      等于把 kb-web 连同正在跑的管道面板一起顶掉。
// 认的编号写法和落盘那条路一致(`[1]` / `[1, 2]` / `[1、2]`):两边不一样的话,
// 同一种写法在新旧日报上一个能点一个不能。
const DAILY_REF_TEXT = /\[(\d{1,3}(?:\s*[,，、]\s*\d{1,3})*)\]/g;

function enhanceDailyBody(el) {
  const { refs, list } = dailyRefMap(el);
  if (refs.size) {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
      // 已经是链接的编号、代码块里的数字、清单自己那一段都别碰
      acceptNode: n => n.parentElement && (n.parentElement.closest('a, code, pre')
        || (list && list.contains(n))) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
    const todo = [];
    // 粗筛用无状态的那条:带 g 的正则 test() 会把 lastIndex 留在命中处,
    // 下一个文本节点就从半截位置开始找,开头的编号会被漏掉。
    for (let n = walker.nextNode(); n; n = walker.nextNode())
      if (/\[\d/.test(n.nodeValue)) todo.push(n);
    // 先收集再替换:边走边改会把 walker 的游标扔在一个已经离开文档的节点上。
    for (const node of todo) {
      const text = node.nodeValue, frag = document.createDocumentFragment();
      let last = 0;
      for (const m of text.matchAll(DAILY_REF_TEXT)) {
        const nums = m[1].split(/[,，、]/).map(n => n.trim());
        // 一个都对不上就整段别动:那多半不是出处编号(年份区间、条目序号之类)。
        if (!nums.some(n => refs.has(n))) continue;
        frag.append(text.slice(last, m.index));
        for (const n of nums) {
          const hit = refs.get(n);
          if (!hit) { frag.append(`[${n}]`); continue; }
          const a = document.createElement('a');
          a.href = safeUrl(hit.url); a.title = hit.title; a.textContent = `[${n}]`;
          frag.append(a);
        }
        last = m.index + m[0].length;
      }
      frag.append(text.slice(last));
      node.replaceWith(frag);
    }
  }
  for (const a of el.querySelectorAll('a[href]')) {
    if (/^https?:/i.test(a.getAttribute('href') || '')) {
      a.target = '_blank'; a.rel = 'noopener noreferrer';
    }
    // 角标样式认的是"链接文字就是 [12]",落盘写进去的和刚补上的一视同仁。
    if (/^\[\d{1,3}\]$/.test((a.textContent || '').trim())) a.classList.add('daily-ref');
  }
}

async function copyDailyReport() {
  try {
    await navigator.clipboard.writeText($('daily-copy').dataset.body || '');
    toast('日报已复制', { type: 'ok' });
  } catch (e) { toast('复制失败', { detail: e.message }); }
}

async function deleteDailyReport() {
  if (!dailyCurrent) return;
  if (!await confirmBox({ title: `删除 ${dailyCurrent} 的日报？`,
    body: '只删这份日报，素材与索引不受影响。删掉后可以重新生成。', ok: '删除', danger: true })) return;
  try {
    await pipeJson('/api/daily/reports/' + encodeURIComponent(dailyCurrent), { method: 'DELETE' });
    dailyCurrent = '';
    $('daily-copy').hidden = $('daily-del').hidden = true;
    $('daily-reader-title').textContent = '还没有选中日报';
    $('daily-reader-meta').textContent = '';
    $('daily-reader-body').innerHTML = '<div class="daily-empty"><h3>已删除</h3><p>可以重新生成这一天的日报。</p></div>';
    // 删一份日报会把它带走的条目放回队列,所以按钮上那个计数也得重新问一次
    // ——不问的话页面上还写着「没有新内容」,而此刻明明又有了。
    await Promise.all([loadDailyReports(false), loadDailyWindow()]);
  } catch (e) { toast('删除失败', { detail: e.message }); }
}

// ── 生成 ────────────────────────────────────────────────────────────────────

async function runDaily(sync) {
  if (dailyBusy || dailyState?.running) return;
  // 改过区间就按改的来;没改就什么都不传,让后端按上一份日报接着算——区间的
  // 截止时刻必须是**生成那一刻**,同步跑掉的几小时也该算进来,在这里钉死的话
  // 那几小时会掉进两份日报中间的缝里。
  const custom = dailyWindowDirty
    ? { since: $('daily-since').value, until: $('daily-until').value } : {};
  if (custom.since && custom.until && custom.since >= custom.until) {
    toast('起始时刻要早于截止时刻'); return;
  }
  // 同步范围一个都没勾:同步阶段会什么都不做、却一路报成功,"同步全部并生成"
  // 静静地变成"直接生成"。后端也会拒(daily_run.start),这里先拦一道,好把
  // 话说在按钮边上。
  const channels = pickedDailyChannels();
  if (sync && !channels.length) {
    toast('先勾选至少一个同步范围', { key: 'daily',
      detail: '同步范围在下面的「每天定时生成」里；只想用已入库的内容出报，点左边那颗生成按钮。' });
    return;
  }
  // 覆盖提示按「这一份会写到哪一天」判,和按钮叫什么名字共用 dailyTargetDate()。
  const date = dailyTargetDate();
  if (dailyReports.some(r => r.date === date) &&
      !await confirmBox({ title: `重新生成 ${date} 的日报？`,
        body: '这一天已经有一份日报，重新生成会覆盖它。', ok: '重新生成' })) return;
  dailyBusy = true; renderDailyButtons();
  try {
    await pipePost('/api/daily/run', { ...custom, sync, channels });
    toast(sync ? '已开始同步并生成日报' : '已开始生成日报',
      { key: 'daily', type: 'ok', detail: sync ? '依次同步所选来源后生成，离开页面不影响。' : '直接用已入库的内容生成。' });
  } catch (e) {
    toast('未能开始', { key: 'daily', detail: e.message });
  } finally { dailyBusy = false; refreshDailyStatus(); }
}

async function stopDaily() {
  try {
    // 说明文案跟着后端给的那一份走:受理与不受理(尤其"生成阶段停不了")
    // 的口径只有一份,两边各写一句迟早对不上。
    const r = await pipePost('/api/daily/stop');
    toast('已请求停止', { key: 'daily', type: 'info', detail: r.detail || '' });
  } catch (e) { toast('停止请求未完成', { key: 'daily', detail: e.message }); }
  finally { refreshDailyStatus(); }
}

function renderDailyButtons() {
  const running = !!dailyState?.running;
  $('daily-gen').disabled = $('daily-sync-gen').disabled = dailyBusy || running;
  $('daily-sync-gen').textContent = running ? '生成中…' : '同步全部并生成';
  renderDailyGenButton();
  // 进了生成阶段就停不了了:模型已经在出稿,daily.generate() 自己把文件写完
  // 才返回。按钮留着能点、点了又被拒,不如置灰并把原因写在上面。
  const generating = running && dailyState?.phase === 'generate';
  $('daily-stop').disabled = generating || !!dailyState?.stop_requested;
  $('daily-stop').title = generating ? '正在出稿，这一阶段不能中途停止' : '';
  $('nav-daily-dot').hidden = !running;
}

// 同步缺口横幅:哪些来源这一轮没同步成功。toast 会自己消失,而"这份日报只用了
// 已入库的素材"是看这份日报时一直要知道的事——一个来源都没同步到的那一轮,页面
// 原来只有一句绿色的「日报已生成」(2026-09-30 审查实测)。
function dailyChannelResults(s) {
  // 重启 kb-web 之后进程内状态是空的,上一轮的通道结果留在落盘的 last_run 里。
  return (s?.channels?.length ? s.channels : s?.last_run?.channels) || [];
}

function renderDailySyncNote(s) {
  const bad = dailyChannelResults(s).filter(c => c.status !== 'ok');
  $('daily-sync-note').hidden = !bad.length;
  if (!bad.length) return;
  $('daily-sync-note-text').textContent =
    `本轮有 ${bad.length} 个来源没有同步成功：`
    + bad.map(c => `${c.label}（${c.detail || '未同步成功'}）`).join('；')
    // 只有真出了报才提这一句;停止/失败那两档日报压根没生成。
    + (s?.phase === 'done' ? '。日报用的是已经入库的内容。' : '。');
}

function dailyPhaseText(s) {
  if (s.phase === 'failed') return '上次生成失败：' + (s.error || '未知原因');
  if (s.phase === 'stopped') return s.detail || '已停止，这一轮没有生成日报';
  if (s.phase === 'done') return s.detail || '日报已生成';
  return s.detail || '正在准备';
}

async function refreshDailyStatus() {
  clearTimeout(dailyTimer); dailyTimer = null;
  let s;
  try {
    s = await pipeJson('/api/daily/status');
  } catch {
    queueDailyPoll(); return;          // 状态读不到不清空面板,下一跳再试
  }
  const finished = dailyState?.running && !s.running;
  // 「有没有出现一轮新的已结束运行」。只认 running→idle 那个跳变是不够的:不同步
  // 直接生成那条路(桩模型、素材不多时)在两次轮询之间就跑完了,页面一次都没见过
  // running=true —— 于是按钮上那个数字归零了,而列表和阅读区还停在上一版(审查
  // 实测:接口已经是「素材清单(4 条)」,页面仍显示 3 条和旧的生成时刻,只有整页
  // 刷新才看得到新内容)。后台定时那一轮、别的页面点的那一轮同理。
  // 第一跳(dailyDoneRun === null)不算新结果:那时页面刚打开,正文本来就是现读的。
  const doneRun = s.running ? ''
    : [s.run_id, s.finished_at].filter(Boolean).join('|');
  const fresh = dailyDoneRun !== null && doneRun && doneRun !== dailyDoneRun;
  dailyDoneRun = doneRun;
  dailyState = s;
  $('daily-progress').hidden = !s.running;
  $('daily-progress-text').textContent = dailyPhaseText(s);
  // 失败原因留在横幅里,直到下一轮真的跑起来才清——列表读取失败的提示也用
  // 这条横幅,顺手清掉的话用户只看到一片空白列表而没有任何原因。
  if (!s.running && s.phase === 'failed') dailyError(s.error || '日报生成失败', 'run');
  else if (s.running) clearDailyError('run');
  renderDailyButtons();
  renderDailySyncNote(s);
  if (finished) {
    const bad = dailyChannelResults(s).filter(c => c.status !== 'ok');
    if (s.phase === 'failed') toast('日报未生成', { key: 'daily', detail: s.error || '' });
    else if (s.phase === 'stopped') toast('已停止', { key: 'daily', type: 'info', detail: s.detail || '' });
    // 有来源没同步到就不报成功色:日报是出了,但它不是"同步全部并生成"承诺的
    // 那一份,标成绿勾等于把降级说成完整成功。
    else if (bad.length) toast(`日报已生成，但 ${bad.length} 个来源未同步成功`,
      { key: 'daily', type: 'info', detail: s.detail || '' });
    else toast('日报已生成', { key: 'daily', type: 'ok', detail: s.detail || '' });
    if (dailyActive()) {
      dailyCurrent = s.result?.date || dailyCurrent;
      // 上一份日报的截止时刻变了,默认区间跟着往前挪。
      await Promise.all([loadDailyReports(), loadDailyWindow()]);
    }
  } else if (fresh && dailyActive()) {
    // 没见过 running 的那一轮:把列表、正文和默认区间补上(loadDailyReports 自己
    // 会重开当前选中那一份,所以阅读区跟着一起更新),**不弹 toast** —— 这条路也
    // 会在别处(定时、CLI、另一个标签页)跑完一轮时走到,弹窗就成了噪音。
    dailyCurrent = s.result?.date || dailyCurrent;
    await Promise.all([loadDailyReports(), loadDailyWindow()]);
  }
  // 素材是**从外面**进来的:同步中心那边跑完一轮、CLI 补抓、定时同步,页面这边
  // 一个事件都收不到。只在"日报从运行变成完成"那一跳重读的话,按钮上的数字会
  // 一直停在打开页面那一刻(审查实测:后台插进一篇,页面仍写着「没有新内容」,
  // 要点一次「改区间」再「接上一份日报」才更新)。所以空闲时每一跳都问一次。
  if (!s.running && dailyActive()) await refreshDailyPending();
  queueDailyPoll();
}

async function refreshDailyPending() {
  // 只更新按钮上那个数字,**不碰两个时刻输入框**:默认区间的截止时刻是"此刻",
  // 每跳重填一次会让展开着的「改区间」里的值自己往前跳。
  if (dailyWindowDirty || !dailyWindow) return;
  try {
    dailyWindow.pending = (await pipeJson('/api/daily/window')).pending;
  } catch { return; }
  renderDailyGenButton();
}

function queueDailyPoll() {
  clearTimeout(dailyTimer);
  // 不在日报页时也慢速轮询:定时日报是后台起的,侧栏那颗点得亮起来,
  // 否则用户在别的页面上完全看不出正在生成。
  dailyTimer = setTimeout(refreshDailyStatus,
    dailyState?.running ? 3000 : dailyActive() ? 15000 : document.hidden ? 120000 : 60000);
}

$('daily-list').addEventListener('click', e => {
  const row = e.target.closest('[data-report]');
  if (row) openDailyReport(row.dataset.report);
});
