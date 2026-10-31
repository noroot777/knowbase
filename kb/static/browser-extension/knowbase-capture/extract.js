function readKnowbaseArticle(mode) {
  const address = new URL(location.href);
  if (!['http:', 'https:'].includes(address.protocol)) throw new Error('此页不是可采集的 HTTP / HTTPS 文章');
  if (['x.com', 'twitter.com'].some(host => address.hostname === host || address.hostname.endsWith(`.${host}`))) {
    throw new Error('此入口不采集 X 时间线或观点，请使用对应的 X 来源通道');
  }
  const title = (document.querySelector('h1')?.innerText || document.title || '').trim().slice(0, 1000);
  let body = '';
  if (mode === 'selection') {
    body = window.getSelection()?.toString().trim() || '';
    if (!body) throw new Error('请先在文章里选中正文，再点一次「所选文字」那一颗');
  } else {
    const selector = 'article,[itemprop="articleBody"],#js_content,.rich_media_content,.entry-content,.post-content,.article-body,.markdown-body,main,[role="main"]';
    const candidates = [...document.querySelectorAll(selector)].filter(node => {
      const style = getComputedStyle(node);
      return style.display !== 'none' && style.visibility !== 'hidden' && node.getClientRects().length;
    });
    const blocks = new Set(['P', 'DIV', 'SECTION', 'ARTICLE', 'H1', 'H2', 'H3', 'H4', 'LI', 'PRE', 'BLOCKQUOTE', 'BR', 'TR']);
    function cleanText(candidate) {
      const copy = candidate.cloneNode(true);
      const originals = [candidate, ...candidate.querySelectorAll('*')];
      const copies = [copy, ...copy.querySelectorAll('*')];
      if (originals.length > 15000) return '';
      for (let index = originals.length - 1; index > 0; index -= 1) {
        const style = getComputedStyle(originals[index]);
        if (style.display === 'none' || style.visibility === 'hidden') copies[index].remove();
      }
      copy.querySelectorAll('script,style,nav,aside,footer,header,form,input,textarea,select,button,dialog,[hidden],[aria-hidden="true"],[role="navigation"],[role="dialog"],[contenteditable="true"],.comments,#comments').forEach(node => node.remove());
      const output = [];
      function visit(node) {
        if (node.nodeType === Node.TEXT_NODE) { output.push(node.textContent); return; }
        const block = blocks.has(node.nodeName);
        if (block) output.push('\n');
        node.childNodes.forEach(visit);
        if (block) output.push('\n');
      }
      visit(copy);
      return output.join('').split('\n').map(line => line.replace(/[\t \u00a0]+/g, ' ').trim()).join('\n').replace(/\n{3,}/g, '\n\n').trim();
    }
    const scored = candidates.map(node => {
      const text = cleanText(node);
      const linkText = [...node.querySelectorAll('a')].reduce((total, link) => total + (link.innerText || '').length, 0);
      return {text, score: text.length - linkText * 1.5 + (node.matches('article,[itemprop="articleBody"],#js_content') ? 150 : 0)};
    }).sort((first, second) => second.score - first.score);
    body = scored[0]?.text || '';
    if (!body) throw new Error('没有识别出文章区域；请先在页面选好全文，再点「所选文字」那一颗');
  }
  // 「够不够」「超不超」由服务端判(`kb/browser_capture._admit_body`:按**行**去重后的
  // 字数门槛)。在这里再写一遍数字就是两份口径 —— 实测复读的登录提示页在这边 1367
  // 字过线、那边去重只剩 260 字,于是人读完整页、勾完确认才被拒。提取器只回答
  // 「有没有从这一页读到东西」,上面两个分支各自已经挡住空正文。
  return {page_url: location.href, title, body, method: mode};
}
