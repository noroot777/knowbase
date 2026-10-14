// Patch stable stage nodes; polling must not steal focus, close details or jump logs.
const PIPE_STATUS = { pending: '等待中', running: '执行中', ok: '已完成', skipped: '已跳过', not_planned: '本次不执行', error: '需处理', stopped: '未执行', '': '未开始' };
function pipeHasFailure(stage) {
  return stage?.status === 'error' || /失败 [1-9]|\d+ 个账号失败/.test(stage?.detail || '');
}
function pipeResult(s) { return s.running ? s : s.last_run || s; }
function pipePlannedStages(result) {
  const keys = Object.keys(STAGE_META);
  if (Array.isArray(result.planned_stages)) return keys.filter(k => result.planned_stages.includes(k));
  // Older snapshots encode exclusions in the skipped reason. A runtime skip or
  // an early stop remains part of the original plan and must not shrink it.
  return keys.filter(k => {
    const stage = result.stages?.[k];
    return stage?.status && !(stage.status === 'skipped' && ['本次无需重试', '未选择'].includes(stage.detail));
  });
}
function pipeRunLabel(result) {
  const legacyRetry = !result.run_kind && Object.values(result.stages || {}).some(st => st.status === 'skipped' && st.detail === '本次无需重试');
  return result.run_kind === 'retry' || legacyRetry ? '重试' : '同步';
}
function pipeStateHint(s, result, failed, skipped, progress) {
  if (s.running) {
    if (s.stop_requested) return '正在等待执行中的步骤收尾；全部结束前暂不能新建同步，已完成结果会保留。';
    if (failed) return `已有 ${failed} 个步骤失败，本轮仍在执行。请等全部步骤结束后再新建同步；失败项不会阻止下一轮。`;
    return `本轮${progress.label}尚未结束，暂不能新建同步。任务在后台执行，切换页面不会中断。`;
  }
  if (failed) return `${result.stopped ? '本轮已停止，结果已保留。' : ''}上次有 ${failed} 个步骤失败，不影响新建同步。可重新配置一次同步，或按上次抓取范围重试失败项。`;
  if (result.stopped) return '已完成结果已保留。可重新配置同步，已有内容会按现有规则去重。';
  if (skipped) return `${skipped} 个步骤被跳过，具体原因见下方。`;
  if (result.finished_at && progress.total && progress.completed === progress.total) return `本次${progress.label}计划的 ${progress.total} 个步骤均已完成。`;
  return '同步在后台执行，切换页面不会中断。';
}
function pipeDuration(start, finish) {
  const from = new Date((start || '').replace(' ', 'T')).getTime();
  const to = finish ? new Date(finish.replace(' ', 'T')).getTime() : Date.now();
  if (!Number.isFinite(from) || !Number.isFinite(to)) return '';
  const minutes = Math.floor(Math.max(0, to - from) / 60000);
  return minutes < 1 ? '不到 1 分钟' : minutes < 60 ? `${minutes} 分钟` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
}
function initPipeStageRows() {
  if ($('pipe-stages').children.length) return;
  $('pipe-stages').innerHTML = Object.entries(STAGE_META).map(([key, meta], i) => `
    <div class="pipe-stage" id="pipe-stage-${key}" data-status="">
      <span class="pipe-stage-number">${i + 1}</span><div><h3>${meta.name}</h3><p class="pipe-stage-desc">${meta.desc}</p><p class="pipe-stage-detail"></p><p class="pipe-stage-pending" hidden></p>
      <details hidden><summary></summary><div class="pipe-failures"></div></details></div><span class="pipe-stage-status"></span>
    </div>`).join('');
}
function renderPipeState(s) {
  initPipeStageRows();
  const result = pipeResult(s), stages = result.stages || {};
  const planned = pipePlannedStages(result), label = pipeRunLabel(result);
  for (const key of Object.keys(STAGE_META)) {
    const st = stages[key] || { status: '', detail: '' }, row = $('pipe-stage-' + key);
    const status = !planned.includes(key) && st.status === 'skipped' ? 'not_planned' : pipeHasFailure(st) ? 'error' : st.status || '';
    row.dataset.status = status;
    row.querySelector('.pipe-stage-status').textContent = PIPE_STATUS[status] || '状态未知';
    row.querySelector('.pipe-stage-detail').textContent = st.detail || (s.running && status === 'pending' ? '前序步骤结束后自动开始' : '');
    const pending = row.querySelector('.pipe-stage-pending');
    pending.hidden = s.running || pipePending[key] == null;
    pending.textContent = pipePending[key] != null ? `当前待处理 ${pipePending[key]} 项` : '';
    const details = row.querySelector('details'), failures = st.failures || [];
    details.hidden = !failures.length;
    const waiting = failures.every(f => f.pending);
    details.querySelector('summary').textContent = `${waiting ? '待处理' : '问题'}明细 · ${failures.length} 项`;
    const list = details.querySelector('.pipe-failures');
    const signature = JSON.stringify(failures);
    if (list.dataset.signature !== signature) {
      const scroll = list.scrollTop;
      list.innerHTML = failures.map(f => `<p><b>${esc(f.title || '未命名内容')}</b><span>${esc(f.reason || '')}</span></p>`).join('');
      list.dataset.signature = signature; list.scrollTop = scroll;
    }
  }
  const values = planned.map(k => stages[k] || {}), failed = values.filter(pipeHasFailure).length;
  const completed = values.filter(st => st.status === 'ok' && !pipeHasFailure(st)).length;
  const active = Object.keys(STAGE_META).filter(k => stages[k]?.status === 'running').map(k => STAGE_META[k].name);
  const skipped = values.filter(st => st.status === 'skipped').length;
  let title = '准备好收录新内容';
  if (s.running) title = s.stop_requested ? '正在收尾，随后停止' : active.length ? (label === '重试' ? '正在重试：' : '') + active.join(' + ') : `${label}正在启动`;
  else if (result.stopped) title = `${label}已停止`;
  else if (failed) title = `${label}结束，${failed} 个步骤需处理`;
  else if (result.finished_at) title = skipped ? `${label}结束，部分步骤已跳过` : planned.length && completed === planned.length ? `${label}已完成` : `${label}结束，本次步骤未全部完成`;
  if ($('pipe-state-title').textContent !== title) $('pipe-state-title').textContent = title;
  const scopes = pipeRunScopes(result.crawl_filter), filter = result.crawl_filter || {};
  // 一轮里既没有抓取也没有导出时,`crawl_filter` 里那个来源名**说明不了任何事**:
  // 只有这两步认它,index 是全量扫 vault、extract 扫的是全库待提取队列(日报的
  // 「入库处理」就是这样一轮)。而 `start_pipeline` 一律会往 filter 里盖一个来源名,
  // 照着念等于告诉用户"这一轮在同步公众号",实际它跟来源毫无关系。
  const libraryOnly = planned.length && !planned.includes('crawl') && !planned.includes('export');
  $('pipe-running-channel').textContent = libraryOnly
    ? `${s.running ? '本次' : '上次'}范围：全库（不按来源）`
    : scopes.length
    ? `${s.running ? '本次' : '上次'}来源：${scopes.map(scope => channelLabel(scope.channel)).join(' + ')}`
    : '选择来源后开始一次同步';
  const range = filter.since_date ? filter.since_date + ' 至 ' + filter.until_date : '不限时间';
  // 「重试失败项」那一轮的范围是**按来源各自**收窄的（见 pipeline_retry.retry_crawl_scopes）:
  // 收窄到失败项的那个来源写「仅重试失败的 N 个订阅」,而报不出失败名单的来源（公众号
  // 的失败是文章级的）跑的就是原范围。给所有来源一律写「仅重试失败项」是一句谎话 ——
  // 它承诺了那个来源并没有发生的收窄;照 accounts 一律写「全部订阅」也是,那正是这颗
  // 按钮刚被修掉的那个行为。
  const scopeText = item => item.retry === 'targets'
    ? `仅重试失败的 ${item.accounts.length} 个订阅`
    : item.accounts?.length ? item.accounts.length + ' 个指定订阅' : '全部订阅';
  const scope = scopes.map(item => `${scopes.length > 1 ? channelLabel(item.channel) + ' ' : ''}${scopeText(item)}`).join(' · ');
  // 同上:全库那一轮没有「订阅范围」可言,订阅数和日期窗口对它都不成立。
  $('pipe-run-scope').textContent = libraryOnly
    ? `索引与提取${result.finished_at ? ' · ' + result.finished_at + ' 结束' : ''}`
    : scopes.length
    ? `${scope} · ${range}${result.finished_at ? ' · ' + result.finished_at + ' 结束' : ''}` : '';
  $('pipe-progress-label').textContent = planned.length ? `${completed} / ${planned.length} 完成` : '—';
  const duration = pipeDuration(result.started_at, s.running ? '' : result.finished_at);
  $('pipe-elapsed').textContent = duration ? `${s.running ? '已运行' : '耗时'} ${duration}` : '';
  // Use the same successful-step count as the label; unexecuted work never fills the bar.
  $('pipe-progress-fill').style.width = (planned.length ? Math.round(completed / planned.length * 100) : 0) + '%';
  $('pipe-state-hint').textContent = pipeStateHint(s, result, failed, skipped, { label, completed, total: planned.length });
  // 需处理的问题在整轮结束后统一收进总览卡片:哪几步、各自卡在什么,配一颗重试按钮,
  // 不用去每个步骤卡片里找「需处理」。运行中不出现(失败项不阻止本轮继续)。
  const failedStages = planned.filter(k => pipeHasFailure(stages[k]));
  const backlog = !s.running && pipePending.extract > 0;
  const issues = $('pipe-issues');
  issues.hidden = s.running || !(failedStages.length || backlog);
  if (!issues.hidden) {
    $('pipe-issues-title').textContent = failedStages.length
      ? `${failedStages.length} 个步骤需处理`
      : `还有 ${pipePending.extract} 项内容待提取`;
    $('pipe-issues-list').innerHTML = failedStages.map(k =>
      `<li><b>${STAGE_META[k].name}</b><span>${esc(stages[k]?.detail || '执行失败')}</span></li>`).join('')
      + (failedStages.length && backlog ? `<li><b>提取知识</b><span>另有 ${pipePending.extract} 项待提取</span></li>` : '');
  }
  $('pipe-retry').hidden = s.running || !(failed || pipePending.extract > 0);
  renderPipeLog((s.log?.length ? s.log : result.log) || [], result.finished_at);
  renderPipeButtons();
}
function renderPipeLog(lines, finishedAt) {
  const log = $('pipe-log'), box = $('pipe-log-box');
  const text = lines.length ? lines.join('\n') : finishedAt ? `上次结束于 ${finishedAt}，没有保留详细日志。` : '尚无运行记录。开始同步后，这里会实时显示进展。';
  if (log.textContent === text) return;
  const follow = $('pipe-log-follow').checked, top = box.scrollTop;
  log.textContent = text;
  if (follow) box.scrollTop = box.scrollHeight;
  else box.scrollTop = top;
  $('pipe-log-latest').hidden = follow;
  $('pipe-log-note').textContent = follow ? '保留最近 80 条' : '已暂停跟随 · 有新进展';
}
function followPipeLog() {
  $('pipe-log-follow').checked = true;
  $('pipe-log-box').scrollTop = $('pipe-log-box').scrollHeight;
  $('pipe-log-latest').hidden = true; $('pipe-log-note').textContent = '保留最近 80 条';
}
function togglePipeLogFollow() {
  if ($('pipe-log-follow').checked) followPipeLog();
  else { $('pipe-log-latest').hidden = false; $('pipe-log-note').textContent = '已暂停跟随'; }
}
$('pipe-log-box').addEventListener('scroll', () => {
  const box = $('pipe-log-box');
  if (box.scrollHeight - box.scrollTop - box.clientHeight > 32 && $('pipe-log-follow').checked) {
    $('pipe-log-follow').checked = false; togglePipeLogFollow();
  }
});
new ResizeObserver(() => {
  if ($('pipe-log-follow').checked) $('pipe-log-box').scrollTop = $('pipe-log-box').scrollHeight;
}).observe($('pipe-log-box'));
function downloadPipeLog() {
  const url = URL.createObjectURL(new Blob([$('pipe-log').textContent], { type: 'text/plain;charset=utf-8' }));
  const a = document.createElement('a'); a.href = url; a.download = '知识库同步日志.txt'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
