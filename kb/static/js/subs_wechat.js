// 「我的订阅」里**公众号那一个视图**:连接卡 + 订阅列表 + 「同步全部」+ 它那块提示条。
//
// 从 `sources.js` 里分出来,因为它是那一页上**唯一不走 feeds 那套接口**的视图
// (`/api/intake/{kind}` 只对 feeds 来源成立,`sourceModules` 也只收那一半) ——
// 它读的是采集通道(`pipe_channels.js`)和 `loadTargets()`,和同文件里那一大套
// `source*` 状态机没有共享状态。拆开也是为了守住单文件 700 行那条硬上限(CLAUDE.md)。
//
// **仍然是普通脚本、仍然共享全局作用域**:`index.html` 把它排在 `sources.js` 前面,
// 因为这里的 `let` 有 TDZ,而 `sources.js` 尾部那几个 document 级监听器会读
// `wechatNoticesAutoOpened`(都在回调里读,所以顺序其实只是为了读代码的人顺着看)。
// 反过来这里用到的 `subsKind` / `subsCatalog` / `PIPE_INTAKE_STAGES` 都住在
// `sources.js`,只在点击之后才取值。布局口径见 docs/decisions/subs-layout.md。

let wechatNoticesAutoOpened = false;

function closeWechatNotices(restoreFocus = false) {
  const notices = $('subs-wechat-notices');
  const focused = restoreFocus && notices.contains(document.activeElement);
  notices.open = false;
  notices.classList.remove('is-auto-open');
  wechatNoticesAutoOpened = false;
  if (focused) notices.querySelector('summary').focus({ preventScroll:true });
}
function updateWechatNotices(detailChanged = false) {
  const detail = !$('src-detail').hidden, running = !$('subs-wechat-progress').hidden;
  const notices = $('subs-wechat-notices'), summary = $('subs-wechat-notices-summary');
  notices.hidden = !detail && !running;
  if (notices.hidden || (!detail && wechatNoticesAutoOpened)) closeWechatNotices();
  notices.classList.toggle('has-error', detail && $('src-detail').classList.contains('error'));
  summary.textContent = detail ? $('src-detail').textContent : $('subs-wechat-progress-text').textContent;
  summary.title = summary.textContent;
  if (detail && detailChanged) {
    if (!notices.open) wechatNoticesAutoOpened = true;
    notices.open = true;
  }
  notices.classList.toggle('is-auto-open', wechatNoticesAutoOpened && notices.open);
}

// ── 公众号视图:连接卡 + 订阅列表 + 同步全部 ────────────────────────────────
let subsWechatBusy = false;
// 这个视图管的通道由**来源目录**决定(模块声明的 channels,退役名过别名表),
// 不信 `kb-sync-channel` 里那份全局选择 —— 旧版同步中心用它记「上次看哪个来源的
// 连接状态」,存量值可能是 rss/social(都是合法通道,别名迁移不会动它们)。
// 沿用的话公众号视图的连接卡、订阅列表、「同步全部」全跟着串到别的来源上。
function wechatSubsChannel() {
  const spec = subsCatalog.find(m => m.id === subsKind && m.manager !== 'feeds');
  return adoptChannels(spec?.channels || []).find(id => pipeChannels.some(c => c.id === id)) || '';
}
async function openWechatSubs() {
  renderWechatSubsStatus();
  // 失败要留重试入口:初始化失败后通道还没绑上,这时唯一能把视图带回正轨的
  // 就是重新跑一遍本函数(连接卡那颗「检查连接」按的也是它)。
  const retry = '<button class="text-button" onclick="openWechatSubs()">重新读取</button>';
  try { await loadPipeChannels(); } catch (e) {
    refreshPipeSource();   // 连接卡如实落「连接未确认」,不许停在「检查中…」
    $('tgt-list').innerHTML = `<div class="empty">${esc(e.message)}。${retry}</div>`;
    return;
  }
  const channel = wechatSubsChannel();
  if (!channel) {
    refreshPipeSource();
    $('tgt-list').innerHTML = `<div class="empty">这个来源的采集通道暂不可用，请确认采集器配置后重试。${retry}</div>`;
    return;
  }
  // 绑到全局选择上:channelUrl(连接卡、订阅列表的请求)和运行态判断都读它
  setPipeChannel(channel);
  renderWechatSubsStatus();
  refreshPipeSource();
  loadTargets();
}
// 同步状态由 renderPipeButtons 顺手驱动(pipe.js 的轮询回路),这里只管画。
function renderWechatSubsStatus() {
  const section = $('subs-wechat');
  if (!section || section.hidden) return;
  const running = !!pipeSnapshot?.running;
  const mine = wechatSubsChannel();
  // 整份快照递进去，不是只递 crawl_filter：一轮只跑 index/extract 的运行里，
  // filter 上那个来源名是 `start_pipeline` 自动盖的，照着念会把公众号说成正在同步。
  const ours = running && !!mine && pipeRunScopes(pipeSnapshot).some(s => s.channel === mine);
  $('subs-wechat-sync').disabled = subsWechatBusy || running || !!pipeAction;
  $('subs-wechat-sync').textContent = ours ? '同步中…' : '同步全部';
  $('subs-wechat-progress').hidden = !running;
  // 全库那一轮(索引/提取)不归任何来源,但它做的正是这些内容 —— 不能跟
  // 「别的来源在抓取」共用一句话。判据和文案都在 pipe_channels.js。
  $('subs-wechat-progress-text').textContent = ours
    ? pipeRunNote(pipeSnapshot, '正在同步公众号')
    : pipeLibraryOnly(pipeSnapshot)
    ? pipeRunNote(pipeSnapshot) + '，新任务会顺延。'
    : '其他来源正在同步，新任务会顺延。';
  updateWechatNotices();
}
// 公众号那一页的「同步全部」:**也走起跑弹窗**,锁定在本机绿泡泡那条通道上。
// 改之前它直接起跑而**不带步骤清单**,被后端兜底成六段全跑 —— 顺手跑掉几十分钟的
// 全库整理和 Wiki,页面上一个字都不说(`docs/decisions/sync-entry.md`)。
async function syncWechatSubs() {
  if (subsWechatBusy || pipeSnapshot?.running) return;
  subsWechatBusy = true; renderWechatSubsStatus();
  try {
    await loadPipeChannels();
    // 提交的通道同样按来源目录现解析,不拿全局选择当然值
    const channel = wechatSubsChannel();
    if (!channel) throw new Error('这个来源的采集通道暂不可用，请确认采集器配置后重试');
    setPipeChannel(channel);
    await openPipeModal({ channel, accounts: [], stages: PIPE_INTAKE_STAGES });
  } catch (e) { toast('未能打开同步', { detail: e.message }); }
  finally { subsWechatBusy = false; renderWechatSubsStatus(); }
}
