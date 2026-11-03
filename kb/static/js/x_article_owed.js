/* 「问题与待办」里 X 账号「正文待补」那一条的详情:还在等长文正文的推文,一条一个打开长文页的链接。

   自动同步拿不到正文的长文(不登录那一档只有公开接口拿得到,X 的文章页不登录是登录墙)作为候选
   挂在 X 五天队列里,**还没入库**,所以文章那一侧的「从浏览器采集」够不着它们。人在自己登录着
   的浏览器里点开链接、点采集扩展的「采集这篇长文」,正文交回候选(`kb/feeds/x_article_capture.py`),
   下一次同步这个账号时入库。

   **列表只是旁证,拿不到也不拦住整个详情**(同 `careLoadItemLog`)。这个账号的正文待办里一篇长文
   都没有时整块不画 —— 那几条在等的是别的正文(长推展开),跟人工补长文无关。 */

async function careRenderXArticles(host, row) {
  const box = careBlock(host, '', []);
  let items;
  try {
    const r = await fetch(`/api/x-article-capture/owed?source_id=${encodeURIComponent(row.source_id)}`);
    if (!r.ok) throw new Error(await apiFail(r, '还在等长文的推文没读出来'));
    items = (await r.json()).items;
  } catch (e) {
    careBlock(box, '', [e.message]);
    return;
  }
  if (!items.length) { box.remove(); return; }
  const list = document.createElement('ul');
  list.className = 'care-x-articles';
  for (const item of items) {
    const li = document.createElement('li');
    const link = document.createElement('a');
    link.className = 'care-detail-link';
    link.href = item.article_url || item.url;
    link.target = '_blank';
    link.rel = 'noopener';
    // 推文内容来自 X,一律 textContent;箭头是我们自己的图标,单独插。`published` 已经是项目时区的
    // 「YYYY-MM-DD HH:MM」,原样摆 —— 交给 `careTime` 会按浏览器所在时区再解析一遍。
    link.textContent = [item.published, item.text || '（推文没写字，只有长文）']
      .filter(Boolean).join(' · ');
    link.insertAdjacentHTML('beforeend', arrow('ext'));
    li.appendChild(link);
    list.appendChild(li);
  }
  box.appendChild(list);
  const note = document.createElement('p');
  note.className = 'care-exit-note';
  note.textContent = `${items.length} 篇长文的正文还没取到。在你登录着 X 的浏览器里点开它，等正文显示出来，`
    + '再点采集扩展的「采集这篇长文」；下一次同步这个账号时入库。';
  box.appendChild(note);
}
