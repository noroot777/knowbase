/** 「**这个浏览器**里装了扩展、而且配过」—— 这件事只有扩展自己知道,所以由它交给页面。
 *
 *  采集抽屉要据此决定首次引导摆在哪(顶上摊开 / 底部折叠)。它原先问的是服务端
 *  「全库有没有一张采集回执」—— 那是**另一个问题**:回执是全库共享的历史,引导
 *  回答的是此刻这个浏览器的配对状态。两种实测错法都出自这个错位:①另一个浏览器
 *  (或重装之前的这一个)采成过一次,于是还没配对的这一个也看不到引导,他只会点下
 *  「发起采集」然后等一个永远不来的采集;②只用「改用这一页的正文」收录新链接的人
 *  一直有配对,那条路却只写 `body_provenance`、从不插审计表,引导就永远摊在他眼前。
 *
 *  交出去的只有一个 data 属性。**页面内容一个字都不读**,也不碰别的 DOM。
 *
 *  **`matches` 只能写到回环地址那一级** —— Chrome 的匹配式里写不了端口,而服务
 *  地址的端口是人自己配的。收窄那一道落在这里:存着的 `server` 和这一页不是同一个
 *  origin 就什么都不标。配到 8787 的扩展在 8788 那个知识库上够不着服务端,对那一页
 *  来说它和没装是一回事,引导就该摊开 —— 这一道没有的话,「配错了端口」会被当成
 *  「配好了」,而那正是最需要看到引导的人。
 *
 *  **跟着 `storage` 变**:人在知识库页面开着的时候完成配对(这是最常见的顺序 ——
 *  密钥就是从那一页复制的),只在注入时读一次的话,引导要等他刷新才收起来。
 */
(() => {
  'use strict';
  const MARK = 'knowbaseCapture';

  const mine = server => {
    try { return new URL(server).origin === location.origin; } catch { return false; }
  };

  function mark(store) {
    const paired = !!store.key && !!store.server && mine(store.server);
    if (paired) document.documentElement.dataset[MARK] = chrome.runtime.getManifest().version;
    else delete document.documentElement.dataset[MARK];
  }

  const reread = () => chrome.storage.local.get(['server', 'key'], mark);

  reread();
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && ('key' in changes || 'server' in changes)) reread();
  });
})();
