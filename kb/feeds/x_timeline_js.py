# -*- coding: utf-8 -*-
"""注进 X 时间线页面的那几段 JS —— 收集器、复位、滚动、页面状态。

从 `x_browser` 拆出来的(那边顶着 700 行硬上限)。拆的面是「**它是不是跑在页面里
的代码**」:这边四个常量全是要交给 `page.eval` 的字符串,`x_browser` 那边才是
Python 这一侧的判据和流程。**选这个面而不是选那一族判据**,是因为这几段是稳定的
数据、而且全仓**一条桩都没有**(只被读:`x_browser._STATE_JS` 一类);判据那边正
相反 —— 回归在 `x_browser._entry` / `_select` 上打桩,搬走就拦不住了(名字带的是
绑定不是引用),而且那几个函数还在频繁改。

`x_browser` 里**按旧位置再导出一次**,全仓回归里那些 `x_browser._COLLECT_JS` /
`_STATE_JS` 的指向照旧成立。
"""

from __future__ import annotations

from kb.feeds import x_account, x_post
from kb.feeds.x_page_state import STATE_JS

# 页面里的收集器:挂在 window 上跨多次 eval 累积,因为虚拟列表会把滚过去的
# 节点回收掉 —— 滚完再统一取就只剩最后一屏。按推文 id 去重。
#
# **`items` 的顺序就是推文在时间线上出现的顺序**,这件事下面 `_old_enough` 要用:
# Map 记插入顺序,而首次插入发生在这条推文第一次出现在 DOM 里的时候;对已有 key
# 再 `set` 不会改变它的位置。所以「连续几条」问的是页面顺序,不是时间排序 ——
# 按时间排序的话,三条不相邻的旧转推会被拼成一条"连续的老尾巴"。
_COLLECT_JS = r'''
(() => {
''' + x_post.TEXT_JS + r'''
  const S = window.__kbX = window.__kbX || {seen: new Map()};
  const norm = s => (s || '').replace(/ /g, ' ').trim();
  let parsed = 0, articles = 0;
  for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
    articles++;
    const time = art.querySelector('time[datetime]');
    const anchor = art.querySelector('a[href*="/status/"]');
    if (!time || !anchor) continue;
    const m = anchor.getAttribute('href').match(/^\/([^\/]+)\/status\/(\d+)/);
    if (!m) continue;
    parsed++;
    const ctx = norm(art.querySelector('[data-testid="socialContext"]')?.innerText);
    const text = postText(ownNode(art, '[data-testid="tweetText"]'));
    const truncated = postMore(art);
    // 作者块的第一行是显示名,后面跟着 @handle 和时间,只取第一行。
    const who = norm(art.querySelector('[data-testid="User-Name"]')?.innerText).split('\n')[0];
    // **长文卡片**:X 把推文里那条 `/i/article/<id>` 链接藏起来、改渲染成一张
    // 卡片,所以「写了一句介绍 + 一张长文卡片」的推文在 `tweetText` 上看不出
    // 任何异样 —— 正文非空,补全那一步的另两条判据都不命中,整篇文章就这么漏
    // 掉了。卡片本身在 DOM 里仍然是一条链接,把它读出来当信号(**地址不用它**,
    // 见 `x_article.HINT`:文章页地址由推文自己的 URL 推出来)。
    // **排掉引用推里面的那一张**(`div[role="link"]` 是引用容器):那是别人的
    // 文章,按它去补就是把别人的正文记成这条推文的内容。排错了也只是回到漏掉
    // 的现状,而收错了是污染 —— 这个方向上宁可多排。
    const quote = art.querySelector('div[role="link"]');
    const quoted = !!art.querySelector('[data-testid="quoteTweet"], div[role="link"] [data-testid="tweetText"]');
    const card = [...art.querySelectorAll('a[href*="/article/"]')]
                   .find(a => !(quote && quote.contains(a)));
    const art_url = card ? card.href : '';
    const id = m[2];
    const prev = S.seen.get(id);
    // 同一条推文会在多次滚动里重复出现;文本偶尔首次渲染不全(折叠/懒加载),
    // 所以后出现的更长文本覆盖前一次。卡片同理可能这一次没渲染出来,所以它
    // **只补不清**:认过一次就记着。
    if (!prev || (text && text.length > (prev.text || '').length)) {
      S.seen.set(id, {id, author: m[1], name: who, datetime: time.getAttribute('datetime'),
                      text, context: ctx, url: `https://x.com/${m[1]}/status/${id}`,
                      article: art_url || (prev && prev.article) || '',
                      quoted: quoted || !!prev?.quoted,
                      truncated: truncated || !!prev?.truncated});
    } else if (art_url && prev && !prev.article) {
      prev.article = art_url;
    }
    if (truncated) S.seen.get(id).truncated = true;
    if (quoted) S.seen.get(id).quoted = true;
  }
  return JSON.stringify({articles, parsed, items: [...S.seen.values()]});
})()
'''

_RESET_JS = 'window.__kbX = {seen: new Map()}; window.scrollTo(0, 0); true'
# 滚一屏,顺带回答「滚完是不是停在底部了」。这个答案只在随后"没滚出新东西"时
# 才用得上(见 `_BOTTOM_TRIES`):那时页面没长高,这次测量仍然有效。
_SCROLL_JS = r'''
(() => {
  const el = document.scrollingElement || document.documentElement;
  window.scrollBy(0, Math.round(window.innerHeight * 0.85));
  const left = el.scrollHeight - (window.scrollY + window.innerHeight);
  return JSON.stringify({bottom: left < 40});
})()
'''
# 登录态失效的几种落地页。X 会跳 /i/flow/login,也会在原地渲染登录按钮。
_STATE_JS = r'''
(() => {
''' + STATE_JS + x_account.ACCOUNT_JS + r'''
  return JSON.stringify({...xPageState(), account: xAccountState()});
})()
'''
