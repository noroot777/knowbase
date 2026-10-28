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
// 「在这之前发出去的待处理查询一律作废」。**运行版本管不了这件事**:
// 删掉明细里一条内容不产生新的一轮,`result_version` 一个字不变 —— 于是在途那次
// 查询(带的是删除**之前**的数字)照旧对得上版本、照旧被应用,还顺手把欠账清掉,
// 面板就永久停在删除前那个数(第 1 轮审查实测:实际从 2 行删到 1 行,页面仍写
// 「当前待处理 2 项」,轮询、切历史再回来都不变,只有整页刷新才归零)。
// 所以给它另一条身份闸:同一轮之内条目集合也会变。
let pipePendingEpoch = 0;
// 「这份回包是哪一轮的」。**版本由服务端算**(`result_version`),前端不再从
// run_id / 时间戳自己拼 —— 自己拼的那份在两档上没有值:新进程还没跑过任何一轮时
// 顶层身份是空的,从没跑过的新库里永远是空的。而空串在这里要同时表达「还不知道」
// 和「已经追平」两件事,两种处理各错一档(忙着重查 / 永远不补)。服务端那份口径
// 恒非空(没跑过就是 "none"),也兼容升级前没有 run_id 的旧记录。
// 快照还是 null 时才是空串 —— 那是「还没有任何回包」,确实不该拿来比。
const pipeVersion = s => (s && s.result_version) || '';
// **历史清单那一块对的是另一个凭据**:最新那一轮**已经收尾的**版本(`settled_version`)+ 整份
// 清单的指纹(`history_rev`)。被另一轮接替的那批日报是原位补上结论的,最新那一轮没变、清单
// 却变了 —— 只比前者,那份缓存就一直停在出报之前。回包那一侧拼法相同(`pipeHistoryVersionOf`),
// 两边才比得上。**跑不跑都要对齐**:正在跑的那一轮不在历史里,所以不拿 `result_version`
// (跑着时它说的是这一轮),而另一轮同步跑多久,被它接替的日报补上的结论就得在页面外面等多久。
// 老进程没有 `settled_version` 时退回原来那档(跑着不对齐)。
const pipeHistoryVersionOf = (version, rev) => version ? `${version}|${rev || ''}` : '';
const pipeHistoryVersion = s => pipeHistoryVersionOf(
  s && 'settled_version' in s ? s.settled_version : (s && !s.running ? pipeVersion(s) : ''),
  s && s.history_rev);

async function pipeJson(path, options = {}, timeout = 15000) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timer = setTimeout(abort, timeout);
  try {
    const r = await fetch(path, { ...options, signal: controller.signal });
    // 状态码跟着错误走:调用方要能分出「服务端核对之后明确拒绝了(什么都没跑)」
    // 和「跑了但没答复」—— 后者才该记成"结果不知道"。只多挂一个属性,原来按
    // `e.message` 取文案的那些调用方一个字都不用改。
    if (!r.ok) { const err = new Error(await apiFail(r, '请求未完成')); err.status = r.status; throw err; }
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
// 条目集合刚刚变过(删掉了一条),在途那次待处理查询的回包不作数。
// **不清 `pipePendingWanted`**:欠账留着,`refreshPipePending` 的循环会按新 epoch
// 再查一次 —— 清掉的话这一次刷新机会就没了。
function invalidatePipePending() { pipePendingEpoch++; }
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
      // **来源名要靠那份目录,所以它的依赖写在这里。** 面板上「上次来源」「这一轮
      // 在同步哪几个来源」都过 `channelLabel()`,目录没落地时它只能念回裸 id
      // (`wechat_local`)。原来这份目录是页头那颗「定时同步」在页面加载时顺带读进
      // 来的 —— 那颗按钮连同接口一起删了,于是同步中心打开就显示裸 id,直到用户
      // 点开「新建同步」才补上(实测:退役通道那组探针当场红在「上次来源不是一个
      // 裸 id」和存量选择没改道上)。目录一个进程只读一次(`loadPipeChannels` 自带
      // 守卫),所以这里**不是**每轮轮询多一个请求。
      //
      // **并排起跑,而且不等它。** `Promise.all` 那一版会把状态压在目录后面:目录
      // 慢或读不到时(`pipeJson` 默认等 15 秒)这一片什么都不画,而状态正是用来
      // 发现"后台已经在跑了"的 —— 一份依赖的失败不许把主事实一起拖下去(同
      // `docs/decisions/sync-schedule.md` 里目录与配置各自说的那条)。目录晚到就
      // 补画一次,`renderPipeState` 拿的是当时最新那份快照,重画一遍无副作用。
      loadPipeChannels().then(() => { if (pipeSnapshot) renderPipeState(pipeSnapshot); })
        .catch(() => {});
      const s = await pipeJson('/api/pipeline/status?source=false', { signal: pipeStatusAbort.signal });
      if (version !== pipeStatusVersion) return;
      const wasRunning = pipeSnapshot?.running;
      // 「有没有出现一轮新的已结束运行」。只靠 running→idle 这个跳变是不够的:
      // 跑得极快的那一轮(纯图片文章落终态、全是跳过)在两次轮询之间就结束了,
      // 页面一次都没见过 running=true，于是待处理数停在旧值 —— 后端已经是 0 了,
      // 面板还写着「当前待处理 1 项」，只有手动刷新页面才归零(2026-10-09 审查
      // 实测:状态接口 pending.extract=0 而 UI 显示 1)。后台定时跑的那一轮同理。
      const doneRun = s.running ? '' : pipeVersion(s);
      // 「最近一次读到的结束身份」。附属结果的刷新判据已经不看它了(见下面
      // `pipeFreshCheck` 那段),它留给 `queuePipeRefresh` 的节奏和探针当观测点。
      pipeDoneRun = doneRun;
      pipeRestoreGap = s.restore_gap || '';
      pipeSnapshot = s; pipeConnected = true; pipeLastRead = Date.now();
      $('pipe-connection').hidden = true;
      renderPipeState(s);
      if (wasRunning && !s.running) {
        // 来源连接卡在「我的订阅」的公众号视图上,只有它亮着才值得探一次
        if (!$('subs-wechat').hidden) refreshPipeSource();
        pipeJson('/api/stats').then(stats => {
          $('stats').textContent = `${stats.documents.toLocaleString()} 篇内容已收录`;
        }).catch(() => {});
        toast(pipeRunLabel(pipeResult(s)) + (s.stopped || s.last_run?.stopped ? '已停止' : '已结束'), { key: 'pipeline', type: 'info', detail: '各步骤结果已更新，可查看待处理明细。' });
      }
      // **两个附属结果的刷新只有这一处判据:落后就补。** 原来是三个事件分支各自
      // 直接调一次(running→idle、没见过 running 的那一轮、首次读到结束状态),
      // 外加这一道按退避补漏 —— 两套判据在同一个 tick 上重叠,一次读失败会被紧跟
      // 着的那次成功立刻盖过去,「失败了要说一句」就永远轮不到。收敛成一处之后
      // 那三档自然都被覆盖:身份一变 applied 就落后,首次 applied 是空的也落后。
      //
      // `doneRun` 在跑的时候是空串,那时 applied 也会被清成空 —— 跑完才有得对齐,
      // 中途不该扫文件(`refreshPipePending` 自己那道 running 守卫是同一个意思)。
      // 待处理数照旧只在空闲时对齐(跑着时扫文件没意义),历史清单不等空闲(见 `pipeHistoryVersion`)
      pipeFreshCheck({ history: pipeHistoryVersion(s), pending: doneRun },
                     { history: loadPipeHistory, pending: refreshPipePending });
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
  const changed = box.textContent !== text || box.classList.contains('error') !== !!bad;
  box.textContent = text;
  box.classList.toggle('error', !!bad);
  box.hidden = !text;
  updateWechatNotices(changed);
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
  // 「出现了一轮新的已结束运行」原来一轮只说一次(靠 pipeDoneRun 比身份),
  // 直接 return 就等于把唯一那次机会扔了 —— 后端待处理已经是 0,面板永久停在
  // 「当前待处理 1 项」,再轮询多少次也不消失(2026-10-09 第 2 轮审查实测:
  // 切到检索再切回来引发一次查询、趁它在途点「重试失败项」,放行旧回包之后
  // done="run-2"、inFlight=false 而界面仍是 1,只有手动刷新页面才归零)。
  // 所以记下「还欠一次」,等在途那次收尾了补发,**真的应用了才算刷新完**。
  if (pipePendingRequest) { pipePendingWanted = true; return; }
  pipePendingWanted = true;
  // **对不齐就退出循环,交给 `pipeFreshCheck` 那道有界补读。** 原来只在「还没有
  // 快照」那一档退出,别的对不上情形一律接着查 —— 而重启之后顶层身份是空的,
  // 回包和快照都比不出版本:既不应用、也不清欠账,循环立刻发下一次查询,实测
  // 1.3 秒里发出两千多次(每次都去扫一遍库)。这个上限是兜底,正常收敛只要一次:
  // 服务端版本只会前进,再查一次就拿到了。
  let spins = 0;
  while (pipePendingWanted && spins++ < 3) {
    // 跑起来了就别再扫文件:积压数由这一轮结束时那次刷新给出。欠着的那一笔
    // 留着,下一次进来接着补。
    if (pipeSnapshot?.running) return;
    const epoch = pipePendingEpoch;
    pipePendingRequest = pipeJson(channelUrl('/api/pipeline/status?source=false&pending=true'), {}, 60000);
    try {
      const s = await pipePendingRequest;
      // 在途期间条目集合变过(删了一条):这份回包说的是删之前,接着查。
      // 欠账还挂着,所以循环会再发一次。
      if (epoch !== pipePendingEpoch) continue;
      // **应用的凭据是这份回包自己的身份**(`s.run_id`),不是此刻的页面快照。原来
      // 空身份(首次打开、快照还是 null)一律放行、再拿当前快照给它盖章 —— 于是一份
      // 属于上一轮的待处理数被标成「已追平最新那一轮」:缺口不存在、提示不出现、
      // 补读也不会发生,面板永久写着「还有 1 项内容待提取」而后端早就是 0(实测:
      // 旧回包 run_id 末尾 -10、extract=1,后端已经是 -11、extract=0,切走再回来
      // 才归零)。
      const got = pipeVersion(s);
      const want = pipeVersion(pipeSnapshot);
      if (!s.running && want && got === want) {
        pipePending = s.pending || {}; pipePendingWanted = false;
        renderPipeState(pipeSnapshot);
        pipeFreshOk('pending', got);
      } else if (!s.running && want) {
        // 回包属于另一轮(在途期间又跑完了一轮)。服务端的版本只会前进,再查一次
        // 就对上了 —— 欠账留着,接着循环。
        continue;
      } else {
        // 还没有快照、或者这会儿又跑起来了:现在比不出来也不该读,**欠账留着、
        // 退出循环**。在这里接着查没用(快照不会因为我们重查而出现),等状态回包
        // 之后 `refreshPipe` 那一道(`pipeFreshCheck`)按退避来接。
        return;
      }
    } catch (e) {
      // 和历史那条同一个形状:不在这里无限重试,但**缺口要留着**(`pipe_fresh.js`)。
      // 只清欠账、保留旧值的话,后续轮询只知道「这一轮见过了」,面板上那个待处理数
      // 就一直是处理前的数字,而且一个字都不说。
      pipePendingWanted = false;
      pipeFreshMiss('pending', e.message);
    }
    finally { pipePendingRequest = null; }
  }
  // **「请求成功了、可取回来的是另一轮的数字」也要回报一次。** 不回报的话这一块
  // 的预算一次都不扣:每轮状态轮询都再补三次查询,既到不了上限、也到不了「连试两
  // 次才说」那个提示阈值 —— 面板一个字不说,而后台一直在扫库。历史那边的同类回包
  // 是扣预算的(`pipeFreshOk` 拿回包自己的版本盖章),这里跟它一个口径。
  // 上面那几条 `return`(还没有快照、又跑起来了)走不到这里:那两档本来就不该扣。
  if (pipePendingWanted) {
    pipePendingWanted = false;
    pipeFreshMiss('pending', '取到的还是上一轮的结果');
  }
}
function renderPipeButtons() {
  const running = !!pipeSnapshot?.running;
  // Opening a draft is local; only the actual start requires a confirmed connection.
  $('pipe-run').disabled = running || !!pipeAction;
  $('pipe-run').textContent = pipeAction === 'start' ? '正在启动…' : running ? pipeRunLabel(pipeSnapshot) + '进行中' : '新建同步';
  // 翻历史时按不下去 —— 见 pipe_view.js 里那道 hidden 旁边的理由。恢复不完整时
  // 同样按不下去(后端那道闸会 409,这里是**先说清楚**,别让人点了才知道)。
  $('pipe-retry').disabled = !pipeConnected || running || !!pipeAction
    || (typeof pipeViewing !== 'undefined' && !!pipeViewing)
    || (typeof pipeRestoreGap !== 'undefined' && !!pipeRestoreGap);
  $('pipe-retry').textContent = pipeAction === 'retry' ? '正在启动重试…' : '重试失败项';
  // 翻着留档那一轮时不给:那张卡讲的不是此刻,停的却是此刻这一轮
  $('pipe-stop').hidden = !(running || pipeDailyLive()) || !pipeSnapshot?.stop_supported
    || (typeof pipeViewing !== 'undefined' && !!pipeViewing);
  const stopping = !!pipeSnapshot?.stop_requested || !!pipeSnapshot?.flow_live?.stopping;
  // 日报已经开始生成:那一档后端不受理停止(`daily_run.request_stop`),按钮先说清楚
  const generating = pipeFlowLive(pipeSnapshot)?.running === 'daily';
  $('pipe-stop').disabled = !pipeConnected || !!pipeAction || stopping || generating;
  $('pipe-stop').textContent = pipeAction === 'stop' || stopping ? '等待当前步骤结束…'
    : generating ? '日报生成中，停不下来' : '当前步骤结束后停止整轮';
  $('nav-sync-dot').hidden = !running;
  // 「我的订阅」公众号视图上的同步按钮/进度横幅跟着同一份快照走
  if (typeof renderWechatSubsStatus === 'function') renderWechatSubsStatus();
  renderPipeEst();
}
async function retryFailed() {
  if (pipeAction || pipeSnapshot?.running) return;
  pipeAction = 'retry'; invalidatePipeStatus(); renderPipeButtons();
  try {
    // **后端那句话要念出来,不许丢。** 它带着这一轮真会跑哪几步,以及两档降级:
    // 当日账本读不出来时「今天早些时候那几笔这次可能补不到」、抓取暂时不可用时
    // 「这一步留着下次补」(口径在 `pipeline_retry.start_retry_failed`)。丢掉之后
    // 页面上只有一句一成不变的「将处理失败阶段」,而这一次的射程其实窄了一截
    // （审查 N1）。同一句话另有一份落在运行日志里（toast 是一次性的）。
    const started = await pipePost('/api/pipeline/retry-failed');
    const plan = String(started?.plan || '');
    toast('已开始重试', { key: 'pipeline', type: 'ok',
      detail: (plan ? `本次：${plan}。` : '')
        + '将处理失败阶段及必要的后续步骤，已完成内容按现有规则去重。' });
  } catch (e) { toast('重试未能启动', { key: 'pipeline', detail: e.message }); }
  finally { await refreshPipe(); pipeAction = ''; renderPipeButtons(); }
}
// 日报那一批跑着时,「停止整轮」停的是**整批**(日报那一侧的停止,它会顺手停掉手上
// 这一轮管道)。只停管道的话,日报接着跑下一个来源 —— 按钮上写的「后面排着的类型都
// 不再跑」就成了一句空话。判据是**身份对得上的那份**计划(`pipeFlowLive`,同那条线):
// 日报两轮之间挤进来一次手动同步时,页面上这一轮是那次同步,停的也该是它。
function pipeDailyLive() { return pipeFlowLive(pipeSnapshot)?.kind === 'daily'; }
// 这一下停的是哪一轮:日报那一批认批次身份,其余认这一轮的 run id。
function pipeStopTarget() {
  const s = pipeSnapshot;
  if (pipeDailyLive()) return { daily: true, id: pipeFlowLive(s).owner };
  return s?.running ? { daily: false, id: s.run_id || '' } : null;
}
async function stopPipe() {
  const stoppable = () => !pipeAction && !!pipeStopTarget()
    && !pipeSnapshot.stop_requested && !pipeSnapshot.flow_live?.stopping;
  if (!stoppable()) return;
  // **确认的是点下去那一刻的这一轮。** 确认框开着时这一轮可能收尾、下一轮接上了:
  // 那一下不许落到下一轮头上。前端核一遍(身份变了直接作废),后端在停止那把锁里
  // 再核一遍(确认之后、请求落地之前那道缝前端管不到)。
  const target = pipeStopTarget();
  if (!await confirmBox({
    title: '停止这一轮？',
    body: '当前这一步做完就停，后面排着的类型都不再跑。已完成的结果会保留。' + (target.daily ? '这一轮不出日报。' : ''),
    ok: '停止整轮', danger: true,
  })) return;
  const now = pipeStopTarget();
  if (!now || now.daily !== target.daily || now.id !== target.id) {
    toast('没有停止', { key: 'pipeline', detail: '确认之前那一轮已经结束或换了一轮，刷新一下再看。' });
    return;
  }
  if (!stoppable()) return;
  pipeAction = 'stop'; invalidatePipeStatus(); renderPipeButtons();
  try {
    const r = await pipePost(target.daily ? '/api/daily/stop' : '/api/pipeline/stop',
      target.daily ? { batch: target.id } : { run_id: target.id });
    pipeSnapshot.stop_requested = true; renderPipeState(pipeSnapshot);
    toast('已请求停止', { key: 'pipeline', type: 'info', detail: r?.detail || '等待所有当前执行中的阶段结束；已完成的结果会保留。' });
  } catch (e) { toast('停止请求未完成', { key: 'pipeline', detail: e.message }); }
  finally { await refreshPipe(); pipeAction = ''; renderPipeButtons(); }
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshPipe();
});
