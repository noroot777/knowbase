/* 弹窗探针共用的那层宿主:vm 上下文、`chrome.*` 桩、那张请求路由表,外加把 popup.js
 * 跑起来。**这一份只摆桩,一条断言都不许有** —— 用例留在各自那一份探针里。
 *
 * 桩的保真度本身是承重的:下面每条注释都对应一类「桩写省了,某一维的红永远出不来」。
 * 每份探针自带一份服务端答复的状态(`let here = …` 那几个),通过 `answer(path)` 交到
 * 这里;它交回 undefined 时落到下面那几条**不随用例变**的默认答复上。
 * 硬约束见 docs/decisions/link-grab.md 与 browser-capture.md。
 */
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {popupDom} from './popup_dom.mjs';

export const SERVER = 'http://127.0.0.1:8787';
export const PAGE = 'https://example.org/posts/hello';
//: 提取出来的那一份。`page_url` 和 `method` 由桩按真机现填(见下面 `executeScript`)。
export const captured = {page_url: PAGE, title: '页面标题', body: 'x'.repeat(1500), method: 'article'};
//: 真实 `_store` 那一支的回包形状(`kb/web/grab_api.py`)。**`stored_body` 非带不可**
//: —— 它是「这一次把正文写进去了」那个事实(服务端一处算,两个写入入口共用),而那一
//: 档的**每条出口 HTTP 都是 200**:撞上 `DO NOTHING`、库里只有一段订阅同步先落下的
//: 摘要、重抓失败、已经走完整条链的幂等答复。桩里不带它,「200 却没写」和「真写了」
//: 在这儿就长得一模一样,关页按钮那几条红永远出不来。
export const GRABBED = {status: 200, data: {ok: true, added: 1, duplicate: false, done: false,
                                            content_ok: true, stored_body: true,
                                            detail: '已收下，正在写入知识库'}};

export async function popupHost({tab, answer}) {
  const {element, document} = popupDom();
  const calls = [];
  const extracted = [];
  const removed = [];

  const context = vm.createContext({
    URL, Date, JSON, encodeURIComponent,
    document,
    //: 弹窗关掉自己那一下。记次数而不是空函数:标签关了、弹窗还指着一个不存在的
    //: 页面留在屏幕上,是那颗关页按钮唯一会留下的后遗症。
    window: {closed: 0, close() { this.closed += 1; }},
    chrome: {
      //: **每次交回一份新快照,不是同一个对象** —— 真实 `chrome.tabs.query` 就是这样。
      //: 共用一个对象的话,`here`(上一次渲染时问到的那一份)和此刻这一问**永远相等**,
      //: 于是「标签在这中间跳走了」在这个桩里根本表达不出来:改 `tab.url` 会把历史那
      //: 一份也一起改掉,按下去那一刻的重新核验恒真。
      tabs: {query: async () => [{...tab}], remove: async id => { removed.push(id); }},
      scripting: {executeScript: async ({target, files, args}) => {
        if (files) return [{}];
        extracted.push(args[0]);
        //: **按 `tabId` 交回那个标签的东西。** 提取目标传错时(拿上一次 `/here` 留下
        //: 的快照里那个 id 去读),读到的是**另一个标签**的正文、会被收成这一篇;桩不
        //: 分标签的话这种错一个字都看不出来。
        if (target.tabId !== tab.id) {
          return [{result: {page_url: 'https://example.org/posts/other-tab',
                            title: '另一个标签里那一篇', body: 'y'.repeat(1500), method: args[0]}}];
        }
        //: **地址报的是「页面自己此刻在哪」**(真实 `extract.js` 交回的是
        //: `location.href`),不是摆局时写死的那一个 —— 站点同源换篇之后,提取和收录
        //: 走的就是换到的那一页,而展示快照还停在原来那一页。桩里写死一个常量的话,
        //: 「真写进去的是哪一页」和「屏幕上摆的是哪一页」永远分不开。
        return [{result: {...captured, page_url: tab.url, method: args[0]}}];
      }},
      storage: {local: {get: async () => ({server: SERVER, key: 'k'.repeat(44)}), set: async () => {}}}
    },
    apiResponse: async reply => {
      if (reply.status >= 400) { const error = new Error(reply.detail); error.status = reply.status; throw error; }
      return reply.data;
    },
    fetch: async (url, options = {}) => {
      const path = url.replace(SERVER, '');
      calls.push({path, method: options.method || 'GET', sent: options.body ? JSON.parse(options.body) : null});
      //: 探针自己摆的那几答先问它。**它交回 undefined 才落到下面** —— 「这一问当场
      //: 失败」是靠交回一个 4xx/5xx 表达的,所以这里只能认 undefined,不能认假值。
      const staged = answer(path);
      if (staged !== undefined && staged !== null) return staged;
      if (path.endsWith('/pending')) return {status: 200, data: {intents: []}};
      if (path === '/api/browser-capture/precheck') return {status: 200, data: {ok: true, characters: 1200}};
      if (path === '/api/browser-capture/upload') return {status: 200, data: {ok: true, detail: '正文已补入原条目'}};
      if (path === '/api/grab/from-page/precheck') return {status: 200, data: {ok: true, title: '服务端定的标题', characters: 880}};
      throw new Error('unexpected request ' + path);
    }
  });

  const directory = new URL('../kb/static/browser-extension/knowbase-capture/', import.meta.url);
  const source = await readFile(new URL('popup.js', directory), 'utf8');
  const markup = await readFile(new URL('popup.html', directory), 'utf8');
  vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  return {element, document, context, calls, extracted, removed, source, markup};
}
