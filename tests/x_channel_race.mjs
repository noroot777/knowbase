// X 通道面板上**异步回包的归属**那一族回归:读、动作 POST、保存 PUT 三条路上的
// 回包交错时,谁有资格往面板上写。口径和每一条的事故形态见
// `docs/decisions/frontend-feedback.md`(这一族被拦下来过五轮,每轮都是「又漏了一个
// 落地点」),这里只摆时序。
// 外壳和判据工具在 `x_channel_kit.mjs`;**入口是 `x_channel_probe.mjs`**,它按顺序
// 调下面这个函数 —— 单独跑这一份什么都不会发生。
import { evalJs, sleep, until, clickAt, check, shown, pick, picked, text, fill, state }
  from "./x_channel_kit.mjs";
import { posted, saved, mock } from "./x_channel_mock.mjs";

export async function ownershipSections() {
  // 4.6 「从日常浏览器导入登录态」
  await evalJs(`fetch('/__probe/stamps?reset=1').then(() => true)`);
  check("「导入登录态」真点得到", await clickAt("#source-x-import-btn"));
  // 趁 mock 还没回,先把那句预告读下来(见 mock 里那段 1.2 秒的理由)。
  const importPreface =
    await evalJs(`document.getElementById('source-x-import-state').textContent`);
  await until(`document.getElementById('source-x-import-state')
                 .textContent.includes('上次从日常浏览器导入')`, 20000);
  // 方法要对:真路由只收 POST,发 GET 的话这颗按钮在真页面上永远是 405
  // (口径同「检查连接」那两条,2026-10-08 实测过一次)。
  check("导入请求打的是 import-login 端点,而且是 POST",
        posted.includes("POST /api/intake/social/x-channel/import-login"), posted.join());
  check("导入没有发出任何 GET",
        !posted.some(p => p.startsWith("GET /api/intake/social/x-channel/import-login")),
        posted.join());
  // **留痕那行字要落在页面上。** 它是这个功能唯一的"上次什么时候搬的"露头 ——
  // 不显示的话用户只能靠记忆判断该不该再导一次(cookie 过期是常态)。
  check("留痕落到面板上(带时刻和条数)",
        (await evalJs(`document.getElementById('source-x-import-state').textContent`))
          .includes("13 个 cookie"),
        await evalJs(`document.getElementById('source-x-import-state').textContent`));
  // **按下去之后要先预告那个授权弹窗。** 这一步会停在弹窗上最长 90 秒,而弹窗出现
  // 在另一个窗口里 —— 不预告的话用户看到的是一颗按下去没反应的按钮。
  check("点下去立刻预告了那个授权弹窗",
        importPreface.includes("授权"), importPreface);
  // **导入成功之后,就绪那一行不许还说导入之前的话。** 页面上同时挂着「当场验证
  // 通过」和「浏览器通道还没有登录过 X」时,后者说的是导入**之前**那一刻 —— 用户
  // 得自己再点一次「检查连接」才看得到真相(本轮审查 A 项,它有截图)。
  check("导入成功后那句「还没有登录过 X」不再挂在页面上",
        !(await evalJs(`document.getElementById('source-x-state').textContent`))
          .includes("还没有登录过"),
        await evalJs(`document.getElementById('source-x-state').textContent`));
  // **判据是「导入**之后**有没有再发一条 `/x-channel` GET」。** 原来数的是导入 POST
  // 发过几次,那只是个代理指标:前面几节各点过一次导入,它就会在一条跟就绪毫无关系
  // 的理由上变红(实测过一次)。也不能数"这一轮有没有 get":上一节保存收尾那次重读
  // 本来就该发一条,它只是碰巧也落在这一段里。
  check("导入没有顺手再发一次 GET 去问就绪(后端已经带回来了)",
        !(await evalJs(`fetch('/__probe/stamps').then(r => r.json())
            .then(j => j.stamps.slice(j.stamps.indexOf('import')).join())`)).includes("get"),
        await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`));

  // 4.6b 迟到的回包不许盖掉用户刚改出来的「还没保存」提醒
  // **这一条是异步结果归属。** 导入最长等 240 秒,这期间用户完全可能改了下拉 ——
  // 那时状态行上挂的是「改完记得点保存设置」,而迟到的成功文案一盖,页面看着像
  // 已经生效,实际存下来的还是旧档(第 2 轮审查在真页面上实测,`import-race.png`)。
  check("「导入登录态」再点一次", await clickAt("#source-x-import-btn"));
  // 趁那 1.2 秒还没回,把取法改走(和用户点那一行一样走 change)。
  await pick('feedgrab');
  const racedSay = await state();
  check("改完之后状态行先说的是「还没存下来」", racedSay.includes("还没存下来"), racedSay);
  await sleep(2200);                       // 等那个回包确实回来过
  check("迟到的导入回包没有盖掉「还没存下来」那句提醒",
        (await state()).includes("还没存下来"), await state());
  check("也没把成功文案写到留痕那行上",
        (await evalJs(`document.getElementById('source-x-import-state').textContent`)) === "",
        await evalJs(`document.getElementById('source-x-import-state').textContent`));
  // 收尾:把取法改回去,后面几步仍按 browser 这一档继续。
  await pick('browser');

  // 4.6c 同一道闸的另一半:**迟到的那条读也不许盖**
  // 上面两条钉的是两颗按钮的回包;读那一条原来只判「来源类型变没变」,而类型在
  // 这个场景里一直是 social。摆法见上面 `getHold` 那段注释:**读还在路上,用户
  // 改了取法** —— 那条读回来之后重建下拉、`sel.value = data.current`,把他刚选的
  // 那一档静默换回去,顺带把「改完记得点保存设置」盖成「当前通道已就绪」,而面板上
  // 一切正常(第 3 轮审查 A 项,真页面实测)。
  await evalJs(`fetch('/__probe/slow-get?ms=900').then(() => true)`);
  await evalJs(`loadXChannel('social'); true`);     // 这一条要在路上待 900 毫秒
  await sleep(120);
  // 趁它还没回,用户改了取法(和手点一样走 change)。
  await pick('feedgrab');
  await sleep(1400);                                // 等那条读确实回来过
  check("迟到的那条读没把用户刚选的那一档换回去", (await picked()) === "feedgrab", await picked());
  check("也没把「还没存下来」那句提醒盖成「已就绪」",
        (await state()).includes("还没存下来"), await state());
  // 收尾:改回 browser,后面几步仍按这一档继续。
  await pick('browser');

  // 4.6d 同一道闸的最后一半:**后来的事实要让此前发出的读作废**
  // 上面那条钉的是「读在途、用户改了下拉」。这一条换成「读在途、**导入成功了**」:
  // 先发一条读(它说的是导入之前那一刻:还没登录过 X),再点导入,导入先回来、把
  // 成功记录写上去,那条比它更早发出的读随后才到 —— 它们属于同一代,所以旧代码里
  // 它照旧通得过代次检查,把面板盖回「浏览器通道还没有登录过 X」,成功 toast 和那句
  // 否定同时挂在屏幕上(第 4 轮审查在真页面上实测)。出路是:**写入落地时推进代次**。
  await evalJs(`fetch('/__probe/slow-get?ms=3000&forget=1').then(() => true)`);
  await evalJs(`loadXChannel('social'); true`);     // 这条读要在路上待 3 秒
  await sleep(150);
  check("「导入登录态」在这一档上点得到", await clickAt("#source-x-import-btn"));
  await until(`document.getElementById('source-x-import-state').textContent.includes('导入')`,
              15000);
  const importedSay = await evalJs(`document.getElementById('source-x-import-state').textContent`);
  await sleep(3200);                                // 等那条旧读确实回来过
  check("迟到的那条读没把刚导入成功那行留痕清掉",
        (await evalJs(`document.getElementById('source-x-import-state').textContent`)) === importedSay,
        await evalJs(`document.getElementById('source-x-import-state').textContent`));
  check("也没把状态行盖回「还没有登录过 X」",
        !(await evalJs(`document.getElementById('source-x-state').textContent`)).includes("还没有登录过"),
        await evalJs(`document.getElementById('source-x-state').textContent`));

  // 4.6e 两条读交错:**算谁最后发起,不算谁最后回来**
  // 上面两条摆的都是「慢回包落地时面板已经换了人」。这一条摆的是另一半:两条读都
  // 还属于这块面板,但**先发那条先回来**。「谁最后回来谁算」的话,先发那条(说的是
  // 保存之前那一刻)会赢,而后发那条随后落地时反倒被判成过期 —— 面板停在旧事实上。
  // 判据是**发起顺序**:后发那条才代表用户最新的意图。
  //
  // **摆法是把 `pipeJson` 换成一个可控时序的壳,不走网络层** —— 两条 URL 完全一样
  // 的 GET 被浏览器合流排队了(诊断见上面 `getHold` 那段),慢服务端摆不出交错;
  // 而要钉的恰恰是 `loadXChannel` 自己那段归属逻辑,传输层打桩不影响它。
  await evalJs(`(() => {
    window.__realPipeJson = window.pipeJson;
    let n = 0;
    window.pipeJson = async (path, opts, t) => {
      if (!String(path).endsWith('/x-channel')) return window.__realPipeJson(path, opts, t);
      // **发起时就定序**:放到 await 之后去数的话,序号取决于两条网络 promise 谁先
      // 结算(同 URL 的 GET 还被浏览器合流),于是这条用例自己会飘。
      const which = ++n;
      const r = await window.__realPipeJson(path, opts, t);
      // **两条都要压时间,而且先发那条也不许秒回** —— 不压的话它在第二条发出之前就
      // 落地了,两条读压根不重叠,这条用例于是对「发起时取代次」不敏感(摆这一条时
      // 实测:60 毫秒的间隔里先发那条已经应用完了,注入旧代码照旧全绿)。
      await new Promise(res => setTimeout(res, which === 1 ? 600 : 1300));
      // 先发那条说的是另一档(保存之前那一刻的事实)
      if (which === 1) return { ...r, current: 'feedgrab', ready: false, why: '保存之前那一刻' };
      return r;
    };
    return true;
  })()`);
  await evalJs(`loadXChannel('social'); true`);     // 先发(600 毫秒后回)
  await sleep(80);
  await evalJs(`loadXChannel('social'); true`);     // 后发(1300 毫秒后回),它才是最新的意图
  // **摆盘自检**:发第二条时第一条确实还在路上 —— 不然这条用例测的是另一件事。
  check("两条读确实重叠在途(摆盘自检)", (await picked()) === "browser", await picked());
  await sleep(2000);
  check("先回来的那条旧读没赢", (await picked()) === "browser", await picked());
  check("后发那条读的事实落地了",
        (await evalJs(`xChan && xChan.current`)) === "browser",
        await evalJs(`xChan && xChan.current`));
  check("状态行不是「保存之前那一刻」",
        !(await evalJs(`document.getElementById('source-x-state').textContent`))
          .includes("保存之前那一刻"),
        await evalJs(`document.getElementById('source-x-state').textContent`));
  await evalJs(`window.pipeJson = window.__realPipeJson; true`);   // 收尾:还回去

  // 4.6f 反方向那一半:**旧读先抵达,不许因此废掉随后回来的动作结果**
  // 4.6d 摆的是「动作先回来、旧读随后到」(旧读不许盖)。这一条把抵达顺序倒过来:
  // 旧读**先**抵达 —— 原来它自己落地时会顺手推进代次,于是随后回来的导入反被判成
  // 过期,成功记录、成功 toast、失败原因一个都不显示,面板停在「还没有登录过 X」
  // (第 5 轮审查在隔离浏览器里实测:后端 `ok=true`、页面什么都没有,
  //  `import-login-read-first-ok.png`)。
  // 出路:**面板归属和「谁说的更新」分开记** —— 动作一开始就让在途的读作废
  // (`xChanAct`),而读落地不再碰面板归属,所以它废不掉动作。
  await evalJs(`fetch('/__probe/stamps?reset=1').then(() => true)`);
  await evalJs(`(document.getElementById('toast-host')||{}).innerHTML = ''; true`);  // 只看这一轮的
  await evalJs(`fetch('/__probe/slow-get?ms=600&forget=1').then(() => true)`);
  await evalJs(`loadXChannel('social'); true`);     // 这条读说的是导入之前那一刻
  await sleep(150);
  check("「导入登录态」在这一档上点得到(4.6f)", await clickAt("#source-x-import-btn"));
  await sleep(900);                                 // 旧读已经回来了,导入还没
  // **摆盘自检**:这一轮真的是「读先到、动作还在路上」。顺序反了的话这条用例测的
  // 是 4.6d 那个场景,而不是它自己要钉的那一个。
  const mid = await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`);
  check("旧读先抵达、导入还没回来(摆盘自检)", mid === "get", mid);
  await until(`document.getElementById('source-x-import-state')
                 .textContent.includes('上次从日常浏览器导入')`, 15000);
  check("旧读先抵达也没把随后回来的导入结果判成过期",
        (await evalJs(`document.getElementById('source-x-import-state').textContent`))
          .includes("13 个 cookie"),
        await evalJs(`document.getElementById('source-x-import-state').textContent`));
  check("成功提示也还在(不是只有留痕那行)",
        (await evalJs(`document.querySelector('#toast-host')?.textContent || ''`))
          .includes("13 个 x.com cookie"),
        await evalJs(`document.querySelector('#toast-host')?.textContent || ''`));
  check("状态行没停在旧读那句「还没有登录过 X」",
        !(await evalJs(`document.getElementById('source-x-state').textContent`))
          .includes("还没有登录过"),
        await evalJs(`document.getElementById('source-x-state').textContent`));
  const order = await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`);
  check("这一轮的抵达顺序确实是「读、再动作」", order === "get,import", order);

  // 4.6g 同一族的第三种时序:**动作在途时顺手保存一次**
  // 4.6d/4.6f 摆的都是「读在动作之前发出」。这一条摆的是「读在动作**开始之后**才发出,
  // 可它取到的后端快照仍然在动作提交之前」:导入还没回来,用户不改任何设置又点了一次
  // 「保存设置」—— 保存收尾会重填字段并重新读一遍通道(`source_forms.js`),那条读几十
  // 毫秒就回来了,带着「还没有登录过 X」;按代次判的话它既推进了代次、又先落了地,
  // 随后回来的导入结果反被判成过期,成功记录和成功提示一个都不出现(审查实测:保存
  // +34ms、它那条 GET +49ms、导入成功 +1203ms,`ui-save-during-import.png`)。
  // 出路:**动作的归属按「它说的是哪一档」判,不按代次判**(来源 + 下拉选的那一档 +
  // 存下来的那一档都没变就还是它的),同时记一个「已完成版本」让更早发出的读不许回头盖。
  await evalJs(`fetch('/__probe/stamps?reset=1').then(() => true)`);
  await evalJs(`(document.getElementById('toast-host')||{}).innerHTML = ''; true`);
  check("「导入登录态」在这一档上点得到(4.6g)", await clickAt("#source-x-import-btn"));
  await sleep(60);
  // `forget=1`:让保存触发的那条读说「导入之前那一刻」—— 真后端就是这样,那一刻
  // 导入还没提交(mock 要等 1.2 秒才把 `imported` 置上)。
  await evalJs(`fetch('/__probe/slow-get?ms=0&forget=1').then(() => true)`);
  check("导入途中「保存设置」点得到", await clickAt("#source-settings-save"));
  await sleep(500);                                 // 保存和它那条读都回来了,导入还没
  const midSave = await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`);
  check("保存那条读先抵达、导入还在路上(摆盘自检)", midSave === "get", midSave);
  // **在途动作的交代不许被那次重画盖掉**:那个授权弹窗这会儿正等在另一个窗口里,
  // 这一行是它唯一的说明。
  check("保存收尾那次重画没把「正在导入」的预告清掉",
        (await evalJs(`document.getElementById('source-x-import-state').textContent`))
          .includes("授权"),
        await evalJs(`document.getElementById('source-x-import-state').textContent`));
  await until(`document.getElementById('source-x-import-state')
                 .textContent.includes('上次从日常浏览器导入')`, 15000);
  check("途中保存没让导入结果被判成过期",
        (await evalJs(`document.getElementById('source-x-import-state').textContent`))
          .includes("13 个 cookie"),
        await evalJs(`document.getElementById('source-x-import-state').textContent`));
  check("成功提示也还在",
        (await evalJs(`document.querySelector('#toast-host')?.textContent || ''`))
          .includes("13 个 x.com cookie"),
        await evalJs(`document.querySelector('#toast-host')?.textContent || ''`));
  check("状态行没停在那条读带来的「还没有登录过 X」",
        !(await evalJs(`document.getElementById('source-x-state').textContent`))
          .includes("还没有登录过"),
        await evalJs(`document.getElementById('source-x-state').textContent`));
  check("这一轮的抵达顺序确实是「读、再动作」(4.6g)",
        (await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`))
          === "get,import",
        await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`));

  // 4.6h 4.6g 的另一半:**那条读慢一点回来,也不许回头把结果盖掉**
  // 4.6g 里保存触发的读先回来;这一条让它压 2 秒,于是导入结果先落地、它后到。
  // 它是在动作**开始之后**才发出的,所以「动作一开始就作废在途的读」那一道(`xChanActs`)
  // 放它过 —— 可它取到的后端快照仍然在导入提交之前,盖上来页面就又说「还没有登录过 X」,
  // 而成功记录刚刚才写上去。挡住它的是另一样:**后端已完成版本**(`xChanLands`)——
  // 有动作结果落过地之后,比它更早发出的读一律不许再写。
  await evalJs(`fetch('/__probe/stamps?reset=1').then(() => true)`);
  check("「导入登录态」再点一次(4.6h)", await clickAt("#source-x-import-btn"));
  await sleep(60);
  await evalJs(`fetch('/__probe/slow-get?ms=2000&forget=1').then(() => true)`);
  check("导入途中「保存设置」再点一次", await clickAt("#source-settings-save"));
  await until(`document.getElementById('source-x-import-state')
                 .textContent.includes('上次从日常浏览器导入')`, 15000);
  const beforeLate = await evalJs(`document.getElementById('source-x-import-state').textContent`);
  // **等那条压慢的读确实回来过,别用固定睡眠**:`until` 那一步本身耗时不定,睡死的
  // 话这条用例自己会飘(摆这一条时实测:注入版里它先红在自检上,而不是红在判据上)。
  let lateOrder = "";
  for (let i = 0; i < 40 && lateOrder !== "import,get"; i++) {
    lateOrder = await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`);
    if (lateOrder !== "import,get") await sleep(100);
  }
  check("导入先落地、那条读后到(摆盘自检)", lateOrder === "import,get", lateOrder);
  await sleep(150);                                 // 让它那一下渲染真的发生过
  check("晚到的那条读没把刚写上的导入记录盖掉",
        (await evalJs(`document.getElementById('source-x-import-state').textContent`))
          === beforeLate,
        await evalJs(`document.getElementById('source-x-import-state').textContent`));
  check("也没把状态行盖回「还没有登录过 X」(4.6h)",
        !(await evalJs(`document.getElementById('source-x-state').textContent`))
          .includes("还没有登录过"),
        await evalJs(`document.getElementById('source-x-state').textContent`));

  // 4.6i 同一族的最后一种:**请求断在半路,这件事不许被一次重画抹掉**
  // 前面几条管的都是「谁的回包算数」;这一条管的是「**没有**回包时页面说什么」。
  // 后端那条链是「改 profile 之前先登记、写完才落账」,所以连接断掉证明不了它没发生 ——
  // 而原来的失败路只把那一行 DOM 清空,缓存里的 `login_state`/`ready` 一个字没动:
  // 用户把取法切走再切回来,重画就按那份旧缓存把「当场验证通过」恢复成当前结论,
  // 这一次没跑完一个字都不剩(审查在真页面上实测,`ui-old-trace-after-interruption.png`)。
  // 出路:记成**动作状态**(`xChanUnsure`),重画时顶在旧留痕前面。
  await evalJs(`fetch('/__probe/stamps?reset=1').then(() => true)`);
  await evalJs(`fetch('/__probe/kill-import').then(() => true)`);
  check("「导入登录态」再点一次(4.6i)", await clickAt("#source-x-import-btn"));
  await until(`document.getElementById('source-x-import-state')
                 .textContent.includes('没跑完')`, 20000);
  const unsureLine = await evalJs(`document.getElementById('source-x-import-state').textContent`);
  check("断掉的那一次说的是「不知道」,不是一句空白",
        unsureLine.includes("没跑完") && unsureLine.includes("不知道"), unsureLine);
  // **切走再切回来** —— 审查复现的就是这两下。
  await pick('feedgrab');
  await sleep(120);
  check("切到别档时那条交代不跟着跑(反过来钉一边)",
        !(await evalJs(`document.getElementById('source-x-import-state').textContent`))
          .includes("没跑完"),
        await evalJs(`document.getElementById('source-x-import-state').textContent`));
  await pick('browser');
  await sleep(120);
  const backLine = await evalJs(`document.getElementById('source-x-import-state').textContent`);
  check("切回来之后这一次没跑完还在,没被旧留痕顶掉", backLine.includes("没跑完"), backLine);
  check("旧留痕没被删,只是退到后面当历史",
        backLine.includes("上次从日常浏览器导入"), backLine);
  // **摆盘自检**:这一轮真的只到了一条导入请求(浏览器会在「复用连接上一个字节都
  // 没收到」时自动重发一次 —— 那一下走的是正常分支,页面压根进不了失败路)。
  check("这一轮只有那一条被掐断的导入(摆盘自检)",
        (await evalJs(`fetch('/__probe/stamps').then(r => r.json())
                         .then(j => j.stamps.filter(s => s.startsWith('import')).join())`))
          === "import-killed",
        await evalJs(`fetch('/__probe/stamps').then(r => r.json()).then(j => j.stamps.join())`));
  // **反过来钉一边:它必须撤得掉。** 再导一次、这回真回来了 —— 那条「不知道」就
  // 不该再挂着(一条撤不掉的提醒会把面板永远钉在一次早就过去的故障上)。
  check("「导入登录态」再点一次(4.6i 收尾)", await clickAt("#source-x-import-btn"));
  // **等它真的回来**(别等「正在导入…」那一瞬间:那句话里本来就没有「没跑完」,
  // 按它判的话这一条会在请求还在飞的时候就放行,后面那节点「检查连接」会撞上
  // `xChanBusy` 压根发不出请求)。
  await until(`document.getElementById('source-x-import-state')
                  .textContent.includes('上次从日常浏览器导入')`, 20000);
  // **判据要落在重画上,不是落在那一次动作自己写的那行字上** —— 成功那一下是直接
  // 往 DOM 上写的,所以就算那条记录没撤掉这里也看不出来(注入「不撤」实测:一条红
  // 都没有)。切走再切回来走的才是重画那条路,记录没撤的话它当场又冒出来。
  for (const one of ['feedgrab', 'browser']) { await pick(one); await sleep(120); }
  check("拿到确定答复之后那条「不知道」撤掉了,重画也不再冒出来",
        !(await evalJs(`document.getElementById('source-x-import-state').textContent`))
          .includes("没跑完"),
        await evalJs(`document.getElementById('source-x-import-state').textContent`));

  // 4.6j **结论归不归眼前这一份,跟后端那边变没变,是两个问题。**
  // 这一下写的是那个**共享的** profile —— 换 Chrome 可执行文件位置并不换它。于是
  // 「导入在途时改一下位置并保存」会同时成立两件事:①那条回包说的是旧那一份配置,
  // 不该被当成这一份的结论;②cookie 确确实实已经搬进去了,面板缓存里那句「还没有
  // 登录过 X」已经不成立。上一轮把两件事绑在同一个 `return` 上,于是导入成功的回执
  // 刚弹出来,同屏还写着「还没有登录过 X」「这一轮没走完」,切走再切回来才恢复
  // (审查实测 `import-after-save.jpg`)。
  await evalJs(`(document.getElementById('toast-host')||{}).innerHTML = ''; true`);
  await evalJs(`fetch('/__probe/forget').then(() => true)`);
  await pick('browser');
  await fill('#source-x-chrome', '/review/chrome-a');
  await clickAt("#source-settings-save");
  await until(`xChannelSaved() && xChan.current === 'browser' && xChan.ready === false`, 20000);
  check("起点:面板说这一档还没有登录过(摆盘自检)",
        (await state()).includes("还没有登录过"), await state());
  await evalJs(`fetch('/__probe/stamps?reset=1').then(() => true)`);
  await evalJs(`fetch('/__probe/slow-import?ms=2500').then(() => true)`);
  check("「导入登录态」点得到(4.6j)", await clickAt("#source-x-import-btn"));
  await sleep(400);                                   // 导入在路上(按旧位置)
  await fill('#source-x-chrome', '/review/chrome-b');    // 他改了 Chrome 位置
  await clickAt("#source-settings-save");                // 并且真存下去了
  await until(`xChannelSaved() && xChanDraft().includes('chrome-b')`, 20000);
  check("保存收尾那次读到的仍是导入之前那一刻(摆盘自检)",
        (await state()).includes("还没有登录过"), await state());
  await until(`document.querySelector('#toast-host')?.textContent.includes('搬进独立 profile')`, 20000);
  // **先断言后果**:用户眼前这一屏不许再说"还没有登录过"。
  await until(`!document.getElementById('source-x-state')
                 .textContent.includes('还没有登录过')`, 20000);
  check("导入完成之后,同一屏上不再写着「还没有登录过 X」", true);
  check("那行留痕也跟上了权威记录",
        (await text('#source-x-import-state')).includes("上次从日常浏览器导入"),
        await text('#source-x-import-state'));
  // **机制那一半**:恢复不是靠把那条被丢掉的回包贴上来(它说的是旧那一份配置),
  // 而是收尾时重新问了一次 —— 所以时序上「导入之后还有一条读」。
  const after = await evalJs(`fetch('/__probe/stamps').then(r => r.json())
                                .then(j => JSON.stringify(j.stamps))`);
  const marks = JSON.parse(after);
  check("恢复走的是重新读一次,不是把那条旧回包贴上来",
        marks.lastIndexOf('get') > marks.lastIndexOf('import'), after);
  // **反过来钉一边:回执照旧给**(它说的是用户自己刚点的那颗按钮)。
  check("那次导入的回执照旧给了用户",
        (await text('#toast-host')).includes("搬进独立 profile"), await text('#toast-host'));
}
