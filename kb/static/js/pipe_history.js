// ── 历史运行:翻到某一轮,整屏按那一轮渲染 ──────────────────────────────────────
// 面板那份当前状态只留最后一轮,而一轮的红很容易在被人看见之前就被下一轮覆盖掉:
// 上午那轮翻译报了 503、导出阶段判了红,晚上一次「重试失败项」跑完,面板上就只剩
// 那一轮的结论,那个红没有任何地方查得到。后端为此单独落了一份历史。
let pipeHistory = [], pipeViewing = null, pipeHistoryRequest = null;
// 「还欠一次历史刷新」。在途那次请求只能回答它开跑那一刻的事实,而「又跑完一轮」
// 这件事**一轮只说一次**(`refreshPipe` 的 wasRunning / newResult 两个分支)——
// 在途时直接复用旧 promise 的话那一次机会就没了:旧回包把列表设成它取到的那两轮,
// 而 `pipeDoneRun` 已经更新成第三轮,后续轮询再也不进那个分支,列表自己追不平,
// 得切走再回来才补齐。所以记一笔欠账,等在途那次收尾了补发。
// 口径同 `pipe.js` 的 `pipePendingWanted`。
let pipeHistoryWanted = false;

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
  if (pipeHistoryRequest) { pipeHistoryWanted = true; return; }
  pipeHistoryWanted = true;
  while (pipeHistoryWanted) {
    // **这一笔欠账清在发请求之前。** 清在"接受回包之后"的话,在途期间别人记的那
    // 一笔会被它一起清掉 —— 而那一笔恰恰是说"你这次取的已经不是最新的了"。
    pipeHistoryWanted = false;
    // 发请求**之前**记下「现在是哪一轮」。回包回来时它变了,说明这期间又跑完了
    // 一轮,而这份回包里没有它 —— 记一笔欠账,循环按新快照再查一次。身份认 run_id
    // (`pipeRunKey` 那一份口径:`started_at` 只精确到秒,分不开落在同一秒的两轮)。
    // **空身份(首次进页面,还没有快照)时这一道短路** —— 那一档由状态侧兜住:
    // `refreshPipe` 首次读到一轮已结束时会补调一次本函数(那边的 firstRead 分支),
    // 落到在途就是下面那笔欠账、落到空闲就是一次新查询。两边都不许单独拆掉。
    const runKey = pipeRunKey(pipeSnapshot);
    pipeHistoryRequest = pipeJson('/api/pipeline/runs');
    try {
      const data = await pipeHistoryRequest;
      if (runKey && runKey !== pipeRunKey(pipeSnapshot)) { pipeHistoryWanted = true; continue; }
      pipeHistory = Array.isArray(data.runs) ? data.runs : [];
      // 后端那边留档失败了没有 —— 和「这一屏取到没取到」是两件事
      pipeFreshNote = typeof data.error === 'string' ? data.error : '';
      // 正在看的那一轮可能已经被挤出记录(或者刚被同一轮次的新结果替换):
      // 跟着那份新清单重新认一次,认不出来就退回最新 —— 留着一份谁都查不到的
      // 快照,屏幕上那条「正在查看」就指着一轮列表上没有的运行。
      if (pipeViewing) {
        const again = pipeHistory.find(r => r.run_id === pipeViewing.run_id);
        if (again) pipeViewing = again;
        else { exitPipeHistory(); break; }
      }
      renderPipeHistory();
      if (pipeViewing) renderPipeState(pipeSnapshot || { running: false });
      // **渲染成功了才算追平这一轮**(`pipe_fresh.js` 那套 desired/applied)
      pipeFreshOk('history', pipeRunKey(pipeSnapshot));
    } catch (e) {
      // 读不到**不在这里**无限重试(循环开头已经把那一笔欠账清了,这里也不置回去)
      // —— 但缺口要如实留着:交给 `pipe_fresh.js`,由后续状态轮询按退避补读,面板上
      // 同时说明这一块还是旧的。原来这里什么都不记,于是「见过这一轮」和「屏幕上对
      // 上了」被当成同一件事,列表长期停在旧那份清单、一个字都不说。
      pipeFreshMiss('history', e.message);
      if (!pipeHistory.length) {
        $('pipe-history-list').innerHTML =
          `<p class="pipe-history-empty">没能读到历史记录：${esc(e.message)}</p>`;
      }
    } finally { pipeHistoryRequest = null; }
  }
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
