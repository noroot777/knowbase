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
  // 「日报」是业务批次自己的身份(`open_batch('daily')` → `_merge_runs` 的 kind)。
  // 原来整批一律写死成「同步」,于是调度器起的日报轮和一次手点的同步在这张清单里
  // 长得一模一样 —— 而这张清单是排查「今天到底发生了什么」的唯一入口。
  if (result.run_kind === 'daily') return '日报';
  return result.run_kind === 'retry' || legacyRetry ? '重试' : '同步';
}
function pipeStateHint(s, result, failed, skipped, progress, owed = 0) {
  // **翻历史时这句话要跟着换成只读语义。** 下面那几句写的是「可以重新配置一次同步,
  // 或按上次抓取范围重试失败项」—— 而翻历史时那颗按钮已经藏掉也禁用了(理由在
  // `renderPipeState` 里),提示还指着一个按不到的操作,说的又是**另一轮**的失败。
  if (typeof pipeViewing !== 'undefined' && pipeViewing) {
    const recordHint = '运行记录只读；带「不再抓取」的条目仍可永久移除。';
    if (failed) return `这一轮有 ${failed} 个步骤失败。${recordHint}要重试当前失败请先回到最新。`;
    if (result.stopped) return `这一轮被停止了，已完成结果当时已保留。${recordHint}`;
    if (skipped) return `这一轮有 ${skipped} 个步骤被跳过，具体原因见下方。${recordHint}`;
    return `这一轮的步骤都已完成。${recordHint}`;
  }
  if (s.running) {
    if (s.stop_requested) return '正在等待执行中的步骤收尾；全部结束前暂不能新建同步，已完成结果会保留。';
    if (failed) return `已有 ${failed} 个步骤失败，本轮仍在执行。请等全部步骤结束后再新建同步；失败项不会阻止下一轮。`;
    return `本轮${progress.label}尚未结束，暂不能新建同步。任务在后台执行，切换页面不会中断。`;
  }
  if (failed) return `${result.stopped ? '本轮已停止，结果已保留。' : ''}上次有 ${failed} 个步骤失败，不影响新建同步。可重新配置一次同步，或按上次抓取范围重试失败项。`;
  // **这一轮全绿不等于今天没欠账。** 一天里会跑好几轮,而上面那句话只念得出最后
  // 一轮:今天早些时候失败的那几步在一轮全绿收尾之后就不在它里面了,而「重试失败
  // 项」补的正是它们(后端口径 `pipeline_retry.today_failed`)。不说的话按钮冒出来
  // 了却没有任何一句话解释它为什么在。
  // **排在上面那句之后**:这一轮自己报了红时念的是它自己那几步(屏幕上的卡片正指着
  // 它们),反过来就是把用户的注意力从眼前这一轮引开。于是这句话只在「这一轮一个红
  // 都没有」时出现 —— 两句话数的是同一批步骤也不会念两遍。
  if (owed) return `本轮的步骤都完成了，但今天早些时候还有 ${owed} 个步骤没补上，可按它们各自的范围重试失败项。`;
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
/* ── 失败明细的一行(硬约束在 docs/decisions/failure-drop.md 第一节)────────
   两件事只在**来源给得出身份**时才有:①标题点得开(`url`,去原文核对是不是真的
   没内容了);②那颗「不再抓取」(`drop`,原样回传给后端,怎么删只有采集器知道)。
   来源给不出身份时两样都不出现 —— 指着另一篇文章让人删是不可恢复的。        */
function pipeFailureRow(f) {
  const title = esc(f.title || '未命名内容');
  const head = f.url
    ? `<a href="${esc(safeUrl(f.url))}" target="_blank" rel="noopener noreferrer">${title}${arrow('ext')}</a>`
    : title;
  const drop = f.drop && f.drop.channel && f.drop.id
    ? `<button class="text-button pipe-failure-drop" data-drop="${esc(JSON.stringify(f.drop))}">不再抓取</button>`
    : '';
  return `<p><span class="pipe-failure-head">${head}${drop}</span><span>${esc(f.reason || '')}</span></p>`;
}
// 事件委托挂在稳定的容器上:明细那几段 innerHTML 每轮轮询都可能重建,
// 逐个按钮绑定会在重建的那一下全部丢掉。
$('pipe-stages').addEventListener('click', e => {
  const btn = e.target.closest('.pipe-failure-drop');
  if (btn) dropPipeFailure(btn);
});
async function dropPipeFailure(btn) {
  let ref = null;
  try { ref = JSON.parse(btn.dataset.drop || ''); } catch {}
  if (!ref || !ref.channel || !ref.id) return toast('这一条没有可用的标识，无法移除');
  // 标题要把按钮自己的字摘掉 —— `.textContent` 会把「不再抓取」一起念进来,
  // 于是确认框里写着「某个号 · 某篇不再抓取。将把它…」。
  const head = btn.closest('p')?.querySelector('.pipe-failure-head');
  const name = (head ? [...head.childNodes].filter(n => n !== btn)
    .map(n => n.textContent).join('') : '').trim() || '这一条';
  if (!await confirmBox({
    title: '以后不再抓取这一条？',
    body: `${name}。将把它从采集范围里永久移除：删除已采集的内容，再次发现同一篇也不再收录。确认原文确实没有正文再操作，此操作不可撤销。`,
    ok: '不再抓取', danger: true,
  })) return;
  btn.disabled = true;
  try {
    // **等待预算要盖住后端那一串**:采集器 CLI 最长等 20 秒,它里面那把 SQLite
    // 还配了 15 秒 busy timeout。按 `pipePost` 默认的 15 秒算,锁等一下前端就先
    // 报「没删成」、把按钮放回去,而后端随后删成功了 —— 用户照着提示再点一次,
    // 看到的是另一套话术,而第一次其实已经生效。
    const r = await pipePost('/api/pipeline/failures/drop', ref, 45000);
    toast('已移除', { type: 'ok', detail: r.detail || '以后不再抓取这一条。' });
    // 状态和历史清单里都不该再有它。轮询那一份可能正在途中、带的是旧名单,
    // 所以先把它作废再重读(同「重试失败项」那两颗按钮的收尾)。
    // **待处理数也要作废**:删掉一条不产生新的一轮,靠运行版本比不出变化,
    // 在途那次查询会把删除前的数字盖回来(见 `invalidatePipePending`)。
    invalidatePipeStatus();
    invalidatePipePending();
    await refreshPipe(true);
    if (typeof loadPipeHistory === 'function') loadPipeHistory();
  } catch (e) {
    // **超时/断线不等于「没删掉」。** 不可逆的那一步可能已经在后端做完了,断言
    // 「未完成」会让用户以为可以当没发生过。如实说一句「结果未确认」,顺手重读
    // 一次状态 —— 真删成了,那一条当场就从明细里消失,用户不必自己判断。
    btn.disabled = false;
    toast('结果未确认', { detail: e.message + '。这一条可能已经移除，正在重新读取。' });
    invalidatePipeStatus();
    invalidatePipePending();
    refreshPipe(true);
    if (typeof loadPipeHistory === 'function') loadPipeHistory();
  }
}
function renderPipeState(s) {
  initPipeStageRows();
  // **翻历史时整屏按「那一轮已经结束」渲染。** `running` / `stop_requested` 说的是
  // 此刻,而被翻到的那一轮早就收尾了 —— 混着用会把上午那一轮的阶段配上此刻的
  // 「执行中」。只换一份快照进来就够了:下面整套渲染(阶段卡片、失败明细、进度条、
  // 日志)一个字都不用改。
  const viewing = typeof pipeViewing === 'undefined' ? null : pipeViewing;
  if (viewing) s = { running: false, stopped: !!viewing.stopped, last_run: viewing, log: [] };
  $('pipe-viewing').hidden = !viewing;
  if (viewing) $('pipe-viewing-text').textContent =
    `正在查看 ${pipeRunWhen(viewing)} 那一轮的结果，不是最新状态。`;
  // 这张卡现在讲的是哪一轮、那一轮红没红 —— 顶边和底色跟着换。只读那一档**转灰不转
  // 黄**:它不是一条警告,是「这不是此刻」。
  $('pipe-state-label').textContent = viewing ? '这一轮的结果' : '当前状态';
  const result = pipeResult(s), stages = result.stages || {};
  const planned = pipePlannedStages(result), label = pipeRunLabel(result);
  for (const key of Object.keys(STAGE_META)) {
    const st = stages[key] || { status: '', detail: '' }, row = $('pipe-stage-' + key);
    const status = !planned.includes(key) && st.status === 'skipped' ? 'not_planned' : pipeHasFailure(st) ? 'error' : st.status || '';
    row.dataset.status = status;
    row.querySelector('.pipe-stage-status').textContent = PIPE_STATUS[status] || '状态未知';
    row.querySelector('.pipe-stage-detail').textContent = st.detail || (s.running && status === 'pending' ? '前序步骤结束后自动开始' : '');
    const pending = row.querySelector('.pipe-stage-pending');
    // 待处理数说的是**此刻**,贴在被翻到的那一轮旁边就是一句谎话
    pending.hidden = s.running || !!viewing || pipePending[key] == null;
    pending.textContent = pipePending[key] != null ? `当前待处理 ${pipePending[key]} 项` : '';
    const details = row.querySelector('details'), failures = st.failures || [];
    details.hidden = !failures.length;
    const waiting = failures.every(f => f.pending);
    // 截断过就把真实总数说出来(后端 `_cap_failures` 留了 failures_total):照数组
    // 长度念的话,三百条失败会被写成一个看着完整的「50 项」,余下那些连"还有"都
    // 不知道。
    const total = st.failures_total || failures.length;
    details.querySelector('summary').textContent =
      `${waiting ? '待处理' : '问题'}明细 · ${total} 项`
      + (total > failures.length ? `（仅列前 ${failures.length} 条）` : '');
    const list = details.querySelector('.pipe-failures');
    const signature = JSON.stringify(failures);
    if (list.dataset.signature !== signature) {
      const scroll = list.scrollTop;
      list.innerHTML = failures.map(f => pipeFailureRow(f)).join('');
      list.dataset.signature = signature; list.scrollTop = scroll;
    }
  }
  const values = planned.map(k => stages[k] || {}), failed = values.filter(pipeHasFailure).length;
  const card = $('pipe-overview');
  if (card) {
    card.classList.toggle('viewing', !!viewing);
    // 运行中不转红:失败项不阻止本轮继续,整张卡这时讲的是「还在跑」
    card.classList.toggle('has-error', !s.running && failed > 0);
  }
  const completed = values.filter(st => st.status === 'ok' && !pipeHasFailure(st)).length;
  const active = Object.keys(STAGE_META).filter(k => stages[k]?.status === 'running').map(k => STAGE_META[k].name);
  const skipped = values.filter(st => st.status === 'skipped').length;
  let title = '准备好收录新内容';
  if (s.running) title = s.stop_requested ? '正在收尾，随后停止' : active.length ? (label === '重试' ? '正在重试：' : '') + active.join(' + ') : `${label}正在启动`;
  else if (result.stopped) title = `${label}已停止`;
  else if (failed) title = `${label}结束，${failed} 个步骤需处理`;
  else if (result.finished_at) title = skipped ? `${label}结束，部分步骤已跳过` : planned.length && completed === planned.length ? `${label}已完成` : `${label}结束，本次步骤未全部完成`;
  if ($('pipe-state-title').textContent !== title) $('pipe-state-title').textContent = title;
  // 这一轮的结论没写进磁盘(满盘、目录没了):本进程内照常可查,重启就退回上一次
  // 写成功的那一份。后端把它记成 `persist_issue` 一格(见 `_with_persist_issue`)
  // —— 只写运行日志的话得展开日志才看得到,而这里那句收尾文案仍是一句干净的
  // 「已完成」,看不出这一轮是降级完成的。
  const persist = $('pipe-persist-issue');
  if (persist) {
    const issue = s.running ? '' : result.persist_issue || '';
    persist.hidden = !issue;
    if (persist.textContent !== issue) persist.textContent = issue;
  }
  const scopes = pipeRunScopes(result), filter = result.crawl_filter || {};
  // 判据在 `pipe_channels.js`:同步中心、公众号页、订阅页共用那一份,别在这儿再判一遍。
  const libraryOnly = pipeLibraryOnly(result);
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
  // 步骤名**按这一轮真正计划了什么念**,不许写死「索引与提取」——「不按来源」
  // 的组合不止它一种(单独重跑一次 wiki、只重建索引都是),写死就会跟实际计划
  // 对不上,而那一轮本身并没有选错阶段。
  const libraryWhat = planned.map(k => STAGE_META[k]?.name).filter(Boolean).join(' · ');
  $('pipe-run-scope').textContent = libraryOnly
    ? `${libraryWhat}${result.finished_at ? ' · ' + result.finished_at + ' 结束' : ''}`
    : scopes.length
    ? `${scope} · ${range}${result.finished_at ? ' · ' + result.finished_at + ' 结束' : ''}` : '';
  $('pipe-progress-label').textContent = planned.length ? `${completed} / ${planned.length} 完成` : '—';
  const duration = pipeDuration(result.started_at, s.running ? '' : result.finished_at);
  $('pipe-elapsed').textContent = duration ? `${s.running ? '已运行' : '耗时'} ${duration}` : '';
  // Use the same successful-step count as the label; unexecuted work never fills the bar.
  $('pipe-progress-fill').style.width = (planned.length ? Math.round(completed / planned.length * 100) : 0) + '%';
  // 需处理的问题在整轮结束后统一收进总览卡片:哪几步、各自卡在什么,配一颗重试按钮,
  // 不用去每个步骤卡片里找「需处理」。运行中不出现(失败项不阻止本轮继续)。
  const failedStages = planned.filter(k => pipeHasFailure(stages[k]));
  const owed = (s.today_failed || []).length;
  $('pipe-state-hint').textContent = pipeStateHint(s, result, failed, skipped, { label, completed, total: planned.length }, owed);
  const backlog = !s.running && !viewing && pipePending.extract > 0;
  const issues = $('pipe-issues');
  issues.hidden = s.running || !(failedStages.length || backlog);
  if (!issues.hidden) {
    $('pipe-issues-title').textContent = failedStages.length
      ? `${failedStages.length} 个步骤需处理`
      : `还有 ${pipePending.extract} 项内容待提取`;
    // **只写哪几步,不抄它们的 detail。** 同一个字符串在下面那几行步骤的
    // `.pipe-stage-detail` 里已经有一份(存量实测有 200 字的),一屏上出现两遍 ——
    // 同元规则「口径只有一份」。这里的职责收敛成「哪几步 + 去哪看」。
    const where = failedStages.map(k => `<b>${esc(STAGE_META[k].name)}</b>`).join('、');
    $('pipe-issues-list').innerHTML = failedStages.length
      ? `${where} · 各自的明细在下面那一步里`
        + (backlog ? `；另有 ${pipePending.extract} 项内容待提取` : '')
      : '在下面的「提取知识」里重试，或新建一次同步。';
  }
  // 「看第一个」:把屏幕带到第一个红的那一步并展开它的明细。**翻历史时照旧给** ——
  // 它只是滚动和展开,不动任何东西(那颗重试按钮才是「会动」的,它另有两道守卫)。
  $('pipe-issues-jump').hidden = issues.hidden || !failedStages.length;
  pipeFirstIssue = failedStages[0] || '';
  // **翻历史时这颗按钮一律藏掉。** 它重试的永远是**最后一轮**的范围
  // (`pipeline_retry.retry_crawl_scopes` 读的是 `load_last_run()`),而屏幕上显示
  // 的是上午那一轮 —— 「看到的那一轮」和「按下去会动的那一轮」必须是同一轮。
  // `renderPipeButtons` 里另有一道 disabled:那一道跟着连接和在途动作走,轮询的
  // finally 会单独调它,两道射程不同。
  // **露不露脸按「今天还欠不欠」判,不按「最近一轮红不红」。** 一天里会跑好几轮,
  // 一轮全绿收尾之后最近那一轮一个红都没有,而今天早些时候那几笔还欠着 —— 按旧判据
  // 按钮当场藏掉,那几笔连点的机会都没有(后端口径 `pipeline_retry.today_failed`,
  // 账本空着时它退回最近一次运行,所以只会多显示、不会少显示)。
  $('pipe-retry').hidden = s.running || !!viewing
    || !(failed || (s.today_failed || []).length || pipePending.extract > 0);
  renderPipeLog((s.log?.length ? s.log : result.log) || [], result.finished_at);
  syncPipeLogPanel(s.running && !viewing, pipeVersion(s));
  // 轮次列那一行「正在运行」跟着此刻的快照走(口径在 `pipe_history.js`)
  if (typeof syncPipeHistoryLive === 'function') syncPipeHistoryLive();
  renderPipeHeadFacts();
  renderPipeButtons();
}

// 第一个需处理的步骤,「看第一个」按它跳。空串 = 这一屏没有红。
let pipeFirstIssue = '';

function jumpToFirstIssue() {
  const row = pipeFirstIssue && $('pipe-stage-' + pipeFirstIssue);
  if (!row) return;
  // 明细本来就是折叠的,跳过去还要再点一下才看得见原因 —— 那正是来这一趟要看的东西
  const details = row.querySelector('details');
  if (details && !details.hidden) details.open = true;
  row.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

// 「这一轮跑起来了」才自动展开日志,**只在翻上来那一下展开,之后不再管**:每次轮询都
// 强制展开的话,用户跑着的时候自己收起来会被下一跳顶开,那比不展开更烦。收是人的事,
// 我们不自动收 —— 收在他正读着最后几行的时候同样是抢。
//
// **记的是「哪一轮已经自动展开过」,不是一个布尔。** 布尔那一版的重置条件是「此刻这一屏
// 不在跑」,而**翻历史也满足它**:跑着的时候手动收起、去翻一轮、再回到最新,日志又被顶
// 开了(第 1 轮审查实测 `open` 从 null 变回 "")。翻历史不是新起了一轮。
let pipeLogOpenedFor = '';
function syncPipeLogPanel(running, version) {
  const panel = $('pipe-log-panel');
  if (!panel) return;
  if (running && pipeLogOpenedFor !== version) { panel.open = true; pipeLogOpenedFor = version; }
  $('pipe-log-hint').textContent = running ? '运行中' : '';
}

// 页头那一行事实数字(CLAUDE.md「页头就一行:标题 + 同一行上的事实数字」)。
//
// **不收参数,自己从全局那两份读** —— 这一行的两半来自两条不同的请求(轮数来自
// `/api/pipeline/runs`,最近那一轮来自状态端点),而它们**各自什么时候回来是不定的**。
// 收参数的话就只有状态那一侧调得动它:实测首屏是「最近 10-16 11:13」、那个「9 轮留档」
// 一直不出现,因为历史回包到的时候没人再写一次这一行。两个写入方共用一个元素、各写半句
// 更糟(后写的那次把另一半抹掉),所以口径是**一个函数读两份状态,两边都能调**。
function renderPipeHeadFacts() {
  const host = $('pipe-head-facts');
  if (!host) return;
  const snap = typeof pipeSnapshot === 'undefined' ? null : pipeSnapshot;
  const rounds = typeof pipeHistory === 'undefined' ? [] : pipeHistory;
  const result = snap ? pipeResult(snap) : {};
  const parts = [];
  if (rounds.length) parts.push(`<b>${rounds.length}</b> 轮留档`);
  if (snap?.running) parts.push('正在同步');
  else if (result.finished_at) parts.push(`最近 <b>${esc(pipeRunWhen(result))}</b>`);
  const html = parts.join('<i>·</i>');
  if (host.innerHTML !== html) host.innerHTML = html;
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
