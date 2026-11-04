let youtubeRankGeneration = 0;
let youtubeSavedBrowser = 'chrome';

// **慢回包落地之前先对一次身份,过期的一个字都不许往页面上写**(口径同
// `x_channel.js` 的 `xChanAge`,硬约束在 docs/decisions/frontend-feedback.md)。
// 这一块有三个慢动作:打开面板要读一次状态(后端查工具和登录态,实测能到 12 秒)、
// 检查登录态要连一次网(最长 120 秒)、更新 yt-dlp 要下载安装(最长 600 秒)。
//
// **身份的粒度不是「这一块面板」,是「这一个资源的这一次请求」。** 三个动作共用一个
// 代次时,开始任何一个都会让另外两个在途的**有效**回包作废,而作废它们的那个动作并不
// 替它们收尾(审查在真页面上实测两种形态:①更新 yt-dlp 期间点一次检查,更新装完回来的
// 新版本号写不进页面,版本行停在旧版、按钮已恢复;②刚切过来、状态还在途时点检查,
// 浏览器下拉永远只剩一项、工具行一直停在「正在检查工具和模型配置…」,重开页面才恢复)。
// 这和「把 Firefox 的旧结论贴到 Chrome 旁边」不是一类:那条结论**本应**作废,这几条不。
//
// 所以拆成两层,各管一件事:
// * **面板身份** `youtubePanel`:换来源、或者重填整块面板时才推 —— 那一刻页面上那几格
//   全换成服务端那一份了,在途的回包一律无主。
// * **各资源自己的代次** `youtubeSeq`:只作废**同一个资源**上一次的请求。
// 再加一层「这块显示区现在谁说话算数」(`youtubeSays`):三个动作写的是重叠的两块区
// (登录态那行、yt-dlp 那行),档位高的在途时档位低的回包不许往那块区里写 —— 但它照旧
// 要把**自己那块**收尾(清掉 loading、补齐清单)。档位:更新 > 检查 > 状态,理由是
// 更新会真的改掉版本,检查会真的连一次网,而状态只是便宜的那一道快照。
let youtubePanel = 0;
const youtubeSeq = {status: 0, check: 0, upgrade: 0};
const youtubeRank = {status: 1, check: 2, upgrade: 3};
// 两块共享显示区各自的**在途请求**(谁还没结束)和**落地修订号**(被写过几回)。
// 档位只拿来比在途的那几个,**不留在显示区上** —— 留下来就只升不降(见 `youtubeRelease`)。
const youtubeFlying = {login: new Set(), tool: new Set()};
const youtubeWrote = {login: 0, tool: 0};

/** 重填整块面板:推面板身份。在途那些的回包会被 `youtubeOwns` 判废,占用一并作废。 */
function youtubePanelAge() {
  youtubeFlying.login.clear(); youtubeFlying.tool.clear();
  return ++youtubePanel;
}
/** 记下这一次请求的身份,**占住**它要写的那几块显示区,并记下各块当时的修订号。 */
function youtubeStart(action, regions) {
  const token = {action, panel: youtubePanel, seq: ++youtubeSeq[action],
                 regions: regions || [], saw: {}};
  for (const region of token.regions) {
    token.saw[region] = youtubeWrote[region];
    youtubeFlying[region].add(token);
  }
  return token;
}
/** 这一次请求**结束了** —— 成功、失败、超时、半路判废都算 —— 松开它占着的那几块。
 *
 * **已经结束的动作不许继续占着显示区。**「动作种类档位高」说的是重叠期间谁先说话,
 * 不是「这份结果永远更新」:不松开的话档位只升不降,之后**新发起**的检查永远写不进
 * 那块区(审查实测:更新完再点检查,版本行停在「正在下载并安装…」一动不动,而登录
 * 结论照旧刷新 —— 像是只坏了半边)。所以三处调用都在 `finally` 里,不挑分支。
 */
function youtubeRelease(token) {
  for (const region of token.regions) youtubeFlying[region].delete(token);
}
/** 这个回包还算不算数:面板没换过,而且这个资源没有更新的请求发出去。 */
function youtubeOwns(token) {
  return sourceKind === 'youtube' && token.panel === youtubePanel
    && token.seq === youtubeSeq[token.action];
}
/** 这块显示区轮不轮得到它说话。**两道判据,少一道就错一头**:
 *
 * ①**还在途**的里面有档位更高的:那块区是它的,这份便宜快照不许抢在它前面写
 *   (检查在途时回来的状态快照会把刚检出来的结论盖成一句笼统的「已配置」)。
 * ②自己发起**之后**这块区已经被人写过:那份比自己新,这份是旧的,丢掉。专治「真正
 *   重叠的旧检查晚回包」—— 更新装完之后它才回来,①已经不拦了,而它带的版本号是
 *   更新前那个,写进去等于把 2099.01.01 倒回旧版。
 *
 * 轮到了就把修订号推一格,后来者按它比。
 */
function youtubeMaySay(token, region) {
  for (const other of youtubeFlying[region]) {
    if (other !== token && youtubeRank[other.action] > youtubeRank[token.action]) return false;
  }
  if (token.saw[region] !== youtubeWrote[region]) return false;
  token.saw[region] = ++youtubeWrote[region];
  return true;
}
// **代次拦不住另一类写入:状态回包改的是人正在填的那两格。**
// 选浏览器和填资料名都不发请求、也就不推代次,而 `/youtube/status` 这一包要等后端
// 查完工具和登录态(审查实测把它压到 12 秒):切到 YouTube 后趁「正在检查登录态
// 配置…」填上 Profile 2,回包落地把输入框改回已存的 Profile 1,一句提示都没有。
// 所以**另立一个「人碰过了」的身份位**:碰过之后状态回包只许补下拉清单,显示值一律
// 用页面上那一份(`youtubeBrowserSpec()`)。保存或换来源重填面板时这一位清零 ——
// 那时页面上那一份就是服务端那一份。
let youtubeBrowserDraft = false;
const youtubePoolLabels = {creator_core:'核心创作者', interview_pool:'AI 访谈池',
  creator_watchlist:'观察名单（不自动排名）', quarantine:'隔离名单（不自动排名）'};
const youtubeNumberFields = {max_video_minutes:['单条时长上限（分钟）',1,180], window_days:['候选回看天数',1,30],
  rank_limit:['候选榜准入名次',1,50],
  run_seconds:['每轮处理时间上限（秒）',60,3600], asr_threads:['本地语音识别线程数',1,8]};

function youtubeEditor(source, kind) {
  let fields = $('youtube-channel-fields');
  if (!fields) {
    fields = document.createElement('fieldset'); fields.id = 'youtube-channel-fields';
    fields.innerHTML = `<legend>YouTube 排名规则</legend><label for="youtube-pool">频道分组</label>
      <select id="youtube-pool">${Object.entries(youtubePoolLabels).map(([key,label]) => `<option value="${key}">${label}</option>`).join('')}</select>
      <label for="youtube-weight">排名权重（0.1–3）</label><input id="youtube-weight" type="number" min="0.1" max="3" step="0.05">
      <label for="youtube-minutes">单条时长上限（分钟）</label><input id="youtube-minutes" type="number" min="1" max="180" step="1" placeholder="跟随全局">
      <p class="hint">权重乘在候选分上。启用订阅不等于批准下载全部视频。</p>`;
    $('source-edit-error').before(fields);
  }
  fields.hidden = kind !== 'youtube'; fields.disabled = kind !== 'youtube';
  let policy = {};
  try { policy = JSON.parse(source?.config || '{}'); } catch {}
  $('youtube-pool').value = policy.pool || 'creator_core';
  $('youtube-weight').value = policy.score_multiplier ?? 1;
  $('youtube-minutes').value = policy.max_video_minutes ?? '';
  for (const id of ['source-edit-feed', 'source-edit-fulltext']) {
    const input = $(id), label = input.closest('label') || document.querySelector(`label[for="${id}"]`);
    input.hidden = kind === 'youtube'; input.disabled = kind === 'youtube';
    if (label) label.hidden = kind === 'youtube';
    const note = (id === 'source-edit-feed' ? input : label)?.nextElementSibling;
    if (note?.classList.contains('hint')) note.hidden = kind === 'youtube';
  }
}

function youtubeChannelPayload() {
  const minutes = $('youtube-minutes').value.trim();
  return {pool:$('youtube-pool').value, score_multiplier:Number($('youtube-weight').value),
    max_video_minutes:minutes ? Number(minutes) : null};
}

function fillYouTubeSettings(settings, sent) {
  let fields = $('youtube-settings-fields');
  if (!fields) {
    fields = document.createElement('div'); fields.id = 'youtube-settings-fields';
    fields.innerHTML = `<h3>登录态与工具</h3>
      <p class="hint">不登录时 YouTube 会对本机出口要求验证，整轮采集一条也处理不了。</p>
      <label for="youtube-cookies_browser">从哪个浏览器读登录态</label>
      <select id="youtube-cookies_browser"></select>
      <label for="youtube-cookies_profile">浏览器用户资料（可选）</label>
      <input id="youtube-cookies_profile" maxlength="48" placeholder="留空用默认资料，例如 Profile 1">
      <p class="hint">只读 cookie 交给 yt-dlp，不打开也不接管浏览器窗口。资料名在 chrome://version 的「个人资料路径」末段。</p>
      <p id="youtube-browser-err" class="field-err"></p>
      <p id="youtube-login" class="hint" role="status"></p>
      <p id="youtube-login-detail" class="hint"></p>
      <button type="button" class="ghost" id="youtube-login-check">检查登录态</button>
      <p id="youtube-ytdlp" class="hint" role="status"></p>
      <button type="button" class="ghost" id="youtube-ytdlp-upgrade">更新 yt-dlp</button>
      <h3>排名与处理预算</h3><p class="hint">只对候选榜入选的视频花算力：先复用字幕，需要时才下载音频，不做画面理解。</p>
      ${Object.entries(youtubeNumberFields).map(([key,[label,min,max]]) => `<label for="youtube-${key}">${label}</label><input id="youtube-${key}" type="number" min="${min}" max="${max}" step="1">`).join('')}
      <label><input id="youtube-ai_only" type="checkbox"> 仅纳入标题或简介明确与 AI 相关的视频</label>
      <label><input id="youtube-asr_enabled" type="checkbox"> 字幕不可用时允许本地语音转录</label>
      <label for="youtube-whisper_model">Whisper 模型文件路径（可选）</label>
      <input id="youtube-whisper_model" maxlength="2048" placeholder="留空尝试已有本地模型">
      <p class="hint">英文专用模型不能用于其他语言。本地转录不花 token，但吃 CPU 和内存。</p>
      <p id="youtube-tools" class="hint" role="status"></p>`;
    $('source-settings-error').before(fields);
  }
  fields.hidden = sourceKind !== 'youtube';
  fields.querySelectorAll('input').forEach(input => { input.disabled = sourceKind !== 'youtube'; });
  if (sourceKind !== 'youtube') return;
  if (sent && JSON.stringify(youtubeSettingsPayload()) !== sent) return;
  // **身份在真要重填的时候才推,不在函数入口推。** 上面两道提前返回什么都没改 ——
  // 在那里推一下等于把一次「什么都没发生」当成换了面板,把在途的检查结论白白作废。
  youtubePanelAge();
  const token = youtubeStart('status', ['login', 'tool']);
  const values = settings.youtube || {};
  for (const key of Object.keys(youtubeNumberFields)) $(`youtube-${key}`).value = values[key] ?? '';
  for (const key of ['ai_only', 'asr_enabled']) $(`youtube-${key}`).checked = !!values[key];
  $('youtube-whisper_model').value = values.whisper_model || '';
  youtubeSavedBrowser = values.cookies_browser || 'chrome';
  youtubeBrowserDraft = false;
  // **已存的那份规格先同步填一次,不等 status。** 等它填有两处会坏:这期间点了检查或
  // 更新,那两个回包都不带浏览器清单、也不会补这两格,人看着的是空白;而这时保存预算
  // 会把空白资料名当成「他不要资料名了」存回去,等于静默改掉他的选择。
  youtubeFillBrowsers([], youtubeSavedBrowser);
  $('youtube-login').textContent = '正在检查登录态配置…';
  $('youtube-login-detail').textContent = '';
  $('youtube-ytdlp').textContent = '';
  $('youtube-login-check').onclick = youtubeCheckLogin;
  $('youtube-ytdlp-upgrade').onclick = youtubeUpgradeYtdlp;
  $('youtube-cookies_browser').onchange = () => { youtubeBrowserDraft = true; };
  $('youtube-cookies_profile').oninput = () => {
    youtubeBrowserDraft = true;
    $('youtube-browser-err').textContent = youtubeBrowserError();
  };
  $('youtube-browser-err').textContent = '';
  $('youtube-tools').textContent = '正在检查工具和模型配置…';
  pipeJson('/api/intake/youtube/status').then(status => {
    if (!youtubeOwns(token)) return;
    const tools = status.tools;
    const missing = [['yt_dlp','yt-dlp'],['ffmpeg','FFmpeg'],['whisper','whisper-cli'],['model','Whisper 模型']]
      .filter(([key]) => !tools[key]).map(([,label]) => label);
    // 工具行和浏览器清单是**这一包自己的**那两块,谁都不会替它写 —— 期间点过检查或
    // 更新也要照旧补齐收尾,否则下拉只剩一项、工具行永远停在「正在检查…」。
    $('youtube-tools').textContent = (status.models_ready ? '中文翻译与内容笔记模型可用。' : status.model_hint + '。')
      + (missing.length ? `缺少 ${missing.join('、')}。字幕不可用时不会伪造转录。` : '下载与本地转录工具已就绪。');
    youtubeFillBrowsers(status.browsers || [], youtubeBrowserDraft ? youtubeBrowserSpec() : youtubeSavedBrowser);
    // 这两块是和检查/更新重叠的区:期间点过那两个的话,这份便宜快照不许盖掉它们。
    if (youtubeMaySay(token, 'login')) youtubeShowLogin(status.login || {});
    if (youtubeMaySay(token, 'tool')) youtubeShowYtdlp(status.ytdlp || {});
  }).catch(error => {
    if (youtubeOwns(token)) $('youtube-tools').textContent = '工具状态读取失败：' + error.message;
  }).finally(() => youtubeRelease(token));
}

function youtubeSettingsPayload() {
  const result = {};
  for (const key of Object.keys(youtubeNumberFields)) result[key] = Number($(`youtube-${key}`).value);
  for (const key of ['ai_only', 'asr_enabled']) result[key] = $(`youtube-${key}`).checked;
  result.whisper_model = $('youtube-whisper_model').value.trim();
  // **这一项漏了就是静默改掉用户的选择**:后端按字段集合校验,缺的那个会落回默认
  // 浏览器,而他只是改了个预算数字、根本没碰登录态那一栏。
  result.cookies_browser = youtubeBrowserSpec();
  return result;
}

// ── 候选排名 ──────────────────────────────────────────────────────────────
//
// 候选榜单独开一个弹窗,不压在订阅清单顶上:订阅管理那一栏只放订阅,榜单要看才点开。
// 数据在**打开那一刻**才取 —— 关着的时候不为它发请求。代次 `youtubeRankGeneration`
// 只管这一个资源:关掉、重开、点重试都推一格,迟到的回包一个字都不许写进弹窗。
//
// **入口显隐跟着「现在是哪一页」走,不跟着「上一次列表渲染成功没有」走。** 挂在
// `updateSourceFilterControls` 上,切来源、切视图那一下就同步;挂在列表渲染上的话,
// 切到一个读取失败的来源时渲染根本不发生,旧入口留在 RSS 页上,点开是 YouTube 的榜
// (审查实测)。打开那一下再核对一次身份,是同一条不变量的第二道。
//
// 弹窗标记在这儿现建、关了就删,不写进 index.html(那份已经顶着 700 行上限),口径同
// `egress.js` 顶上那段:现建的弹窗 MutationObserver 只看得见它的建和删。
let youtubeRankItems = [], youtubeRankResult = null;
// 人点开/收起过哪几格(按格的键记)。搜索时每敲一个字都要重画,不记的话他刚收起的那一组
// 下一个字就又展开了。
const youtubeRankOpen = new Map();
const youtubeRankShelves = {waiting:'暂未排名', ready:'已完成', skipped:'不处理'};
// 榜单这一份数据现在是什么状态。**「还在读」和「读到了、是空的」不许共用一个空数组**:
// 读取途中在搜索框敲一个字,空数组会被当成「还没有候选」,叫人去点同步。
let youtubeRankState = 'loading';
const youtubeRankStates = {candidate:'候选待处理', deferred:'留待下轮', filtered:'轻量过滤', excluded:'不自动处理',
  processing:'正在处理', ready:'中文笔记已完成', failed:'处理失败，待重试'};

function youtubeRankingHere() {
  return sourceKind === 'youtube' && sourceView === 'manage' && sourceActive();
}

function updateYouTubeRankingEntry() {
  const button = $('youtube-rank-open');
  if (button) button.hidden = !youtubeRankingHere();
}

function openYouTubeRanking() {
  if (!youtubeRankingHere()) { updateYouTubeRankingEntry(); return; }
  closeYouTubeRanking();
  const mask = document.createElement('div');
  mask.id = 'youtube-rank-modal'; mask.className = 'modal-mask';
  mask.innerHTML = `<div class="modal youtube-rank-modal" role="dialog" aria-modal="true" aria-labelledby="youtube-rank-title">
    <header class="youtube-rank-heading"><h2 id="youtube-rank-title">候选排名</h2><span id="youtube-rank-count" role="status"></span><button class="icon-button" aria-label="关闭候选排名">×</button></header>
    <input id="youtube-rank-q" type="search" placeholder="搜索标题或频道…" aria-label="搜索候选视频">
    <div id="youtube-ranking" class="youtube-ranking" aria-live="polite"></div></div>`;
  mask.addEventListener('click', event => { if (event.target === mask) closeYouTubeRanking(); });
  mask.querySelector('.icon-button').onclick = closeYouTubeRanking;
  mask.querySelector('#youtube-rank-q').oninput = filterYouTubeRanking;
  // **只记人自己点的那一下,不听 `toggle`。** `toggle` 是异步派发的,而且带着 `open` 被
  // 画出来的那几格也会派一次:每敲一个字重画一遍,上一版那几格的事件晚到,就把「搜索时临时
  // 全展开」记成了他的选择(实测:收起的那一组清空搜索后又展开了)。点击在切换之前同步到达,
  // 所以记的是切换之后的那个状态 `!group.open`。
  mask.querySelector('#youtube-ranking').addEventListener('click', event => {
    const group = event.target.closest('.youtube-rank-group > summary')?.parentElement;
    if (group && !$('youtube-rank-q').value.trim()) youtubeRankOpen.set(group.dataset.group, !group.open);
  });
  document.body.append(mask);
  overlayClosers['youtube-rank-modal'] = closeYouTubeRanking;
  // 先同步登记一次弹窗,让它记下「关掉时把焦点还给谁」是这颗入口按钮,再把焦点挪到搜索框
  // —— 等 MutationObserver 异步登记的话,它记下的就是已经挪进弹窗的那个搜索框。
  syncOverlays();
  $('youtube-rank-q').focus();
  loadYouTubeRanking();
}

function closeYouTubeRanking() {
  ++youtubeRankGeneration;
  $('youtube-rank-modal')?.remove();
}

async function loadYouTubeRanking() {
  const generation = ++youtubeRankGeneration;
  youtubeRankItems = []; youtubeRankState = 'loading';
  $('youtube-rank-count').textContent = '';
  $('youtube-ranking').innerHTML = '<p class="hint">正在读取…</p>';
  try {
    const result = await pipeJson('/api/intake/youtube/ranking');
    if (generation !== youtubeRankGeneration) return;
    youtubeRankItems = result.items; youtubeRankResult = result;
    youtubeRankState = 'ready';
    $('youtube-rank-count').textContent = `${result.total} 条 · 每轮准入前 ${result.rank_limit} 名`;
    filterYouTubeRanking();
  } catch (error) {
    if (generation !== youtubeRankGeneration) return;
    youtubeRankState = 'failed';
    const message = document.createElement('p'); message.setAttribute('role','alert'); message.textContent = '候选排名读取失败：' + error.message;
    const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'ghost'; retry.textContent = '重试'; retry.onclick = loadYouTubeRanking;
    $('youtube-ranking').replaceChildren(message, retry);
  }
}

function filterYouTubeRanking() {
  const box = $('youtube-ranking');
  if (!box || youtubeRankState !== 'ready') return;
  if (!youtubeRankItems.length) {
    box.innerHTML = '<p class="hint">还没有候选。点一次「同步全部」先取轻量候选，不会直接处理全部视频。</p>';
    return;
  }
  const query = $('youtube-rank-q').value.trim().toLowerCase();
  const items = youtubeRankItems.filter(item => (item.title + ' ' + item.channel).toLowerCase().includes(query));
  // **只有一张榜**(仓库主人拍板:不再区分热度和价值),所以这里没有排序下拉 ——
  // 一张榜只有一个顺序,按发布日期分组只是把它摊开,组内照旧按名次排。
  box.innerHTML = youtubeRankGroups(items).map(group => {
    const open = query ? true : youtubeRankOpen.get(group.key) ?? group.open;
    return `<details class="youtube-rank-group" data-group="${group.key}"${open ? ' open' : ''}>
      <summary><b>${group.title}</b><span>${group.meta}</span>${arrow('caretDown')}</summary>${group.items.map(youtubeRankItem).join('')}</details>`;
  }).join('') || '<p class="hint">没有匹配的候选。</p>';
}

/** 上海时区的那一天(`2026-11-04`),读不出日期就是空串。按宿主机时区分组的话,一条
 * 北京时间凌晨发布的视频会被归到前一天(CLAUDE.md 元规则:时间一律按项目时区)。 */
function youtubeRankDay(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('sv-SE', {timeZone:'Asia/Shanghai'});
}

function youtubeRankDayTitle(day) {
  if (!day) return '日期未知';
  const label = new Date(day + 'T12:00:00+08:00').toLocaleDateString('zh-CN',
    {timeZone:'Asia/Shanghai', month:'long', day:'numeric', weekday:'short'});
  const today = youtubeRankDay(Date.now()), yesterday = youtubeRankDay(Date.now() - 86400000);
  return day === today ? '今天 · ' + label : day === yesterday ? '昨天 · ' + label : label;
}

/** 排上名的按**发布日期**分组(新的在上,组内按名次),名次在 `rank_limit` 以内的就地标出
 * 「下一轮准入」。不写「第几期 / 哪天处理」:每天都有新视频排进来,把后面的往后挤,按名次
 * 推算出来的处理日期几乎总是错的,写出来等于替排名许一个兑现不了的诺。
 * 没排上名的三格(`storage.shelf` 判,这里不另判)跟在后面、默认收起。 */
function youtubeRankGroups(items) {
  const limit = youtubeRankResult.rank_limit, days = new Map(), groups = [];
  for (const item of items.filter(item => item.shelf === 'ranked')) {
    const day = youtubeRankDay(item.published);
    if (!days.has(day)) days.set(day, []);
    days.get(day).push(item);
  }
  // 日期未知(空串)排在所有日期之后。
  for (const day of [...days.keys()].sort((left, right) => (right || '0').localeCompare(left || '0'))) {
    const members = days.get(day).sort((left, right) => left.score_rank - right.score_rank);
    const next = members.filter(item => item.score_rank <= limit).length;
    groups.push({key:'d' + day, title:youtubeRankDayTitle(day), open:true, items:members,
      meta:`${members.length} 条${next ? ` · 下一轮准入 ${next} 条` : ''}`});
  }
  for (const [shelf, title] of Object.entries(youtubeRankShelves)) {
    const members = items.filter(item => item.shelf === shelf)
      .sort((left, right) => String(right.published).localeCompare(String(left.published)));
    if (!members.length) continue;
    const total = youtubeRankResult.shelves?.[shelf] || members.length;
    const listed = youtubeRankItems.filter(item => item.shelf === shelf).length;
    groups.push({key:shelf, title, open:false, items:members,
      meta:`${total} 条${listed < total ? ` · 列出最近 ${listed} 条` : ''}`});
  }
  return groups;
}

function youtubeRankItem(item) {
  const facts = [esc(item.channel)];
  if (item.shelf === 'ranked') {
    facts.push(`第 ${item.score_rank} 名 / ${item.score} 分`);
    // 按估算日期分进那一组的,要说一声那天是估的(flat 列表只给粗日期,见 `approximate`)。
    if (item.approximate) facts.push('日期为估算');
  } else if (item.published) facts.push(esc(sourceTime(item.published)));
  facts.push(item.approximate ? '播放量未知' : item.views + ' 次观看');
  const next = item.shelf === 'ranked' && item.score_rank <= youtubeRankResult.rank_limit;
  // 已完成那一格的状态就是格名本身,再写一遍「中文笔记已完成」是同一句话说两次。
  const state = item.state === 'ready' ? '' :
    esc(youtubeRankStates[item.state] || item.state) + (item.reason ? '：' + esc(item.reason) : '');
  return `<article class="youtube-rank-item"><a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a>
    <p class="hint">${next ? '<b class="youtube-rank-next">下一轮准入</b>' : ''}${facts.join(' · ')}</p>${state ? `<p class="hint">${state}</p>` : ''}</article>`;
}

// ── 登录态与 yt-dlp ────────────────────────────────────────────────────────
//
// 浏览器清单、每一档的名字、就绪判断都由后端给(`/api/intake/youtube/status`),
// 前端不写死一张表 —— 口径同 `x_channel.js` 顶上那句。
// **这里用下拉而不是 X 那边的单选**:那四条通道要准备的东西完全不同(一个要装命令行
// 工具、一个要开调试开关),摆在下拉里看不出代价;而这七个浏览器要做的事是同一件
// ——「你平时在哪个浏览器里登录 YouTube」,选项之间没有代价差别。

// 下拉 + 资料名拼成交给后端的那一份规格(`chrome` 或 `chrome:Profile 1`)。
// **保存、检查、回填都走这一对函数** —— 口径只有一份,否则「存下去的」和
// 「页面上显示的」会从某一次改动起对不上,而两边各自看着都对。
function youtubeBrowserSpec() {
  const browser = $('youtube-cookies_browser')?.value || youtubeSavedBrowser;
  const profile = ($('youtube-cookies_profile')?.value || '').trim();
  return profile ? `${browser.split(':')[0]}:${profile}` : browser.split(':')[0];
}

/** 资料名的就地校验。**字段级的错写在字段下面,不飘 toast**(CLAUDE.md 前端反馈那节)。 */
function youtubeBrowserError() {
  const profile = ($('youtube-cookies_profile')?.value || '').trim();
  if (profile && !/^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,47}$/.test(profile)) {
    return '用户资料名只能用字母、数字、空格和 . _ + - ，且要以字母或数字开头';
  }
  return '';
}

/** 重填下拉清单,并把 `saved` 这一份规格显示出来(拆成「下拉 + 资料名」两格)。
 *
 * **`saved` 是「该显示哪一份」,不是「服务端存的是哪一份」** —— 人碰过这两格之后
 * 调用方传的是页面上那一份,于是这里写回去是个空操作,草稿原样留着;没碰过才传服务端
 * 那一份。这样「显示值从哪来」仍然只有一处判断。 */
function youtubeFillBrowsers(browsers, saved) {
  const box = $('youtube-cookies_browser');
  if (!box) return;
  box.innerHTML = browsers.map(item =>
    `<option value="${esc(item.id)}">${esc(item.label)}${item.installed ? '' : '（本机未找到）'}</option>`).join('');
  // **带 profile 的设置值不在清单里,要补一条。** 存量设置可以写成 `chrome:Profile 1`,
  // 不补的话下拉框会把它静默显示成第一项,而用户只是进来看了一眼预算 —— 下一次保存
  // 就把他的 profile 丢了(同 X 那边「退役通道名要留成别名」那一类坑)。
  const [browser, ...rest] = String(saved || '').split(':');
  if (browser && !browsers.some(item => item.id === browser)) {
    const extra = document.createElement('option');
    extra.value = browser; extra.textContent = browser + '（当前设置）';
    box.prepend(extra);
  }
  box.value = browser;
  const profile = $('youtube-cookies_profile');
  if (profile) profile.value = rest.join(':');
}

function youtubeShowLogin(login) {
  const line = $('youtube-login'), detail = $('youtube-login-detail');
  if (!line) return;
  // **「取到了元数据」和「同时报了一条警告」要并列说,不是二选一。** cookie 解密失败只是
  // 一条 warning:这一轮确实取到了公开视频,所以不许报成「登录态无效」(人刚取到东西,
  // 那句话当场变谎话);但也不许只说前半句 —— 那条警告恰恰是「这一轮没带上登录态」的
  // 唯一证据,扔了之后面板上一切正常(审查实测)。
  line.textContent = [login.message || '登录态状态未知。', login.warning || ''].filter(Boolean).join(' ');
  // 没就绪的那几档要一眼看得出来,不能和普通说明长一个样。
  const bad = login.ready === false;
  line.setAttribute('role', bad || login.warning ? 'alert' : 'status');
  line.classList.toggle('field-err', bad);
  line.classList.toggle('hint', !bad);
  if (detail) detail.textContent = login.detail ? 'yt-dlp 原文：' + login.detail : '';
}

function youtubeShowYtdlp(info) {
  const line = $('youtube-ytdlp');
  if (!line) return;
  if (!info.installed) {
    line.textContent = '没有找到 yt-dlp。点下面的更新会装一份到数据目录，系统里 brew 或 pip 装的那份不动。';
    line.classList.add('field-err'); line.classList.remove('hint');
    return;
  }
  line.classList.remove('field-err'); line.classList.add('hint');
  const origin = info.origin === 'bundled' ? '数据目录自备' : '系统 PATH';
  let text = `yt-dlp ${info.installed}（${origin}）。`;
  if (info.outdated) text += `上游已到 ${info.latest}，可以更新。`;
  else if (info.latest) text += '已是上游最新版。';
  else if (info.latest_error) text += '没查到上游版本：' + info.latest_error;
  else text += '点「检查登录态」会一起查上游版本。';
  // **解 JS 挑战那两样单独说一句。** YouTube 会对一部分请求发一道 JS 挑战,解不开就只
  // 剩预览图格式 —— 元数据和字幕照旧拿得到,下音频做本地转录那一步会失败(实测:带着
  // 有效登录态也一样)。缺脚本和缺运行时让用户做的事不同,所以分开说。
  // **这两样各是三态:没问到 / 问到了确实没有 / 问到了就是这个版本。**
  // 「没问到」说成「确实没装」的后果实测过两回:①便宜那条状态快照不起子进程、压根没查
  // (`toolchain.local` 答 null),一打开订阅管理就红字说两样都缺,而机器上明明都装着,
  // 离开再回来一样;②`yt-dlp -v` 临时报错、一行抬头都没印的那一轮,页面照旧让人去装
  // yt-dlp-ejs 和 deno,真正的检查错误一个字都没显示 —— 让同一个替身恢复抬头、不装不卸
  // 任何东西,那两句误报就消失了。**所以缺失一律判严格相等 `=== ''`**:
  // `!info.script` 把 null / undefined 也算成缺,等于把三态压回两态。
  const unknown = (value) => value === null || value === undefined;
  if (unknown(info.script) || unknown(info.runtime)) {
    text += info.challenge_error
      ? ' 查不出解那道 JS 挑战要的脚本和运行时：' + info.challenge_error
      : ' 解那道 JS 挑战要的脚本和运行时，点一次「检查登录态」才会查。';
  }
  if (info.script === '') text += ' 还缺 JS 挑战的解题脚本（yt-dlp-ejs），更新一次会一起装上。';
  // **要装的是 deno,不是「四个里随便一个」**:yt-dlp 默认只启用 deno,只装了 node 的
  // 机器上这一行照旧是缺(口径见 `login.RUNTIME` 那段一手源码与实测)。
  if (info.runtime === '') text += ' 机器上没有 deno，装一个才解得开那道挑战 —— yt-dlp 只默认启用它这一个运行时。';
  // **版本旧不是「抓不到」的原因。** 实测新旧两版撞的是同一道登录墙,所以这里只报版本,
  // 不把它写成一条待修的故障 —— 否则用户会以为更新一下登录墙就过了。
  line.textContent = text;
}

async function youtubeCheckLogin() {
  // 规格不合法就别连那一次网:后端会原样拒掉,而这里能把原因写在字段下面。
  const invalid = youtubeBrowserError();
  $('youtube-browser-err').textContent = invalid;
  if (invalid) return;
  // 记下自己的身份,并把两块显示区占到「检查」这一档 —— 否则那条便宜的状态快照回来
  // 会把刚检出来的结论盖成一句笼统的「已配置」。**它只是不许写这两块,不是整包作废**:
  // 浏览器清单和工具行照旧由它补齐。
  const token = youtubeStart('check', ['login', 'tool']);
  const button = $('youtube-login-check');
  const previous = button.textContent;
  button.disabled = true; button.textContent = '正在检查…';
  $('youtube-login').textContent = '正在连一次 YouTube 验证登录态…';
  $('youtube-login-detail').textContent = '';
  try {
    const result = await pipePost('/api/intake/youtube/check', {}, 120000);
    if (!youtubeOwns(token)) return;
    const login = result.login || {}, used = login.browser || '';
    // **身份要跟回包时的现状比,不是跟点击那一刻的快照比。**(审查实测:检查在途时
    // 改选并保存另一个浏览器,结论就贴在新选择旁边说「有效」。)分两档:
    // ①连「已保存那一份」都换掉了 —— 这条结论无主,丢掉,只留一句说明;
    // ②只是下拉上有未保存的新选择 —— 结论照显示,但要写明它说的是哪一个。
    if (used && used !== youtubeSavedBrowser) {
      if (!youtubeMaySay(token, 'login')) return;
      $('youtube-login').textContent =
        `这次检查的是「${used}」，而现在保存的是「${youtubeSavedBrowser}」：结论已作废，请重新检查。`;
      $('youtube-login').classList.remove('field-err'); $('youtube-login').classList.add('hint');
      toast('登录态结论已作废：期间换过浏览器', {type: 'info'});
      return;
    }
    if (youtubeMaySay(token, 'login')) youtubeShowLogin(login);
    // **版本这一行可能正被一次更新占着**:那次更新装完才是最新的事实,这份顺带查到的
    // 版本不许盖在它上面(审查实测:更新回来的 2099.01.01 写不进页面,版本行停在旧版)。
    if (youtubeMaySay(token, 'tool')) youtubeShowYtdlp(result.ytdlp || {});
    const spec = youtubeBrowserSpec();
    if (used && spec && used !== spec) {
      $('youtube-login-detail').textContent =
        `这条结论来自已保存的「${used}」，不是你现在选的「${spec}」：先点保存设置，再检查一次。`;
    }
    // ready 不等于被证实(取元数据这一步公开视频也过得去),所以这里有三档文案,
    // 绿色只留给真的拿到证据那一天。
    // ready 不等于被证实,带着警告的 ready 更不等于 —— 那句话要出现在这一下里,
    // 否则人只看 toast 就以为这一轮干净过了。
    toast(login.ready ? (login.warning ? '取到了元数据，但工具同时报了一条警告'
                                       : (login.confirmed ? '登录态有效' : '工具链和出口通；登录态仍未被证实'))
                      : (login.message || '登录态检查未通过'),
          {type: login.ready && !login.warning ? (login.confirmed ? 'ok' : 'info') : 'err',
           detail: login.warning || ''});
  } catch (error) {
    if (!youtubeOwns(token) || !youtubeMaySay(token, 'login')) return;
    $('youtube-login').textContent = '检查没能完成：' + error.message;
    toast('登录态检查没能完成', {type: 'err', detail: error.message});
  } finally {
    youtubeRelease(token);
    button.disabled = false; button.textContent = previous;
  }
}

async function youtubeUpgradeYtdlp() {
  const button = $('youtube-ytdlp-upgrade');
  if (!await confirmBox({title: '更新 yt-dlp？',
      body: '会往数据目录装一份最新的 yt-dlp；系统里用 brew 或 pip 装的那份不动。需要联网，可能要几分钟。'})) return;
  // 更新是档位最高的那一个:它真的会换掉版本,**在途期间**任何一份别处查到的版本号都是
  // 旧的。只是「期间」—— 它一结束就得松开那块区(`finally` 里的 `youtubeRelease`)。
  const token = youtubeStart('upgrade', ['tool']);
  const previous = button.textContent;
  button.disabled = true; button.textContent = '正在更新…';
  $('youtube-ytdlp').textContent = '正在下载并安装 yt-dlp，这一步可能要几分钟…';
  try {
    const result = await pipePost('/api/intake/youtube/ytdlp/upgrade', {}, 600000);
    if (!youtubeOwns(token)) return;
    if (youtubeMaySay(token, 'tool')) youtubeShowYtdlp(result.ytdlp || {});
    toast('yt-dlp 已更新到 ' + (result.ytdlp?.installed || '最新版'), {type: 'ok'});
  } catch (error) {
    if (!youtubeOwns(token) || !youtubeMaySay(token, 'tool')) return;
    $('youtube-ytdlp').textContent = '更新失败：' + error.message;
    toast('yt-dlp 更新失败', {type: 'err', detail: error.message});
  } finally {
    youtubeRelease(token);
    button.disabled = false; button.textContent = previous;
  }
}
