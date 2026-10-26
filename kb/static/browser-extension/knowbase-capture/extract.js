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
    if (!body) throw new Error('请先在文章里选中正文，再点击读取所选文字');
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
    if (!body) throw new Error('没有识别出文章区域；请在页面选好全文，再使用读取所选文字');
  }
  if (body.length < 400) throw new Error('只读到不足 400 字符，请确认全文已加载，或选好完整正文');
  if (body.length > 200000) throw new Error('正文超过 20 万字符，本次未截断或上传');
  return {page_url: location.href, title, body, method: mode};
}
