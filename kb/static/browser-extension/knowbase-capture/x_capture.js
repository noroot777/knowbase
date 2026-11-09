/* X 长文的人工补正文 —— 弹窗里单独一块,接管 X 的推文页和长文页。
 *
 * 不登录那一档拿不到的长文(X 的文章页不登录是登录墙),作为候选挂在知识库的 X 五天队列里等正文。
 * 人在自己登录着的浏览器里打开那篇长文、点一下扩展,这里在那一页上跑**同一段**读页脚本
 * (`x_article_read.js`,登录通道的浏览器后路也用它),把快照原样交给服务端。
 *
 * **这里一个字都不判**:这一页是不是那一篇、读没读到正文、能不能写,全由服务端答
 * (`/api/x-article-capture/here` 和 `/save`,判据在 `kb/feeds/x_article_capture.py`)。
 * 它跟「从浏览器采集」那一套(`popup.js` 的待办、候选、关页许可)不共用任何状态 —— 那一套补的是
 * 已经入库的文章,这里补的是还没入库的推文;共用的只有连接和配对密钥(`popup.js` 交过来的 `call`)。
 * 硬约束见 docs/decisions/x-article.md「人工补长文」。
 */
(() => {
  'use strict';
  const PAGE = /^https:\/\/(?:www\.|mobile\.)?(?:x|twitter)\.com\/[A-Za-z0-9_]{1,20}\/(?:article|status)\/\d+(?:[/?#]|$)/;
  const element = id => document.getElementById(id);
  let tools = null;
  let answer = null;
  let busy = false;

  const claims = url => PAGE.test(url || '');
  const LABELS = {fill: '采集这篇长文', absent: '采集', unfinished: '继续收录'};

  function render() {
    const post = answer && answer.post;
    element('x-state').textContent = answer ? answer.detail : '正在确认这一篇…';
    // 推文内容来自 X,一律 textContent(拿 innerHTML 拼等于把源站的字当标记送进这个有 chrome.* 的上下文)。
    element('x-post').textContent = post ? [post.author, post.published, post.text].filter(Boolean).join(' · ') : '';
    element('x-post').hidden = !post;
    // 三档有出路,都由服务端那一答定:`fill` 读这一页补进那条在等的推文;`absent`(库里没有、同步也没在
    // 收)交给手动收录;`unfinished`(收下了、还没走完导出和索引)重投一次,补上欠的那一步。`syncing`
    // 一颗都不给 —— 同步下一轮就投,现在再收是两份。**按推文 id 分档,不按地址**(`x_article_capture.here`)。
    const situation = answer && answer.situation;
    element('x-act').hidden = !Object.hasOwn(LABELS, situation || '');
    element('x-act').textContent = LABELS[situation] || '';
    element('x-act').disabled = busy;
    element('x-refresh').disabled = busy;
  }

  async function ask(tab) {
    answer = null;
    element('x-url').textContent = tab.url;
    render();
    answer = await tools.call('/api/x-article-capture/here', {method: 'POST', body: JSON.stringify({url: tab.url})});
    render();
  }

  async function show(tab, given) {
    tools = given;
    element('here').hidden = true;
    element('x-article').hidden = false;
    await ask(tab);
    tools.status('');
  }

  function release() {
    if (element('x-article').hidden) return;
    element('x-article').hidden = true;
    element('here').hidden = false;
    answer = null;
  }

  async function currentTab() {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!tab?.id || !claims(tab.url)) throw new Error('当前标签已经不是这篇长文了，请重新确认');
    return tab;
  }

  //: `absent` / `unfinished` 两档:交服务端给的 `grab_url` 给手动收录那个入口(`/api/grab`)。前者收成
  //: 一条新的;后者那一条已经在收件桶里,重投落进它的重复那一档、补上欠的导出或索引。扩展不自己拼地址。
  async function grab() {
    busy = true;
    render();
    try {
      const result = await tools.call('/api/grab', {method: 'POST', body: JSON.stringify({url: answer.grab_url})});
      try { await ask(await currentTab()); } catch { /* 已经收下了,刷新失败不改变这件事 */ }
      tools.status(result.detail || '已收下');
    } catch (error) { tools.status(error.message, true); }
    finally { busy = false; render(); }
  }

  async function collect() {
    if (busy || !answer) return;
    if (['absent', 'unfinished'].includes(answer.situation)) {
      if (answer.grab_url) return grab();
      // 只有比扩展旧的服务会这样答(那时 `absent` 还不给收录地址):按钮露着、点了却没动静,人只会以为采不了。
      return tools.status('知识库服务还是旧版本，重启 kb-web（./kb.sh restart）后点「重新确认」', true);
    }
    if (answer.situation !== 'fill') return;
    busy = true;
    render();
    try {
      // 标签现读一次:上一次确认之后,人可能已经点到了别的推文上。
      const tab = await currentTab();
      const results = await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['x_article_read.js']});
      const raw = results[0]?.result;
      if (typeof raw !== 'string') throw new Error('页面没有交回内容，未上传任何东西');
      const result = await tools.call('/api/x-article-capture/save', {method: 'POST', body: JSON.stringify(
        {post_id: answer.post.post_id, page_url: tab.url, snapshot: JSON.parse(raw)})});
      // 已经写回了。这一问失败只是「状态没刷新」,不该让人以为没保存。
      try { await ask(tab); } catch { /* 下面照旧报服务端那句话 */ }
      tools.status(`${result.title}：${result.detail}（${result.characters} 字符）`);
    } catch (error) { tools.status(error.message, true); }
    finally { busy = false; render(); }
  }

  element('x-act').addEventListener('click', collect);
  element('x-refresh').addEventListener('click', async () => {
    if (busy || !tools) return;
    busy = true;
    render();
    try { await ask(await currentTab()); tools.status(''); }
    catch (error) { tools.status(error.message, true); }
    finally { busy = false; render(); }
  });

  globalThis.knowbaseXArticle = {claims, show, release};
})();
