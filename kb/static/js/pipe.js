// Live progress never waits for collector probes or filesystem backlog scans.
const STAGE_META = {
  crawl: { name: '抓取内容', desc: '从所选来源收集文章' },
  export: { name: '导出文章', desc: '保存到本地知识库' },
  index: { name: '建立索引', desc: '让全文与语义检索可用' },
  extract: { name: '提取知识', desc: '识别实体、主题与摘要' },
  projects: { name: '整理工具', desc: '拆分工具与项目条目' },
  wiki: { name: '更新 Wiki', desc: '生成关联页面与知识图谱' },
};
let pipeTimer = null, pipeSnapshot = null, pipePending = {};
let pipeRequest = null, pipeStatusAbort = null, pipeStatusVersion = 0;
let pipeAction = '', pipeConnected = false, pipeLastRead = 0;
let pipeSourceVersion = 0, pipePendingRequest = null;

async function pipeJson(path, options = {}, timeout = 15000) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timer = setTimeout(abort, timeout);
  try {
    const r = await fetch(path, { ...options, signal: controller.signal });
    if (!r.ok) throw new Error(await apiFail(r, '请求未完成'));
    return await r.json();
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('连接超时，请检查服务后重试');
    throw e;
  } finally {
    clearTimeout(timer); options.signal?.removeEventListener('abort', abort);
  }
}
function pipePost(path, body) {
  return pipeJson(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}
function invalidatePipeStatus() {
  clearTimeout(pipeTimer); pipeTimer = null;
  pipeStatusVersion++; pipeStatusAbort?.abort(); pipeRequest = null;
}
function queuePipeRefresh() {
  clearTimeout(pipeTimer);
  // A lightweight status read also discovers new background runs and retries an initial failure.
  pipeTimer = setTimeout(() => { pipeTimer = null; refreshPipe(); }, document.hidden ? 8000 : pipeSnapshot?.running ? 1500 : 6000);
}
async function refreshPipe(withPending = false) {
  if (withPending) { refreshPipeSource(); refreshPipePending(); }
  if (pipeRequest) return pipeRequest;
  clearTimeout(pipeTimer);
  const version = ++pipeStatusVersion;
  pipeStatusAbort = new AbortController();
  const request = (async () => {
    try {
      const s = await pipeJson('/api/pipeline/status?source=false', { signal: pipeStatusAbort.signal });
      if (version !== pipeStatusVersion) return;
      const wasRunning = pipeSnapshot?.running;
      pipeSnapshot = s; pipeConnected = true; pipeLastRead = Date.now();
      $('pipe-connection').hidden = true;
      renderPipeState(s);
      if (wasRunning && !s.running) {
        refreshPipePending(); refreshPipeSource();
        pipeJson('/api/stats').then(stats => {
          $('stats').textContent = `${stats.documents.toLocaleString()} 篇内容已收录`;
        }).catch(() => {});
        toast(pipeRunLabel(pipeResult(s)) + (s.stopped || s.last_run?.stopped ? '已停止' : '已结束'), { key: 'pipeline', type: 'info', detail: '各步骤结果已更新，可查看待处理明细。' });
      }
    } catch (e) {
      if (version !== pipeStatusVersion) return;
      pipeConnected = false;
      $('pipe-connection').hidden = false;
      $('pipe-connection').querySelector('span').textContent = `状态暂时无法更新：${e.message}。${pipeLastRead ? '保留上次结果，正在自动重连。' : '正在自动重连。'}`;
      renderPipeButtons();
    } finally {
      if (version === pipeStatusVersion) { pipeRequest = null; renderPipeButtons(); queuePipeRefresh(); }
    }
  })();
  pipeRequest = request;
  renderPipeButtons();
  return request;
}
async function refreshPipeSource() {
  const version = ++pipeSourceVersion;
  $('src-status').textContent = '检查中…';
  try {
    await loadPipeChannels();
    const channel = pipeChannel;
    const s = await pipeJson(channelUrl('/api/pipeline/status', channel), {}, 60000);
    if (version !== pipeSourceVersion || channel !== pipeChannel) return;
    const src = s.source || {};
    $('src-label').textContent = src.channel_label || src.label || '来源采集器';
    $('src-status').textContent = src.online ? '已连接' : '未连接';
    $('src-status').style.color = src.online ? 'var(--ok)' : 'var(--danger)';
    $('src-detail').textContent = [src.message, src.hint].filter(Boolean).join(' · ') || (src.online ? '来源可用，可以开始同步。' : '可检查采集器配置。来源离线时抓取会跳过，已有内容仍可处理。');
    $('src-console').style.display = src.console_url ? '' : 'none';
    if (src.console_url) $('src-console').href = safeUrl(src.console_url);
    pipeRangeHint = src.range_hint || pipeRangeHint;
    renderPipeRange();
  } catch (e) {
    if (version !== pipeSourceVersion) return;
    $('src-status').textContent = '连接未确认';
    $('src-detail').textContent = e.message + '。可以重新检查连接，运行进度不受影响。';
  }
}
async function refreshPipePending() {
  if (pipePendingRequest || pipeSnapshot?.running) return;
  const startedAt = pipeSnapshot?.started_at;
  pipePendingRequest = pipeJson(channelUrl('/api/pipeline/status?source=false&pending=true'), {}, 60000);
  try {
    const s = await pipePendingRequest;
    if (!pipeSnapshot?.running && (!startedAt || startedAt === pipeSnapshot?.started_at)) {
      pipePending = s.pending || {}; if (pipeSnapshot) renderPipeState(pipeSnapshot);
    }
  } catch { /* Backlog estimates never replace live progress or block actions. */ }
  finally { pipePendingRequest = null; }
}
function renderPipeButtons() {
  const running = !!pipeSnapshot?.running;
  // Opening a draft is local; only the actual start requires a confirmed connection.
  $('pipe-run').disabled = running || !!pipeAction;
  $('pipe-run').textContent = pipeAction === 'start' ? '正在启动…' : running ? pipeRunLabel(pipeSnapshot) + '进行中' : '新建同步';
  $('pipe-channel-current').disabled = running || !!pipeAction;
  $('pipe-retry').disabled = !pipeConnected || running || !!pipeAction;
  $('pipe-retry').textContent = pipeAction === 'retry' ? '正在启动重试…' : '重试失败项';
  $('pipe-stop').hidden = !running || !pipeSnapshot?.stop_supported;
  $('pipe-stop').disabled = !pipeConnected || !!pipeAction || !!pipeSnapshot?.stop_requested;
  $('pipe-stop').textContent = pipeAction === 'stop' || pipeSnapshot?.stop_requested ? '等待当前阶段结束…' : '本阶段结束后停止';
  $('nav-sync-dot').hidden = !running;
  renderPipeEst();
}
async function retryFailed() {
  if (pipeAction || pipeSnapshot?.running) return;
  pipeAction = 'retry'; invalidatePipeStatus(); renderPipeButtons();
  try {
    await pipePost('/api/pipeline/retry-failed');
    toast('已开始重试', { key: 'pipeline', type: 'ok', detail: '将处理失败阶段及必要的后续步骤，已完成内容按现有规则去重。' });
  } catch (e) { toast('重试未能启动', { key: 'pipeline', detail: e.message }); }
  finally { await refreshPipe(); pipeAction = ''; renderPipeButtons(); }
}
async function stopPipe() {
  if (pipeAction || !pipeSnapshot?.running || pipeSnapshot.stop_requested) return;
  pipeAction = 'stop'; invalidatePipeStatus(); renderPipeButtons();
  try {
    await pipePost('/api/pipeline/stop');
    pipeSnapshot.stop_requested = true; renderPipeState(pipeSnapshot);
    toast('已请求停止', { key: 'pipeline', type: 'info', detail: '等待所有当前执行中的阶段结束；已完成的结果会保留。' });
  } catch (e) { toast('停止请求未完成', { key: 'pipeline', detail: e.message }); }
  finally { await refreshPipe(); pipeAction = ''; renderPipeButtons(); }
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshPipe();
});
