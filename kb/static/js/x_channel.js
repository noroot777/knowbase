// X 的采集通道:独立 profile 的登录态 / 日常浏览器 / feedgrab(cookie)。
//
// 通道清单、人话名字、就绪判断全部由后端给(`/api/intake/<kind>/x-channel`)。
// **前端不许把通道 id 和它们各自要填什么写死成一张表**:这里只按后端给的
// id 决定显示哪一块,新增/退役一条通道是后端那一份注册表的事。
// (退役名的翻译在后端做 —— 这份选择存在 feed_settings 里,`get_settings`
//  读出来就已经 resolve 过了,浏览器手里拿不到退役名。)
let xChan = null, xChanKind = '', xChanBusy = false;

// **代次、归属、刷新记账那一族搬去了 `x_channel_own.js`**(本文件到了 700 行上限):
// `xChanAge/Read/Act`、`xChanOwnsRead/OwnsAct/Mine`、`xChanSeq/Fresh/Reads` 与
// `xChanRefresh/Touched` 都在那一份里,同一个全局作用域,直接按名字用。

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
  // **这一条读覆盖到共享资料的哪一版,在发起这一刻就定了。** 发出去之后动作才完成
  // 的话,它手里是变完之前的快照 —— 落地时拿这个数跟 `xChanSeq` 比,对不上就不许算
  // 把欠着的那次刷新还上了(理由与实测见 `xChanSeq`)。
  const seq = xChanSeq;
  let data, failed = null;
  xChanReads += 1;
  try {
    data = await pipeJson(`/api/intake/${kind}/x-channel`);
  } catch (e) {
    failed = e;
  }
  // **落地了才算没人占着,而且这一格必须在下面那几条出口之前还掉** —— `finally`
  // 比 `return` 晚跑,那一格还按着的时候 `xChanRefresh()` 一律让开,于是欠着的那次
  // 刷新再没有事件推它:审查实测,被作废的那条补读回来是 503,留痕一直停在「这一轮
  // 没走完」,用户显式再点一次检查才补回来。
  xChanReads -= 1;
  if (failed) {
    // **这条读已经被更晚的动作或更晚的读作废了**:它一个字都没写进面板,用户看不见
    // 这次失败 —— 所以它也不该把欠着的那次刷新吞掉,这里接着推。
    if (want !== sourceKind || !xChanOwnsRead(tag)) { xChanRefresh(); return; }
    // 读不出来时**把这一块收干净**,只留那句原因:留着上一次那份清单和按钮,
    // 点下去是对着一份读不出来的设置做动作。
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
    $('source-x-state').textContent = `读不出可选的取法（${failed.message}）。`;
    // **这一档刻意不自动重试**:面板上就写着那句原因,再自动打一轮只会把它刷掉。
    // 欠着的那次由下一个动作或下一次进来补 —— 跟上面那种「被作废、压根没呈现过的
    // 失败」是两件事(审查也把这两档分开:不要求所有看得见的错误立刻无限重试)。
    return;
  }
  // 这期间切走了、又读了一次、改了下拉、或者有个动作已经开跑:别覆盖别人的面板,
  // 也别拿「那之前」的一份盖掉更新的事实。最后那一道是**版本**:它通过了归属检查,
  // 但它是在那个动作完成之前发出去的,手里是变完之前的快照 —— 接它就等于用一份旧
  // 快照把「还欠一次刷新」这件事抹掉(审查 R1 第一处)。
  // 三种都不算还上了那次刷新,所以当场再补一次(有动作在飞时 `xChanRefresh` 自己
  // 会让开,由那个动作收尾时叫)。
  if (want !== sourceKind || !xChanOwnsRead(tag) || seq < xChanSeq) { xChanRefresh(); return; }
  xChan = data; xChanKind = kind;
  // **这一条真落地了、而且覆盖得到最新那一版,欠的那次刷新还上了。** 判据落在
  // "被接住"这一刻,不是发出去那一刻 —— 两者之间隔着一整道归属检查和这道版本检查。
  xChanFresh = seq;
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
  let sent = false;              // 请求发出去过(发出去了就可能动过那份共享资料)
  try {
    if (!await xChanEnsureSaved()) return;
    sent = true;
    const r = await pipePost(`/api/intake/${xChanKind}/x-channel/login`, xChanExpect());
    toast(r.detail, { type: 'info', ms: 12000 });
  } catch (e) {
    // 被那道闸拒绝了:说清为什么就行,真相由下面那个出口读回来(见 `xChanRefused`)。
    if (xChanRefused(e)) toast(e.message, { type: 'info', ms: 12000 });
    else toast('打不开登录窗口', { detail: e.message });
  }
  finally {
    xChanBusy = false; $('source-x-login-btn').disabled = false;
    // **它一个字都不往面板上写,所以 `covered` 永远是 false**:那个窗口一开,后端
    // 的就绪判断立刻就不一样了(「登录窗口还开着」),而页面上还是开之前那一份。
    // 保存都没过去的那一档 `sent` 是 false —— 什么都没发生,只把欠着的那次推一下。
    xChanTouched(sent, false);
  }
}

async function importXLoginState() {
  if (xChanBusy) return;
  xChanBusy = true; $('source-x-import-btn').disabled = true;
  // **先保存再导入**(理由见 `xChanEnsureSaved`):后端写的是存下来那一档的
  // profile。保存失败就不往下做 —— 那时导入会导到另一档头上。
  if (!await xChanEnsureSaved()) {
    // 这一次什么都没发出去,但**欠着的那次刷新不该因为按了一下按钮就停住**:
    // `xChanBusy` 按住期间 `xChanRefresh` 一律让开,这会儿刚放开,补一次。
    // **这一条单拆掉一条红都没有**(实测 175 项全绿):走到这儿的那几种形态里,
    // 保存失败那条路自己会重读一次、`xChan` 是 null 那条路也会,欠着的那次顺带就
    // 还上了。留着是因为它守的是**出口**不是某一种形态 —— 口径同 `xChanActs`:
    // 单拆不红不等于冗余,拆掉只是把余量用掉一格(检查那颗按钮下面同此)。
    xChanBusy = false; $('source-x-import-btn').disabled = false; xChanRefresh(); return;
  }
  // **动作一开始就让在途的那些读作废**(它们说的是导入之前那一刻)。
  // 取在保存**之后**:保存自己会重读一次,那条读说的是导入之前,不该落地。
  const tag = xChanAct();
  let mine = false;           // 这个结果最后有没有写进这块面板
  // 它的回包带着**变完之后**的事实、而且落在了这块面板上。只有成功那条路算得上:
  // 断在半路的那一档连回包都没有,后端搬没搬成都不知道(见 `xChanUnsure`)。
  let covered = false;
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
    covered = true;                // 这一份回包就是搬完之后的事实,下面几行把它摆上去
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
    // **权威记录由收尾那一个出口去读**(`covered` 留着 false:这一下连回包都没有,
    // 后端可能已经写完了,它那条留痕比页面上的缓存新)。这里不自己再打一条 ——
    // 它发在收尾推那一格**之前**,手里是变完之前的快照,落地时照样会被版本那道闸
    // 丢掉,白跑一趟。读不回来也没关系:上面那句「不知道」留在那儿,比旧结论诚实。
  }
  // 恢复成"此刻该不该能点",不是无条件放开(这期间下拉可能已经被改走了)。
  // `xChanPending` 一并清掉:没有动作在飞了,重画照常写那两行结论。
  finally {
    xChanBusy = false; xChanPending = null;
    $('source-x-import-btn').disabled = false;
    // 结果没写进这块面板时才重画:**写进去了就别动它** —— 一次没验成的导入写的是
    // 「这一次为什么没成」,而重画写的是留痕那行「什么时候搬的」,后者答不了前者。
    // 没写进去的那一档必须重画,否则上面按住的「正在…」会永远挂在那儿。
    if (!mine) xChannelPicked();
    // **「这条结论归不归眼前这一份」和「那份共享资料变没变」是两个问题**:后者一律
    // 推一格,由 `xChanTouched` 一处判(它也会把这个动作刚顶掉的那条在途补读接上)。
    xChanTouched(true, covered);
  }
}

async function checkXChannel() {
  if (xChanBusy) return;
  xChanBusy = true; $('source-x-check').disabled = true;
  // 先保存再检查:后端检的是**存下来**的那一档,草稿没存下去的话,检出来的
  // 结论贴在另一档旁边就是谎话(理由见 `xChanEnsureSaved`)。
  if (!await xChanEnsureSaved()) {
    // 什么都没发出去,但欠着的那次刷新要推一下(理由、以及「单拆 0 红」那笔账,
    // 都同导入那条)。
    xChanBusy = false; $('source-x-check').disabled = false; xChanRefresh(); return;
  }
  const tag = xChanAct();     // 同一道,理由见 `xChanGen`(这条也是审查 grep 出来的)
  let mine = false, covered = false;    // 两个问题,理由同导入那条
  xChannelWorking('正在检查…');
  try {
    // 这一步会真起一个 Chrome 或真跑一次 feedgrab doctor,比普通请求慢得多。
    const r = await pipePost(`/api/intake/${xChanKind}/x-channel/check`,
                             xChanExpect(), 120000);
    toast(r.detail, { type: r.ready ? 'ok' : 'err' });   // 回执先给,理由同导入那条
    // 刚验出来的结论比在途的那些读新;但它只说得了**它跑的那一份**(`r.config`)。
    if (!(mine = xChanOwnsAct(tag, r.config))) return;
    covered = true;                            // 理由同导入那条
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
    // 权威状态由收尾那一个出口去读,理由同导入异常那条。
  } finally {
    xChanBusy = false; xChanPending = null;
    $('source-x-check').disabled = false;
    // 同一道,理由同导入那条。**但保护不许因此放宽**:这条结论该不该显示仍然
    // 按它跑的那一份配置判(上面那道 `xChanOwnsAct`),这里只管那份共享资料。
    if (!mine) xChannelPicked();
    xChanTouched(true, covered);
  }
}
