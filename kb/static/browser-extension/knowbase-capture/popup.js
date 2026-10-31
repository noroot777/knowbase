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

  function status(message, error = false) {
    element('status').textContent = message;
    element('status').classList.toggle('error', error);
  }

  function setBusy(value) {
    busy = value;
    for (const name of ['act', 'act-page', 'act-selection'])
      element(name).disabled = value || !situation;
    for (const name of ['connect', 'refresh']) element(name).disabled = value;
    // 「改用它」有几颗要看服务端交回几个待办,所以从那张列表上现读,不记一份名单。
    for (const button of element('elsewhere-list').querySelectorAll('button')) button.disabled = value;
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
    refused: null,
  };

  //: **「这一档有没有主动作」只由 `PLANS` 答一处。** 按钮藏起来了不等于按不到
  //: (程序化点击、辅助技术、以后某次改动忘了藏),而 `complete` / `refused` /
  //: `undecided` 这三档按下去会落进 `absent` 那一支 —— 那是拿 `/api/grab` 把同一篇
  //: 收成第二行,正是候选列表存在要防的事。
  const actionable = () => !!(situation && PLANS[situation.situation]);

  // 人点开扩展最先想知道的事:**这一页此刻能做什么。** 一问一答 ——
  // 原先是「问 grab 这一页在不在库里」加一个「补回哪个待办」的下拉框,两份口径
  // 查条目的方式还不一样(一份规范形 vs 两份),迟早在屏幕上互相打脸。
  async function loadHere() {
    situation = null;
    renderSituation();
    try {
      here = await currentTab();
    } catch (error) {
      here = null;
      element('here-state').textContent = '这一页不能采集';
      element('here-url').textContent = error.message;
      return;
    }
    element('here-url').textContent = plainUrl(here.url);
    if (chosenFor !== here.url) chosen = chosenFor = '';
    situation = await api('/here', {method: 'POST',
                                    body: JSON.stringify({url: here.url, intent_id: chosen})});
    renderSituation();
    // 这一行**清空**:标题那一行就是服务端对这一页的答复,再在下面补一句「已连接」
    // 只是噪声(界面底线:让布局本身解释界面,不加次级描述)。`#status` 留给动作
    // 结果和错误 —— 调用方在 `loadHere()` **之后**写它,所以这里清不掉那些话。
    status('');
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
      // 这一个待办已经用掉了,下一问不该再指名它(指名一个用掉的会被答成
      // `undecided`,而人此刻只想看见刚写进去那张回执)。
      chosen = chosenFor = '';
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
      const result = await grabApi('', {method: 'POST', body: JSON.stringify({url: here.url})});
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
      const captured = await extractFrom(here, mode);
      const payload = {url: captured.page_url, title: captured.title, body: captured.body};
      const verified = await grabApi('/from-page/precheck', {method: 'POST', body: JSON.stringify(payload)});
      const result = await grabApi('/from-page', {method: 'POST', body: JSON.stringify(payload)});
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
