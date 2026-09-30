// 每日日报:定时设置、立即生成、历史日报的阅读。
// 生成过程借用同步管道(每个通道跑一趟),细节留在同步中心里看;这里只展示
// "现在跑到哪个来源了"和结果,同一件事不做两套进度展示。
let dailySettings = null, dailyChannels = [], dailyReports = [], dailyWindow = null;
let dailyCurrent = '', dailyState = null, dailyTimer = null;
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
}

async function loadDailyWindow() {
  if (dailyWindowDirty) return;
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
  return `已保存：每天 ${s.time} ${s.sync ? '先同步再生成' : '直接生成'}${next}`;
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
// 采集通道在设置里换过了：候选换成新的那条，原来勾着绿泡泡的仍然勾着绿泡泡。
// 这一组只在读设置时渲染一次，不在这儿换的话它会一直列着一条已经不存在的来源。
document.addEventListener('kb:channels-changed', e => {
  if (!dailyLoaded) return;
  dailyChannels = pipeChannels;
  const picked = pickedDailyChannels().map(c => (c === e.detail.before ? e.detail.after : c));
  dailySettings = { ...dailySettings, channels: [...new Set(picked)] };
  renderDailyChannels();
});

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
}

function renderDailyList() {
  $('daily-list').innerHTML = dailyReports.map(r => `<button class="daily-row ${r.date === dailyCurrent ? 'on' : ''}"
    data-report="${esc(r.date)}"><b>${esc(r.date)}</b>
    <span>${r.docs} 条素材${r.total > r.docs ? ` / 窗口内 ${r.total}` : ''}${r.late ? ` · 补录 ${r.late}` : ''}</span>
    <small>${esc(Object.entries(r.sources || {}).map(([k, n]) => `${k} ${n}`).join(' · ') || '—')}${
      r.sync_failed?.length ? ` · ${r.sync_failed.length} 个来源未同步` : ''}</small></button>`).join('')
    || '<div class="daily-empty"><h3>还没有日报</h3><p>选好日期，点右上角生成。</p></div>';
}

async function openDailyReport(date) {
  dailyCurrent = date;
  renderDailyList();
  try {
    const r = await pipeJson('/api/daily/reports/' + encodeURIComponent(date));
    if (dailyCurrent !== date) return;
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
    $('daily-copy').hidden = $('daily-del').hidden = false;
    $('daily-copy').dataset.body = r.body;
  } catch (e) {
    if (dailyCurrent !== date) return;
    $('daily-reader-body').innerHTML = `<div class="daily-empty" role="alert"><h3>读取失败</h3><p>${esc(e.message)}</p></div>`;
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
    await loadDailyReports(false);
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
      detail: '同步范围在下面的「每天定时生成」里；只想用已入库的内容出报就点「直接生成」。' });
    return;
  }
  // 覆盖提示按「这一份会写到哪一天」判:自定义区间落在截止时刻那天,
  // 默认区间落在今天(生成时刻)。
  const date = custom.until ? custom.until.slice(0, 10) : new Date().toLocaleDateString('sv');
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
  }
  queueDailyPoll();
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
