/** 从浏览器采集 · 左侧抽屉的外壳。
 *
 *  **这块原先是一个独立页面**(`/browser-capture?item=<key>`),从「我的订阅」或
 *  「问题与待办」点进去要跳出当前页面;补完正文再想回到刚才看的那一条,得自己找
 *  回来。现在它是贴在左边的抽屉,底下那一页原样留着。
 *
 *  **DOM 由这里建,不写进 `index.html`。** 那个文件已经 694 行,逼着 700 行上限;
 *  更要紧的是这一块只有点开采集才用得上,常驻在文档里只是让每次解析都多读一遍。
 *
 *  **每个 id 都带 `cap-` 前缀。** 独立页那会儿用的是 `title`、`message`、`create`、
 *  `pending` 这类裸名字 —— 搬进主界面就会跟别人撞上,而 `getElementById` 撞车不报
 *  错,只是安静地拿到另一个元素。
 */
(() => {
  'use strict';
  let drawer = null;
  let mask = null;
  let watcher = null;
  //: 和状态机那边同一个办法:每次打开是新的一代。外壳这边的异步续体是读扩展目录
  //: 那一问和「显示 / 复制配对密钥」—— 它们回来时只问「现在有没有抽屉」是不够的,
  //: 关掉再打开之后那是**另一个**抽屉,旧续体会往新抽屉里写。
  let generation = 0;

  //: 抽屉里那一整套。`cap-setup` 那一块的位置是会变的:这个浏览器还没配过扩展时它
  //: 在最上面且摊开(见 `renderSetup`),配过之后收进底部的 <details>。
  //:
  //: **不加次级标题,也不复述下面那样东西是什么。** 原先每块都顶着一行标题(「这次
  //: 补哪一篇」「这篇文章正在等待的采集」),每个输入框下面还跟一句说明它行为的小字
  //: (「默认匹配原网址」—— 那一栏本来就是预填好的)。留下来的只有**屏幕上看不出来
  //: 的那些**:15 分钟的有效期、「继续导出与索引」那一下的真实射程(它和确认框
  //: 必须对得上,`tests/test_browser_capture.py` 钉着)。
  const TEMPLATE = `
  <div class="cap-head">
    <b>从浏览器采集</b>
    <button class="cap-close" type="button" aria-label="关闭采集抽屉">✕</button>
  </div>
  <div class="cap-scroll">
    <div id="cap-setup-slot"></div>
    <p id="cap-message" role="status" aria-live="polite">正在读取目标条目…</p>
    <div class="cap-block">
      <div class="cap-block-head"><h3 id="cap-title">等待条目</h3><span id="cap-kind"></span></div>
      <p id="cap-original-url" class="url"></p>
      <label for="cap-page-url">已打开文章的最终网址</label>
      <input id="cap-page-url" type="url" placeholder="https://…" disabled>
      <label class="cap-also"><input id="cap-also-open" type="checkbox">同时在新标签打开这一篇</label>
      <div class="actions">
        <button id="cap-create" type="button" disabled>发起浏览器采集</button>
        <button id="cap-draft-cancel" class="secondary" type="button" hidden>退出重新采集</button>
      </div>
      <p id="cap-intent-note" class="hint"></p>
      <div id="cap-pending" hidden>
        <h2>正在等待的采集</h2>
        <ul id="cap-pending-list" class="pending-list"></ul>
      </div>
    </div>
    <div id="cap-saved" class="cap-block" hidden>
      <h2>正文已补入，入库还没结束</h2><p id="cap-receipt" class="mono"></p>
      <p class="warning">「继续」跑导出与索引两段：同类来源其他待办会一起导出，可能联网补抓并调用翻译模型，索引检查全库。条目化提取不在这一步。</p>
      <div class="actions">
        <button id="cap-continue" type="button">继续导出与索引</button>
        <button id="cap-restart" class="secondary" type="button">重新采集正文</button>
      </div>
      <p id="cap-run-note" role="status"></p>
    </div>
    <div id="cap-setup-folded"></div>
    <p class="hint">待采集请求 15 分钟有效，服务重启要重新发起；已保存的回执不受此限，关掉抽屉也找得回来。</p>
  </div>`;

  //: 装扩展那一块。两种摆法共用这一份 HTML —— 各写一遍的话,改一处文案就会留下
  //: 另一处的旧说法,而人看到的恰恰是他那一种。
  const SETUP = `
    <h2>首次使用：装扩展并配对</h2>
    <ol>
      <li>在 Chrome 扩展管理页(<code>chrome://extensions</code>)打开右上角「开发者模式」。</li>
      <li>点「加载已解压的扩展程序」，选中下面这个目录——<b>就在这台机器上，不用下载</b>：</li>
    </ol>
    <div class="cap-path">
      <p class="mono" id="cap-ext-path">读取中…</p>
      <button id="cap-copy-path" class="secondary" type="button">复制路径</button>
    </div>
    <p class="hint">改过扩展代码就去管理页点一次「重新加载」，版本号在弹窗头部。浏览器不在这台机器上才需要
      <a class="download" href="/api/browser-capture/extension.zip" download>下载 ZIP</a>。</p>
    <ol start="3">
      <li>扩展里填本机服务地址 <code id="cap-service-url"></code>，再粘贴下面的配对密钥。</li>
    </ol>
    <div class="actions"><button id="cap-pair" class="secondary" type="button">显示本机配对密钥</button></div>
    <div id="cap-pairing" hidden>
      <label for="cap-pairing-key">配对密钥</label>
      <input id="cap-pairing-key" readonly type="password" autocomplete="off">
      <button id="cap-copy-key" class="secondary" type="button">复制密钥</button>
      <p class="hint">只留在本机，别发给网站或他人。</p>
    </div>`;

  //: **建了就 append、关了就 remove**,不是切 `display`。workspace.js 的
  //: MutationObserver 只盯**页面初始那批**浮层的 style 变化,后来挂上去的那个改
  //: display 它看不见;而 body 的 childList 它盯着,所以建删这条路上 `syncOverlays`
  //: 照旧会跑 —— 焦点陷阱、inert、Esc 关闭链全靠它。先例是 `egress.js` 那一块。
  function build() {
    generation += 1;
    mask = document.createElement('div');
    mask.id = 'cap-mask';
    mask.addEventListener('click', closeCaptureDrawer);
    drawer = document.createElement('aside');
    drawer.id = 'cap-drawer';
    drawer.setAttribute('role', 'dialog');
    drawer.setAttribute('aria-modal', 'true');
    drawer.setAttribute('aria-label', '从浏览器采集');
    drawer.innerHTML = TEMPLATE;
    drawer.querySelector('.cap-close').addEventListener('click', closeCaptureDrawer);
    document.body.append(mask, drawer);
    //: **配对是在另一个窗口里完成的**(扩展弹窗),而这一块引导的位置正按它摆。不盯着
    //: 那个标记的话,人配完回到这一页,引导还整块摊在顶上 —— 他刚做完的事在界面上
    //: 没有任何回应,得关掉再打开才看得出来。反过来一边同理(密钥被清掉、改了服务
    //: 地址),那时引导必须自己回到顶上。
    watcher = new MutationObserver(renderSetup);
    watcher.observe(document.documentElement,
                    {attributes: true, attributeFilter: ['data-knowbase-capture']});
  }

  //: **「这个浏览器里装了扩展、而且配过」** —— 只有扩展自己知道,所以它在回环地址
  //: 的页面上标一个 data 属性(`present.js`),这里读那一个。
  //:
  //: 原先问的是服务端 `/setup` 的 `used_before`(审计表里有没有过一行)。那是**另一个
  //: 范畴的事实**:回执是全库共享的历史,而这里要决定的是此刻这个浏览器要不要看引导。
  //: 错位出来的两种形态都真会发生 —— 另一个浏览器采成过一次,这个还没配对的就看不到
  //: 引导了;反过来,只用「改用这一页的正文」收录新链接的人一直有配对,那条路却只写
  //: `body_provenance`、从不插审计表,引导永远摊在他眼前。
  const paired = () => !!document.documentElement.dataset.knowbaseCapture;

  //: 没配过 → 摆在最上面、摊开;配过 → 收进底部那个折叠块。
  //: **位置和折叠状态一起变**:只把 <details> 默认打开的话,它还是在整屏最下面,
  //: 而首次使用的人恰恰是最需要先看到它的那一个 —— 扩展没装、密钥没配,上面那栏
  //: 网址和「发起采集」对他毫无意义,点下去只会等一个永远不来的采集。
  //:
  //: **位置先定下来,不等服务端那一问。** 判据本来就是本地事实;等 `/setup` 回来再摆
  //: 的话,这一大块会在首次使用的人眼前跳一下,而那一问只是去取装哪个目录。
  async function renderSetup() {
    const gen = generation;
    const top = document.getElementById('cap-setup-slot');
    const folded = document.getElementById('cap-setup-folded');
    if (!top || !folded) return;
    if (paired()) {
      folded.innerHTML = `<details><summary>装扩展与配对</summary>${SETUP}</details>`;
      top.replaceChildren();
    } else {
      top.innerHTML = `<section class="cap-setup">${SETUP}</section>`;
      folded.replaceChildren();
    }
    const service = document.getElementById('cap-service-url');
    if (service) service.textContent = location.origin;
    bindSetup(gen);
    let directory = '';
    try { directory = (await (await fetch('/api/browser-capture/setup', {cache: 'no-store'})).json()).extension_dir; }
    catch { /* 下面按读取失败办,改让他下载 ZIP */ }
    if (!current(gen)) return;
    const path = document.getElementById('cap-ext-path');
    if (path) path.textContent = directory || '（读取失败，请改用下载 ZIP）';
  }

  function bindSetup(gen) {
    const copyPath = document.getElementById('cap-copy-path');
    if (copyPath) copyPath.addEventListener('click', async () => {
      const value = document.getElementById('cap-ext-path').textContent;
      try {
        await navigator.clipboard.writeText(value);
        // 剪贴板那一问可以迟迟不决(权限弹窗、系统忙)。人这时关掉抽屉、打开下一篇,
        // 这句迟到的提示就落在了下一篇头上 —— 和写动作同一道闸。
        if (current(gen)) toast('路径已复制，在扩展管理页「加载已解压的扩展程序」里粘贴。', {type: 'ok', key: 'cap-path'});
      } catch {
        if (current(gen)) toast('无法自动复制，请手动选中上面那行路径。', {type: 'info', key: 'cap-path'});
      }
    });
    const pair = document.getElementById('cap-pair');
    if (pair) pair.addEventListener('click', async () => {
      try {
        const response = await fetch('/api/browser-capture/pairing', {cache: 'no-store'});
        const pairing = await apiResponse(response, '读取配对密钥失败');
        // 人关掉抽屉就是收回了「把密钥显示出来」那一下:这一答迟到之后不许自己
        // 摊在下一次打开的抽屉上(它可能已经在别人眼前了)。
        if (!current(gen)) return;
        document.getElementById('cap-pairing-key').type = 'password';
        document.getElementById('cap-pairing-key').value = pairing.key;
        document.getElementById('cap-pairing').hidden = false;
      } catch (error) { if (current(gen)) toast(error.message, {key: 'cap-pairing'}); }
    });
    const copyKey = document.getElementById('cap-copy-key');
    if (copyKey) copyKey.addEventListener('click', async () => {
      const field = document.getElementById('cap-pairing-key');
      try {
        await navigator.clipboard.writeText(field.value);
        if (current(gen)) toast('密钥已复制，只粘贴到你安装的 Knowbase 扩展中。', {type: 'ok', key: 'cap-copy'});
      } catch {
        // 同上。失败这一支还会**动 DOM**:迟到之后 `select()` 选的是早就 remove 掉的
        // 那个输入框,而屏幕上那句「请复制选中的配对密钥」指着一处什么都没选中的地方。
        if (!current(gen)) return;
        field.type = 'text';
        field.select();
        toast('无法自动复制，请复制选中的配对密钥。', {type: 'info', detail: '按 Cmd-C（Windows 为 Ctrl-C）复制已选中的文字。', key: 'cap-copy'});
      }
    });
  }

  const current = gen => gen === generation;

  function openCaptureDrawer(itemKey) {
    if (drawer) closeCaptureDrawer();
    build();
    // Esc 走全站那一条分派链(`workspace.js` 按最上层浮层找这张表),不自己再挂一个
    // keydown —— 两处都关的话,叠在抽屉之上的确认框按一次 Esc 会连抽屉一起带走。
    overlayClosers['cap-drawer'] = closeCaptureDrawer;
    renderSetup();
    window.startCapture(itemKey);
  }

  //: 关掉要**把轮询一起停了**。独立页那会儿关掉标签就什么都没了;抽屉不一样 ——
  //: 底下那一页还活着,`setTimeout` 链不停就会一直每 2 秒打一次接口,开过几次之后
  //: 变成几条并行的链。
  function closeCaptureDrawer() {
    if (!drawer) return;
    generation += 1;
    //: 跟轮询一样要停:底下那一页还活着,不断开的话这个已经没了的抽屉还会在每次
    //: 标记变化时跑一遍 `renderSetup`(白打一次 `/setup`,再往早就 remove 掉的那套
    //: DOM 里写),开过几次就是几个。
    watcher.disconnect();
    watcher = null;
    window.stopCapture();
    mask.remove();
    drawer.remove();
    mask = null;
    drawer = null;
  }

  window.openCaptureDrawer = openCaptureDrawer;
  window.closeCaptureDrawer = closeCaptureDrawer;
})();
