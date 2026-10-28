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
const youtubeNumberFields = {max_videos:['每轮最多处理视频数',1,20], max_minutes:['每轮累计时长（分钟）',1,240],
  max_video_minutes:['单条时长上限（分钟）',1,180], window_days:['候选回看天数',1,30],
  rank_limit:['候选榜准入名次',1,50],
  run_seconds:['每轮处理时间上限（秒）',60,3600], asr_threads:['本地语音识别线程数',1,8]};

function youtubeEditor(source, kind) {
  let fields = $('youtube-channel-fields');
  if (!fields) {
    fields = document.createElement('fieldset'); fields.id = 'youtube-channel-fields';
    fields.innerHTML = `<legend>YouTube 排名规则</legend><label for="youtube-pool">频道分组</label>
      <select id="youtube-pool">${Object.entries(youtubePoolLabels).map(([key,label]) => `<option value="${key}">${label}</option>`).join('')}</select>
      <label for="youtube-weight">排名权重（0.1–3）</label><input id="youtube-weight" type="number" min="0.1" max="3" step="0.05">
      <p class="hint">分组决定是否进入候选排名，权重直接乘在候选分上。启用订阅不等于批准下载全部视频。</p>`;
    $('source-edit-error').before(fields);
  }
  fields.hidden = kind !== 'youtube'; fields.disabled = kind !== 'youtube';
  let policy = {};
  try { policy = JSON.parse(source?.config || '{}'); } catch {}
  $('youtube-pool').value = policy.pool || 'creator_core';
  $('youtube-weight').value = policy.score_multiplier ?? 1;
  for (const id of ['source-edit-feed', 'source-edit-fulltext']) {
    const input = $(id), label = input.closest('label') || document.querySelector(`label[for="${id}"]`);
    input.hidden = kind === 'youtube'; input.disabled = kind === 'youtube';
    if (label) label.hidden = kind === 'youtube';
    const note = (id === 'source-edit-feed' ? input : label)?.nextElementSibling;
    if (note?.classList.contains('hint')) note.hidden = kind === 'youtube';
  }
}

function youtubeChannelPayload() {
  return {pool:$('youtube-pool').value, score_multiplier:Number($('youtube-weight').value)};
}

function fillYouTubeSettings(settings, sent) {
  let fields = $('youtube-settings-fields');
  if (!fields) {
    fields = document.createElement('div'); fields.id = 'youtube-settings-fields';
    fields.innerHTML = `<h3>登录态与工具</h3>
      <p class="hint">元数据、字幕和音频都用你浏览器里的 YouTube 登录态读取。不登录时 YouTube 会对本机出口要求验证，整轮采集一条也处理不了。</p>
      <label for="youtube-cookies_browser">从哪个浏览器读登录态</label>
      <select id="youtube-cookies_browser"></select>
      <label for="youtube-cookies_profile">浏览器用户资料（可选）</label>
      <input id="youtube-cookies_profile" maxlength="48" placeholder="留空用默认资料，例如 Profile 1">
      <p class="hint">只读取 cookie 交给 yt-dlp，不会打开或接管浏览器窗口。YouTube 登在哪个用户资料里，
        就把那个资料名填在上面（Chrome 的地址栏输入 chrome://version 可以看到「个人资料路径」的末段）。</p>
      <p id="youtube-browser-err" class="field-err"></p>
      <p id="youtube-login" class="hint" role="status"></p>
      <p id="youtube-login-detail" class="hint"></p>
      <button type="button" class="ghost" id="youtube-login-check">检查登录态</button>
      <p id="youtube-ytdlp" class="hint" role="status"></p>
      <button type="button" class="ghost" id="youtube-ytdlp-upgrade">更新 yt-dlp</button>
      <h3>排名与处理预算</h3><p class="hint">只对候选榜入选的视频花算力。先复用字幕，需要时仅下载音频；英文译成中文后才入库。画面理解关闭。</p>
      ${Object.entries(youtubeNumberFields).map(([key,[label,min,max]]) => `<label for="youtube-${key}">${label}</label><input id="youtube-${key}" type="number" min="${min}" max="${max}" step="1">`).join('')}
      <label><input id="youtube-ai_only" type="checkbox"> 仅纳入标题或简介明确与 AI 相关的视频</label>
      <label><input id="youtube-asr_enabled" type="checkbox"> 字幕不可用时允许本地语音转录</label>
      <label for="youtube-whisper_model">Whisper 模型文件路径（可选）</label>
      <input id="youtube-whisper_model" maxlength="2048" placeholder="留空尝试已有本地模型">
      <p class="hint">英文专用模型不能用于其他语言；本地转录不消耗模型 token，但会占用 CPU 和内存。缓存字幕和译文可在重试时复用。</p>
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

async function renderYouTubeRanking() {
  const generation = ++youtubeRankGeneration;
  if (sourceKind !== 'youtube' || sourceView !== 'manage' || !sourceActive()) return;
  const selected = $('source-filter').value, query = $('source-q').value.trim().toLowerCase();
  try {
    const result = await pipeJson('/api/intake/youtube/ranking' + (selected ? '?source_id=' + encodeURIComponent(selected) : ''));
    if (generation !== youtubeRankGeneration || sourceKind !== 'youtube' || sourceView !== 'manage' || !sourceActive()) return;
    $('youtube-ranking')?.remove();
    const panel = document.createElement('section'); panel.id = 'youtube-ranking'; panel.className = 'youtube-ranking';
    const items = result.items.filter(item => (item.title + ' ' + item.channel).toLowerCase().includes(query));
    const states = {candidate:'候选待处理', deferred:'留待下轮', filtered:'轻量过滤', excluded:'不自动处理',
      processing:'正在处理', ready:'中文笔记已完成', failed:'处理失败，待重试'};
    // **只有一张榜**(仓库主人拍板:不再区分热度和价值),所以这里没有排序下拉 ——
    // 一张榜只有一个顺序,给它配个「排序」反而让人以为还有另一种排法。
    panel.innerHTML = `<h3>候选排名 · ${result.total} 条</h3><p class="hint">同一视频只处理一次；未入选的视频不下载、不转录。下方仅显示前 100 条候选。</p>
      <div id="youtube-rank-items"></div><p class="hint">${result.total ? '频道管理' : '尚未检查频道。点击同步全部，先获取轻量候选；不会直接处理全部视频。'}</p>`;
    $('source-list').prepend(panel);
    $('youtube-rank-items').innerHTML = [...items].sort((left,right) => (left.score_rank || 100000) - (right.score_rank || 100000))
      .map(item => `<article class="youtube-rank-item"><a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a>
        <p class="hint">${esc(item.channel)} · 第 ${item.score_rank || '—'} 名 / ${item.score} 分 · ${item.approximate ? '播放量未知' : item.views + ' 次观看'}</p>
        <p class="hint">${esc(states[item.state] || item.state)}${item.reason ? '：' + esc(item.reason) : ''}</p></article>`).join('') || '<p class="hint">没有匹配的候选。</p>';
  } catch (error) {
    if (generation !== youtubeRankGeneration || sourceKind !== 'youtube' || sourceView !== 'manage') return;
    $('youtube-ranking')?.remove();
    const panel = document.createElement('section'); panel.id = 'youtube-ranking'; panel.className = 'youtube-ranking';
    const message = document.createElement('p'); message.setAttribute('role','alert'); message.textContent = '候选排名读取失败：' + error.message;
    const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'ghost'; retry.textContent = '重试排名'; retry.onclick = renderYouTubeRanking;
    panel.append(message, retry); $('source-list').prepend(panel);
  }
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
