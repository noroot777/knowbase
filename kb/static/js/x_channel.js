// X 的采集通道:独立 profile 的登录态 / 日常浏览器 / feedgrab(cookie)。
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
  // `key` = 这一次动作会跑的那**一整份**配置(此刻后端存着的那份;发起之前
  // `xChanEnsureSaved` 已经确认它就是页面上这一份)。异常那条路上没有回包可问,
  // 靠它回答「这个结果说的是哪一份」。
  return (xChanPending = { kind: xChanKind, channel: xChannelValue(),
                           saved: xChan && xChan.current, key: xChanSaved });
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
// **这一族有两个问题,别合成一个判据。** 这一个问的是「那两行『正在…』还归这个
// 动作说吗」:来源、下拉上选的那一档、存下来的那一档三样没变就还是它 —— 用户在
// 导入途中顺手改一下 Chrome 可执行文件位置,那个授权弹窗照样等在另一个窗口里,
// 预告不该因此消失。**结论**能不能落地是另一个问题,判据严得多,见 `xChanOwnsAct`。
function xChanMine(t) {
  return !!t && t.kind === xChanKind && t.channel === xChannelValue()
         && t.saved === (xChan && xChan.current);
}
// 回包落地前都走这两个出口,**成功和失败两条路都要过**(写成函数而不是在六处各
// 写两行,是因为这一族问题被拦下来过五轮,每轮都是「又漏了一个落地点」)。
function xChanOwnsRead(t) {
  return t.gen === xChanGen && t.acts === xChanActs && t.lands === xChanLands;
}
// 这个结果能不能更新面板上的当前状态。**判据是「它跑的是哪一份配置」,不是
// 「还是同一档取法」。** 同一档里换一条可执行文件位置并保存,旧那次检查的结论就
// 不再是这一份的结论了 —— 审查实测:检查在途时把位置改成一个不存在的文件并保存,
// 页面已经读到「路径不可执行」,随后旧检查成功回来,状态行被写成「feedgrab 已就
// 绪」+ 绿点,而输入框里和那条可复制命令里都是那条不存在的路径。
// `config` 是**后端回来的那一份**(它执行时核对过,见 `intake_api._x_act`),它才是
// 「这次跑的是什么」的权威答复;拿不到时(异常那条路上没有回包)退回发起那一刻
// 记下的 `t.key`。
// **`config` 这一道单独拆掉一条红都没有**(实测:改成只认 `t.key`,146 项全绿)——
// 因为后端那道闸已经保证了"跑的就是我们发出去那一份"。留着它是因为它**不依赖
// 我们自己这边的记账对不对**:`t.key` 是前端相信的东西,`config` 是服务端说的。
// 口径同 `xChanActs` 那条:别因为单拆不红就当它冗余,拆掉只是把余量用掉一格。
// 两头都要:**后端存着的还是它**(`=== xChanSaved`)、
// **页面上此刻显示的也还是它**(`xChannelSaved()`)—— 只认前一半的话,草稿已经改
// 成别的东西了,一条结论照样会贴在它旁边。
function xChanOwnsAct(t, config) {
  const ran = config ? xChanKey(config) : t.key;
  if (t.kind !== xChanKind || ran !== xChanSaved || !xChannelSaved()) return false;
  xChanLands += 1;              // 从此刻起,更早发出的读说的都是旧事实
  return true;
}
// **结论没能落在这块面板上时,一律重取一次权威状态。** 跟「这条结论能不能贴到
// 面板上」是两个问题:后者看配置身份(`xChanOwnsAct`),这一个只问"后端那边有没有
// 可能已经不是面板上说的样子了" —— 两种来法都算:
//  ①**这个动作可能已经写过东西**。导入写的是那个**共享的** profile
//    (`x_channels.BROWSER_PROFILE` 只有一个,换 Chrome 可执行文件位置并不换它),
//    检查会真起一个 Chrome 打开它;**失败和断连同样算** —— 没拿到回包证不了后端
//    没写(超时不等于没发生)。
//  ②**被那道闸拒绝**:它什么都没写,但这件事本身说明我们手里那份「后端存着什么」
//    是错的(多半是另一个标签页改过),同样得重新问一次。
// 判据宁可宽:多读一次的代价只是一条 GET,漏掉的代价是面板上挂着一条已经不成立
// 的旧结论,而用户看不出它是旧的(审查实测:导入在途时改 Chrome 位置并保存,导入
// 成功的回包因为配置变了被整条丢掉,面板一直说「还没有登录过 X」「这一轮没走完」,
// 切走再切回来才恢复)。
// **失效是一份要留着的状态,不是"发过一条 GET"这个动作。** 一条读发出去之后,
// 后面任何一个动作、一次改选择都能合法把它作废(`xChanOwnsRead`)—— 上一版把
// 「已经发过了」当成刷新完成,于是那份变化永远补不回来:审查实测,导入完成后
// 那条补读被扣住,这期间点一次「检查连接」(它只更新 `ready/why`,答不了留痕
// 那行),补读回来被判过期丢掉,留痕一直停在「这一轮没走完」,切走再切回来才恢复。
// 口径:**置上这个标记,只有一条真落了地、被这块面板接住的读才清得掉它**;
// 中途被作废、读失败、被更晚的动作顶掉,都再补一次。
let xChanDirty = false;
// 在飞的读有几条。**必须是计数不是布尔**:两条读同时在飞时,先落地那条的收尾会
// 把布尔清成 false,于是它被判过期之后当场又补一条,而后发那条还没回来 —— 补一条
// 顶掉一条,一路滚下去。计数则让「还有人在飞」说了算,补读只在真没人时发。
let xChanReads = 0;
function xChanStale() { xChanDirty = true; xChanRefresh(); }
// 欠着一次刷新、而且此刻没人占着,就去补。**没有定时器**:补读自己落地会清掉
// 标记,被顶掉的那次由顶掉它的那个动作在收尾时再叫一次(两处都在 `finally` 里)。
function xChanRefresh() { if (xChanDirty && !xChanReads && !xChanBusy) loadXChannel(sourceKind); }

// 后端那道闸拒绝了(409):它核对过,这一次**什么都没跑**。所以不许记成「结果
// 不知道」—— 那一档是留给"真跑过、只是没拿到答复"的。要做的是重读一次,把现在
// 真正存着的那一份摆出来。
function xChanRefused(e) { return !!e && e.status === 409; }

// 选中的那一档。**换成一组单选而不是下拉**:四个名字摆在下拉里看不出差别,
// 而它们要准备的东西完全不同(一个要在终端装东西、一个要去浏览器开调试开关)。
// 单选的每一行跟着它自己那句话,选之前就看得见代价。
function xChannelValue() {
  return $('source-x-picker').querySelector('input:checked')?.value
         || (xChan && xChan.current) || '';
}

// 「换一种」那块展开没有。展开是**用户的动作**,重画不许顺手收起来 ——
// 他正对着清单读那几句说明时,一条迟到的读回来就把清单收了。
function toggleXChannelPicker() { xChanPicker($('source-x-picker').hidden); }

// 展开/收起那张清单。**只有这一处改它的可见性**(展开是用户的动作,重画不许顺手
// 收起来 —— 他正对着清单读那几句说明时,一条迟到的读回来就把清单收了)。
function xChanPicker(on) {
  $('source-x-picker').hidden = !on;
  const btn = $('source-x-switch');
  btn.setAttribute('aria-expanded', String(on));
  // 箭头走 `icons.js` 的那一份几何(项目硬性:页面里不许出现 ▾ ▴ 这些字符)。
  btn.innerHTML = '换一种' + arrow(on ? 'caretUp' : 'caretDown');
}

// 把清单画出来。**名字和那句说明都来自后端**(口径同顶上那句"不许把通道
// 写死成一张表"):新增/退役一条通道只改后端那份注册表。
function xChannelList() {
  const box = $('source-x-picker'), now = xChannelValue();
  box.innerHTML = '';
  for (const c of (xChan && xChan.channels) || []) {
    const row = document.createElement('label');
    row.className = 'xchan-opt';
    const radio = document.createElement('input');
    radio.type = 'radio'; radio.name = 'x-channel'; radio.value = c.id;
    radio.checked = c.id === now;
    radio.addEventListener('change', xChannelChanged);
    const name = document.createElement('b');
    name.textContent = c.label;
    const why = document.createElement('span');
    why.textContent = c.hint || '';
    row.append(radio, name, why);
    box.appendChild(row);
  }
}

// 清单上换了一档(每个单选自己的 change)。**代次在这儿推进,不在
// `xChannelPicked` 里** —— 后者同时是**渲染**入口(读回来之后、保存之后都要走一趟),
// 在它里面推进等于「每次重画都算换了面板」,而那正是「读先到就废掉随后回来的动作」
// 那条的机制。**这一条是冗余带,别删**:注入「把 `xChanAge()` 搬回 `xChannelPicked`」
// 之后探针 0 条红(有了 `xChanActs` 那道闸之后,能被这次多余推进作废的动作要么已经
// 被它作废过、要么比那条读更晚发起)。留着是因为「重画等于新事实」这个说法本身是错
// 的 —— 下一次有人加第三类落地点时,它决定那个落地点会不会又回到单计数器的形状。
function xChannelChanged() {
  xChanAge();                 // 改了选择:在途的那些回包从此不属于这块面板
  // **选中即收起**:清单是"换一种"时才需要的东西,选完了该回到状态卡 ——
  // 否则那三句长说明一直占着屏幕,把刚换的这一档的状态和按钮挤到下面去。
  xChanPicker(false);
  xChannelPicked();
}

// 按当前选择重画这一块。**纯渲染,不动代次**(理由见上一条)。
function xChannelPicked() {
  if (!xChan) return;
  const now = xChannelValue();
  const show = (id, on) => { $(id).hidden = !on; };
  show('source-x-feedgrab', now === 'feedgrab');
  show('source-x-browser', now === 'browser');
  // 那条要在终端里跑的命令**留在卡片上,不进折叠块** —— 它是 feedgrab 这一档
  // 唯一的下一步,收进「可执行文件位置」里的话,不展开就一个字都看不见,而
  // 状态行那边又按去重把它让掉了(见 `xChannelSay`),两处一起沉默。
  show('source-x-cmd', now === 'feedgrab' && !!xChan.login_command);
  // 这一档什么都不用填时,「可执行文件位置」整块收起来 —— 空的折叠块比没有更糟。
  show('source-x-adv', now === 'feedgrab' || now === 'browser');
  const meta = (xChan.channels || []).find(c => c.id === now);
  $('source-x-name').textContent = meta?.label || now;
  $('source-x-login').textContent = xChan.login_command || '';
  // 「打开登录窗口」「从日常浏览器导入」都只对独立 profile 那一档成立:别的档
  // 手里的登录态不在我们这个 profile 里,点了也写不到它该在的地方。
  // **按「选中的那一档」显示,不按「存下来的那一档」** —— 按钮此刻是可点的
  // (点下去会先把草稿存下来,见 `xChanEnsureSaved`),所以它跟着选择走。
  show('source-x-login-btn', now === 'browser');
  show('source-x-import-btn', now === 'browser');
  show('source-x-import-hint', now === 'browser');
  // 这几行只在"页面上这一份已经存下去了"时才是当前通道的事实。**判据用
  // `xChannelSaved()` 这一份,不另写一个"通道名一样就算"** —— 后者在"只改了
  // 可执行文件位置"时为真,于是 `ready`/`hint`/留痕会贴在一份还没生效的配置旁边。
  const saved = xChannelSaved();
  // **有一个属于这块面板的动作正在飞时,那两行结论归它写,重画不许顺手盖掉。**
  // 它们是这个动作此刻唯一的交代(「正在从日常浏览器读登录态…」「正在检查…」)——
  // 实测:导入途中点一次保存,保存收尾那次重读把预告清成了导入之前的旧留痕,而
  // 那个授权弹窗正等在另一个窗口里。改了选择/改了存下来那一档就不是它的了
  // (`xChanMine` 当场为假),于是「这一份还没存下来」照常显示。
  // **`xChanMine` 问的是另一个问题,所以它的判据也不同**:它问「这块面板此刻还在
  // 说那一档吗」(来源 + 选中的档 + 存着的档),不问「存下去了没有」—— 在途那次导入
  // 写的是那个 profile,用户顺手改一下 Chrome 可执行文件位置并不让它的结果改说别人。
  const busy = xChanMine(xChanPending);
  // **上一次没跑完的那条交代也归这一类**:它说的是「这一档此刻的结果不知道」,
  // 而缓存里那份 `ready/why`/`login_state` 说的是它之前那一刻 —— 让后者盖上来就是
  // 把一条历史冒充成当前结论(理由与实测见 `xChanUnsure`)。
  const unsure = xChanUnsureNow();
  if (!busy) xChannelSay(unsure && unsure.slot === 'state' ? unsure.text : (saved
    ? (xChan.ready ? '当前通道已就绪。' : (xChan.why || '当前通道还没有配好。'))
    : '这一份还没存下来。点下面任何一颗按钮会先替你存一次，也可以点「保存设置」。'));
  // 「上次什么时候导的」只在选择没变时才是当前通道的事实(口径同状态行)。
  // **文案整句由后端给**(`x_login_import.summary`):结论码是那一块自己的词汇表,
  // 前端照它写一张表就是把同一个判断放到两处。
  // 旧留痕**不删,但退到后面**:它自己就带着「上次……」和那个时刻,摆在未知那句
  // 后面读起来正是「这一次不知道,上一次是那样」,不会冒充这一次的结论。
  const trace = saved ? (xChan.login_state || '') : '';
  if (!busy) $('source-x-import-state').textContent =
    unsure && unsure.slot === 'import' ? `${unsure.text}${trace ? ' ' + trace : ''}` : trace;
}

// 状态那一行。**「现在怎么样」和「下一步怎么办」写进同一句**,不再是两行
// 分不出主次的灰字。
//
// 「下一步」由后端给(`readiness` 的第三项),前端只负责显示:有些通道要做的事
// 塞不进一个输入框(比如去浏览器地址栏开一次远程调试开关),它就从这里出来 ——
// 这样**新增一条通道不用改这个文件**,口径同顶上那句"不许把通道和它们各自要填
// 什么写死成一张表"。它和状态行一样,只在选择没变时才是事实。
//
// **拼之前去一次重**:同一句话在同一屏上连着出现两遍的来法有两种(见下面两条
// 注释),按内容判而不是按通道写死一张表,所以各档共用这一份。
// 实测截图在 `docs/decisions/frontend-feedback.md`。
function xChannelSay(state) {
  // 只有选择没变时「下一步」才是事实(口径同状态行)。
  const next = xChannelSaved() ? (xChan.hint || '') : '';
  // ①feedgrab 那档的「下一步」就是卡片上那条可复制的命令本身(它的第三项必须
  //   带着它 —— 待配置横幅那边没有可复制块,见 `x_channels.readiness`);
  // ②「检查连接」返回的 detail 已经把原因和下一步拼在一起了。
  const dup = !next || next === $('source-x-login').textContent || state.includes(next);
  const say = dup ? state : `${state} ${next}`;
  $('source-x-state').textContent = say;
  // 色点是这句话的提前量:**它不单独承载信息**,旁边永远有字。未保存的草稿、
  // 以及那条「不知道」都算不出就绪与否 —— 那时它是中性的灰。
  const dot = $('source-x-dot');
  const known = xChannelSaved() && !xChanUnsureNow();
  dot.className = 'xchan-dot' + (!known ? '' : xChan.ready ? ' ok' : ' warn');
}

// 一个动作正在跑:这一刻**没有结论**,所以色点回到中性灰,别让上一次的绿/褐
// 挂在一句「正在…」旁边。
function xChannelWorking(text) {
  $('source-x-state').textContent = text;
  $('source-x-dot').className = 'xchan-dot';
}

// 后端此刻存着的**那一整份**(`xChanKey` 的形式,没读到过就是 null)。
// **判据是整份配置,不是通道名。** 原来这里比的是「选中的通道 === `xChan.current`」,
// 于是改了 Chrome 可执行文件位置、再点检查/登录/导入,`xChannelSaved()` 当场为真、
// 「先保存」那一步直接放行 —— 三颗按钮都按**旧路径**跑了一遍,输入框里却是新值,
// 服务端连一次 PUT 都没收到(审查实测:`path-ignored.jpg`,三个入口对称复现)。
// 「通道名存下来了」≠「这张表单此刻这一份存下来了」,两者只在"用户只动了通道"那
// 一种情况下恰好相等,而那正好是上一轮唯一试过的情况。
let xChanSaved = null;

// 一份配置的身份。**顺序和取值口径只有这一处** —— 发出去的 body、页面上的草稿、
// 后端回来的那一份都过它,否则"一样不一样"会在三处各判一遍。
function xChanKey(c) {
  return JSON.stringify([(c && c.x_channel) || '',
                         ((c && c.x_feedgrab_bin) || '').trim(),
                         ((c && c.x_chrome_path) || '').trim()]);
}
// 页面上此刻这一份。
function xChanDraft() { return xChanKey(xChannelPayload(xChanKind)); }

// 「页面上这一份,后端已经存下来了吗」。
function xChannelSaved() {
  return xChanSaved !== null && xChanDraft() === xChanSaved;
}

async function loadXChannel(kind) {
  const panel = $('source-xchan');
  panel.hidden = sourceModules.get(kind)?.adapter !== 'social';
  // 换到没有通道的来源时那份快照也作废:留着它会让下一次进来时
  // `xChannelSaved()` 拿上一类来源的配置当答案。
  if (panel.hidden) { xChanSaved = null; return; }
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
  xChanReads += 1;
  try {
    data = await pipeJson(`/api/intake/${kind}/x-channel`);
  } catch (e) {
    // 读不出来时**把这一块收干净**,只留那句原因:留着上一次那份清单和按钮,
    // 点下去是对着一份读不出来的设置做动作。
    if (want !== sourceKind || !xChanOwnsRead(tag)) return;
    xChan = null;
    $('source-x-picker').innerHTML = '';
    xChanPicker(false);
    $('source-x-name').textContent = '读不出来';
    $('source-x-adv').hidden = true;
    $('source-x-feedgrab').hidden = $('source-x-browser').hidden = true;
    $('source-x-login-btn').hidden = $('source-x-import-btn').hidden = true;
    $('source-x-import-hint').hidden = true;
    // **可执行的"下一步"要跟着一起收走。** 那条终端命令是上一档读出来的,读不出
    // 当前取法之后它不再是任何人的下一步 —— 留在卡片上就是叫人去照抄一条属于
    // 另一档的命令(审查 N1)。留痕同理:它说的是"上次什么时候搬的",而我们这会儿
    // 连"现在是哪一档"都答不上来。
    $('source-x-cmd').hidden = true;
    $('source-x-login').textContent = '';
    $('source-x-import-state').textContent = '';
    $('source-x-dot').className = 'xchan-dot';
    $('source-x-state').textContent = `读不出可选的取法（${e.message}）。`;
    return;
  } finally {
    // **落地之后才算没人占着。** 这里不直接重试:读失败时面板上写着那句原因,
    // 再自动打一轮只会把它刷掉;欠着的那次由下一个动作或下一次进来补上。
    xChanReads -= 1;
  }
  // 这期间切走了、又读了一次、改了下拉、或者有个动作已经开跑:别覆盖别人的面板,
  // 也别拿「那之前」的一份盖掉更新的事实。**但欠着的那次刷新不算还上了** ——
  // 它一个字都没写进面板,所以当场再补一次(有动作在飞时 `xChanRefresh` 自己
  // 会让开,由那个动作收尾时叫)。
  if (want !== sourceKind || !xChanOwnsRead(tag)) { xChanRefresh(); return; }
  xChan = data; xChanKind = kind;
  // **这一条真落地了,欠的那次刷新还上了。** 判据落在"被接住"这一刻,不是发出去
  // 那一刻 —— 两者之间隔着一整道归属检查。
  xChanDirty = false;
  // **清单整份按这条读重画**:它是权威的那一份(更早发出的读在上面那道
  // 归属检查里已经被挡掉了),名字和说明都以它为准。
  xChannelList();
  // **但「选中哪一档」是草稿,不是这条读说得了的。** 代次那道闸按「谁最后发起」
  // 排,挡得住"读在途、用户改了选择"那一种;挡不住"保存收尾顺手再读一次,而用户
  // 在保存途中已经改了选择"—— 那条读是在他改完**之后**才发出的,代次是新的,
  // 落地时却会把他刚选的那一档换回服务端那一份。所以再加一道:**手里有没存下去
  // 的草稿时,这条读只更新「后端那一份」,一个控件都不碰。**
  if (xChanSaved === null || xChanDraft() === xChanSaved) xChanSelect(data.current);
  xChanSaved = xChanSavedKey(data);
  xChannelPicked();
}

// 把选中项挪到某一档。清单还没画出来(或那一档不在清单里)时先画一遍。
function xChanSelect(id) {
  const box = $('source-x-picker');
  let hit = box.querySelector(`input[value="${CSS.escape(id || '')}"]`);
  if (!hit) { xChannelList(); hit = box.querySelector(`input[value="${CSS.escape(id || '')}"]`); }
  for (const r of box.querySelectorAll('input')) r.checked = r === hit;
}

// 后端那一份的身份。两种形状各有自己的取法,但都归到 `xChanKey` 那一个口径上:
//  - `/x-channel` 的回包:`config` 里就是完整那一份(后端照它核对动作请求);
//  - settings 的形状(保存那条路的回包):三个字段在顶层。
// **两条都不许靠"沿用页面上现有的那两个值"去拼。** 那是 GET 还只给 `current` 时
// 的老办法,而它是猜 —— 同一档取法在另一个标签页里换了路径时,拼出来的身份就是
// 错的,于是这一页会一直以为自己那一份还存着(审查实测过那条路径)。
function xChanSavedKey(data) {
  if (data && data.config) return xChanKey(data.config);           // `/x-channel` 的回包
  if (data && 'x_channel' in data) return xChanKey(data);          // settings 的形状
  // 两种都不是:**这一份是什么我们不知道**,那就别假装知道。返回一个对不上的
  // 身份,面板于是说「还没存下来」、动作会先存一次 —— 代价是多发一次 PUT,
  // 而反过来猜错的代价是拿一份不知道是什么的配置去跑(元规则:误报顶多重来
  // 一次,漏报是静默用错东西)。
  return '?';
}

// 动作请求里带着的「我眼前那一份」。**后端照它核对**(`intake_api._x_act`):
// 对不上就一个字都不跑,因为那说明这一页手里的「已经保存了」已经过期 ——
// 另一个标签页改过设置时,这一页既不知道、也不会重新 PUT(审查实测:第二页把
// 路径改成一个不存在的文件并保存,第一页直接点「检查连接」,检的是第二页那条)。
function xChanExpect() { return xChannelPayload(xChanKind); }

// 保存设置时附带的那几个字段。社交来源之外返回空对象。
function xChannelPayload(kind) {
  if (!xChan || xChanKind !== kind) return {};
  return { x_channel: xChannelValue(),
           x_feedgrab_bin: $('source-x-bin').value.trim(),
           x_chrome_path: $('source-x-chrome').value.trim() };
}

// 把后端那一份落到面板上。`sent` 给的是「这一次保存发出去的那份草稿身份」——
// 不传 = 这是一次权威的整份加载(刚打开面板、换了来源),照它重填。
//
// **「后端存着什么」永远记下来,「页面上填什么」要看草稿这期间动没动。**
// 原来这里无条件重填:保存在途时用户改回另一档,旧的 PUT 回包落地就把他刚选的
// 那一档静默换掉、动作接着按被擦掉那一档跑下去(审查实测 `race-during.jpg` /
// `race-after.jpg`)。这正是前几轮给 GET / 动作 POST 修过的那条归属问题,在这一轮
// 新加的「PUT → 填表 → 接着做」这条链上复发 —— 新链路要自己过一遍同样的闸。
function fillXChannelFields(settings, sent) {
  if (!('x_channel' in (settings || {}))) return;
  // ①后端那一份:无条件记下。它和草稿是两样东西,记它永远不会和草稿打架,
  //   而不记就等于"存成功了但页面不知道存的是什么",状态行只能瞎猜。
  xChanSaved = xChanSavedKey(settings);
  if (xChan) xChan.current = settings.x_channel;
  // ②草稿:只有「这期间没人动过」才照它重填。动过了就留着用户那一份 ——
  //   状态行会据此说「这一份还没存下来」,那是实话。
  if (sent === undefined || xChanDraft() === sent) {
    $('source-x-bin').value = settings.x_feedgrab_bin || '';
    $('source-x-chrome').value = settings.x_chrome_path || '';
    if (xChan) xChanSelect(settings.x_channel);
    // 草稿换人了(保存之后/换了来源之后):在途的回包从此不属于这块面板。
    xChanAge();
  }
  xChannelPicked();
}

// **先保存,再执行。** 三颗按钮任何时候都能点:后端读的是**存下来**的那一份,
// 所以草稿跟它不一样时,替用户先存一次,再做他刚点的那件事。
//
// 原来是把按钮灰掉、挂一句 title 说「先点保存设置」。那等于让用户替我们走一趟
// 本来就该我们走的流程 —— 他看到的是一颗按不动的按钮,而唯一的解释藏在
// hover 才出得来的 title 里(用户反馈:「那肯定不合常理」)。后端那道拒绝照旧留着
// (`intake_api.x_channel_login`),两道各有射程:这里管正常路径,那里管这里漏掉时。
//
// 返回 false = 没存成或者存完已经不是这一档了,这一次动作不做 ——
// 原因已经写在表单那行红字上。
async function xChanEnsureSaved() {
  // **「还没读到可操作的配置」跟「用户改过草稿」是两件事,不许混成一件。**
  // 读失败那一档 `xChan` 是 null,`xChannelPayload` 于是交出一份空草稿 —— 原来
  // 直接往下走:先发一次**空的 PUT**,再被保存收尾那次重读恢复出来的真实设置判成
  // 「保存完成时这份设置已经又改过了」,而用户一个字都没改(审查 N1 实测:GET 503
  // 之后第一次点「检查连接」,只收到一条 `PUT {}`,检查压根没发出去)。
  // 出路是**先把那次读补上**:读回来了这一份本来就等于存着的那一份,
  // 下面那个 `xChannelSaved()` 当场为真,这一次动作照常接着做。
  if (!xChan) {
    await loadXChannel(sourceKind);
    if (!xChan) {
      toast('还没读到现在用的是哪一档，这一次没有执行。等这一行恢复了再点一次。',
            { type: 'info' });
      return false;
    }
  }
  if (xChannelSaved()) return true;
  const want = xChanDraft(), kind = xChanKind;
  // 保存这一步自己会重读一次通道(`fillSourceSettings` 收尾那条
  // `loadXChannel`),**而且要等它落地**:不等的话,紧接着的 `xChanAct()` 会把它
  // 当成"动作之前那一刻"的读作废掉,于是新那一档的 `hint`/`login_command`/`ready`
  // 永远停在旧档的值上 —— 实测形态是切到 feedgrab、检查跑完了,卡片上那条
  // 「feedgrab login twitter」始终不出现(审查 `slow-read-after.jpg`)。
  if (!await saveSourceSettingsNow()) return false;
  // 两个条件都要:①存下来的**整份都是**他刚点的那一份(这期间切走了来源、
  // 改了选择、改了路径,接着做下去就是做到另一份配置头上);②**页面此刻显示的
  // 还是那一份** —— 光有①不够:保存途中他又改了一次,①照旧成立(我们确实存成了
  // 发出去那一份),可屏幕上已经是另一份了,这时候动手就是拿一份他刚放弃的配置
  // 去跑,而结论会贴在新那一份旁边。
  if (kind !== xChanKind || xChanSaved !== want || !xChannelSaved()) {
    // **别无声退场。** 用户按了一颗按钮,这一下既没执行也没报错的话,他只会再按
    // 一次。说清"为什么停下了" —— 停的理由恰恰是我们没拿他后来那一份去动手。
    if (kind === xChanKind)
      toast('保存完成时这份设置已经又改过了，这一次没有接着执行。',
            { type: 'info', ms: 10000 });
    return false;
  }
  const label = (xChan && (xChan.channels || []).find(c => c.id === xChan.current)?.label)
                || (xChan && xChan.current) || '';
  toast(`已先保存「${label}」这一档，再继续。`, { type: 'info' });
  return true;
}

async function openXLoginWindow() {
  // **这一条刻意不对代次**(同一族里只有它不对):它一个字都不往面板状态上写,
  // 那条 toast 说的是「用户自己刚点的那个动作真的发生了」。守上它的后果是用户顺手
  // 改了选择之后这句话消失,而那个登录窗口确实开着 —— 要守的是往面板上写,不是
  // 发过异步请求(口径见 `docs/decisions/frontend-feedback.md`)。
  if (xChanBusy) return;
  xChanBusy = true; $('source-x-login-btn').disabled = true;
  try {
    if (!await xChanEnsureSaved()) return;
    const r = await pipePost(`/api/intake/${xChanKind}/x-channel/login`, xChanExpect());
    toast(r.detail, { type: 'info', ms: 12000 });
  } catch (e) {
    // 被那道闸拒绝了:说清为什么,再把真相读回来(理由见 `xChanRefused`)。
    if (xChanRefused(e)) { toast(e.message, { type: 'info', ms: 12000 }); loadXChannel(sourceKind); }
    else toast('打不开登录窗口', { detail: e.message });
  }
  finally { xChanBusy = false; $('source-x-login-btn').disabled = false; }
}

async function importXLoginState() {
  if (xChanBusy) return;
  xChanBusy = true; $('source-x-import-btn').disabled = true;
  // **先保存再导入**(理由见 `xChanEnsureSaved`):后端写的是存下来那一档的
  // profile。保存失败就不往下做 —— 那时导入会导到另一档头上。
  if (!await xChanEnsureSaved()) {
    xChanBusy = false; $('source-x-import-btn').disabled = false; return;
  }
  // **动作一开始就让在途的那些读作废**(它们说的是导入之前那一刻)。
  // 取在保存**之后**:保存自己会重读一次,那条读说的是导入之前,不该落地。
  const tag = xChanAct();
  let mine = false;           // 这个结果最后有没有写进这块面板
  // **先把「那个浏览器会弹一次授权」说在前面。** 这一步会停在授权弹窗上最长 90 秒
  // (`cdp_attach.ATTACH_TIMEOUT`),而弹窗出现在另一个窗口里 —— 不预告的话用户
  // 看到的是一颗按下去就没反应的按钮,等他发现那个弹窗时这边可能已经超时了。
  $('source-x-import-state').textContent =
    '正在从日常浏览器读登录态…那个浏览器会弹一次授权，点「允许」。';
  try {
    // 它要连一次日常浏览器(等用户点授权,最长 90 秒),再起一个无头 Chrome 把
    // cookie 写进去、当场打开 x.com 验一次 —— 比「检查连接」还慢一截,所以超时
    // 给得比它宽。
    const r = await pipePost(`/api/intake/${xChanKind}/x-channel/import-login`,
                             xChanExpect(), 240000);
    // **回执先给,不进这块面板的归属判断**(口径同 `openXLoginWindow` 那条 toast):
    // 它说的是「用户自己刚点的那颗按钮有结果了」,面板此刻在说谁都不改变这件事 ——
    // 而把它也守上的后果是用户顺手保存一次就再也不知道那次导入成没成。
    toast(r.detail, { type: r.ok ? 'ok' : 'err', ms: 12000 });
    // **过期的回包一个字都不许往页面上写。** 这期间下拉可能已经改走了,那时状态行
    // 上挂着的是「改完记得点保存设置」—— 用成功文案把它盖掉就是报喜不报忧。
    // 比它更早发出的那些读说的都是导入之前那一刻 —— 那一道在**点下去的那一刻**
    // 就落了(`xChanAct`),不靠这一句,所以这里只问「还是这块面板吗」。
    // **按后端回来的那一份判**:它说清了这一次跑的是哪一份配置。
    if (!(mine = xChanOwnsAct(tag, r.config))) return;
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
    // 拒绝那一档**什么都没跑**,所以不记「搬没搬成不知道」—— 记了就是把一次
    // 明确的拒绝说成一次悬着的导入(理由见 `xChanRefused`)。
    if (xChanRefused(e)) { toast(e.message, { type: 'info', ms: 12000 }); return; }
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
    $('source-x-import-btn').disabled = false;
    // 结果没写进这块面板时才重画:**写进去了就别动它** —— 一次没验成的导入写的是
    // 「这一次为什么没成」,而重画写的是留痕那行「什么时候搬的」,后者答不了前者。
    // 没写进去的那一档必须重画,否则上面按住的「正在…」会永远挂在那儿。
    // **「这条结论归不归眼前这一份」和「后端那边变没变」是两个问题**,
    // 结论没落地就重取一次(两种来法见 `xChanStale`)。
    if (!mine) { xChannelPicked(); xChanStale(); }
    // **这个动作可能刚刚把一条在途的补读顶掉了**(它一开始就让更早的读作废)——
    // 那次刷新还欠着,这里补上。结论归了这块面板也一样欠着:一次成功的「检查
    // 连接」只更新 `ready/why`,答不了留痕那行「上次搬成了没有」。
    // **导入这一侧单拆掉一条红都没有**(实测 163 项全绿):导入自己的回包带着
    // 新的 `ready/why/login_state`,所以"它自己是 mine、却还欠着别人的刷新"那
    // 一种顺序,现有场景里摆不出来。留着是为了两颗按钮同一条规矩 —— 口径同
    // `xChanActs` 那条:单拆不红不等于冗余,拆掉只是把余量用掉一格。
    // (检查那一侧反过来是承重的,拆掉 4.6l 当场红。)
    else xChanRefresh();
  }
}

async function checkXChannel() {
  if (xChanBusy) return;
  xChanBusy = true; $('source-x-check').disabled = true;
  // 先保存再检查:后端检的是**存下来**的那一档,草稿没存下去的话,检出来的
  // 结论贴在另一档旁边就是谎话(理由见 `xChanEnsureSaved`)。
  if (!await xChanEnsureSaved()) {
    xChanBusy = false; $('source-x-check').disabled = false; return;
  }
  const tag = xChanAct();     // 同一道,理由见 `xChanGen`(这条也是审查 grep 出来的)
  let mine = false;
  xChannelWorking('正在检查…');
  try {
    // 这一步会真起一个 Chrome 或真跑一次 feedgrab doctor,比普通请求慢得多。
    const r = await pipePost(`/api/intake/${xChanKind}/x-channel/check`,
                             xChanExpect(), 120000);
    toast(r.detail, { type: r.ready ? 'ok' : 'err' });   // 回执先给,理由同导入那条
    // 刚验出来的结论比在途的那些读新;但它只说得了**它跑的那一份**(`r.config`)。
    if (!(mine = xChanOwnsAct(tag, r.config))) return;
    xChanSettled(tag, 'state');                // 理由同导入那条
    if (xChan) { xChan.ready = r.ready; xChan.why = r.detail; }
    xChannelSay(r.detail);
  } catch (e) {
    if (xChanRefused(e)) { toast(e.message, { type: 'info', ms: 12000 }); return; }
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
    // 同一道,理由同导入那条。**但保护不许因此放宽**:这条结论该不该显示仍然
    // 按它跑的那一份配置判(上面那道 `xChanOwnsAct`),这里只是重取一次权威状态。
    if (!mine) { xChannelPicked(); xChanStale(); }
    else xChanRefresh();     // 顶掉的那次补读还欠着,理由同导入那条
  }
}
