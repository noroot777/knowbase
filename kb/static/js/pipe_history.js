// ── 历史运行:翻到某一轮,整屏按那一轮渲染 ──────────────────────────────────────
// 面板那份当前状态只留最后一轮,而一轮的红很容易在被人看见之前就被下一轮覆盖掉:
// 上午那轮翻译报了 503、导出阶段判了红,晚上一次「重试失败项」跑完,面板上就只剩
// 那一轮的结论,那个红没有任何地方查得到。后端为此单独落了一份历史。
let pipeHistory = [], pipeViewing = null, pipeHistoryRequest = null;

// 「这一轮结果怎么一句话说」。**判据复用阶段卡片那两个**(`pipePlannedStages` /
// `pipeHasFailure`)—— 自己再数一遍的话,列表上写「已完成」而点进去是两个红。
function pipeRunSummary(run) {
  const planned = pipePlannedStages(run), stages = run.stages || {};
  const failed = planned.filter(k => pipeHasFailure(stages[k])).length;
  if (failed) return { status: 'error', text: `${failed} 个步骤需处理` };
  if (run.stopped) return { status: 'stopped', text: '已停止' };
  const skipped = planned.filter(k => stages[k]?.status === 'skipped').length;
  if (skipped) return { status: 'skipped', text: `${skipped} 个步骤已跳过` };
  return { status: 'ok', text: '已完成' };
}

// 「10-14 09:16」。落盘的是「YYYY-MM-DD HH:MM:SS」本地时刻,**按字符切不去解析它**
// —— `new Date('2026-10-14 09:16:00')` 在各家浏览器上行为不一致,而这里只是显示。
function pipeRunWhen(run) {
  const stamp = run.finished_at || run.started_at || '';
  return stamp.slice(5, 16) || '时间未知';
}

function pipeRunWhere(run) {
  const scopes = pipeRunScopes(run.crawl_filter);
  return scopes.length ? scopes.map(s => channelLabel(s.channel)).join(' + ') : '未记录来源';
}

function renderPipeHistory() {
  const list = $('pipe-history-list'), note = $('pipe-history-note');
  if (!pipeHistory.length) {
    list.innerHTML = '<p class="pipe-history-empty">还没有跑完过一轮。同步结束后这里会按轮次留档。</p>';
    note.textContent = '';
    return;
  }
  note.textContent = `保留最近 ${pipeHistory.length} 轮`;
  list.innerHTML = pipeHistory.map(run => {
    const sum = pipeRunSummary(run), on = pipeViewing && pipeViewing.run_id === run.run_id;
    // 身份走 `data-run` 读、**不拼进 onclick**:`esc` 把引号转成实体,而属性值里的
    // 实体会在解析时还原 —— 拼进去的话一个带引号的身份就把那句 JS 拆了。这一行
    // 其余几个字段也都来自落盘文件,一处放松就是一个注入口。
    return `<button type="button" class="pipe-history-row${on ? ' on' : ''}" data-status="${esc(sum.status)}"
      data-run="${esc(run.run_id)}" aria-current="${on ? 'true' : 'false'}"
      onclick="viewPipeRun(this.dataset.run)">
      <span class="pipe-history-when">${esc(pipeRunWhen(run))}</span>
      <span class="pipe-history-kind">${esc(pipeRunLabel(run))}</span>
      <span class="pipe-history-where">${esc(pipeRunWhere(run))}</span>
      <span class="pipe-history-result">${esc(sum.text)}</span></button>`;
  }).join('');
}

async function loadPipeHistory() {
  if (pipeHistoryRequest) return pipeHistoryRequest;
  pipeHistoryRequest = (async () => {
    try {
      const data = await pipeJson('/api/pipeline/runs');
      pipeHistory = Array.isArray(data.runs) ? data.runs : [];
      // 正在看的那一轮可能已经被挤出记录(或者刚被同一轮次的新结果替换):
      // 跟着那份新清单重新认一次,认不出来就退回最新 —— 留着一份谁都查不到的
      // 快照,屏幕上那条「正在查看」就指着一轮列表上没有的运行。
      if (pipeViewing) {
        const again = pipeHistory.find(r => r.run_id === pipeViewing.run_id);
        if (again) pipeViewing = again;
        else { exitPipeHistory(); return; }
      }
      renderPipeHistory();
      if (pipeViewing) renderPipeState(pipeSnapshot || { running: false });
    } catch (e) {
      if (!pipeHistory.length) {
        $('pipe-history-list').innerHTML =
          `<p class="pipe-history-empty">没能读到历史记录：${esc(e.message)}</p>`;
      }
    } finally { pipeHistoryRequest = null; }
  })();
  return pipeHistoryRequest;
}

function viewPipeRun(runId) {
  const run = pipeHistory.find(r => r.run_id === runId);
  if (!run) return;
  // 同一行再点一次 = 回到最新
  if (pipeViewing && pipeViewing.run_id === runId) return exitPipeHistory();
  pipeViewing = run;
  renderPipeHistory();
  renderPipeState(pipeSnapshot || { running: false });
  $('pipe-state-title').focus({ preventScroll: false });
}

function exitPipeHistory() {
  if (!pipeViewing) return;
  pipeViewing = null;
  renderPipeHistory();
  renderPipeState(pipeSnapshot || { running: false });
}
