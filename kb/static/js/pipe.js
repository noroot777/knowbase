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
// 上一次见到的「已结束运行」身份。null = 这个页面还没读到过任何状态
// (那一档只记基线、不刷新:初始化本来就会拉一次待处理数)。
let pipeDoneRun = null;
let pipeSourceVersion = 0, pipePendingRequest = null;
// 「还欠一次待处理数刷新」。在途的那次查询只能回答它开跑那一刻的事实,
// 新结果来了得补一次,而「有新结果」这件事一轮只说一次。
let pipePendingWanted = false;
// 「这份回包是哪一轮的」。身份认 run_id —— started_at 只精确到秒,而这一档
// 治的正是「几百毫秒就结束」那种运行:两轮落在同一秒里时拿它当身份分不开,
// 上一轮的积压数会被当成当前事实应用上去。跟 pipeDoneRun 同一个口径。
const pipeRunKey = s => (s ? (s.run_id || s.started_at || '') : '');

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
// timeout 省略时沿用 pipeJson 的默认值。**要 POST 就走这个**:直接
// pipeJson(path, {}, 120000) 是 GET —— 选项对象里没有 method,fetch 默认 GET,
// 而后端那条路由只收 POST,于是按钮永远报 405(2026-10-08 审查实测「检查连接」)。
function pipePost(path, body, timeout) {
  return pipeJson(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }, timeout);
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
  if (withPending) refreshPipePending();
  if (pipeRequest) return pipeRequest;
  clearTimeout(pipeTimer);
  const version = ++pipeStatusVersion;
  pipeStatusAbort = new AbortController();
  const request = (async () => {
    try {
      const s = await pipeJson('/api/pipeline/status?source=false', { signal: pipeStatusAbort.signal });
      if (version !== pipeStatusVersion) return;
      const wasRunning = pipeSnapshot?.running;
      // 「有没有出现一轮新的已结束运行」。只靠 running→idle 这个跳变是不够的:
      // 跑得极快的那一轮(纯图片文章落终态、全是跳过)在两次轮询之间就结束了,
      // 页面一次都没见过 running=true，于是待处理数停在旧值 —— 后端已经是 0 了,
      // 面板还写着「当前待处理 1 项」，只有手动刷新页面才归零(2026-10-09 审查
      // 实测:状态接口 pending.extract=0 而 UI 显示 1)。后台定时跑的那一轮同理。
      const doneRun = s.running ? '' : (s.run_id || s.finished_at || '');
      const newResult = pipeDoneRun !== null && doneRun && doneRun !== pipeDoneRun;
      pipeDoneRun = doneRun;
      pipeSnapshot = s; pipeConnected = true; pipeLastRead = Date.now();
      $('pipe-connection').hidden = true;
      renderPipeState(s);
      if (wasRunning && !s.running) {
        refreshPipePending();
        // 来源连接卡在「我的订阅」的公众号视图上,只有它亮着才值得探一次
        if (!$('subs-wechat').hidden) refreshPipeSource();
        pipeJson('/api/stats').then(stats => {
          $('stats').textContent = `${stats.documents.toLocaleString()} 篇内容已收录`;
        }).catch(() => {});
        toast(pipeRunLabel(pipeResult(s)) + (s.stopped || s.last_run?.stopped ? '已停止' : '已结束'), { key: 'pipeline', type: 'info', detail: '各步骤结果已更新，可查看待处理明细。' });
      } else if (newResult) {
        // 没见过 running 的那一轮:只把积压数补上,不弹「已结束」——
        // 这条路也会在页面打开期间别处跑完一轮时走到，弹窗就成了噪音。
        refreshPipePending();
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
// 公众号那条连接说明:空串 = 没什么要说的,整条横幅收起来
function setSourceDetail(text, bad) {
  const box = $('src-detail');
  box.textContent = text;
  box.classList.toggle('error', !!bad);
  box.hidden = !text;
}
async function refreshPipeSource() {
  const version = ++pipeSourceVersion;
  $('src-status').textContent = '检查中…';
  try {
    await loadPipeChannels();
    // 这张连接卡只在「我的订阅」的公众号视图上:通道按来源目录现解析
    // (wechatSubsChannel),不拿全局选择当然值 —— 首次初始化失败过一次时
    // pipeChannel 还是存量的 rss/social,照它查就是把别的来源的「已连接」
    // 当成本页的连接结果(2026-10-08 第 2 轮审查实测)。
    const channel = wechatSubsChannel();
    if (!channel) throw new Error('这个来源的采集通道暂不可用，请确认采集器配置');
    setPipeChannel(channel);
    const s = await pipeJson(channelUrl('/api/pipeline/status', channel), {}, 60000);
    if (version !== pipeSourceVersion || channel !== pipeChannel) return;
    const src = s.source || {};
    $('src-label').textContent = src.channel_label || src.label || '来源采集器';
    $('src-status').textContent = src.online ? '已连接' : '未连接';
    $('src-status').style.color = src.online ? 'var(--ok)' : 'var(--danger)';
    // 说明挪成一条横幅(口径同 feeds 的 #source-configuration),所以**有话才出现**:
    // 接通且采集器没话说时那句「来源可用，可以开始同步。」是复述徽章,页头上白占一行。
    // 采集器自己给的 message/hint 一个字都不许吞 —— 接通之后它照样会说"本机库只读到
    // N 个会话"这类要紧事,那正是该看见的那一句。
    setSourceDetail([src.message, src.hint].filter(Boolean).join(' · ')
      || (src.online ? '' : '可检查采集器配置。来源离线时抓取会跳过，已有内容仍可处理。'), !src.online);
    $('src-console').style.display = src.console_url ? '' : 'none';
    if (src.console_url) $('src-console').href = safeUrl(src.console_url);
  } catch (e) {
    if (version !== pipeSourceVersion) return;
    $('src-status').textContent = '连接未确认';
    $('src-status').style.color = 'var(--danger)';
    setSourceDetail(e.message + '。可以重新检查连接，运行进度不受影响。', true);
  }
}
async function refreshPipePending() {
  if (pipeSnapshot?.running) return;
  // 在途那次查询是上一轮的事实:它的回包会被下面那道身份闸丢掉,而
  // 「出现了一轮新的已结束运行」一轮只说一次(pipeDoneRun 已经更新成新身份),
  // 直接 return 就等于把唯一那次机会扔了 —— 后端待处理已经是 0,面板永久停在
  // 「当前待处理 1 项」,再轮询多少次也不消失(2026-10-09 第 2 轮审查实测:
  // 切到检索再切回来引发一次查询、趁它在途点「重试失败项」,放行旧回包之后
  // done="run-2"、inFlight=false 而界面仍是 1,只有手动刷新页面才归零)。
  // 所以记下「还欠一次」,等在途那次收尾了补发,**真的应用了才算刷新完**。
  if (pipePendingRequest) { pipePendingWanted = true; return; }
  pipePendingWanted = true;
  while (pipePendingWanted) {
    // 跑起来了就别再扫文件:积压数由这一轮结束时那次刷新给出。欠着的那一笔
    // 留着,下一次进来接着补。
    if (pipeSnapshot?.running) return;
    const runKey = pipeRunKey(pipeSnapshot);
    pipePendingRequest = pipeJson(channelUrl('/api/pipeline/status?source=false&pending=true'), {}, 60000);
    try {
      const s = await pipePendingRequest;
      if (!pipeSnapshot?.running && (!runKey || runKey === pipeRunKey(pipeSnapshot))) {
        pipePending = s.pending || {}; pipePendingWanted = false;
        if (pipeSnapshot) renderPipeState(pipeSnapshot);
      }
      // 回包被丢掉(这期间换了一轮运行)时 wanted 还是 true,循环里按新快照
      // 再查一次 —— 快照不再变动就收敛,它只随真实运行的起止变化。
    } catch { pipePendingWanted = false; /* 查不到不无限重试,积压数从不挡操作 */ }
    finally { pipePendingRequest = null; }
  }
}
function renderPipeButtons() {
  const running = !!pipeSnapshot?.running;
  // Opening a draft is local; only the actual start requires a confirmed connection.
  $('pipe-run').disabled = running || !!pipeAction;
  $('pipe-run').textContent = pipeAction === 'start' ? '正在启动…' : running ? pipeRunLabel(pipeSnapshot) + '进行中' : '新建同步';
  $('pipe-retry').disabled = !pipeConnected || running || !!pipeAction;
  $('pipe-retry').textContent = pipeAction === 'retry' ? '正在启动重试…' : '重试失败项';
  $('pipe-stop').hidden = !running || !pipeSnapshot?.stop_supported;
  $('pipe-stop').disabled = !pipeConnected || !!pipeAction || !!pipeSnapshot?.stop_requested;
  $('pipe-stop').textContent = pipeAction === 'stop' || pipeSnapshot?.stop_requested ? '等待当前阶段结束…' : '本阶段结束后停止';
  $('nav-sync-dot').hidden = !running;
  // 「我的订阅」公众号视图上的同步按钮/进度横幅跟着同一份快照走
  if (typeof renderWechatSubsStatus === 'function') renderWechatSubsStatus();
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
