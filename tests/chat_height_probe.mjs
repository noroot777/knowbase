// 问答面板高度回归:首次配置横幅在与不在,三档断点下输入区(模型下拉 + 发送
// 按钮所在的那一行)都必须完整落在首屏内。
//
// 这条曾经不成立 —— .chat-layout 在三档断点上各写死一个 calc(100dvh - 96/162/175px),
// 那几个数是照「页头上面还有多少东西」估的,而 #wechat-welcome 横幅不在估算里,
// 于是首次配置没做完时输入框下半截整块掉到视口外。
//
// CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、不连网、
// 始终无头、临时 profile),口径同 tests/redesign_probe.mjs。
//
//   HEADLESS_SHELL=<bin> node tests/chat_height_probe.mjs <baseURL>
import { spawn } from "node:child_process";
import { writeSync } from "node:fs";

const BIN = process.env.HEADLESS_SHELL;
const base = process.argv[2];

const proc = spawn(BIN, ["--headless", "--remote-debugging-port=0", "--disable-gpu",
                         "--hide-scrollbars", "--window-size=1280,900", "about:blank"],
                   { stdio: ["ignore", "pipe", "pipe"] });
const port = await new Promise((res, rej) => {
  let buf = "";
  proc.stderr.on("data", d => {
    buf += d;
    const m = buf.match(/ws:\/\/127\.0\.0\.1:(\d+)\//);
    if (m) res(+m[1]);
  });
  proc.on("exit", c => rej(new Error(`exit ${c}\n${buf}`)));
  setTimeout(() => rej(new Error("no devtools port")), 20000);
});

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));

let id = 0;
const pend = new Map();
// 页面自己报的错都收着:ResizeObserver 现在观察 .chat-inputbar,而改文本域高度
// 又会改它的高 —— 这条回路真要收不住,浏览器会喊
// 「ResizeObserver loop completed with undelivered notifications」。
const pageErrors = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails.exception?.description
                    || m.params.exceptionDetails.text || "");
  if (m.method === "Log.entryAdded" && m.params.entry.level === "error")
    pageErrors.push(m.params.entry.text || "");
});
const send = (method, params = {}) => {
  const mid = ++id;
  return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); });
};
const evalJs = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

await send("Runtime.enable");
await send("Page.enable");
await send("Log.enable");

const results = [];
const check = (name, cond, extra = "") => results.push({ name, ok: !!cond, detail: extra });

// 后端全程 503:这里量的是 CSS 布局,不需要真数据。
await send("Page.navigate", { url: base + "/" });
for (let i = 0; i < 60; i++) {
  if (await evalJs(`!!document.querySelector('.chat-inputbar')`)) break;
  await sleep(200);
}

// 三档断点各取真实窗口:>900 桌面、≤900 出顶栏、≤760 聊天区改竖排。
// 每档都要带一个矮的 —— 只测 900px 高的话,地板(min-height)比可用空间还高的
// 那一类问题量不出来:700x720 带横幅时可用高度只有 456px,原来的 520px 地板
// 把整块撑出页面(2026-09-30 审查)。
const VIEWPORTS = [
  { w: 1280, h: 900, name: "桌面 1280x900" },
  { w: 1280, h: 720, name: "矮桌面 1280x720" },
  { w: 860, h: 900, name: "窄屏 860x900" },
  { w: 860, h: 720, name: "矮窄屏 860x720" },
  { w: 700, h: 900, name: "竖排 700x900" },
  { w: 700, h: 720, name: "矮竖排 700x720" },
  { w: 390, h: 844, name: "手机 390x844" },
  { w: 375, h: 667, name: "小手机 375x667" },
  // 手机横屏:高度只剩三四百像素,是整套里最紧的一档。三档断点里它落在 ≤900
  // (出顶栏、聊天区仍横排),而**可用高度比任何竖屏都少**——.chat-main 上那条
  // 200px 地板在这里必然比剩余空间高,整块输入区被顶出首屏(2026-09-30 复审
  // 实测 812×375 带横幅:发送按钮下沿 411.19 / 视口 375)。
  { w: 812, h: 375, name: "横屏 812x375" },
  { w: 844, h: 390, name: "横屏 844x390" },
  { w: 896, h: 414, name: "横屏 896x414" },
  // 又矮又窄:竖排 + 高度只剩 360,留白那几处必须同时收
  { w: 640, h: 360, name: "横屏 640x360" },
  // 最窄的在用机型:竖排 + 历史栏展开时剩给卡片的更少
  { w: 320, h: 568, name: "窄手机 320x568" },
  // 600 / 601 两侧:600 落在 ≤600 那档(横幅折行、比 601 高一截),是整套里卡片最矮的
  // 一格 —— 复审第 3 轮实测卡片只有 57.95px,装不下 82px 的一行输入区,文本域被从
  // **上边**裁掉、点不到也打不进字。601 是同一个问题的轻症(顶部被切约 5px)。
  { w: 600, h: 360, name: "窄横屏 600x360" },
  { w: 601, h: 360, name: "窄横屏 601x360" },
  // 窄且短:≤600 时首次配置横幅本来就折行,复审第 2 轮那次强行 nowrap 把文字挤成
  // 一条细柱、横幅反而从 136.7 长到 230.1px,卡片只剩 90.9px(实测 320×520)。
  { w: 320, h: 500, name: "窄短屏 320x500" },
  { w: 320, h: 520, name: "窄短屏 320x520" },
  // 更矮的横屏:568×320 是 320×480 那代机型横过来的样子,也是复审第 4 轮复现的那格。
  // 带首次配置横幅时卡片被挤到只剩 2px(内容高 0)—— 而 `!card.clientHeight` 被当成
  // 「面板没显示」,一级紧凑都不加,写问题的地方、模型下拉、发送按钮全部消失,
  // 点原位置打不进字、滚页面也没用。600×320 是同一格的另一个宽度。
  { w: 568, h: 320, name: "矮横屏 568x320" },
  { w: 600, h: 320, name: "矮横屏 600x320" },
  { w: 600, h: 340, name: "矮横屏 600x340" },
  { w: 601, h: 320, name: "矮横屏 601x320" },
  // 再矮一档:横幅按回一行之后文字仍占两行,只剩「那句说明文字」和「写问题的地方」
  // 二选一 —— 第四级就是为这一格存在的。320×360 是另一侧:真窄屏上按回一行只会
  // 更糟(第三级自己退回去),同样得靠第四级。
  { w: 480, h: 320, name: "矮横屏 480x320" },
  { w: 320, h: 360, name: "窄矮屏 320x360" },
  // 整套里最紧的一格:长模型名把下拉撑到 240px,320 宽的框里放不下「下拉 + 发送」,
  // flex-wrap 把发送挤到第三行 —— 输入区 82 → 112px、最小占地 92 → 122px,而卡片
  // 只有 92px,文本域被从上边裁掉(只剩下缘 15px)、点中心打到 #main-content
  // (2026-09-30 复审第 5 轮实测)。第五级就是为这一格存在的。
  { w: 320, h: 320, name: "窄矮屏 320x320" },
  // 带横幅能撑住的最矮一档横屏(四级收完卡片 99px / 需求 92px)。
  { w: 812, h: 240, name: "极矮横屏 812x240" },
  // **只测关掉横幅**的 812×200:第一级收的全是卡片里面那点空隙,按定义不改卡片
  // 外高 —— 「没换来卡片高度就摘掉」于是把它一律判成无效,最小需求从 92 退回
  // 144px,四级走完仍差 29px,输入框下沿和发送按钮重新被裁(2026-09-30 复审第 5
  // 轮实测:发送底边 204.59 / 视口 200)。复现步骤本来就是「先点稍后配置再缩窗口」。
  // **横幅那一半在这个高度上物理放不下**:200 - 56(顶栏)= 144,四级收完的横幅
  // 连边距 56px、标题 26.6px,剩 61.4px 而一行输入区要 92px —— 把标题整行拿掉也
  // 只有 85.6px。能退的都退完还是不够时,如实标出来比挑一条控件藏掉强
  // (≥901 宽没有顶栏,同样 200 高就放得下,实测 1280×200 卡片 115px)。
  { w: 812, h: 200, name: "极矮横屏 812x200", banners: [false] },
  // 旧的 max-height: 520 阈值两侧:520 好好的、521 恢复常规留白就被裁,
  // 540/550 同样被裁 —— 所以这几档一起钉,防的是「换一个固定阈值」那种修法。
  { w: 375, h: 520, name: "小手机 375x520" },
  { w: 375, h: 521, name: "小手机 375x521" },
  { w: 375, h: 540, name: "小手机 375x540" },
  { w: 375, h: 550, name: "小手机 375x550" },
];
// ≤760 才有「历史对话」这个开关(#chat-history-toggle),展开后历史栏排在
// 输入区上方、独占 185px,是另一个把输入区挤下去的来源。
const HISTORY_BREAKPOINT = 760;

// 长对话:flex 项的自动最小尺寸按内容算,一段几千像素的消息流会把面板整个顶高、
// 让 #chat-msgs 那条 overflow-y: auto 失去意义,输入区跟着被推到页面最底下。
// 空库跑不出这个形态,所以这里自己塞一段够长的内容进去。
// 历史列表同理:没有后端时它只有一行「加载中…」,撑不满 max-height,量不出
// 「展开历史对话把发送按钮顶出首屏」那个形态,所以也自己塞满。
const FILL = `(() => {
  const box = document.getElementById('chat-msgs');
  if (!document.getElementById('probe-fill')) {
    const d = document.createElement('div');
    d.id = 'probe-fill'; d.style.height = '4000px';
    box.appendChild(d);
  }
  const convs = document.getElementById('chat-convs');
  if (!convs.querySelector('.probe-conv')) {
    convs.innerHTML = '';
    for (let i = 0; i < 20; i++) {
      const it = document.createElement('div');
      it.className = 'conv-item probe-conv';
      it.innerHTML = '<div class="conv-title">测试对话 ' + i + '</div><div class="conv-meta">刚刚</div>';
      convs.appendChild(it);
    }
  }
})()`;

// 只看「bottom < innerHeight」是不够的:.chat-main 是 overflow: hidden 的,
// 输入区被它裁掉时元素照样在首屏坐标里,人却看不见也点不到。所以同时量卡片的
// 裁剪边界(padding box 底边),再用 elementFromPoint 做一次真实命中测试 ——
// 后者才是「这个按钮现在点得到吗」的直接答案。
const measure = `(() => {
  const bar = document.querySelector('.chat-inputbar');
  const send = document.getElementById('chat-send');
  const model = document.getElementById('ask-model');
  const panel = document.getElementById('askpanel');
  const box = document.getElementById('chat-msgs');
  const card = document.querySelector('.chat-main');
  const ta = document.getElementById('chat-input');
  const r = bar.getBoundingClientRect(), cr = card.getBoundingClientRect();
  // 中心点 + 贴下边沿各探一次:只探中心的话「下半截被裁掉」探不出来
  // (实测旧代码那格,按钮底边越线 6.6px,中心还在裁剪线上方)。
  const hit = el => {
    const b = el.getBoundingClientRect();
    return [b.top + 2, b.top + b.height / 2, b.bottom - 2].every(y => {
      const t = document.elementFromPoint(b.left + b.width / 2, y);
      return !!t && (t === el || el.contains(t));
    });
  };
  const tr = ta.getBoundingClientRect();
  const cs = getComputedStyle(card);
  return {
    barTop: r.top,
    cardClipTop: cr.top + parseFloat(cs.borderTopWidth),
    // 聚焦一个被 overflow: hidden 裁掉的子元素,浏览器会把这个容器**内部**滚起来,
    // 于是文本域从上边被推出去、而下沿看着还在卡片里(2026-09-30 复审第 3 轮
    // 600×360 实测 scrollTop=36)。只比下沿会得出错误的通过结论。
    cardScrollTop: card.scrollTop,
    taTop: tr.top, taBottom: tr.bottom,
    taVisible: Math.max(0, Math.min(tr.bottom, cr.bottom) - Math.max(tr.top, cr.top)),
    hitInput: hit(ta),
    barBottom: r.bottom, sendBottom: send.getBoundingClientRect().bottom,
    modelBottom: model.getBoundingClientRect().bottom,
    cardClipBottom: cr.bottom - parseFloat(getComputedStyle(card).borderBottomWidth),
    hitSend: hit(send), hitModel: hit(model),
    taHeight: ta.getBoundingClientRect().height, taLines: ta.value.split('\\n').length,
    innerHeight: window.innerHeight, innerWidth: window.innerWidth,
    panelWidth: panel.getBoundingClientRect().width,
    barWidth: r.width, barHeight: r.height,
    modelWidth: model.getBoundingClientRect().width,
    layoutHeight: document.querySelector('.chat-layout').getBoundingClientRect().height,
    cardHeight: cr.height,
    msgsHeight: box.getBoundingClientRect().height,
    msgsScrollable: box.scrollHeight > box.clientHeight + 1,
    scrollW: document.documentElement.scrollWidth,
    tight: document.body.classList.contains('chat-tight'),
    tight2: document.body.classList.contains('chat-tight-2'),
    tight3: document.body.classList.contains('chat-tight-3'),
    tight4: document.body.classList.contains('chat-tight-4'),
    tight5: document.body.classList.contains('chat-tight-5'),
    // 第三/四级把「首次配置」这块往回收,但**它本身必须还完整可用**:
    // 标题在、两个按钮都在、都点得到。收的是那句解释性说明,不是功能。
    bannerOk: (() => {
      const wb = document.getElementById('wechat-welcome');
      if (!wb || wb.hidden) return true;
      const btns = [...wb.querySelectorAll('button')];
      const title = document.getElementById('wechat-welcome-title');
      return btns.length >= 2 && btns.every(hit) && !!title && hit(title);
    })(),
  };
})()`;

const tierOf = m => m.tight5 ? "五级" : m.tight4 ? "四级" : m.tight3 ? "三级"
                 : m.tight2 ? "二级" : m.tight ? "一级" : "否";

// 12 行 —— 够顶到 150px 那个舒适上限,正是审查复现出问题的输入量。
const LONG_TEXT = Array.from({ length: 12 }, (_, i) => `第${i + 1}行输入内容测试`).join("\n");

// 用 CDP 的 Input.insertText 真的往聚焦的文本域里敲,走的是浏览器自己的输入
// 通道:autoGrowChat 挂在 input 事件上,直接赋 value 不触发它,那样测的就不是
// 用户实际会遇到的路径。
async function typeIntoInput(text) {
  await evalJs(`(() => { const t = document.getElementById('chat-input');
    t.value = ''; t.dispatchEvent(new Event('input', { bubbles: true })); t.focus(); })()`);
  if (text) await send("Input.insertText", { text });
  await sleep(80);
}

// 真的用鼠标点一下文本域中心,再敲字 —— 光看几何是不够的:600×360 那次文本域
// 的矩形完好、下沿也在卡片里,可它被父卡片从上边裁掉,点中心打到的是
// `#main-content`,字一个也进不去(2026-09-30 复审第 3 轮)。
async function clickAndType(tag, text) {
  await evalJs(`(() => { const t = document.getElementById('chat-input');
    t.value = ''; t.dispatchEvent(new Event('input', { bubbles: true })); t.blur(); })()`);
  const box = await evalJs(`(() => {
    const b = document.getElementById('chat-input').getBoundingClientRect();
    return { x: b.left + b.width / 2, y: b.top + b.height / 2 };
  })()`);
  for (const type of ["mousePressed", "mouseReleased"])
    await send("Input.dispatchMouseEvent",
               { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
  await send("Input.insertText", { text });
  await sleep(80);
  const got = await evalJs(`(() => ({
    focused: document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : '',
    value: document.getElementById('chat-input').value,
  }))()`);
  check(`${tag}:点文本域中心能真的打进字`, got.value === text,
        `焦点在 ${got.focused || "(无)"},value=${JSON.stringify(got.value)}`);
  await typeIntoInput("");
}

// 一个视口 × 一种(横幅, 历史栏)组合:量一次、判四条。
async function probe(vp, banner, history) {
  await evalJs(`(() => {
    switchTab('ask');
    document.getElementById('wechat-welcome').hidden = ${!banner};
    document.getElementById('askpanel').classList.toggle('history-open', ${history});
  })()`);
  await evalJs(FILL);
  await typeIntoInput("");
  await sleep(120);
  const m = await evalJs(measure);
  const tag = `${vp.name}·${banner ? "横幅在" : "横幅关"}${history ? "·历史栏开" : ""}`;
  const slack = m.innerHeight - Math.max(m.barBottom, m.sendBottom, m.modelBottom);
  check(`${tag}:输入区完整在首屏内`, slack >= 0,
        `底边 ${m.barBottom.toFixed(1)} / 视口 ${m.innerHeight},余 ${slack.toFixed(1)}px`
        + `(可用 ${m.layoutHeight.toFixed(1)} / 卡片 ${m.cardHeight.toFixed(1)} / 输入框 ${m.barHeight.toFixed(1)})`);
  // 连输入框自己的下边框都不许被卡片切掉 —— 复审第 2 轮把 640×360 那 2px 列为
  // 「不拦但你该知道」,既然留白已经按实际余量收,顺手把它也清掉。
  check(`${tag}:输入框整体没被卡片裁掉`, m.barBottom <= m.cardClipBottom + 0.5,
        `输入框底边 ${m.barBottom.toFixed(2)} / 卡片裁剪线 ${m.cardClipBottom.toFixed(2)}`);
  // 上边界和「文本域点不点得到」得单独查:被裁掉的是**顶部**时,下沿和按钮全在卡片
  // 里,只比下沿会判成通过(2026-09-30 复审第 3 轮 600×360:文本域只剩下缘 9px,
  // 点中心打到 #main-content,真敲字进不去输入框)。
  check(`${tag}:输入框上沿没被卡片裁掉`, m.barTop >= m.cardClipTop - 0.5,
        `输入框上沿 ${m.barTop.toFixed(2)} / 卡片裁剪上沿 ${m.cardClipTop.toFixed(2)}`);
  check(`${tag}:文本域完整可见且点得到`,
        m.taVisible >= m.taHeight - 0.5 && m.hitInput,
        `可见 ${m.taVisible.toFixed(1)}/${m.taHeight.toFixed(1)}px,命中 ${m.hitInput}`
        + `(紧凑 ${tierOf(m)} / 卡片 ${m.cardHeight.toFixed(1)})`);
  // 收留白不许把「首次配置」这块收没:标题和两个按钮一个都不能少,而且点得到。
  check(`${tag}:首次配置横幅仍完整可用`, m.bannerOk, `紧凑 ${tierOf(m)}`);
  check(`${tag}:卡片没有被聚焦滚出内部偏移`, m.cardScrollTop === 0,
        `card.scrollTop=${m.cardScrollTop}`);
  check(`${tag}:模型下拉与发送按钮没被卡片裁掉`,
        m.sendBottom <= m.cardClipBottom + 0.5 && m.modelBottom <= m.cardClipBottom + 0.5,
        `发送底边 ${m.sendBottom.toFixed(2)} / 下拉底边 ${m.modelBottom.toFixed(2)} / 卡片裁剪线 ${m.cardClipBottom.toFixed(2)}`);
  check(`${tag}:模型下拉与发送按钮点得到`, m.hitSend && m.hitModel,
        `elementFromPoint 命中 发送=${m.hitSend} 下拉=${m.hitModel}`);
  check(`${tag}:没有横向溢出`, m.scrollW <= m.innerWidth + 1,
        `scrollWidth ${m.scrollW} > ${m.innerWidth}`);
  check(`${tag}:面板铺满内容区宽度`, m.panelWidth > Math.min(vp.w, 1600) * 0.7,
        `panelWidth ${m.panelWidth.toFixed(1)}`);
  check(`${tag}:长对话在消息区内部滚动`, m.msgsScrollable && m.msgsHeight < m.innerHeight,
        `消息区高 ${m.msgsHeight.toFixed(1)} / 可滚 ${m.msgsScrollable}`);

  // 多行输入:文本域会长高,不许把模型下拉和发送按钮顶出卡片的裁剪边界。
  await typeIntoInput(LONG_TEXT);
  const g = await evalJs(measure);
  const t2 = `${tag}·12 行输入`;
  check(`${t2}:输入事件确实送达`, g.taLines === 12, `文本域 ${g.taLines} 行`);
  check(`${t2}:输入区完整在首屏内`,
        g.innerHeight - Math.max(g.barBottom, g.sendBottom, g.modelBottom) >= 0,
        `底边 ${g.barBottom.toFixed(1)} / 视口 ${g.innerHeight}`);
  check(`${t2}:输入框整体没被卡片裁掉`, g.barBottom <= g.cardClipBottom + 0.5,
        `输入框底边 ${g.barBottom.toFixed(2)} / 卡片裁剪线 ${g.cardClipBottom.toFixed(2)}`);
  check(`${t2}:模型下拉与发送按钮没被卡片裁掉`,
        g.sendBottom <= g.cardClipBottom + 0.5 && g.modelBottom <= g.cardClipBottom + 0.5,
        `发送底边 ${g.sendBottom.toFixed(2)} / 下拉底边 ${g.modelBottom.toFixed(2)} / 卡片裁剪线 ${g.cardClipBottom.toFixed(2)}(文本域 ${g.taHeight.toFixed(2)}px)`);
  check(`${t2}:发送按钮和模型下拉点得到`, g.hitSend && g.hitModel,
        `elementFromPoint 命中 发送=${g.hitSend} 下拉=${g.hitModel}`);
  await typeIntoInput("");

  // 最紧的那几档再真点一次:几何全过也可能点不进字(见 clickAndType 的注释)。
  // **横幅关掉的那一半也要点**:812×200 那条裁切正是「点了稍后配置之后」才发作的,
  // 只在带横幅时真点会整条漏掉(2026-09-30 复审第 5 轮)。
  if (vp.h <= 520) await clickAndType(tag, "点击后输入的测试问题");
}

// ── 模型选项异步到达 ────────────────────────────────────────────────────────
// 卡片一动不动、输入框自己变高的那一类:模型下拉的选项是 fetch 回来的,长模型名
// 把它从 40px 撑到 240px,发送按钮被 flex-wrap 挤到新行 —— 只观察 .chat-main 的话
// 这一整类全漏(2026-09-30 第 3 轮审查:发送按钮下沿越过卡片裁剪线约 10px)。
// 走的是 settings.js 里那个真的 loadModels():只把 /api/models 那一条 fetch 打桩,
// 填 option 的代码一行没动 —— 直接往 select 里插 option 就绕开了被测代码。
const LOAD_LONG_MODEL = `(async () => {
  const real = window.fetch;
  const key = 'openai:Qwen/Qwen3-Coder-480B-A35B-Instruct';
  window.fetch = (u, ...rest) => String(u).includes('/api/models')
    ? Promise.resolve(new Response(
        JSON.stringify({ models: [{ key, label: key.slice(7), enabled: true }], default: key }),
        { headers: { 'content-type': 'application/json' } }))
    : real(u, ...rest);
  try { await loadModels(); } finally { window.fetch = real; }
})()`;

// 整个矩阵都带着这个长模型名跑:下拉里只有一行「加载中…」时量不到「选项撑宽下拉、
// 发送按钮被 flex-wrap 挤到第二行」那个形态,而那正是最吃高度的一种排布
// (2026-09-30 复审第 2 轮指出常规矩阵的下拉没有真实选项)。短→长的异步切换在
// 下面单独还有一格。
await evalJs(`switchTab('ask')`);
await evalJs(LOAD_LONG_MODEL);
await sleep(150);

for (const vp of VIEWPORTS) {
  await send("Emulation.setDeviceMetricsOverride",
             { width: vp.w, height: vp.h, deviceScaleFactor: 1, mobile: vp.w <= HISTORY_BREAKPOINT });
  for (const banner of vp.banners || [true, false]) {
    await probe(vp, banner, false);
    // 历史栏只有竖排断点上才收起/展开,宽屏它一直在
    if (vp.w <= HISTORY_BREAKPOINT) await probe(vp, banner, true);
  }
  // 收起历史栏得完全恢复,别把展开态带进下一个视口
  await evalJs(`document.getElementById('askpanel').classList.remove('history-open')`);
}


// 短 → 长的异步切换在几档**吃紧的**视口上各走一遍。只测 375×667 是不够的:
// 那里余量宽,长模型到达后压一压文本域就够了;真正会出事的是旧的 max-height: 520
// 阈值**外侧**那几档 —— 521/540/550 恢复常规留白,按钮换行后文本域已在 32px 下限、
// 卡片仍差十几像素(2026-09-30 复审第 2 轮实测 375×521:发送底边 514.47 / 裁剪线
// 500,滚页面也补不回来)。所以这一格按视口循环,而不是再钉一个固定高度。
for (const vp of [{ w: 375, h: 667 }, { w: 375, h: 521 }, { w: 375, h: 540 },
                  { w: 375, h: 550 }, { w: 320, h: 520 }, { w: 640, h: 360 },
                  // 先写好字、模型列表后到的那条真实顺序:320×320 上原来是发送按钮
                  // 下沿掉到 341.72 / 视口 320,滚页面也回不来(复审第 5 轮)。
                  { w: 320, h: 320 }]) {
  const tag = `模型选项异步到达 ${vp.w}x${vp.h}`;
  await send("Emulation.setDeviceMetricsOverride",
             { width: vp.w, height: vp.h, deviceScaleFactor: 1, mobile: true });
  await evalJs(`(() => {
    switchTab('ask');
    document.getElementById('wechat-welcome').hidden = false;
    document.getElementById('askpanel').classList.add('history-open');
    document.getElementById('ask-model').innerHTML = '';
  })()`);
  await evalJs(FILL);
  await typeIntoInput(LONG_TEXT);
  await sleep(150);
  const pre = await evalJs(measure);
  await evalJs(LOAD_LONG_MODEL);
  await sleep(250);
  const post = await evalJs(measure);
  if (vp.h === 667) {
    // 先确认这一趟真的复现了那个形态(下拉撑宽、按钮换行),否则下面那条形同虚设。
    // 只在余量最宽的那一档确认:窄屏上按钮本来就已经在第二行,量不出「被挤下去」。
    check(`${tag}:长模型名把下拉撑宽了`, post.modelWidth > pre.modelWidth + 100,
          `下拉宽 ${pre.modelWidth.toFixed(1)} → ${post.modelWidth.toFixed(1)}`);
    check(`${tag}:发送按钮被挤到新行`,
          (post.barHeight - post.taHeight) > (pre.barHeight - pre.taHeight) + 20,
          `框内非文本域部分 ${(pre.barHeight - pre.taHeight).toFixed(1)} → ${(post.barHeight - post.taHeight).toFixed(1)}`);
  }
  check(`${tag}:文本域跟着让位,发送按钮仍在卡片内且点得到`,
        post.sendBottom <= post.cardClipBottom + 0.5 && post.hitSend && post.hitModel,
        `文本域 ${pre.taHeight.toFixed(2)} → ${post.taHeight.toFixed(2)}px / 发送底边 ${post.sendBottom.toFixed(2)} / 裁剪线 ${post.cardClipBottom.toFixed(2)} / 命中 发送=${post.hitSend} 下拉=${post.hitModel}`);
  // 旧的 max-height: 520 阈值**外侧**、而且实测常规留白放不下的那两档:按实际余量
  // 判定的话必须收 —— 这一条钉的是「不再靠高度阈值」这件事本身,而不只是结果好看。
  // 550 不在内是因为它常规留白下就放得下(实测卡片 170.3 / 输入框 132),不收才对。
  if (vp.w === 375 && (vp.h === 521 || vp.h === 540)) {
    check(`${tag}:阈值外侧也按实际余量收了留白`, post.tight,
          `body.chat-tight=${post.tight}(卡片 ${post.cardHeight.toFixed(1)} / 输入框 ${post.barHeight.toFixed(1)})`);
  }
  check(`${tag}:输入区完整在首屏内`,
        post.innerHeight - Math.max(post.barBottom, post.sendBottom, post.modelBottom) >= 0,
        `底边 ${post.barBottom.toFixed(1)} / 视口 ${post.innerHeight}(卡片 ${post.cardHeight.toFixed(1)} / 输入框 ${post.barHeight.toFixed(1)})`);
  check(`${tag}:文本域完整可见且点得到`,
        post.taVisible >= post.taHeight - 0.5 && post.hitInput,
        `可见 ${post.taVisible.toFixed(1)}/${post.taHeight.toFixed(1)}px,命中 ${post.hitInput}(紧凑 ${tierOf(post)})`);
  await typeIntoInput("");
  // 模型到达之后再真点一次:几何过了也可能因为卡片内部被滚起来而打不进字。
  if (vp.h <= 360) await clickAndType(tag, "模型到达后输入的测试问题");
  await evalJs(`(() => {
    document.getElementById('askpanel').classList.remove('history-open');
    document.getElementById('ask-model').innerHTML = '';
  })()`);
}

// 横幅一出现,聊天区应当自己变矮(说明高度真的跟着可用空间走,不是写死的)。
// 上面塞的那 4000px 还在页面里,所以这一条也是在长对话下量的。
await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
const heights = await evalJs(`(() => {
  const el = document.querySelector('.chat-layout'), w = document.getElementById('wechat-welcome');
  switchTab('ask'); w.hidden = true;
  const off = el.getBoundingClientRect().height;
  w.hidden = false;
  const on = el.getBoundingClientRect().height;
  return { off, on, banner: w.getBoundingClientRect().height };
})()`);
await typeIntoInput(LONG_TEXT);
const tall = await evalJs(`document.getElementById('chat-input').getBoundingClientRect().height`);
check("空间宽裕时文本域照样长到 150px 上限", Math.abs(tall - 150) < 1, `文本域 ${tall.toFixed(2)}px`);
await typeIntoInput("");

// 已经长起来的文本域不会自己缩回去 —— 可用空间变小时得有人来重算。这里在大
// 视口敲满 12 行,然后**不再碰输入**直接缩到小手机 + 展开历史栏,考的就是
// initChat 里挂在 .chat-main 上的那个 ResizeObserver(窗口缩放、展开历史栏、
// 关掉横幅都走它;后两件事压根不触发 window resize)。
await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
await typeIntoInput(LONG_TEXT);
await send("Emulation.setDeviceMetricsOverride", { width: 375, height: 667, deviceScaleFactor: 1, mobile: true });
await evalJs(`document.getElementById('askpanel').classList.add('history-open')`);
await sleep(200);
const shrunk = await evalJs(measure);
check("缩小窗口后已长高的文本域跟着让位", shrunk.sendBottom <= shrunk.cardClipBottom + 0.5 && shrunk.hitSend,
      `文本域 ${shrunk.taHeight.toFixed(2)}px / 发送底边 ${shrunk.sendBottom.toFixed(2)} / 裁剪线 ${shrunk.cardClipBottom.toFixed(2)} / 命中 ${shrunk.hitSend}`);
await evalJs(`document.getElementById('askpanel').classList.remove('history-open')`);
await typeIntoInput("");
await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
await sleep(120);

check("横幅出现时聊天区跟着让出高度", heights.off - heights.on >= heights.banner,
      `无横幅 ${heights.off} / 有横幅 ${heights.on} / 横幅 ${heights.banner}`);

const roLoop = pageErrors.filter(t => /ResizeObserver loop/i.test(t));
check("ResizeObserver 没有跑成收不住的回路", roLoop.length === 0, roLoop.slice(0, 2).join(" | "));

ws.close();
proc.kill();
const bad = results.filter(r => !r.ok);
// **收尾一律 writeSync,并且不许用 process.exit。** node 对管道上的 stdout/stderr
// 是异步写的,exit() 会把还没刷出去的那部分直接丢掉 —— 管道缓冲是 64 KB,项数涨到
// 七百多条之后两个流都越过了这条线,于是 test_chat_height.py 收到的 JSON 恰好在
// 第 65536 字节处断在一个中文字符中间,报的是 UnicodeDecodeError,看着像超时或者
// 编码问题,其实是输出被掐了(2026-09-30 复审第 2 轮补视口后踩到)。
for (const r of results)
  writeSync(2, `${r.ok ? "  ok" : "FAIL"}  ${r.name}${r.detail ? " — " + r.detail : ""}\n`);
writeSync(2, `${results.length - bad.length} ok, ${bad.length} failed\n`);
// 最后一行给 tests/test_chat_height.py 读(人看的那份走 stderr)
writeSync(1, JSON.stringify(results) + "\n");
process.exitCode = bad.length ? 1 : 0;
