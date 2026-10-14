// ── 同步中心探针的 CDP 壳 ─────────────────────────────────────────────────────
// 从 `pipe_history_probe.mjs` 拆出来的:那份探针贴着 700 行上限,而这一段是**页面
// 无关**的 —— 起无头壳、接 CDP、求值、轮询、真鼠标点击。页面特有的那几个选择器
// (`rows()` / `exportRow()` 之类)仍留在探针自己手里。
//
// `pipe_pending_probe.mjs` 里那份副本没有跟着搬 —— 它是另一块功能的探针,不在这次
// 的射程里。两份要是同时改动,才值得再往上抽一层。
import { launchShell } from "./probe_shell.mjs";

export const sleep = ms => new Promise(r => setTimeout(r, ms));

// 返回的那一套就是原来探针里那几个同名局部量,**`proc` / `ws` 也交回去** ——
// 收尾要 `ws.close(); proc.kill()`。
export async function attachCdp() {
  const { proc, port } = await launchShell(["--window-size=1440,1000"]);
  const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener("open", r, { once: true }));
  let msgId = 0;
  const pend = new Map();
  const pageErrors = [];
  ws.addEventListener("message", ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
    if (m.method === "Runtime.exceptionThrown")
      pageErrors.push(m.params.exceptionDetails?.exception?.description
                      || m.params.exceptionDetails?.text);
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
      pageErrors.push(m.params.args.map(a => a.value ?? a.description ?? "").join(" "));
  });
  const send = (method, params = {}) => {
    const id = ++msgId;
    return new Promise(res => { pend.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
  };
  const evalJs = async expression => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
    return r.result?.result?.value;
  };
  const until = async (expr, ms = 8000) => {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline) { if (await evalJs(expr)) return true; await sleep(120); }
    return false;
  };
  // 等**这一侧**(node 侧)某个事实成立 —— 典型是「那个 hold 被哪个请求消费掉了」,
  // 那是服务端这边的事,页面上看不出来。
  const untilHere = async (fn, ms = 6000) => {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline) { if (fn()) return true; await sleep(100); }
    return false;
  };
  // 真鼠标点在元素中心:按钮的 disabled / hidden 只有真实事件才算数。
  //
  // **先滚到它跟前** —— `dispatchMouseEvent` 用的是视口坐标,而历史列表在整页底部
  // (日志框自己就 440px 高),不滚的话算出来的 y 落在视口下方,点出去谁也没点到。
  //
  // **滚完还要做真实命中测试**:算出来的坐标上可能坐着别的东西(浮在页面底部的
  // toast 就正好压住历史列表),点出去的那一下落在它身上。这两种情况下
  // `getBoundingClientRect()` 照旧给一个有宽高、在视口里的矩形,于是"点了但什么也
  // 没发生"会静默绿着 —— 命中测试把它变成红。
  const clickAt = async sel => {
    const box = await evalJs(`(() => { const e = document.querySelector('${sel}');
      if (!e) return null;
      e.scrollIntoView({ block: 'center', behavior: 'instant' });
      const r = e.getBoundingClientRect();
      if (!r.width || !r.height) return null;
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      if (y < 0 || y > innerHeight) return null;
      const at = document.elementFromPoint(x, y);
      if (!at || (at !== e && !e.contains(at))) return null;
      return JSON.stringify({ x, y }); })()`);
    if (!box) return false;
    const { x, y } = JSON.parse(box);
    for (const type of ["mousePressed", "mouseReleased"])
      await send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 });
    return true;
  };
  return { proc, port, ws, send, evalJs, until, untilHere, clickAt, pageErrors };
}
