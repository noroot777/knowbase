// X 的采集通道:桥接 / feedgrab(cookie) / 我的浏览器登录态。
//
// 通道清单、人话名字、就绪判断全部由后端给(`/api/intake/<kind>/x-channel`)。
// **前端不许把通道 id 和它们各自要填什么写死成一张表**:这里只按后端给的
// id 决定显示哪一块,新增/退役一条通道是后端那一份注册表的事。
// (退役名的翻译在后端做 —— 这份选择存在 feed_settings 里,`get_settings`
//  读出来就已经 resolve 过了,浏览器手里拿不到退役名。)
let xChan = null, xChanKind = '', xChanBusy = false;

// 面板状态的**代次**。慢请求(导入最长 240 秒、检查 120 秒)回来之前,用户完全
// 可能已经改了下拉、保存了另一档、或者切到别的来源去了 —— 那时这份回包说的是
// **过去那个面板**的事,写上去就是一句谎话。实测形态:点了导入,趁它没回把取法
// 改成 feedgrab,6 秒后那句「改完记得点保存设置」被迟到的成功响应清成了空串
// (第 2 轮审查 `import-race.png`)—— 于是页面看着像已经生效,而存下来的还是旧档。
// **判据是代次而不是「来源类型对不对」**:同一个来源里改下拉、保存设置都要让
// 旧回包作废,而类型一直是 social,比类型拦不住。凡是改变「这块面板此刻在说谁」
// 的动作都要 `xChanAge()`;凡是异步回包落地之前都要先过一次归属检查。
//
// **「这块面板在说谁」和「谁说的更新」是两件事,各记一样东西。**
// 一个计数器办不了两件事:让落地的回包顺手推进同一个计数器,等于把「事实所有权」
// 判成**抵达顺序**,而读和动作的抵达顺序跟它们各自说的是哪一刻毫无关系 ——
// 两个方向都实测翻过车:
//  ①动作先到:导入把成功记录写上去并推进代次,那条比它还早发出的读照旧通得过
//    代次检查,面板被盖回「还没有登录过 X」(第 4 轮在真页面上实测);
//  ②读先到:同一条读先抵达、自己推进了代次,随后回来的导入/检查反被判成过期,
//    于是成功记录、成功提示、失败原因一个都不显示(第 5 轮实测
//    `import-login-read-first-ok.png`)。
//  ③动作在途时顺手保存一次:保存会重填字段、再读一遍(`source_forms.js` 收尾那
//    两行),两件事都推进代次;那条读**先回来**(实测 +49 毫秒,而导入 +1203 毫秒)
//    并把「导入之前那一刻」写上去,随后回来的导入结果反被判成过期 —— 页面继续说
//    「还没有登录过 X」,成功记录和成功提示都不出现(`ui-save-during-import.png`)。
//    **发起顺序也救不了这一档**:那条读是在动作开始**之后**才发出的,可它取到的
//    后端快照仍然在动作提交之前。
// 所以记三样 —— 草稿身份、在途动作、后端已完成版本:
//  - `xChanGen` **草稿身份** —— 「这块面板此刻在说哪一档」。改下拉、保存设置、
//    重新去读一遍都推进它;**回包落地一律不碰它**。读与读之间也靠它排(每发一条
//    读就是新一代,先发晚回的那条自然过期)。
//  - `xChanActs` **在途动作** —— 动作**一按下去**就加一,比它更早发出的那些读当场
//    作废(它们说的是动作之前那一刻)。判据落在**发起**而不是落地:一次超时的导入
//    可能已经把 profile 改完了,「没回来」证明不了「没发生」。
//  - `xChanLands` **后端已完成版本** —— 动作结果**落地**一次加一,于是比它更早发出
//    的读(哪怕是在动作开始之后才发的)不许再盖上来:那条读取到的后端快照不保证
//    在动作提交之后。
// **动作的归属不按代次判,按它说的是哪一档判**(`xChanAct` 记下 kind + 当前选择 +
// 存下来的那一档)。按代次判的话,一次「同一档重新保存一遍」就能把在途的动作作废
// 掉 —— 而那次保存压根没改变这块面板在说谁,只是重画了一遍。
// 动作与动作之间不用排 —— `xChanBusy` 本来就只让一颗按钮在飞。
let xChanGen = 0, xChanActs = 0, xChanLands = 0;
//: 在途那个动作的草稿身份(没有动作在飞就是 null)
let xChanPending = null;
//: 上一个动作**请求没跑完**时留下的一条交代:`{kind, channel, slot, text}`。
// **「请求断了」不等于「什么都没发生」**:后端那条链是「改 profile 之前先登记、
// 写完之后才落账」(`x_login_import` 的 `_installing`),连接断在半路时 profile
// 可能已经被改完了。于是从这一刻起,面板上那条旧结论**不再是当前事实,只是上一次的
// 历史** —— 而它原来只被清掉了 DOM 那一下:用户把取法切走再切回来,重画就按缓存里
// 那份旧的 `login_state`/`ready` 把「当场验证通过」恢复成当前结论,这一次没跑完这件
// 事一个字都不剩(审查实测:导入 POST 断在传输层,切走再切回,页面说「当场验证
// 通过」)。所以记成**动作状态**而不是一次性的 DOM 文案,重画时顶在前面。
// 清掉它的只有「又有一个动作真的拿到了回包」(那时面板有了更新的权威结论)。
let xChanUnsure = null;
function xChanAge() { return ++xChanGen; }   // 返回「从此刻起算哪一代」
// 发一条读:它自己就是新一代面板,同时记下此刻的在途动作数和已完成版本号。
function xChanRead() {
  return { gen: xChanAge(), acts: xChanActs, lands: xChanLands };
}
// 开一个动作:**这一下就让在途的那些读作废**,但不改草稿身份。
// `xChanActs` 单独拆掉探针**一条红都没有** —— 它是「旧读先抵达」那一档上**三道各自
// 都够用**的第一道(另两道:读落地不推进代次、动作归属不按代次判),三道一起拆才红。
// 别因此当它是冗余而删:它拦的是最彻底的那一步 —— 那条读**说的是动作之前那一刻**,
// 有它在就根本不落地,没它则落了地只是碰巧盖不住要紧的那几行。
// 实测见 `docs/decisions/frontend-feedback.md`。
function xChanAct() {
  xChanActs += 1;
  return (xChanPending = { kind: xChanKind, channel: xChannelValue(),
                           saved: xChan && xChan.current });
}
// 这个动作说的还是此刻这块面板吗:来源、下拉上选的那一档、**存下来**的那一档
// 三样都没变才算。少了最后一样,「改成别档→保存→再改回来」会让一条旧结论盖掉
// 「改完记得点保存设置」(那正是那条事故的另一条路径)。
// 那条「结果未知」还说得上此刻这块面板吗:同一个来源、同一档取法。
// (切走再切回来之所以要让它重新出现,正是因为它说的是**那一档**的事。)
function xChanUnsureNow() {
  return xChanUnsure && xChanUnsure.kind === xChanKind
         && xChanUnsure.channel === xChannelValue() ? xChanUnsure : null;
}
// 这一次拿到回包了:**同一档、同一处**的那条「不知道」可以撤了。
// 只撤同一处:一次成功的「检查连接」说明这条通道现在能用,但它答不了「刚才那次
// 导入到底搬没搬成」—— 跨处撤等于拿一个结论冒充另一个结论。
function xChanSettled(t, slot) {
  if (xChanUnsure && xChanUnsure.kind === t.kind
      && xChanUnsure.channel === t.channel && xChanUnsure.slot === slot)
    xChanUnsure = null;
}
function xChanMine(t) {
  return !!t && t.kind === xChanKind && t.channel === xChannelValue()
         && t.saved === (xChan && xChan.current);
}
// 回包落地前都走这两个出口,**成功和失败两条路都要过**(写成函数而不是在六处各
// 写两行,是因为这一族问题被拦下来过五轮,每轮都是「又漏了一个落地点」)。
function xChanOwnsRead(t) {
  return t.gen === xChanGen && t.acts === xChanActs && t.lands === xChanLands;
}
function xChanOwnsAct(t) {
  if (!xChanMine(t)) return false;
  xChanLands += 1;              // 从此刻起,更早发出的读说的都是旧事实
  return true;
}

function xChannelValue() {
  return $('source-x-channel').value;
}

// 下拉被人改了(`index.html` 的 onchange)。**代次在这儿推进,不在
// `xChannelPicked` 里** —— 后者同时是**渲染**入口(读回来之后、保存之后都要走一趟),
// 在它里面推进等于「每次重画都算换了面板」,而那正是「读先到就废掉随后回来的动作」
// 那条的机制。**这一条是冗余带,别删**:注入「把 `xChanAge()` 搬回 `xChannelPicked`」
// 之后探针 0 条红(有了 `xChanActs` 那道闸之后,能被这次多余推进作废的动作要么已经
// 被它作废过、要么比那条读更晚发起)。留着是因为「重画等于新事实」这个说法本身是错
// 的 —— 下一次有人加第三类落地点时,它决定那个落地点会不会又回到单计数器的形状。
function xChannelChanged() {
  xChanAge();                 // 改了选择:在途的那些回包从此不属于这块面板
  xChannelPicked();
}

// 按当前选择显示该填什么。各通道要的东西不一样,全塞在一起只会让人以为
// 选了浏览器还得去填桥接地址。**纯渲染,不动代次**(理由见上一条)。
function xChannelPicked() {
  if (!xChan) return;
  const now = xChannelValue();
  const show = (id, on) => { $(id).hidden = !on; };
  show('source-bridge-field', now === 'bridge');
  show('source-x-feedgrab', now === 'feedgrab');
  show('source-x-browser', now === 'browser');
  const meta = (xChan.channels || []).find(c => c.id === now);
  $('source-x-hint').textContent = meta?.hint || '';
  // 这几行只在"选择没变"时才是当前通道的事实;改了选择就是一句过期的话。
  const same = now === xChan.current;
  $('source-x-login').textContent = xChan.login_command || '';
  // **有一个属于这块面板的动作正在飞时,那两行结论归它写,重画不许顺手盖掉。**
  // 它们是这个动作此刻唯一的交代(「正在从日常浏览器读登录态…」「正在检查…」)——
  // 实测:导入途中点一次保存,保存收尾那次重读把预告清成了导入之前的旧留痕,而
  // 那个授权弹窗正等在另一个窗口里。改了下拉/改了存下来那一档就不是它的了
  // (`xChanMine` 当场为假),于是「改完记得点保存设置」照常显示。
  const busy = xChanMine(xChanPending);
  // **上一次没跑完的那条交代也归这一类**:它说的是「这一档此刻的结果不知道」,
  // 而缓存里那份 `ready/why`/`login_state` 说的是它之前那一刻 —— 让后者盖上来就是
  // 把一条历史冒充成当前结论(理由与实测见 `xChanUnsure`)。
  const unsure = xChanUnsureNow();
  if (!busy) xChannelSay(unsure && unsure.slot === 'state' ? unsure.text : (same
    ? (xChan.ready ? '当前通道已就绪。' : (xChan.why || '当前通道还没有配好。'))
    : '改完记得点「保存设置」,保存之后才会生效。'));
  $('source-x-check').hidden = !same;
  // 「打开登录窗口」同理不能在改了选择还没保存时点:后端读的是**存下来**的那一份
  // 通道,于是它只会回一句"当前保存的取法不是浏览器登录态" —— 用户对着一个明明
  // 选着「我的浏览器登录态」的下拉收到这句拒绝,看不出差在哪一步(2026-10-09 实测)。
  // **这里禁用而不是藏掉**(「检查连接」是藏的):那颗按钮产出的是一个**结论**,
  // 贴在新选择旁边就是谎话,没有结论可给就不该出现;这颗是一个**动作**,而且上面
  // 那段说明("在一个独立 profile 里登录一次")指的就是它 —— 藏掉它说明就悬空了,
  // 灰着反倒说得清"现在点不了"。
  const loginBtn = $('source-x-login-btn');
  loginBtn.disabled = !same;
  loginBtn.title = same ? '' : '先点「保存设置」,保存之后才能在这里登录';
  // 「从日常浏览器导入登录态」同理:它写的也是**存下来**那一档的登录态,所以
  // 跟上面那颗同一道闸、同一个理由(禁用而不是藏掉 —— 上面那段说明指的就是它)。
  const importBtn = $('source-x-import-btn');
  importBtn.disabled = !same;
  importBtn.title = same ? '' : '先点「保存设置」,保存之后才能导入';
  // 「上次什么时候导的」只在选择没变时才是当前通道的事实(口径同状态行)。
  // **文案整句由后端给**(`x_login_import.summary`):结论码是那一块自己的词汇表,
  // 前端照它写一张表就是把同一个判断放到两处。
  // 旧留痕**不删,但退到后面**:它自己就带着「上次……」和那个时刻,摆在未知那句
  // 后面读起来正是「这一次不知道,上一次是那样」,不会冒充这一次的结论。
  const trace = same ? (xChan.login_state || '') : '';
  if (!busy) $('source-x-import-state').textContent =
    unsure && unsure.slot === 'import' ? `${unsure.text}${trace ? ' ' + trace : ''}` : trace;
}

// 状态行 + 「下一步怎么办」一起写,**去重按内容判**。
//
// 「下一步」由后端给(`readiness` 的第三项),前端只负责显示:有些通道要做的事
// 塞不进一个输入框(比如去浏览器地址栏开一次远程调试开关),它就从这里出来 ——
// 这样**新增一条通道不用改这个文件**,口径同顶上那句"不许把通道和它们各自要填
// 什么写死成一张表"。它和状态行一样,只在选择没变时才是事实。
//
// 两个显示位的内容却可能是同一句话,所以同一条去重规则要管两头:
//  ①feedgrab 那档的「下一步」就是上面那条可复制的命令本身(它的第三项必须带着
//    它 —— 待配置横幅那边没有可复制块,见 `x_channels.readiness`);
//  ②「检查连接」返回的 detail 已经把原因和地址写法指引拼在一起了
//    (`x_browser.probe`),而「下一步」装的正是同一条指引 —— 原来只在
//    `xChannelPicked` 里对①去过重、检查那一下直接改状态行,于是合法写法和
//    「清掉它就回到自动发现」连着显示两遍(2026-10-09 第 2 轮审查截图)。
// 按内容判而不是按通道写死一张表,所以两档共用这一份。
function xChannelSay(state) {
  $('source-x-state').textContent = state;
  // 只有选择没变时「下一步」才是事实(口径同状态行)。
  const next = xChannelSaved() ? (xChan.hint || '') : '';
  const dup = !next || next === $('source-x-login').textContent || state.includes(next);
  $('source-x-next').textContent = dup ? '' : next;
}

// 「页面上选的这条通道,后端已经存下来了吗」。禁用登录按钮和恢复它都认这一份。
function xChannelSaved() {
  return !!xChan && xChannelValue() === xChan.current;
}

async function loadXChannel(kind) {
  const panel = $('source-xchan');
  panel.hidden = sourceModules.get(kind)?.adapter !== 'social';
  if (panel.hidden) return;
  const want = kind;
  // **代次在发起时取,不是在应用时推。** 这一条读回来之后要重建下拉、重设
  // `xChan`、改按钮能不能点 —— 跟那两颗按钮的回包是同一类写入,凭什么只有它
  // 不用对代次。在应用时才 `xChanAge()` 等于「谁最后回来谁算」:保存设置会
  // 紧跟着再读一次(`loadSourceModule`),两条同来源的 GET 在途时先发的那条晚
  // 回来,就把后发那条刚写好的事实连同用户刚改的下拉一起盖回旧值,而面板上
  // 一切正常。`want !== sourceKind` 只拦得住「切到别的来源」,拦不住这个。
  // 回归钉的是同一道闸的另一个触发点(**读还在途中,用户改了下拉** —— 那条读回来
  // 会把他刚选的那一档静默换回去):同 URL 的两条 GET 被浏览器去重排队了,用慢
  // 服务端摆不出乱序,而这一个摆得出来。见 `docs/decisions/frontend-feedback.md`。
  const tag = xChanRead();
  let data;
  try {
    data = await pipeJson(`/api/intake/${kind}/x-channel`);
  } catch (e) {
    // 读不出来时别把这一块留成空壳:桥接那个输入框还在(它是存量唯一的
    // 取法),顺手给一条重读入口,而不是让人对着一个没有选项的下拉。
    if (want !== sourceKind || !xChanOwnsRead(tag)) return;
    xChan = null;
    $('source-x-channel').innerHTML = '';
    $('source-x-hint').textContent = '';
    $('source-bridge-field').hidden = false;
    $('source-x-feedgrab').hidden = $('source-x-browser').hidden = true;
    $('source-x-check').hidden = true;
    $('source-x-next').textContent = '';
    $('source-x-state').textContent = `读不出可选的取法（${e.message}）。`;
    return;
  }
  // 这期间切走了、又读了一次、改了下拉、或者有个动作已经开跑:别覆盖别人的面板,
  // 也别拿「那之前」的一份盖掉更新的事实
  if (want !== sourceKind || !xChanOwnsRead(tag)) return;
  xChan = data; xChanKind = kind;
  const sel = $('source-x-channel');
  sel.innerHTML = '';
  for (const c of data.channels || []) {
    const o = document.createElement('option');
    o.value = c.id; o.textContent = c.label; sel.appendChild(o);
  }
  sel.value = data.current;
  xChannelPicked();
}

// 保存设置时附带的那几个字段。社交来源之外返回空对象。
function xChannelPayload(kind) {
  if (!xChan || xChanKind !== kind) return {};
  return { x_channel: xChannelValue(),
           x_feedgrab_bin: $('source-x-bin').value.trim(),
           x_chrome_path: $('source-x-chrome').value.trim() };
}

function fillXChannelFields(settings) {
  if (!('x_channel' in (settings || {}))) return;
  $('source-x-bin').value = settings.x_feedgrab_bin || '';
  $('source-x-chrome').value = settings.x_chrome_path || '';
  if (xChan) { xChan.current = settings.x_channel; $('source-x-channel').value = settings.x_channel; }
  // 保存之后/换了来源之后,这块面板说的是另一档了 —— 跟改下拉同一道闸。
  xChanAge();
  xChannelPicked();
}

async function openXLoginWindow() {
  // 未保存时这颗按钮本来就是禁用的;这一句是给别的入口(键盘、脚本)兜底 ——
  // 发上去只会换回一句"当前保存的取法不是……",白跑一趟还留个看不懂的提示。
  //
  // **这一条刻意不对代次**(同一族里只有它不对):它一个字都不往面板状态上写,
  // 那条 toast 说的是「用户自己刚点的那个动作真的发生了」。守上它的后果是用户顺手
  // 改了下拉之后这句话消失,而那个登录窗口确实开着 —— 要守的是往面板上写,不是
  // 发过异步请求(口径见 `docs/decisions/frontend-feedback.md`)。
  if (xChanBusy || !xChannelSaved()) return;
  xChanBusy = true; $('source-x-login-btn').disabled = true;
  try {
    const r = await pipePost(`/api/intake/${xChanKind}/x-channel/login`);
    toast(r.detail, { type: 'info', ms: 12000 });
  } catch (e) { toast('打不开登录窗口', { detail: e.message }); }
  // 恢复成"此刻该不该能点",不是无条件放开:这期间下拉可能已经被改走了。
  finally { xChanBusy = false; $('source-x-login-btn').disabled = !xChannelSaved(); }
}

async function importXLoginState() {
  // 未保存时这颗按钮本来就是禁用的;这一句是给别的入口(键盘、脚本)兜底,
  // 理由同 `openXLoginWindow` —— 后端读的是存下来的那一档。
  if (xChanBusy || !xChannelSaved()) return;
  // **动作一开始就让在途的那些读作废**(它们说的是导入之前那一刻)。
  const tag = xChanAct();
  let mine = false;           // 这个结果最后有没有写进这块面板
  xChanBusy = true; $('source-x-import-btn').disabled = true;
  // **先把「那个浏览器会弹一次授权」说在前面。** 这一步会停在授权弹窗上最长 90 秒
  // (`cdp_attach.ATTACH_TIMEOUT`),而弹窗出现在另一个窗口里 —— 不预告的话用户
  // 看到的是一颗按下去就没反应的按钮,等他发现那个弹窗时这边可能已经超时了。
  $('source-x-import-state').textContent =
    '正在从日常浏览器读登录态…那个浏览器会弹一次授权，点「允许」。';
  try {
    // 它要连一次日常浏览器(等用户点授权,最长 90 秒),再起一个无头 Chrome 把
    // cookie 写进去、当场打开 x.com 验一次 —— 比「检查连接」还慢一截,所以超时
    // 给得比它宽。
    const r = await pipePost(`/api/intake/${xChanKind}/x-channel/import-login`, {}, 240000);
    // **回执先给,不进这块面板的归属判断**(口径同 `openXLoginWindow` 那条 toast):
    // 它说的是「用户自己刚点的那颗按钮有结果了」,面板此刻在说谁都不改变这件事 ——
    // 而把它也守上的后果是用户顺手保存一次就再也不知道那次导入成没成。
    toast(r.detail, { type: r.ok ? 'ok' : 'err', ms: 12000 });
    // **过期的回包一个字都不许往页面上写。** 这期间下拉可能已经改走了,那时状态行
    // 上挂着的是「改完记得点保存设置」—— 用成功文案把它盖掉就是报喜不报忧。
    // 比它更早发出的那些读说的都是导入之前那一刻 —— 那一道在**点下去的那一刻**
    // 就落了(`xChanAct`),不靠这一句,所以这里只问「还是这块面板吗」。
    if (!(mine = xChanOwnsAct(tag))) return;
    xChanSettled(tag, 'import');   // 这一处有了确定的答复
    if (xChan) xChan.login_state = r.login_state || '';
    // **就绪那一行也跟着这一次更新。** 后端把重算过的 `ready/why` 一起回来了
    // (理由在 `intake_api.x_channel_import_login`):不接的话页面会同时挂着
    // 「当场验证通过」和「还没有登录过 X」,而后者说的是导入之前那一刻。
    if (xChan && r.why !== undefined) { xChan.ready = r.ready; xChan.why = r.why; xChannelSay(r.why); }
    // 成了就显示那行留痕(它带着时刻和条数);没成就显示这一次的原因 ——
    // 留痕那行说的是「什么时候搬的」,答不了「这一次为什么没验成」。
    $('source-x-import-state').textContent = r.ok ? (r.login_state || r.detail) : r.detail;
  } catch (e) {
    toast('导入没跑完', { detail: e.message });   // 回执同样先给
    // 失败那条路同一道闸:**超时不等于没发生** —— 后端可能已经把 cookie 搬完了,
    // 这时候放一条「导入之前」的读进来,页面会说回「还没有登录过 X」(那一道同样
    // 落在点下去的那一刻)。
    if (!(mine = xChanOwnsAct(tag))) return;
    // **记成动作状态,不是一次性文案**:清掉 DOM 那一下管不住后面任何一次重画
    // (切走再切回就把旧结论恢复了),理由与实测见 `xChanUnsure`。
    xChanUnsure = { kind: tag.kind, channel: tag.channel, slot: 'import',
                    text: `这一次导入没跑完（${e.message}），搬没搬成不知道。` };
    $('source-x-import-state').textContent = xChanUnsure.text;
    // **再去读一遍权威记录**:后端可能已经写完了,它那条留痕比页面上的缓存新。
    // 读不回来也没关系 —— 上面那句「不知道」留在那儿,比一条旧结论诚实。
    loadXChannel(xChanKind);
  }
  // 恢复成"此刻该不该能点",不是无条件放开(这期间下拉可能已经被改走了)。
  // `xChanPending` 一并清掉:没有动作在飞了,重画照常写那两行结论。
  finally {
    xChanBusy = false; xChanPending = null;
    $('source-x-import-btn').disabled = !xChannelSaved();
    // 结果没写进这块面板时才重画:**写进去了就别动它** —— 一次没验成的导入写的是
    // 「这一次为什么没成」,而重画写的是留痕那行「什么时候搬的」,后者答不了前者。
    // 没写进去的那一档必须重画,否则上面按住的「正在…」会永远挂在那儿。
    if (!mine) xChannelPicked();
  }
}

async function checkXChannel() {
  if (xChanBusy) return;
  const tag = xChanAct();     // 同一道,理由见 `xChanGen`(这条也是审查 grep 出来的)
  let mine = false;
  xChanBusy = true; $('source-x-check').disabled = true;
  $('source-x-state').textContent = '正在检查…';
  try {
    // 这一步会真起一个 Chrome 或真跑一次 feedgrab doctor,比普通请求慢得多。
    const r = await pipePost(`/api/intake/${xChanKind}/x-channel/check`, {}, 120000);
    toast(r.detail, { type: r.ready ? 'ok' : 'err' });   // 回执先给,理由同导入那条
    if (!(mine = xChanOwnsAct(tag))) return;   // 刚验出来的结论比在途的那些读新
    xChanSettled(tag, 'state');                // 理由同导入那条
    if (xChan) { xChan.ready = r.ready; xChan.why = r.detail; }
    xChannelSay(r.detail);
  } catch (e) {
    toast('检查没跑完', { detail: e.message });
    if (!(mine = xChanOwnsAct(tag))) return;
    // 同一道闸、同一个理由(见 `xChanUnsure`):这一下断在传输层,后端那次检查
    // 跑没跑完不知道,所以缓存里那条旧结论从此只是历史,不许靠重画复活。
    xChanUnsure = { kind: tag.kind, channel: tag.channel, slot: 'state',
                    text: `这一次检查没跑完（${e.message}），当前状态不知道。` };
    xChannelSay(xChanUnsure.text);
    loadXChannel(xChanKind);
  } finally {
    xChanBusy = false; xChanPending = null;
    $('source-x-check').disabled = false;
    if (!mine) xChannelPicked();    // 理由同导入那条
  }
}
