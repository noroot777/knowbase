(() => {
  'use strict';
  const element = id => document.getElementById(id);
  let server = '';
  let key = '';
  let busy = false;
  //: 当前标签(`{id, url}`),和服务端对这一页的那一答(`/here`)。
  //: **这一答是这一块界面的全部依据** —— 原先这里还有一个「补回哪个待办」的
  //: 下拉框,要人把服务端已经知道的事再回答一遍。
  let here = null;
  let situation = null;
  //: **人点过「这一页就是这一篇」之后,那一下的身份要留在这儿。** 丢掉它的后果实测
  //: 过:两个条目填了同一个最终网址,点了后一篇那行的「改用它」、界面也说「已改用」,
  //: 可下一问只按网址发出去,服务端在同页那几个待办里按创建时间代选了最早那个 ——
  //: 于是「采集」把**后一篇的正文写进了前一篇**,而后一篇仍然缺正文。
  //:
  //: 所以它是模块级的,不是 `adoptHere` 里的一个局部变量:人点一下「重新确认」也要
  //: 带着它去问,否则那一下就把他的选择抹掉了。
  //: 跟着记下**是在哪一页上点的**。服务端对「带了身份却兑现不了」一律不代选,所以
  //: 标签这期间自己跳走了(站内重定向、推送式改地址)之后再带着它去问,屏幕上会写着
  //: 「你选的那个失效了」—— 其实什么都没失效,他只是站到了另一页上,而那时摆出来的
  //: 候选属于上一页,点下去是把这一页认给了另一篇。换页就当他没点过。
  let chosen = '';
  let chosenFor = '';
  //: **这一次采集已经落地了,而且是在这个地址上。** 采完那一页就没用了 ——
  //: 抽屉里「同时在新标签打开这一篇」默认勾着,所以它多半是刚刚为这次采集开出来
  //: 的那个标签,人接下来要做的事就是把它关掉。
  //:
  //: **记的是「哪个标签的哪一页」,不是光一个地址**。标签自己跳走了(站内重定向、
  //: 推送式改地址、他点开了下一篇)之后,屏幕上那颗「关掉这一页」关的就不是他采过
  //: 的那一页,而是他此刻正读着的东西;只记地址的话,同一篇文章开了两个标签时还会
  //: 把另一个标签认成这一个。换页就当没采过 —— 服务端那一答 `saved` 另算一条路。
  let doneFor = null;

  function status(message, error = false) {
    element('status').textContent = message;
    element('status').classList.toggle('error', error);
  }

  //: 这一轮新冒出来的那颗默认动作,**等不忙了再收焦点**。
  //:
  //: 三条成功路径都是 `setBusy(true)` → 写进去 → `loadHere()` 里把按钮放出来 → 最后
  //: 才 `setBusy(false)`,而按钮在中间那一段是**禁用的**;真实浏览器对 disabled 的元素
  //: 不给焦点,那一下静默落空 —— 屏幕上按钮好端端地在,回车却没有反应,人只能去点它,
  //: 而「默认选中」要的正是不必去点。(假 DOM 不管 disabled,所以探针一度看不出来:
  //: `tests/popup_dom.mjs` 的 `focus()` 现在照真实 DOM 的规矩拒绝。)
  let wantFocus = '';

  function flushFocus() {
    if (busy || !wantFocus) return;
    const target = element(wantFocus);
    wantFocus = '';
    // 这中间它可能又藏回去了(刷新读到别的情形)—— 那就不该再把焦点送过去。
    if (!target.hidden && !target.disabled) target.focus();
  }

  function setBusy(value) {
    busy = value;
    for (const name of ['act', 'act-page', 'act-selection'])
      element(name).disabled = value || !situation;
    for (const name of ['connect', 'refresh', 'close-tab']) element(name).disabled = value;
    // 「改用它」有几颗要看服务端交回几个待办,所以从那张列表上现读,不记一份名单。
    for (const button of element('elsewhere-list').querySelectorAll('button')) button.disabled = value;
    flushFocus();
  }

  function localServer(value) {
    const url = new URL(value);
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname)
        || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
      throw new Error('只允许 http://127.0.0.1:端口 或 http://localhost:端口 的本机服务');
    }
    return url.origin;
  }

  async function call(path, options = {}) {
    if (!server || !key) throw new Error('请先从知识库的采集抽屉复制配对密钥并连接');
    const response = await fetch(`${server}${path}`, {
      credentials: 'omit', redirect: 'error', cache: 'no-store',
      headers: {'Content-Type': 'application/json', Authorization: `Bearer ${key}`}, ...options
    });
    return apiResponse(response, '浏览器采集请求失败');
  }
  // 两族端点、同一条连接和同一份配对密钥。`/api/grab` 那几个服务端不验密钥
  // (它只收一条地址,内容由服务端自己去抓),`/from-page` 验 —— 一律带上,
  // 省得在这里按端点记一张「谁要密钥」的表。
  const api = (path, options) => call('/api/browser-capture' + path, options);
  const grabApi = (path, options) => call('/api/grab' + path, options);

  // 只为了好读:百分号编码过的中文地址在这里显示成人能对着地址栏核对的样子。
  // **判据不在这里** —— 网址是不是同一页一律由服务端 /precheck 判(见
  // docs/decisions/browser-capture.md「准入判据只有一份」)。
  function plainUrl(value) {
    try { return decodeURI(value); } catch { return value; }
  }

  async function currentTab() {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!tab?.id || !/^https?:\/\//.test(tab.url || '')) {
      throw new Error('请在要采集的文章标签上打开扩展，而不是扩展管理页或知识库页面');
    }
    return tab;
  }

  //: situation → 这一块摆哪几颗按钮、各写什么字。
  //:
  //: **这张表不是判据。** 「这一页是哪一条、写不写得了、正文还欠不欠」由服务端
  //: `/here` 一处回答(硬约束见 docs/decisions/browser-capture.md「准入判据只有
  //: 一份」),这里只负责把那一答摆成界面;`null` = 这一档没有可做的事,照旧只显示
  //: 服务端那句话。
  //:
  //: 两档的主按钮**都叫「采集」**:点下去之后发生什么由上面那句答复说清(「正文还
  //: 缺着」还是「还不在知识库里」),按钮只说动作。两颗各起一个名字,等于把同一件
  //: 事按内部实现分成两个词让人去记。
  //:
  //: `fill` 那一档**一开始就摆出「改用所选文字」**:会落到这一档的多半是自动补抓
  //: 已经试过并失败的页面(要登录、脚本渲染、403),扩展自己的识别同样可能认不出
  //: 正文,而人站在那一页上选一段是现成的出路。`absent` 那一档等服务端先自己去抓,
  //: 抓不到才露出来 —— **露不露面和叫什么是两件事**,各占一个字段。
  //:
  //: 合成一个字段(空串 = 一开始不显示)的后果实测过:`absent` 失败之后那颗按钮
  //: 只被 `hidden = false` 放出来,名字还是空串 —— 屏幕上是一颗 26×36 的空白小方块,
  //: 而 `extract.js` 回的那句「请选好全文再读一次」正指着它。
  const PLANS = {
    absent: {act: '采集', selection: '改用所选文字', selectionAtFirst: false},
    fill: {act: '采集', selection: '改用所选文字', selectionAtFirst: true},
    complete: null,
    //: `undecided` = 同一页上不止一个条目在等、而服务端不肯替人代选。**一颗采集按钮
    //: 都不给**:这一档没有「哪一篇」这个答案,给了就是让他按一下赌一把,而赌错是
    //: 正文被另一篇覆盖。出路在下面那张候选列表里,点哪一行就是选哪一篇。
    undecided: null,
    //: `saved` = 这一次操作已经写进去了。同样一颗按钮都不给:这一档的意思是「做完
    //: 了」,摆一颗「采集」等于请他再写一遍,而他此刻只需要看见那张回执。
    saved: null,
    refused: null,
  };

  //: **服务端只有这两档算「这一页此刻没有待做的采集」。** 关页许可的另一半依据
  //: (另一半是凭据,见 `granted()`)。
  //:
  //: **白名单不是黑名单**:以后多一档(或者某一档的含义变了),它自动不算做完 ——
  //: 漏给只是少一个出口,多给是关掉人家还没采的那一页,不可撤销。
  //:
  //: **不能复用 `PLANS[s] === null`**:那张表答的是「这一档摆不摆主动作」,而
  //: `refused` / `undecided` 同样没有主动作,却恰恰是「这一页还有事没定下来」。
  const SETTLED = {complete: true, saved: true};

  //: 关页前那一问等多久(毫秒)。它决定一次不可撤销的动作,所以**宁可到点认输**:
  //: 超时按「问不着」处理 = 拒绝关页,人再按一次。摆在这儿是因为它是这颗按钮的
  //: 一部分口径,不是网络层的默认值 —— 别顺手改成 `call()` 的全局超时,上传一篇
  //: 长正文、服务端自己去抓一页都可能正当地慢过它。
  const ASK_MS = 4000;

  //: **「这一档有没有主动作」只由 `PLANS` 答一处。** 按钮藏起来了不等于按不到
  //: (程序化点击、辅助技术、以后某次改动忘了藏),而 `complete` / `refused` /
  //: `undecided` / `saved` 这几档按下去会落进 `absent` 那一支 —— 那是拿 `/api/grab`
  //: 把同一篇收成第二行,正是候选列表存在要防的事。
  const actionable = () => !!(situation && PLANS[situation.situation]);

  // 人点开扩展最先想知道的事:**这一页此刻能做什么。** 一问一答 ——
  // 原先是「问 grab 这一页在不在库里」加一个「补回哪个待办」的下拉框,两份口径
  // 查条目的方式还不一样(一份规范形 vs 两份),迟早在屏幕上互相打脸。
  //: **许可跟着身份走,所以身份换好了才渲染。** 原来是先清空 `situation` 渲染一次、
  //: 再去问当前标签 —— 那一次用的是**上一页**的身份,于是「关掉这一页」按上一页的
  //: 许可继续摆着;而下面那一问失败时(503、断网、超时、401)它就停在那儿,`here`
  //: 却已经换成了人此刻正读的新页面。实测后果:点一下说「没有关掉它」,再点一下就
  //: 把那个还没采的新页面关掉了 —— 因为那时 `tab` 和 `here` 都是新页、两两相等。
  //: 身份先换、立刻重画,这一问成不成都不影响:许可那一刻就已经按新身份算过了。
  //: `signal` 可选:给**关页前那一问**用(见那颗按钮)。普通刷新不给 —— 那一下失败
  //: 只是屏幕上少一句话,人再点一次就是了;关页那一下不一样,它决定一次不可撤销的动作。
  async function loadHere(signal = null) {
    situation = null;
    try {
      here = await currentTab();
    } catch (error) {
      here = null;
      renderSituation();
      element('here-state').textContent = '这一页不能采集';
      element('here-url').textContent = error.message;
      return;
    }
    renderSituation();
    element('here-url').textContent = plainUrl(here.url);
    if (chosenFor !== here.url) chosen = chosenFor = '';
    situation = await api('/here', {method: 'POST', signal,
                                    body: JSON.stringify({url: here.url, intent_id: chosen})});
    //: **一旦服务端明确说这一页还有事没做完,就把许可就地撤掉 —— 撤销落在凭据上,
    //: 不是只落在这一答上。** `situation` 每问一次就换一份、问失败时是 `null`,所以
    //: 只靠「此刻那一答在不在白名单里」判的话,**那个「不允许」活不过下一次刷新**:
    //: 下一问失败(503、断网、超时)时它退回「未知」,而「未知」照旧沿用凭据 ——
    //: 于是已经收回去的按钮**重新露面、还接走焦点**,一个回车关掉的就是那一页。
    //: 实测形态:采完 A 之后给同一条目再发起一次采集,点「重新确认」按钮正确收回;
    //: 再点一次而这一问 503,按钮回来了,回车真发出了 `tabs.remove`(后端查:那个新
    //: 请求仍 `pending`、回执为空,只有上一次操作那张旧回执)。
    //:
    //: **撤了之后只有新的成功才能再授权**(`grant()` 那三条路),刷新一万次、服务端
    //: 下一答又说「齐了」都换不回来。
    //: **这一句只在服务端真答上来时才算数**(`situation &&` 管的是「答上来了、却是
    //: 一份空答复」)。那一问失败时 `api()` 抛出去、压根走不到这一行,凭据原样留着 ——
    //: 「没问着」不是「答的是还欠着事」,不许把一次有效的成功丢在一次网络抖动上。
    //: **但「留着」只够摆那颗按钮,不够关页**:按下去那一刻要的是「此刻确实没欠账」,
    //: 而不是「没听说有欠账」—— 那一道在 `granted()` 的 `unknownOk` 上,见那一段。
    if (situation && !SETTLED[situation.situation]) doneFor = null;
    //: **这里只读、不授权。** 原先 `saved` 那一答会就地 `grant(here, here.url, true)`,
    //: 理由是「上传那一答在路上丢了、或者服务重启了,人点一下「重新确认」也该有出口」。
    //: 那条路**必须撤掉**:服务端那张回执里只有文章和操作的身份,**没有 Chrome 标签
    //: 身份** —— 它说得出「这一篇的正文写进去了」,说不出「写进去的那一次发生在你眼前
    //: 这个标签里」。于是那一下把许可**挪到了人此刻在读的那个标签**上,随后 `granted()`
    //: 拿标签 id 去比的是它自己刚写进去的值,等于没比。
    //: 实测形态:在 71 号标签上采成,弹窗不关、当前标签变成同一地址的另一个 72 号
    //: (同一篇开了两个标签,或者采完那一个已经被他关了、眼前这个是后开的),按下去
    //: 关掉的是 72 —— 而 72 压根没参与这一次采集。
    //: **代价摆在这儿**:上传那一答真丢了的时候,屏幕上照旧写着「已经写进这一条了」,
    //: 但不给关页出口,人自己关掉那个标签。漏一个出口的代价是多按一下;关错一个标签
    //: 是人正在读的东西没了,不可撤销。
    //: 许可一律只由「这一次真写进去的那一份身份」建立(`grant()` 那三条路),只读刷新
    //: 只能**撤销**它(上一行),不能创建、不能移交。回归 `tests/popup_close_tab_gate.mjs`
    //: (d)(d2)。
    renderSituation();
    // 这一行**清空**:标题那一行就是服务端对这一页的答复,再在下面补一句「已连接」
    // 只是噪声(界面底线:让布局本身解释界面,不加次级描述)。`#status` 留给动作
    // 结果和错误 —— 调用方在 `loadHere()` **之后**写它,所以这里清不掉那些话。
    status('');
  }

  //: **采完了没有 —— 一律问那张凭据(`doneFor`),不看屏幕上这一刻摆着什么。**
  //: 凭据只有一个来源:**这一次真写进去的那一份身份**(`grant()` 那三条采集路径,各自
  //: 带上当时那个标签的 `{id, url}`)。服务端那一答 `saved` **不是**第二个来源 ——
  //: 回执里没有 Chrome 标签身份,拿它授权等于把许可挪给人此刻在读的那个标签,理由写在
  //: `loadHere()` 里那一段。
  //:
  //: **`situation` 和 `here` 都是可变的展示状态,谁也不能当凭据用。** 前者每问一次
  //: 就换一份、问失败时是 `null`;后者是「最近一次查到的当前标签」,人换一页它就
  //: 跟着换。拿它们授权就等于「刷新通常会成功」加「按钮刚才还在」,两条都不是
  //: 「这一页采成了」。
  //:
  //: 反过来一边同样钉住:**没采完不许露面**。只是开着弹窗看一眼「这一条早就齐了」
  //: 的人并没有一个「刚开出来、现在没用了」的标签,给他一颗关页按钮等于请他关掉
  //: 自己正在读的东西。
  //: **许可那一问只有一处 `granted()`:渲染和按下去共用它**,只是两处各自说清自己的
  //: 参照和自己对「不知道」的态度 —— 渲染问的是展示快照 `here`、「问不着」照旧摆着;
  //: 按下去问的是那一刻现读到的标签、「问不着」一律拒(关标签不可撤销,而两者之间
  //: 页面完全可能已经换了、服务端那边也完全可能已经又挂上一件没做完的事)。
  //:
  //: 它要三件事同时成立:
  //: - **凭据对得上这一页**(标签 + 地址,原样字符串相等);
  //: - **人没有换过操作**(`doneFor.intent === chosen`)。`chosen` 是他在候选列表里
  //:   点的那一条,采成之后不会自己变 —— 变只因为他点了别的那一行(`adoptHere`)。
  //:   这一维**不依赖服务端那一答**,所以换操作之后哪怕 `/here` 正好失败,旧许可
  //:   也当场作废;
  //: - **服务端此刻没说这一页还欠着事**(`SETTLED`)。
  //:
  //: **实测形态(缺最后两维)**:A 采成了,服务端那边 B 的待办也指着这一页;人点
  //: B 那一行的「这一页就是这一篇,改用它」,屏幕上同时写着 B「正文还缺着」和一颗
  //: 「关掉这一页」—— 一次采集都没再发生,按下去关掉的就是 B 还没采的那一页
  //: (后端查:B `pending`、回执为空、正文仍是 11 字)。同一条目上点一次「重新采集
  //: 正文」再回弹窗「重新确认」是同一个缺口:服务端这一答变回「正文还缺着」,而旧
  //: 许可照旧摆着。**一次真的成功,不该给后来另一项还没完成的操作授权。**
  //:
  //: **最后那一维有两档,调用方必须明说 —— 因为「问不着」在展示和消费上不是一回事。**
  //: `unknownOk` = 「服务端此刻答不上来」算不算放行:
  //: - **摆那颗按钮算**(`captureLanded()`)。那一问失败只是「不知道」,不是「有了新
  //:   操作」;按它收走出口的话,采完之后网络一抖那颗就没了,而那一页正是采集抽屉刚
  //:   为它开出来的。摆着它什么也关不掉 —— 按下去还要再问一次。
  //: - **按下去那一刻不算**。那一下是 `tabs.remove`,不可撤销,所以要的是「此刻确实
  //:   没欠账」,不是「没听说有欠账」:上一次成功只证明上一次写成了,证明不了这一篇
  //:   此刻没有更晚的欠账(服务端那一侧为此专门不答 `saved` 了),而 503 / 断网 /
  //:   超时正好把那句否决吞掉 —— 「什么都没变、只是断网」和「已经又发起了一次重采、
  //:   偏巧也断网」在弹窗这边长得一模一样。**分不清就不许关。**
  //:   实测形态:采完一篇后再为它发起重采,不点「重新确认」直接按这颗按钮,而这一问
  //:   503 —— 那一页正等着被重采,却被关掉了(后端独立查:新请求仍 `pending`、回执
  //:   为空)。代价不对称:拒了是人自己关一下那个标签,关错了是那一页再也采不成。
  //: 回归 `tests/popup_close_tab_gate.mjs` (g3)(g3b)。
  const granted = (tab, unknownOk) =>
    !!(doneFor && tab && tab.id === doneFor.id && tab.url === doneFor.url
       && doneFor.intent === chosen
       && (situation ? SETTLED[situation.situation] : unknownOk));
  const captureLanded = () => granted(here, true);

  //: **凭据只能由「这一次真写进去的那一份身份」建立 —— 三条路都过这一处。**
  //: - `url` 取**这一次真交给服务端的那个地址**:两条读页的路是 `extract.js` 在页面
  //:   里现报的 `location.href`(也正是 precheck / upload / from-page 收下的那一个),
  //:   服务端自己去抓那一路是 `/here` 那一答针对的地址,`saved` 那一路同理。
  //: - `tab` 取**这一次操作开头现读到的那一个标签**,不是 `here`。
  //:
  //: **不许拿 `here` 补造凭据。** 它是上一次 `/here` 留下的展示快照,而站点会在弹窗
  //: 开着的时候同源换篇(采失败之后自动翻下一篇最常见)。实测形态:在 A 上采失败、
  //: 站点悄悄换到 B、点「改用这一页的正文」—— 提取、预检、收录的全是 B(服务端那边
  //: B `known:true`、A `known:false`),凭据却记成了 A。于是**真采成的 B 没有出口**,
  //: 而人回到 A 点一次「重新确认」,服务端照旧说「这一页还不在知识库里」,那颗
  //: 「关掉这一页」反倒露着面、还接走了焦点 —— 一个回车关掉的是那篇**还没采的** A。
  //: 「请求和凭据取的是同一份展示快照」不等于对:两者一致只说明它们同源,不说明
  //: 那一份就是真写进去的那一页。
  //: **`stored` = 服务端说「这一次这一页的正文真写进去了」。** 没有它就不授予 ——
  //: 两个新条目入口(`/api/grab`、`/api/grab/from-page`)共用同一份写入响应契约,
  //: 而那份契约里**HTTP 200 不代表这一次写了正文**:撞上 `DO NOTHING`、库里只有
  //: 一段订阅同步先落下的 RSS 摘要、重抓失败、已经走完整条链的幂等答复,全都是
  //: 200 + `duplicate`。实测形态:打开扩展时这条还没收录,后台同步先收下它的摘要,
  //: 再点「采集」—— 服务端回 200 / `added:0` / `duplicate:true` / `content_ok:true`,
  //: 一个字的正文都没采到,随后那一问照旧说「正文还缺着」,而「关掉这一页」露着面、
  //: 接走了焦点,一个回车关掉的就是那篇**还没采的**原文。重抓明确失败时同样。
  //:
  //: **这一问只能由服务端答,扩展一个字都不自己判**(`stored_body`,`grab_api` 一处
  //: 算、两个入口共用)。从 `added` / `content_ok` / `done` 都推不出来,理由写在它
  //: 那一侧;而「正文存了、还没导出索引」仍然算采成,所以也不许要求 `done`。
  //:
  //: 写成函数声明(不是 `const`):三条调用方都在它下面,而写法一致比省一个关键字
  //: 要紧 —— 声明会提升,日后谁把某一条路挪到前面去也不会踩一个只在那条路上才出现
  //: 的 TDZ。
  function grant(tab, url, stored) {
    if (!stored) return;
    //: `intent` = 这一次是为**他选的哪一条**做的(`chosen`,没点过候选就是空串)。
    //: 消费时拿它跟此刻的 `chosen` 比:换一行就作废。见 `granted()`。
    doneFor = {id: tab.id, url, intent: chosen};
  }

  function renderSituation() {
    const plan = situation ? PLANS[situation.situation] : null;
    element('here-state').textContent = situation ? situation.detail : '正在确认这一页…';
    const title = (situation && situation.item && situation.item.title) || '';
    element('here-title').textContent = title;
    element('here-title').hidden = !title;
    element('here-actions').hidden = !plan;
    element('act-page').hidden = true;
    element('act-selection').hidden = !(plan && plan.selectionAtFirst);
    //: **「默认选中」= 这一刻它就是回车要走的那一下**,所以露面的那一次顺手收下
    //: 焦点。只在 `hidden` 真的翻过来时做:每 `renderSituation()` 一次就抢一次的话,
    //: 人刚把焦点挪到「重新确认」上就被拽回来了。
    const close = element('close-tab');
    const landed = captureLanded();
    if (landed && close.hidden) { close.hidden = false; wantFocus = 'close-tab'; flushFocus(); }
    else if (!landed) { close.hidden = true; if (wantFocus === 'close-tab') wantFocus = ''; }
    if (plan) {
      element('act').textContent = plan.act;
      // 名字一律先写上,**不管这一刻露不露面**:失败那条路上只改 `hidden`,它要是
      // 还指望那时候补名字,放出来的就是一颗没有字的按钮。
      element('act-page').textContent = '改用这一页的正文';
      element('act-selection').textContent = plan.selection;
    }
    renderElsewhere();
  }

  //: 等着的、但指着另一页的那些待办 —— **这一项是承重的**。站点按浏览器语言跳转
  //: 时(`openai.com/index/x` → `/zh-Hans-CN/index/x`)按网址一问是「不在库里」,
  //: 只凭这一句就请人收录的话,库里立刻多出一行同一篇文章。把那个待办摆出来、
  //: 给一颗「改用这一页的地址」,他一眼就看出是同一篇。
  //:
  //: **有几条就摆几条**(服务端一直交回全部,`here()` 只排掉这一答自己用掉的那个)。
  //: 原先只画 `elsewhere[0]`、adopt 也只认 `[0]`:两个条目的待办同时指着别处时
  //: (人把最终网址粘错了),他正站着的这一页偏偏是第二条,那就一颗按钮都对不上 ——
  //: 只能回采集抽屉里一条条改地址,而扩展这一侧存在的理由恰恰是不必回去。
  //:
  //: 每行自己那颗按钮在这里当场绑上:这些行每次重画都是新元素,**按 id 绑一次**的
  //: 写法重画之后点下去什么都不会发生。标题和网址一律 `textContent` —— 它们来自
  //: 订阅源,拿 innerHTML 拼等于把源站的字当标记送进弹窗这个有 `chrome.*` 的上下文。
  function renderElsewhere() {
    const others = (situation && situation.elsewhere) || [];
    element('elsewhere').hidden = !others.length;
    element('elsewhere-list').replaceChildren(...others.map(other => {
      const row = document.createElement('li');
      const title = document.createElement('p');
      title.className = 't';
      title.textContent = other.title;
      const address = document.createElement('p');
      address.className = 'url';
      address.textContent = plainUrl(other.page_url);
      const adopt = document.createElement('button');
      adopt.className = 'secondary';
      adopt.type = 'button';
      adopt.disabled = busy;
      adopt.textContent = '这一页就是这一篇，改用它';
      adopt.addEventListener('click', () => adoptHere(other.key));
      row.append(title, address, adopt);
      return row;
    }));
  }

  // 在那个标签上读一次正文。**补正文和收录共用**:同一个提取器、同一句错误话术。
  async function extractFrom(tab, mode) {
    await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['extract.js']});
    const results = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: method => {
      try { return readKnowbaseArticle(method); }
      catch (error) { return {error: error.message}; }
    }, args: [mode]});
    const captured = results[0]?.result;
    if (captured?.error) throw new Error(captured.error);
    if (!captured?.body) throw new Error('页面没有返回正文，未上传任何内容');
    return captured;
  }

  // 读完直接写进去。**没有预览区、也没有那道「我已核对」了。**
  //
  // 撤掉的是**事前那一问**,不是任何一道准入:长度、网址、条目版本、那条 UPDATE
  // 自己 WHERE 里的 CAS 全在服务端原样判(`_admit_body` / `_admit_page` /
  // `_admit_item`)。人就站在那一页上,他比屏幕清楚自己要的是哪一篇,再把同一份
  // 正文塞进 450px 宽的弹窗里请他重读一遍,拦不住任何服务端拦不住的东西。
  //
  // **代价是真的,记在这里**:登录墙、摘要页只要过得了那道去重 400 字符就会被写
  // 进去,原先指望那道勾挡一手。出路是事后的 —— 写完就把标题和字数摆出来,不对
  // 就到知识库里那一条上「重新采集正文」。完整取舍见
  // docs/decisions/browser-capture.md。
  async function read(mode) {
    if (busy) return;
    setBusy(true);
    try {
      const intent = situation && situation.intent;
      if (!intent) throw new Error('这一页没有要补回的条目，请重新确认这一页');
      const tab = await currentTab();
      // 读之前先问服务端:这个请求还在不在、这一页是不是它指定的那一篇、条目还是
      // 绑定的那一版吗。不通过就一个字都不读 —— 否则人读完整页才被拒(实测三种:
      // 网址带中文、复读提示页、读的那几分钟里条目被同步改过)。
      await api('/precheck', {method: 'POST', body: JSON.stringify({intent_id: intent.id, page_url: tab.url, method: mode})});
      const captured = await extractFrom(tab, mode);
      // 再问一次,这次带上正文:读的过程中跳转了吗、这份正文过得了入库那道门槛吗。
      // 还留着是因为**它答出字数**(服务端按行去重之后那一份),写完要摆给人看;
      // 而两次 precheck 与 save 过的是同一组准入,这里先问一次只是早一道。
      const verified = await api('/precheck', {method: 'POST', body: JSON.stringify(
        {intent_id: intent.id, page_url: captured.page_url, body: captured.body, method: mode})});
      const result = await api('/upload', {method: 'POST', body: JSON.stringify({...captured, intent_id: intent.id})});
      //: **写成功之后不许把身份放掉。** 放掉之后下一问只按网址和「还剩哪几个待办」
      //: 重判,同一颗「采集」会被另一篇接管:实测两篇都指着这一页,保存完他选的那一
      //: 篇、不做任何重新选择再按一下,正文就写进了另一篇(那一篇 content_rev 0→1、
      //: 正文是这一页的)。服务端认得这张回执(`saved` 那一档),所以带着它去问得到
      //: 的是「这一次采集已经写进这一条了」,而不是「你选的那个失效了」。要换另一篇
      //: 得他自己在候选列表里点一下 —— 那正是这颗按钮不该替他决定的事。
      //: **在刷新之前记下「采完的是哪一页」。** `loadHere()` 会重问服务端,而它那
      //: 一答在最常走的那条路上是 `complete`(没点过「改用它」→ 没有身份 → 服务端
      //: 跳过回执查找) —— 光等 `saved` 的话「关掉这一页」在那条路上永远不露面。
      //: `/upload` 这一路**没有「200 却没写」那一档**:它要么抛错(上面那个 `api()`
      //: 会把非 2xx 变成异常),要么就是把这一份正文写进了那个条目 —— 回执由服务端
      //: 当场记下,`saved` 那一档随后认得它。所以这里的事实就是「成功返回」本身。
      grant(tab, captured.page_url, true);
      // 已经写进去了。这一问失败只是「状态没刷新」,不该让人以为正文没保存。
      try { await loadHere(); } catch { /* 下面照旧报服务端那句话 */ }
      status(`${captured.title}：${result.detail}（${verified.characters} 字符）`);
    } catch (error) {
      const retry = !error.status || error.status >= 500
        ? '。若连接中断，可再采一次；服务端不会重复替换。' : '';
      status(error.message + retry, true);
    }
    finally { setBusy(false); }
  }

  // 主按钮一颗,**做哪件事由服务端那一答定**(`situation`):
  // - `fill`(库里有这一条、正文还缺着)→ 直接读这一页。不必先让服务端去抓一次:
  //   会落到这一档恰恰是因为自动补抓已经抓不到了。
  // - `absent`(不在库里)→ 先让服务端自己去抓(走完整的八道准入,还能取到标题、
  //   作者、日期),抓不到才轮到下面那两颗兜底。
  element('act').addEventListener('click', async () => {
    if (busy || !actionable()) return;
    if (situation.situation === 'fill') return read('article');
    // 下面是 `absent` 那一档。**不在这里判是为什么抓不到**:无论登录墙、关卡
    // 还是脚本渲染,人的下一步都一样,而他正看着这一页。
    setBusy(true);
    try {
      // 标签现读一次:凭据的那一半身份要来自**此刻**,不是上一次 `/here` 留下的快照。
      const tab = await currentTab();
      const result = await grabApi('', {method: 'POST', body: JSON.stringify({url: here.url})});
      //: 三条路都算采完(这一条是服务端自己去抓的那条)。判据是**这一页的内容已经
      //: 进知识库了**,不是「这个标签是谁开的」—— 后者扩展根本无从知道,而真要按它
      //: 分流,同一颗按钮会在三条路上时有时无,人看不出规律。
      //: 地址用**服务端这一答针对的那一个**(`here.url`,也就是刚交给它去抓的那一个):
      //: 标签这期间要是换了页,凭据就和当前这一页对不上,那颗按钮自己不露面 —— 那正是
      //: 想要的,收进去的毕竟是另一页。
      grant(tab, here.url, result.stored_body);
      // 同下面提交那一处:收下之后刷新失败不改变已经收下这件事,更不该把
      // 下面那两颗兜底按钮露出来 —— 那会让人把已入库的这一页再读一遍正文。
      try { await loadHere(); } catch { /* 照旧报「已收下」 */ }
      status(result.detail || '已收下');
    } catch (error) {
      status(error.message, true);
      // 两颗一起露出,因为**服务端抓不到**和**它自动识别不出正文**是两回事:
      // 前者给「改用这一页的正文」就够了,后者(整页是脚本渲染的壳、正文在
      // iframe 里、页面没有能认出的文章容器)只有人自己选中那一段才取得到。
      // 只给前一颗的话,`extract.js` 回的「请选好全文再读一次」就指向一颗
      // 不存在的按钮 —— 人照着做完,无处可点。
      element('act-page').hidden = false;
      element('act-selection').hidden = false;
    }
    finally { setBusy(false); }
  });

  element('act-page').addEventListener('click', () => actionable() && collect('article'));
  // 这一颗在两档里都有,各归各家:`fill` 走补回原条目(`/upload`),`absent` 走
  // 收录成新条目(`/from-page`)。**判据仍在服务端** —— 这里只是按它那一答选路。
  element('act-selection').addEventListener('click', () =>
    actionable() && (situation.situation === 'fill' ? read : collect)('selection'));

  // 用这一页的正文收录。和补正文那条路同一套:先问服务端(不通过就一个字都不读
  // 第二遍)、字数用服务端那一份、读完直接收录。
  async function collect(mode) {
    if (busy || !here) return;
    setBusy(true);
    try {
      // **从现读到的那个标签提取,不从 `here` 提。** 后者是上一次 `/here` 的快照,
      // 站点在这中间换过篇的话,它的 `id` 可能已经不是人眼前这一个了。
      const tab = await currentTab();
      const captured = await extractFrom(tab, mode);
      const payload = {url: captured.page_url, title: captured.title, body: captured.body};
      const verified = await grabApi('/from-page/precheck', {method: 'POST', body: JSON.stringify(payload)});
      const result = await grabApi('/from-page', {method: 'POST', body: JSON.stringify(payload)});
      grant(tab, captured.page_url, result.stored_body);   // 同上:这一次真写进去了才算
      // 已经收下了。这一问失败只是「状态没刷新」—— 不该让人以为没收下。
      try { await loadHere(); } catch { /* 下面照旧报「已收下」 */ }
      status(`${verified.title}：${result.detail || '已收下'}（${verified.characters} 字符）`);
    } catch (error) { status(error.message, true); }
    finally { setBusy(false); }
  }

  // 「这一页就是这一篇,改用它」—— 和采集抽屉上那颗「改用这个地址」同一个动作、
  // 同一个 `create`(旧待办由服务端顶掉,不是先取消再发起),只是搬到了人所在的
  // 地方。**不是放行**:正文照旧要过服务端那一整套准入。
  async function adoptHere(key) {
    if (busy || !here) return;
    setBusy(true);
    try {
      // **接住交回来的那个待办。** 它就是「他选了哪一篇」的身份凭证;扔掉它、转头
      // 只按网址再问一次,服务端就只能在同页那几个待办里代选 —— 见 `chosen` 那段。
      const adopted = await api('/here/adopt', {method: 'POST',
                                                body: JSON.stringify({key, page_url: here.url})});
      chosen = adopted.id;
      chosenFor = here.url;
      await loadHere();
      status('已改用这一页的地址。');
    } catch (error) { status(error.message, true); }
    finally { setBusy(false); }
  }
  element('connect').addEventListener('click', async () => {
    if (busy) return;
    setBusy(true);
    try {
      server = localServer(element('server').value.trim());
      key = element('key').value.trim();
      if (!/^[A-Za-z0-9_-]{40,80}$/.test(key)) throw new Error('请完整复制本机知识库显示的配对密钥');
      // **先用一次不依赖当前标签的鉴权调用验密钥**(`/pending` 只读、不建任何
      // 东西),再去问这一页。拿 `/here` 验不行:人在扩展管理页或知识库页面上
      // 配对时 `currentTab()` 就先拒了,于是密钥对不对一个字都没验、还因为没
      // 抛出而被当成连上了,密钥照旧存进本地。
      await api('/pending');
      await chrome.storage.local.set({server, key});
      await loadHere();
      element('connection').open = false;
    } catch (error) { status(error.message, true); }
    finally { setBusy(false); }
  });
  //: 「关掉这一页」—— 采完之后那一页就没用了(抽屉里「同时在新标签打开这一篇」
  //: 默认勾着,它多半就是刚刚为这次采集开出来的那个标签)。
  //:
  //: **关的永远是当前这一个标签,不枚举也不碰别的。** `chrome.tabs.remove` 不需要
  //: 任何额外权限(`tabs` 那一条只管 `url`/`title` 这几个敏感字段),清单一个字不改。
  //:
  //: **按下去这一刻现问一次当前标签,跟那张凭据对,对不上就不关。** 这颗按钮露不
  //: 露面是**上一次渲染**时算的,而关标签不可撤销 —— 两者之间标签完全可能已经不是
  //: 那一页了:站内 SPA 路由、站点采完自动翻下一篇、他自己点开了另一条。
  //:
  //: **对的是 `doneFor`,不是 `here`。** 后者是「最近一次查到的当前标签」,人换一页
  //: 它就跟着换:拿它当参照时,`tab` 和 `here` 会一起变成那个还没采的新页面、两两
  //: 相等、校验通过,于是关掉的正是他此刻在读的东西。凭据反过来是**只在真采成那
  //: 一刻写下来的**,屏幕上后来发生什么都不会改它。
  //:
  //: 对不上就改为重新问一次服务端(那一下会把这颗按钮自己收回去)并说明为什么没关。
  //: **判据是原样字符串相等,不做任何规范化** —— 这里只决定「关不关」,宁可多留一个
  //: 标签,也不能关错一个。
  //:
  //: **按下去那一刻也现问一次服务端,问完再决定。** 凭据证明的是「那一次写成功了」,
  //: 证明不了「这一页此刻没有更晚的欠账」:采完之后他去采集抽屉里点了一次「重新采集
  //: 正文」再回来按这颗按钮,屏幕上这一份答复早就过期了(它停在采成那一刻)。只在
  //: 「重新确认」那一下更新的话,这中间这一下关掉的就是一页正等着被重采的原文。
  //: 服务端那一侧另有自己的一道(旧回执不为新欠账背书,`browser_capture.here`),
  //: 两份实现各有自己的回归 —— 这里这一道管的是「他压根没再问过」那一段窗口。
  //:
  //: **而「问不着」在这一下是否决。** 服务端那道否决正是靠这一问送过来的,503 / 断网 /
  //: 超时把它一并吞掉:弹窗这边「什么都没变、只是断网」和「已经又发起了一次重采、偏巧
  //: 也断网」长得一模一样,而前者关掉的是一个没用的标签、后者关掉的是一页正等着被重采
  //: 的原文(那一页一关,那个待办再也兑现不了 —— 它只有人站在那一页上才采得成)。
  //: 分不清就不许关:拒了是人自己关一下那个标签,关错了不可撤销。**保留的是回执和那句
  //: 「已经写进这一条了」,收回的只是这一下的许可。**
  //: 口径那一维在 `granted(tab, false)` 上(展示那一侧照旧 `true`),所以这里不另判
  //: 一次「问着了没有」—— `situation` 只用来挑话说。
  //:
  //: **这一问还得有界。** 服务端接了连接却迟迟不答(进程卡在维护锁上、磁盘满)时
  //: `fetch` 自己不会醒,而这一下已经把按钮按住了(`busy`):没有截止时间的话,人等到的
  //: 是一颗永远按不动的按钮,连「再试一次」都点不了。到点按「问不着」处理 —— 那是拒绝,
  //: 不是放行。本机一问一答是毫秒量级,`ASK_MS` 留的是人等得起的上限。
  //:
  //: 标签一关,弹窗多半跟着就没了(它挂在那个标签的操作上);`window.close()` 管
  //: 它没走的那一种,不然屏幕上会留一个指着不存在的页面的弹窗。
  element('close-tab').addEventListener('click', async () => {
    if (busy) return;
    setBusy(true);
    try {
      //: **挑话用的是撤销**前**那一份凭据。** `loadHere()` 读到「这一页还有事没做完」
      //: 会把它清掉,而那时人压根没换页 —— 拿清完之后的 `doneFor` 挑话,说出来的是
      //: 「这一页已经不是刚才采的那一篇了」,下一步指错了方向。
      const held = doneFor;
      try { await loadHere(AbortSignal.timeout(ASK_MS)); }
      catch { /* 问不着 —— 下面那一道 `granted(tab, false)` 会拒,这里不代它判 */ }
      //: 标签读得越晚越好:这一问是一次网络往返,中间站点完全可能自己翻了下一篇。
      const tab = await currentTab();
      if (!granted(tab, false)) {
        //: **拒了之后再挑话说,判据仍然只有 `granted()` 一处。** 三句分得清是因为
        //: 人要做的下一步不同:换页了就去把那一页找回来;这一页还有事没做完就是先把
        //: 它采完;问不着就是再按一次(多半下一下就通了)。换页排在最前面 —— 身份
        //: 对不上时,先说哪一页才是他要关的那一页比先说网络更有用。
        const moved = !!held && (tab.id !== held.id || tab.url !== held.url);
        status(moved ? '这一页已经不是刚才采的那一篇了，没有关掉它。'
               : !situation ? '没能跟知识库确认这一页此刻的状态，没有关掉它。请再按一次。'
               : '这一页还有一件没采完的事，没有关掉它。', true);
        return;
      }
      await chrome.tabs.remove(tab.id);
      window.close();
    } catch (error) { status(error.message, true); }
    finally { setBusy(false); }
  });

  element('refresh').addEventListener('click', async () => {
    if (busy) return;
    setBusy(true);
    try { await loadHere(); } catch (error) { status(error.message, true); }
    finally { setBusy(false); }
  });

  async function initialize() {
    setBusy(true);
    // 版本号摆在头部:这个扩展是「加载已解压的扩展程序」装的,改了代码要人去扩展
    // 管理页点一次「重新加载」。装的是哪一版只有这里看得出来 —— 没有它,「我到底
    // 重载过没有」只能靠猜。
    //
    // **单独一个 try,不许挡住下面那一整段。** 它纯粹是块装饰;放在主流程里的话,
    // 一旦取不到清单(宿主没给 `chrome.runtime`),整个初始化当场跳进 catch ——
    // 人看到的是「连接本机知识库」摊开着、一句报错,而这一页其实好端端的。
    try { element('version').textContent = 'v' + chrome.runtime.getManifest().version; }
    catch { /* 显示不出版本号不影响采集 */ }
    try {
      const stored = await chrome.storage.local.get(['server', 'key']);
      element('server').value = stored.server || 'http://127.0.0.1:8787';
      element('key').value = stored.key || '';
      server = localServer(element('server').value);
      key = stored.key || '';
      if (!key) { element('connection').open = true; status('首次使用：连接本机服务并填写配对密钥。'); return; }
      await loadHere();
    } catch (error) { element('connection').open = true; status(error.message, true); }
    finally { setBusy(false); }
  }
  initialize();
})();
