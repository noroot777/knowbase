/* 扩展在知识库页面上留的那个标记:**「这个浏览器配过扩展没有」只有它答得出来。**
 *
 * 采集抽屉据此决定首次引导摊开还是收起。原先问的是服务端「全库有没有一张采集回执」,
 * 那是**另一个范畴的事实** —— 回执是全库共享的历史,引导问的是此刻这个浏览器的配对
 * 状态。错位出来的两种形态都真会发生:①另一个浏览器(或重装之前的这一个)采成过一次,
 * 于是还没配对的这一个也看不到引导,他只会点下「发起采集」然后等一个永远不来的采集;
 * ②只用「改用这一页的正文」收录新链接的人一直有配对,那条路却只写 `body_provenance`、
 * 从不插审计表,引导就永远摊在他眼前。
 *
 * 钉五件事:①配过就标,标的是版本号;②没密钥 = 没配过,一个字都不标;③**服务地址和
 * 这一页不是同一个 origin 时不标**;④跟着 `storage` 变,不等刷新;⑤结构上只碰那一个
 * 属性。第③条是承重的:`matches` 只能写到回环地址那一级(Chrome 的匹配式里写不了端口),
 * 收窄那一道就落在脚本自己身上 —— 配到 8787 的扩展在 8788 那个知识库上够不着服务端,
 * 对那一页来说它和没装是一回事;这一道没有的话「端口配错了」会被当成「配好了」,而那
 * 恰恰是最需要看到引导的人。
 */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL(
  '../kb/static/browser-extension/knowbase-capture/present.js', import.meta.url), 'utf8');
const KEY = 'k'.repeat(44);

//: `store` 原样交给假的 `chrome.storage.local.get`,所以用例改它就等于改了扩展本地
//: 存的那一份;`fire` 是手动触发一次 `onChanged`。
function run(store, origin = 'http://127.0.0.1:8787') {
  const dataset = {};
  let changed = null;
  vm.runInContext(source, vm.createContext({
    URL,
    location: {origin},
    document: {documentElement: {dataset}},
    chrome: {
      runtime: {getManifest: () => ({version: '1.2.0'})},
      storage: {
        local: {get: (names, done) => done(store)},
        onChanged: {addListener: callback => { changed = callback; }}
      }
    }
  }));
  return {
    dataset,
    //: 先确认监听器真挂上了 —— 没挂的话下面那几条会以 TypeError 收场,红是红了,却看
    //: 不出红在哪件事上。
    fire(changes, area = 'local') {
      assert.equal(typeof changed, 'function', '没挂 onChanged 等于「配对完成要等刷新才算」');
      changed(changes, area);
    }
  };
}

// ① 配过:标上。标的是版本号 —— 人靠弹窗头部那个数字确认「重新加载」点过没有,
//    这一处同源,顺手让「页面上看到的是哪一版」也答得出来。
assert.equal(run({server: 'http://127.0.0.1:8787', key: KEY}).dataset.knowbaseCapture, '1.2.0',
  '配过就该标出来');

// ② 没密钥 = 没走完配对,一个字都不标(这正是要看到引导的那个人)。
assert.equal(run({server: 'http://127.0.0.1:8787', key: ''}).dataset.knowbaseCapture, undefined,
  '只填了服务地址、还没粘密钥的不算配过');
assert.equal(run({}).dataset.knowbaseCapture, undefined, '什么都没存过时同理');

// ③ 服务地址不是这一页:不标。
assert.equal(run({server: 'http://127.0.0.1:8788', key: KEY}).dataset.knowbaseCapture, undefined,
  '配到另一个端口的扩展在这一页上够不着服务端,对它来说和没装一样');
assert.equal(run({server: 'http://localhost:8787', key: KEY}).dataset.knowbaseCapture, undefined,
  'localhost 和 127.0.0.1 是两个 origin —— 浏览器自己也这么算,这里不许比得更宽');
assert.equal(run({server: '不是个地址', key: KEY}).dataset.knowbaseCapture, undefined,
  '存着的地址解析不了时按「够不着」办,不是按「配过」办');

// ④ 跟着 `storage` 变。**密钥就是从知识库那一页复制的**,所以配对几乎总发生在它开着的
//    时候;只在注入时读一次的话,引导要等人刷新才收起来。反过来一边也钉上:密钥被清掉
//    (换服务、重新配对)就该当场撤掉标记,否则抽屉会在一个已经配不上的浏览器里收起引导。
const store = {server: 'http://127.0.0.1:8787', key: ''};
const session = run(store);
assert.equal(session.dataset.knowbaseCapture, undefined);
store.key = KEY;
session.fire({key: {newValue: KEY}});
assert.equal(session.dataset.knowbaseCapture, '1.2.0', '配好那一刻就该标上,不必等刷新');
store.key = '';
session.fire({key: {newValue: ''}});
assert.equal(session.dataset.knowbaseCapture, undefined, '密钥被清掉就该把标记撤掉');

// ⑤ 结构:它只许碰那一个属性。这个内容脚本跑在人**每一个**本机页面上,读页面内容
//    这件事不在它的职责里 —— README 的「权限与边界」正按这个写。
assert.doesNotMatch(source, /document\.(body|cookie|title)|innerHTML|textContent|querySelector/,
  '这个脚本不许读页面内容');

console.log('capture present mark: passed');
