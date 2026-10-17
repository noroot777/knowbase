// 每日日报:立即生成、历史日报的阅读、页头那两扇抽屉的开合。
// 定时设置那一片在 daily_sched.js(拆出去只因为行数)。
// 生成过程借用同步管道(每个通道跑一趟),细节留在同步中心里看;这里只展示
// "现在跑到哪个来源了"和结果,同一件事不做两套进度展示。
let dailyReports = [], dailyWindow = null;
let dailyCurrent = '', dailyState = null, dailyTimer = null;
// 上一次看到的那一轮**已结束**运行的身份(还没查过状态是 null,正在跑是空串)。
// 身份是 run_id 和完成时刻**两段拼起来的**,哪一段变了都算新一轮:承重的是
// run_id(`finished_at` 只精确到秒,同一秒里跑完的两轮拿它分不开),而完成时刻
// 是它撞号时的兜底 —— 多刷一次列表的代价,换"再不会静默停在旧正文上"。
let dailyDoneRun = null;
let dailyBusy = false;
let dailyWindowDirty = false;
// 从收藏面板点一条日报片段回来时的落点:{ref: 日期, spec: 那段话的锚点}。
// **由这一片自己消费**(`openDailyReport` 成功之后),不让收藏面板那边也发一次
// 打开请求 —— 两条路各发一次就是 dailyReadSeq 要挡的那种先发后到。
let dailyPendingSnip = null;
// `/api/daily/window` 的**代次**(同 `schedules.js` 那道门):发请求前占一个号,
// 回包落地时号被更晚的读取顶掉就整趟作废 —— 不写事实、也不填输入框。
let dailyWindowSeq = 0;
// **「两个时刻还没填过」是页面的职责,不是某一趟请求的私事。** 只用代次判一次的
// 后果是:初始化那趟(要填表)被空闲轮询(只画按钮)顶掉,填表职责跟着整趟一起
// 作废 —— 刷新日报页后区间永久停在「正在算覆盖区间…」、两个时刻框空着,要点一次
// 「接上一份日报」才回来(第 2 轮审查 N4 实测,连续多轮轮询都不自愈)。所以职责
// 挂在页面上,由**落地的那一趟**接手,不管它自己是为什么发起的。
let dailyWindowFillDue = false;

function dailyActive() { return activeTab === 'daily'; }

function dailyTime(value) {
  if (!value) return '';
  const d = new Date(value.replace(' ', 'T'));
  return Number.isNaN(d.getTime()) ? value
    : d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

// 横幅只有一条,却有两个来源:生成失败的原因、列表读不出来的原因。清空时必须
// 认一下是谁写的——列表刷新成功就顺手清掉的话,刚生成失败的那句原因一闪就没了
// (生成失败后紧接着就会刷一次列表),用户只看到按钮弹回去、什么也没发生。
let dailyErrorKind = '';

function dailyError(message = '', kind = '') {
  $('daily-error').hidden = !message;
  $('daily-error-text').textContent = message;
  dailyErrorKind = message ? kind : '';
}

function clearDailyError(kind) {
  if (dailyErrorKind === kind) dailyError();
}

async function loadDailyPanel() {
  await Promise.all([loadDailySettings(), loadDailyReports(), loadDailyWindow()]);
  refreshDailyStatus();
}

// ── 覆盖区间 ────────────────────────────────────────────────────────────────
// 日报覆盖的是「上一份日报的截止时刻 → 这一次的生成时刻」这段时间里**发布**的
// 内容,不是某个自然日、也不是这一轮抓到了什么。默认区间由后端按上一份日报算,
// 这里只负责显示和让人改。

function dailyClock(iso) {
  // '2026-09-29T08:00' → '09-29 08:00'。不过 Date:本地时区解析再格式化,
  // 中途换一次时区表示只会给自己找麻烦,后端给的本来就是本地时刻。
  return iso ? iso.slice(5, 16).replace('T', ' ') : '';
}

function renderDailyWindowLabel() {
  if (!dailyWindow) return;
  const since = dailyClock($('daily-since').value || dailyWindow.since);
  $('daily-window-label').textContent = dailyWindowDirty
    ? `覆盖 ${since} → ${dailyClock($('daily-until').value)}`
    : `覆盖 ${since} 起到生成时刻`;
  renderDailyGenButton();
}

// 生成按钮的名字、它前面那句「有 N 条新内容」(0 条时按钮整颗藏掉)、以及那段
// 写着抓取事实的 tooltip 都在 daily_chip.js —— `dailyTargetDate()` /
// `renderDailyGenButton()` 也在那边。

async function fetchDailyWindow(fillInputs) {
  // **服务器事实统一从这一个口进来。** 抓取事实、队列积压、默认区间每次都接;
  // `dailyWindowDirty` 只挡"改写用户填着的那两个时刻",不挡事实本身 —— 绑在
  // 一起的后果是改过覆盖区间之后,同步完成多久按钮都还写着上一次的次数
  // (审查实测:API 已返回 n=2,按钮停在「第 1 次抓取 · +3 条」)。
  //
  // `fillInputs` 另外由调用方给:空闲轮询一律不填,因为默认区间的截止时刻是
  // "此刻",每跳重填一次会让展开着的「改区间」里的值自己往前跳。**等回包的那段
  // 时间里用户可能刚开始填**,所以落地前再认一次 `dailyWindowDirty` —— 按发请求
  // 时算出来的旧判据覆盖,等于把他刚填的两个时刻吞掉(审查 N2)。
  //
  // 但「还没填过」这件事**不随整趟作废**(审查 N4):要填表的那趟被顶掉时,职责
  // 留在 `dailyWindowFillDue` 上,由真正落地的那趟接手。
  if (fillInputs) dailyWindowFillDue = true;
  const seq = ++dailyWindowSeq;
  let got;
  try {
    got = await pipeJson('/api/daily/window');
  } catch {
    // **最新这一趟读失败 ⇒ 上一趟那些「会授权某个动作」的结论当场过期。**
    // 后端算不出来时自己会答 `pending: null`,但那一路覆盖不到 HTTP 错误、断连、
    // 解析失败和 `pipeJson` 的超时 —— 留着上一次的 0 就是用一次历史成功当无限期
    // 证据:页面继续写「没有新内容」、继续藏着按钮,而库里明明又进了新文章
    // (审查实测:只对这一个接口注入网络失败,服务端已有新增,面板照旧说 0)。
    //
    // **过期的是两件:`pending`(授权"把按钮藏掉")和 `has_report`(授权"不问一声
    // 就覆盖")。** 后者漏掉过一次:今天还没有日报时读到 `has_report=false`,随后
    // 这个接口开始失败、而生成**成功**了 —— 列表里已经是新出的那一份,页面却还攥着
    // 生成**之前**那句"今天没有日报",于是再点生成**不弹覆盖确认**,静悄悄盖掉刚出
    // 的日报(审查实测:两颗按钮都发出了 POST /api/daily/run)。一句过期的"没有"
    // 不能继续当"可以不问"的凭据;丢掉它之后这件事退回列表那份(它正确地含着今天),
    // 答案变成"不知道",于是照旧弹确认。
    //
    // 只用来**显示**的那些(抓取事实、默认区间)留着 —— 它们不授权任何动作,清掉
    // 只会让面板平白少几句话。读成功的那一趟会把两件一起填回来。
    // 代次这一道照旧要过:一条迟到的失败不许盖掉更晚那趟刚读到的好事实。
    if (seq === dailyWindowSeq && dailyWindow)
      dailyWindow = { ...dailyWindow, pending: null, has_report: null };
    return false;
  }
  // 被更晚的那趟顶掉:整趟作废。照自己这份旧回包画会把「第 2 次抓取」退回
  // 「第 1 次」,而新事实已经显示出来了(审查 N2)。
  if (seq !== dailyWindowSeq) return false;
  dailyWindow = got;
  if (dailyWindowFillDue && !dailyWindowDirty) {
    $('daily-since').value = got.since;
    $('daily-until').value = got.until;
    dailyWindowFillDue = false;
  }
  return true;
}

async function loadDailyWindow() {
  // 整块重载。改过区间就不覆盖用户填的那两个时刻,但按钮上的名字、抓取事实和
  // 队列照样要重算 ——「这一天有没有日报」「今天抓了几次」都跟区间改没改无关。
  if (await fetchDailyWindow(!dailyWindowDirty)) renderDailyWindowLabel();
  else renderDailyGenButton();
}

// ── 页头的两扇抽屉 ──────────────────────────────────────────────────────────
// 「改区间」和「改定时」各开一扇,都是页头底下的整宽横条。**同一时刻只开一扇**:
// 两扇叠起来小半屏高,下面的日报列表和正文被推出视野,而人只是想改一个时刻。
// 开合只认 DOM 上的 `hidden`,不另记一份状态 —— 两份状态就有对不上的那一天。
const DAILY_DRAWERS = { window: 'daily-window', sched: 'daily-sched' };

function dailyDrawerOpen(name) { return !$(DAILY_DRAWERS[name]).hidden; }

function setDailyDrawer(name, open) {
  for (const [key, box] of Object.entries(DAILY_DRAWERS)) {
    const on = open && key === name;
    $(box).hidden = !on;
    $(box + '-toggle').setAttribute('aria-expanded', String(on));
  }
}

// Esc 跟抽屉里那颗「取消」是同一件事(确认框那边也是这个约定)。绑在抽屉元素上
// 而不是 document:只有焦点在抽屉里才算数,别处按 Esc 不该惊动它。
function dailyDrawerKey(event, name) {
  if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) return;
  event.stopPropagation();
  if (name === 'window') cancelDailyWindow(); else cancelDailySched();
}

// 开抽屉时记一份「进来之前是什么样」,给「取消」用。
let dailyWindowDraft = null;

function toggleDailyWindow() {
  const open = !dailyDrawerOpen('window');
  if (open) dailyWindowDraft = { dirty: dailyWindowDirty,
    since: $('daily-since').value, until: $('daily-until').value };
  setDailyDrawer('window', open);
}

// 「取消」= 放弃**这次打开之后**改的那几下并收起,不是"清掉自定义区间"。进来时
// 本来就挂着一段自定义区间的话,取消之后它照旧挂着(人想丢掉它有「接上一份日报」
// 那颗按钮)。进来时是默认区间就回到默认 —— 走 resetDailyWindow 重算一次,因为
// 默认区间的截止时刻是"此刻",照进来那一刻的旧值填回去等于把时间钉死了。
async function cancelDailyWindow() {
  const was = dailyWindowDraft;
  setDailyDrawer('window', false);
  if (!was || !was.dirty) { await resetDailyWindow(); return; }
  dailyWindowDirty = true;
  $('daily-since').value = was.since;
  $('daily-until').value = was.until;
  renderDailyWindowLabel();
}

function markDailyWindow() {
  dailyWindowDirty = true;
  renderDailyWindowLabel();
}

async function resetDailyWindow() {
  // **先用已知的默认区间把三样一次性摆一致:两个时刻、页头那句话、要提交的区间
  // (`dailyWindowDirty`),再去取新的默认值。** 顺序反过来(先清 dirty、等重读
  // 回来才还原输入和页头)时,重读一失败屏幕上就留着一段已经被取消掉的自定义
  // 区间 —— 页头写着「覆盖 01-01 → 01-02」,而此刻点生成走的是默认区间、写的是
  // 今天那一份(审查实测:真鼠标点完确认框,最终请求里根本没有 since/until)。
  // 取不回新的默认值不要紧,三样仍然是自洽的"接上一份日报"。
  dailyWindowDirty = false;
  if (dailyWindow) {
    $('daily-since').value = dailyWindow.since;
    $('daily-until').value = dailyWindow.until;
    renderDailyWindowLabel();
  } else {
    // 连默认区间都还没读到过:两个时刻清空、那行字退回"还在算",一样不留下
    // 任何与提交内容不符的说法(落地的那一趟会把它们填上)。
    $('daily-since').value = $('daily-until').value = '';
    $('daily-window-label').textContent = '正在算覆盖区间…';
  }
  await loadDailyWindow();
}

// ── 历史日报 ────────────────────────────────────────────────────────────────

// 「这次列表读取是不是还算数」。正文那边的序号只约束 `openDailyReport` 自己发的
// 请求,**挡不住上游**:一次较早的列表响应后到,它会整份换掉 `dailyReports`、据此
// 重选日报,再**新发起**一次旧日期的正文读取 —— 那一次恰好持有最新序号,于是落到
// 屏幕上(审查实测:新生成的今天那一份先显示出来,旧列表一到就从列表里消失、阅读区
// 跳回昨天、按钮退回「生成日报」,而后台列表一直有今天;完成身份已经消费过,后面
// 多少轮轮询都不恢复,连覆盖确认都失效了 —— 文件还在,前端却认为今天还没生成)。
// 所以成功、失败、重选、按钮重绘都在同一个代次里判。
let dailyLoadSeq = 0;

async function loadDailyReports(keepSelection = true) {
  const seq = ++dailyLoadSeq;
  let data;
  try {
    data = await pipeJson('/api/daily/reports');
  } catch (e) {
    if (seq !== dailyLoadSeq) return;   // 被更新的一次刷新替代了,连错也不报
    dailyError('日报列表读取失败：' + e.message, 'list');
    return;
  }
  if (seq !== dailyLoadSeq) return;     // 整份结果丢掉:列表、选中、按钮都不动
  dailyReports = data.reports;
  clearDailyError('list');
  $('daily-count').textContent = dailyReports.length;
  $('daily-latest').textContent = dailyReports.length
    ? `最近一份 ${dailyReports[0].date}` : '还没有生成过日报';
  $('daily-list-note').textContent = dailyReports.length ? `共 ${dailyReports.length} 份` : '';
  renderDailyList();
  // 收藏回跳指定了哪一天就打开那一天(它只在那一份里才找得到那段话)。
  // **那一份已经不在了就当场说清楚、并把这次回跳丢掉** —— 退回"打开第一份"的话,
  // 屏幕上是另一天的日报、没有任何提示,而待定位还挂着:用户以为跳到了,实际
  // 在读别的东西(审查实测:目标 1999-01-01,最后显示 2026-10-14)。
  let jump = '';
  if (dailyPendingSnip) {
    if (dailyReports.some(r => r.date === dailyPendingSnip.ref)) {
      jump = dailyPendingSnip.ref;
    } else {
      toast('这条收藏出自 ' + dailyPendingSnip.ref + ' 的日报，那一份已经不在了',
        { type: 'info', detail: '收藏里那段原文还在' });
      dailyPendingSnip = null;
    }
  }
  const wanted = jump || (keepSelection && dailyReports.some(r => r.date === dailyCurrent)
    ? dailyCurrent : dailyReports[0]?.date);
  // await:调用方("生成完了,刷新一下")等的是"新那一份已经在屏幕上",不是
  // "列表请求回来了"。不等的话阅读区还挂着上一份的正文和元信息,看起来像是
  // 生成没生效(2026-09-30 复审在全新沙箱上抓到,回归因此假红)。
  if (wanted) await openDailyReport(wanted);
  if (seq !== dailyLoadSeq) return;     // 读正文期间又刷了一次,按钮归那一次画
  // 按钮叫"生成"还是"重新生成"取决于目标那天有没有日报,而那正是这份列表。
  renderDailyGenButton();
}

function renderDailyList() {
  $('daily-list').innerHTML = dailyReports.map(r => `<button class="daily-row ${r.date === dailyCurrent ? 'on' : ''}"
    data-report="${esc(r.date)}"><b>${esc(r.date)}</b>
    <span>${r.docs} 条素材${r.total > r.docs ? ` / 窗口内 ${r.total}` : ''}${r.late ? ` · 补录 ${r.late}` : ''}</span>
    <small>${esc(Object.entries(r.sources || {}).map(([k, n]) => `${k} ${n}`).join(' · ') || '—')}${
      r.sync_failed?.length ? ` · ${r.sync_failed.length} 项未完成` : ''}</small></button>`).join('')
    || '<div class="daily-empty"><h3>还没有日报</h3><p>选好日期，点右上角生成。</p></div>';
}

// 「这次正文读取是不是还算数」。**两道各管一段,缺一不可**:
// ①`dailyCurrent !== date` —— 选中的已经不是这一天了。删除那条路把 dailyCurrent 清成
//   空串却**不经过本函数**(所以序号不动),在途的那次读取只有这一道挡得住,否则刚删掉的
//   正文会被画回阅读区。
// ②序号 —— **同一天又读了一次**,这一次已经被替代。原来只有①:用户点开今天这一份(请求
//   慢),紧接着点「重新生成」,生成完那一跳自动重读同一天 —— 两次的 date 逐字相同,于是
//   先发后到的那份旧正文把新的盖回去,新文章闪一下就消失,而完成身份已经记下、后面多少轮
//   轮询都不会再补刷(审查实测:左边列表写着 5 条,正文只剩 4 条,只有手动再点一次才回来)。
let dailyReadSeq = 0;

// 正文落地走 `setDailyBody`(kb/static/js/daily_body.js):正文和「这是哪一天的」
// 一起落,「收藏此段」从那里读出处。
async function openDailyReport(date) {
  dailyCurrent = date;
  renderDailyList();
  const seq = ++dailyReadSeq;
  const stale = () => dailyCurrent !== date || seq !== dailyReadSeq;
  try {
    const r = await pipeJson('/api/daily/reports/' + encodeURIComponent(date));
    if (stale()) return;
    $('daily-reader-title').textContent = '日报 · ' + r.date;
    $('daily-reader-meta').textContent =
      `覆盖 ${dailyClock(r.window_since.replace(' ', 'T'))} → ${dailyClock(r.window_until.replace(' ', 'T'))} 发布的内容 · ` +
      `${r.docs} 条素材${r.total > r.docs ? `（窗口内共 ${r.total} 条）` : ''}` +
      `${r.late ? ` · 补录 ${r.late} 条` : ''} · ${modelLabel(r.model)} · 生成于 ${r.generated}`;
    // 正文是模型写的 markdown(外部内容),mdHtml 里过 DOMPurify。开头那行
    // `# 日报 · 日期` 是给 Obsidian 看的,面板上标题栏已经写着同一句,渲染出来
    // 就是连着两行一模一样的标题;跟在它后面那句「> 覆盖 … 发布的内容」同理,
    // 元信息那一行已经写了。两处都只在面板上剥掉,vault 里的文件一个字不改。
    // 正文里那段「本轮有 N 项没有完成」跟着 md 一起渲染出来(剥掉的只有
    // 标题和覆盖区间这两行重复信息),所以历史日报翻回来也看得见当时的同步缺口。
    const body = renderDailyReportBody(r);
    $('daily-copy').hidden = $('daily-del').hidden = false;
    $('daily-copy').dataset.body = r.body;
    // 收藏回跳的落点。排在 enhanceDailyBody 之后:那一步会把出处编号换成
    // 链接元素,换掉的是**元素结构**而不是字符 —— 锚点那套按全文偏移找,
    // 顺序反了也对得上,但高亮的 Range 会落在已经离开文档的节点上。
    dailyPendingSnip = consumeDailySnipJump(dailyPendingSnip, date, body);
    // 出处本地有中文译文的那几处,在编号后面补一个入口(要问一次后端,所以是
    // 异步的,不等它)。**排在最后**:它只往正文里添元素、不动已有节点和字符,
    // 上面那两步都已经做完了 —— 反过来把一次网络往返挡在正文和回跳前面,屏幕
    // 上就会空着等。口径在 daily_zh.js。
    // **这道 try 只防一种事:那个函数压根不在**(脚本没加载、加载顺序错)——
    // 那是一个同步的 ReferenceError,会被下面的 catch 接住,把一份**已经读到了**
    // 的日报换成「读取失败」四个字,而那块提示连「收藏此段」都收不了
    // (见 daily_body.js);代价完全不对等。**函数里面出的错到不了这儿**:它是
    // async 的,内部抛出来是一个 rejected promise(不 await 就落到 unhandled
    // rejection 那条路上,日报正文一个字不动)—— 如实记着,别以为这道管着它。
    try { markDailyTranslations(body, date); } catch (err) { console.warn(err); }
  } catch (e) {
    // 失败这一侧走同一道判据:被替代的那次读取报出来的错也是陈旧的,
    // 让它覆盖新正文等于拿一句「读取失败」盖掉一份读到了的日报。
    if (stale()) return;
    setDailyBody(`<div class="daily-empty" role="alert"><h3>读取失败</h3><p>${esc(e.message)}</p></div>`);
  }
}

async function copyDailyReport() {
  try {
    await navigator.clipboard.writeText($('daily-copy').dataset.body || '');
    toast('日报已复制', { type: 'ok' });
  } catch (e) { toast('复制失败', { detail: e.message }); }
}

async function deleteDailyReport() {
  if (!dailyCurrent) return;
  if (!await confirmBox({ title: `删除 ${dailyCurrent} 的日报？`,
    body: '只删这份日报，素材与索引不受影响。删掉后可以重新生成。', ok: '删除', danger: true })) return;
  try {
    await pipeJson('/api/daily/reports/' + encodeURIComponent(dailyCurrent), { method: 'DELETE' });
    dailyCurrent = '';
    $('daily-copy').hidden = $('daily-del').hidden = true;
    $('daily-reader-title').textContent = '还没有选中日报';
    $('daily-reader-meta').textContent = '';
    setDailyBody('<div class="daily-empty"><h3>已删除</h3><p>可以重新生成这一天的日报。</p></div>');
    // 删一份日报会把它带走的条目放回队列,所以按钮上那个计数也得重新问一次
    // ——不问的话页面上还写着「没有新内容」,而此刻明明又有了。
    await Promise.all([loadDailyReports(false), loadDailyWindow()]);
  } catch (e) { toast('删除失败', { detail: e.message }); }
}

// ── 生成 ────────────────────────────────────────────────────────────────────

async function runDaily(sync) {
  if (dailyBusy || dailyState?.running) return;
  // 改过区间就按改的来;没改就什么都不传,让后端按上一份日报接着算——区间的
  // 截止时刻必须是**生成那一刻**,同步跑掉的几小时也该算进来,在这里钉死的话
  // 那几小时会掉进两份日报中间的缝里。
  const custom = dailyWindowDirty
    ? { since: $('daily-since').value, until: $('daily-until').value } : {};
  if (custom.since && custom.until && custom.since >= custom.until) {
    toast('起始时刻要早于截止时刻'); return;
  }
  // 同步范围一个都没勾:同步阶段会什么都不做、却一路报成功,"同步全部并生成"
  // 静静地变成"直接生成"。后端也会拒(daily_run.start),这里先拦一道,好把
  // 话说在按钮边上。
  const channels = pickedDailyChannels();
  if (sync && !channels.length) {
    toast('先勾选至少一个同步范围', { key: 'daily',
      detail: '同步范围在页头「改定时」那一扇里（已经替你打开）；只想用已入库的内容出报，点左边那颗生成按钮（没有新内容时它不显示）。' });
    // 话说在按钮边上还不够:要改的那几个勾选框此刻收在抽屉里,看不见就等于没说。
    setDailyDrawer('sched', true);
    return;
  }
  // 覆盖提示按「这一份会写到哪一天」判,和按钮叫什么名字共用同一份口径
  // (`dailyTargetDate` + `dailyTargetReport`)。**不知道的那一档也问** ——
  // 多问一句的代价是一次点击,不问的代价是静悄悄盖掉一份日报。
  const date = dailyTargetDate();
  if (dailyTargetReport() !== false &&
      !await confirmBox({ title: `重新生成 ${date} 的日报？`,
        body: '这一天已经有一份日报，重新生成会覆盖它。', ok: '重新生成' })) return;
  dailyBusy = true; renderDailyButtons();
  try {
    await pipePost('/api/daily/run', { ...custom, sync, channels });
    toast(sync ? '已开始同步并生成日报' : '已开始生成日报',
      { key: 'daily', type: 'ok', detail: sync ? '依次同步所选来源后生成，离开页面不影响。' : '直接用已入库的内容生成。' });
  } catch (e) {
    toast('未能开始', { key: 'daily', detail: e.message });
  } finally { dailyBusy = false; refreshDailyStatus(); }
}

async function stopDaily() {
  try {
    // 说明文案跟着后端给的那一份走:受理与不受理(尤其"生成阶段停不了")
    // 的口径只有一份,两边各写一句迟早对不上。
    const r = await pipePost('/api/daily/stop');
    toast('已请求停止', { key: 'daily', type: 'info', detail: r.detail || '' });
  } catch (e) { toast('停止请求未完成', { key: 'daily', detail: e.message }); }
  finally { refreshDailyStatus(); }
}

function renderDailyButtons() {
  const running = !!dailyState?.running;
  $('daily-gen').disabled = $('daily-sync-gen').disabled = dailyBusy || running;
  $('daily-sync-gen').textContent = running ? '生成中…' : '同步全部并生成';
  renderDailyGenButton();
  // 进了生成阶段就停不了了:模型已经在出稿,daily.generate() 自己把文件写完
  // 才返回。按钮留着能点、点了又被拒,不如置灰并把原因写在上面。
  const generating = running && dailyState?.phase === 'generate';
  $('daily-stop').disabled = generating || !!dailyState?.stop_requested;
  $('daily-stop').title = generating ? '正在出稿，这一阶段不能中途停止' : '';
  $('nav-daily-dot').hidden = !running;
}

// 同步缺口横幅:这一轮有哪几项没跑成功。toast 会自己消失,而"这份日报只用了
// 已入库的素材"是看这份日报时一直要知道的事——一个来源都没同步到的那一轮,页面
// 原来只有一句绿色的「日报已生成」(2026-09-30 审查实测)。
// **文案说「项」不说「来源」**:抓取那一段按来源一项,入库那一段(索引与提取)
// 自己占一项,它不属于任何来源(`daily_run.DIGEST_LABEL`)。
function dailyChannelResults(s) {
  // 重启 kb-web 之后进程内状态是空的,上一轮的通道结果留在落盘的 last_run 里。
  return (s?.channels?.length ? s.channels : s?.last_run?.channels) || [];
}

function renderDailySyncNote(s) {
  const bad = dailyChannelResults(s).filter(c => c.status !== 'ok');
  $('daily-sync-note').hidden = !bad.length;
  if (!bad.length) return;
  $('daily-sync-note-text').textContent =
    `本轮有 ${bad.length} 项没有完成：`
    + bad.map(c => `${c.label}（${c.detail || '未完成'}）`).join('；')
    // 只有真出了报才提这一句;停止/失败那两档日报压根没生成。
    + (s?.phase === 'done' ? '。日报用的是已经入库的内容。' : '。');
}

function dailyPhaseText(s) {
  if (s.phase === 'failed') return '上次生成失败：' + (s.error || '未知原因');
  if (s.phase === 'stopped') return s.detail || '已停止，这一轮没有生成日报';
  if (s.phase === 'done') return s.detail || '日报已生成';
  return s.detail || '正在准备';
}

async function refreshDailyStatus() {
  clearTimeout(dailyTimer); dailyTimer = null;
  let s;
  try {
    s = await pipeJson('/api/daily/status');
  } catch {
    // 状态读不到不清空面板,下一跳再试。**但「有没有新内容」是另一条事实,
    // 不许跟着这一趟一起没了**:它从 /window 来,素材也是从外面(同步中心、
    // CLI、定时)进来的,跟读不读得到"这一轮跑到哪了"没有关系。早退掉的后果
    // 是上一次那个 0 无限期有效 —— /status 一直失败时,页面始终写「没有新内容」、
    // 按钮始终不在,而库里早就进了新文章(审查实测:只让 /status 失败,连驱动
    // 两轮,页面一个字都没变)。那一趟自己再失败的话,它会把这个数改口成
    // 「读不出来」(`fetchDailyWindow` 的 catch),仍然不会留下一个假的 0。
    if (dailyActive()) await refreshDailyPending();
    queueDailyPoll(); return;
  }
  const finished = dailyState?.running && !s.running;
  // 「有没有出现一轮新的已结束运行」。只认 running→idle 那个跳变是不够的:不同步
  // 直接生成那条路(桩模型、素材不多时)在两次轮询之间就跑完了,页面一次都没见过
  // running=true —— 于是按钮上那个数字归零了,而列表和阅读区还停在上一版(审查
  // 实测:接口已经是「素材清单(4 条)」,页面仍显示 3 条和旧的生成时刻,只有整页
  // 刷新才看得到新内容)。后台定时那一轮、别的页面点的那一轮同理。
  // 第一跳(dailyDoneRun === null)不算新结果:那时页面刚打开,正文本来就是现读的。
  const doneRun = s.running ? ''
    : [s.run_id, s.finished_at].filter(Boolean).join('|');
  const fresh = dailyDoneRun !== null && doneRun && doneRun !== dailyDoneRun;
  dailyDoneRun = doneRun;
  dailyState = s;
  $('daily-progress').hidden = !s.running;
  $('daily-progress-text').textContent = dailyPhaseText(s);
  // 失败原因留在横幅里,直到**下一轮有了新结果**才清——列表读取失败的提示也用
  // 这条横幅,顺手清掉的话用户只看到一片空白列表而没有任何原因(所以清的只是
  // 'run' 那一条)。**认「跑起来了」是不够的**:成功的那一轮同样可能整个落在两
  // 次轮询之间(桩模型、素材不多、或者定时/CLI 跑的那一轮),页面一次都没见过
  // running —— 于是正文和按钮都更新成了新的,顶上那条横幅还写着上一次失败的
  // 原因,而面板上再没有任何操作能把它清掉。所以 `fresh` 那一跳也清。
  if (!s.running && s.phase === 'failed') dailyError(s.error || '日报生成失败', 'run');
  else if (s.running || fresh) clearDailyError('run');
  renderDailyButtons();
  renderDailySyncNote(s);
  if (finished) {
    const bad = dailyChannelResults(s).filter(c => c.status !== 'ok');
    if (s.phase === 'failed') toast('日报未生成', { key: 'daily', detail: s.error || '' });
    else if (s.phase === 'stopped') toast('已停止', { key: 'daily', type: 'info', detail: s.detail || '' });
    // 有来源没同步到就不报成功色:日报是出了,但它不是"同步全部并生成"承诺的
    // 那一份,标成绿勾等于把降级说成完整成功。
    // 「项」不是「来源」:入库那一段(索引/提取)是全库的一次,不属于任何来源 ——
    // 磁盘满写成「1 个来源未同步成功」是一句假话。正文横幅、日报文件、这句 toast
    // 共用一个措辞(`kb/daily.py` 的 sync_issues 那一段)。
    else if (bad.length) toast(`日报已生成，但 ${bad.length} 项未完成`,
      { key: 'daily', type: 'info', detail: s.detail || '' });
    else toast('日报已生成', { key: 'daily', type: 'ok', detail: s.detail || '' });
    if (dailyActive()) {
      dailyCurrent = s.result?.date || dailyCurrent;
      // 上一份日报的截止时刻变了,默认区间跟着往前挪。
      await Promise.all([loadDailyReports(), loadDailyWindow()]);
    }
  } else if (fresh && dailyActive()) {
    // 没见过 running 的那一轮:把列表、正文和默认区间补上(loadDailyReports 自己
    // 会重开当前选中那一份,所以阅读区跟着一起更新),**不弹 toast** —— 这条路也
    // 会在别处(定时、CLI、另一个标签页)跑完一轮时走到,弹窗就成了噪音。
    dailyCurrent = s.result?.date || dailyCurrent;
    await Promise.all([loadDailyReports(), loadDailyWindow()]);
  }
  // 素材是**从外面**进来的:同步中心那边跑完一轮、CLI 补抓、定时同步,页面这边
  // 一个事件都收不到。只在"日报从运行变成完成"那一跳重读的话,按钮上的数字会
  // 一直停在打开页面那一刻(审查实测:后台插进一篇,页面仍写着「没有新内容」,
  // 要点一次「改区间」再「接上一份日报」才更新)。所以空闲时每一跳都问一次。
  if (!s.running && dailyActive()) await refreshDailyPending();
  queueDailyPoll();
}

async function refreshDailyPending() {
  // 空闲时每一跳都问一次,**不碰两个时刻输入框**。改过覆盖区间时照样问:抓取
  // 是从外面(同步中心、CLI、定时)进来的事实,它跟用户在这里填了什么没关系。
  //
  // 落地的那一趟把那行区间字一起画上:它可能刚替初始化接手了填表职责(审查
  // N4),而画这行字只**读**输入框、不写它,所以空闲轮询照画不越界。
  // 失败那一趟把「还有多少新内容」抹成了"不知道",**那也得画上去** —— 不画的话
  // 屏幕上留着的还是上一次那个 0,按钮还藏着(同 `loadDailyWindow` 的 else)。
  if (await fetchDailyWindow(false)) renderDailyWindowLabel();
  else renderDailyGenButton();
}

function queueDailyPoll() {
  clearTimeout(dailyTimer);
  // 不在日报页时也慢速轮询:定时日报是后台起的,侧栏那颗点得亮起来,
  // 否则用户在别的页面上完全看不出正在生成。
  dailyTimer = setTimeout(refreshDailyStatus,
    dailyState?.running ? 3000 : dailyActive() ? 15000 : document.hidden ? 120000 : 60000);
}

$('daily-list').addEventListener('click', e => {
  const row = e.target.closest('[data-report]');
  if (row) openDailyReport(row.dataset.report);
});
