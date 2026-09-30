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
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
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

const measure = `(() => {
  const bar = document.querySelector('.chat-inputbar');
  const send = document.getElementById('chat-send');
  const model = document.getElementById('ask-model');
  const panel = document.getElementById('askpanel');
  const box = document.getElementById('chat-msgs');
  const r = bar.getBoundingClientRect();
  return {
    barBottom: r.bottom, sendBottom: send.getBoundingClientRect().bottom,
    modelBottom: model.getBoundingClientRect().bottom,
    innerHeight: window.innerHeight, innerWidth: window.innerWidth,
    panelWidth: panel.getBoundingClientRect().width,
    barWidth: r.width,
    msgsHeight: box.getBoundingClientRect().height,
    msgsScrollable: box.scrollHeight > box.clientHeight + 1,
    scrollW: document.documentElement.scrollWidth,
  };
})()`;

// 一个视口 × 一种(横幅, 历史栏)组合:量一次、判四条。
async function probe(vp, banner, history) {
  await evalJs(`(() => {
    switchTab('ask');
    document.getElementById('wechat-welcome').hidden = ${!banner};
    document.getElementById('askpanel').classList.toggle('history-open', ${history});
  })()`);
  await evalJs(FILL);
  await sleep(120);
  const m = await evalJs(measure);
  const tag = `${vp.name}·${banner ? "横幅在" : "横幅关"}${history ? "·历史栏开" : ""}`;
  const slack = m.innerHeight - Math.max(m.barBottom, m.sendBottom, m.modelBottom);
  check(`${tag}:输入区完整在首屏内`, slack >= 0,
        `底边 ${m.barBottom.toFixed(1)} / 视口 ${m.innerHeight},余 ${slack.toFixed(1)}px`);
  check(`${tag}:没有横向溢出`, m.scrollW <= m.innerWidth + 1,
        `scrollWidth ${m.scrollW} > ${m.innerWidth}`);
  check(`${tag}:面板铺满内容区宽度`, m.panelWidth > Math.min(vp.w, 1600) * 0.7,
        `panelWidth ${m.panelWidth.toFixed(1)}`);
  check(`${tag}:长对话在消息区内部滚动`, m.msgsScrollable && m.msgsHeight < m.innerHeight,
        `消息区高 ${m.msgsHeight.toFixed(1)} / 可滚 ${m.msgsScrollable}`);
}

for (const vp of VIEWPORTS) {
  await send("Emulation.setDeviceMetricsOverride",
             { width: vp.w, height: vp.h, deviceScaleFactor: 1, mobile: vp.w <= HISTORY_BREAKPOINT });
  for (const banner of [true, false]) {
    await probe(vp, banner, false);
    // 历史栏只有竖排断点上才收起/展开,宽屏它一直在
    if (vp.w <= HISTORY_BREAKPOINT) await probe(vp, banner, true);
  }
  // 收起历史栏得完全恢复,别把展开态带进下一个视口
  await evalJs(`document.getElementById('askpanel').classList.remove('history-open')`);
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
check("横幅出现时聊天区跟着让出高度", heights.off - heights.on >= heights.banner,
      `无横幅 ${heights.off} / 有横幅 ${heights.on} / 横幅 ${heights.banner}`);

ws.close();
proc.kill();
const bad = results.filter(r => !r.ok);
for (const r of results)
  console.error(`${r.ok ? "  ok" : "FAIL"}  ${r.name}${r.detail ? " — " + r.detail : ""}`);
console.error(`${results.length - bad.length} ok, ${bad.length} failed`);
// 最后一行给 tests/test_chat_height.py 读(人看的那份走 stderr)
console.log(JSON.stringify(results));
process.exit(bad.length ? 1 : 0);
