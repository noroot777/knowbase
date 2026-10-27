// 日报正文里那些出处编号要能点 —— **从 daily.js 拆出来只因为行数**(那边到了
// CLAUDE.md 那条 700 行上限)。这一块自成一件事:只读渲染之后的那一篇正文,不问
// 任何接口、不碰面板状态,入口就一个 `enhanceDailyBody()`(由 `openDailyReport`
// 调)。排在 daily.js **之前**加载:`DAILY_REF_TEXT` 是 const,const 有 TDZ。
// ── 正文里的出处链接 ────────────────────────────────────────────────────────────
// 序号→原文的对应关系写在每份日报自己的素材清单里,所以这里不问接口,只读这一篇。
// **读的是渲染之后的那个 <ol>,不是 markdown 原文**:清单那几行的目标写法有两种
// (老日报裸写 url、新日报套尖括号),而裸写的 url 自己可以带平衡圆括号 ——
// `https://…/paper-(clean).pdf` 用正则一路吃到第一个 `)` 就断,那一条整行匹配
// 不上、编号也就补不出链接(2026-09-30 审查实测,生产那份日报里真有这么一条)。
// marked 本来就按 CommonMark 把它解析对了,照抄解析结果比再写一遍解析器可靠。
function dailySafeLink(url) {
  if (!url || !window.DOMPurify) return null;
  const link = document.createElement('a');
  link.setAttribute('href', url);
  DOMPurify.sanitize(link, { IN_PLACE: true });
  return link.hasAttribute('href') ? link : null;
}

function dailyMarkdownLink(url) {
  if (!url || /[<>\r\n]/.test(url)) return null;
  const host = document.createElement('div');
  host.innerHTML = mdHtml(`[原文](<${url}>)`);
  return host.querySelector('a[href]');
}

function dailyRefMap(el, materialList = null) {
  const out = new Map();
  const head = !materialList && [...el.querySelectorAll('h2')].find(h => /^素材清单/.test(h.textContent.trim()));
  let list = materialList || (head && head.nextElementSibling);
  // 清单为空时别一路找到文末去认别的有序列表 —— 下一个小节标题就是边界。
  while (list && list.tagName !== 'OL' && list.tagName !== 'H2') list = list.nextElementSibling;
  if (!list || list.tagName !== 'OL') return { refs: out, list: null };
  // 清单是从 1 数起的一段有序列表;真被改过 start 就按它算。
  const base = Number(list.getAttribute('start')) || 1;
  [...list.children].forEach((li, i) => {
    const a = li.querySelector('a[href]');
    // 没有 url 的素材那一行没有链接,补不出来 —— 跳过就是对的。
    if (a) out.set(String(base + i), { url: a.getAttribute('href'), title: li.textContent.trim() });
  });
  return { refs: out, list };
}

// 正文里那些出处编号要能点。新日报落盘时就已经写成链接了(kb/daily.py 的
// `_link_refs`),这里补两件事:
//   ① 那之前生成的日报编号还是纯文本。它们是花过模型钱的产物,不该为了加链接
//      重新生成一份(重算窗口还会换掉内容),照素材清单补上就是。新日报上这一步
//      是空转——编号已经在 <a> 里,遍历时直接跳过。
//   ② 外链一律新标签打开。日报正文和素材清单指的都是站外地址,在当前标签里跳走
//      等于把 kb-web 连同正在跑的管道面板一起顶掉。
// 认的编号写法和落盘那条路一致(`[1]` / `[1, 2]` / `[1、2]`):两边不一样的话,
// 同一种写法在新旧日报上一个能点一个不能。
const DAILY_REF_TEXT = /\[(\d{1,3}(?:\s*[,，、]\s*\d{1,3})*)\]/g;

function enhanceDailyBody(el, materialList = null) {
  const { refs, list } = dailyRefMap(el, materialList);
  if (refs.size) {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
      // 已经是链接的编号、代码块里的数字、清单自己那一段都别碰
      acceptNode: n => n.parentElement && (n.parentElement.closest('a, code, pre')
        || (list && list.contains(n))) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
    const todo = [];
    // 粗筛用无状态的那条:带 g 的正则 test() 会把 lastIndex 留在命中处,
    // 下一个文本节点就从半截位置开始找,开头的编号会被漏掉。
    for (let n = walker.nextNode(); n; n = walker.nextNode())
      if (/\[\d/.test(n.nodeValue)) todo.push(n);
    // 先收集再替换:边走边改会把 walker 的游标扔在一个已经离开文档的节点上。
    for (const node of todo) {
      const text = node.nodeValue, frag = document.createDocumentFragment();
      let last = 0;
      for (const m of text.matchAll(DAILY_REF_TEXT)) {
        const nums = m[1].split(/[,，、]/).map(n => n.trim());
        // 一个都对不上就整段别动:那多半不是出处编号(年份区间、条目序号之类)。
        if (!nums.some(n => refs.has(n))) continue;
        frag.append(text.slice(last, m.index));
        for (const n of nums) {
          const hit = refs.get(n);
          if (!hit) { frag.append(`[${n}]`); continue; }
          const a = dailySafeLink(hit.url);
          if (!a) { frag.append(`[${n}]`); continue; }
          a.title = hit.title; a.textContent = `[${n}]`;
          frag.append(a);
        }
        last = m.index + m[0].length;
      }
      frag.append(text.slice(last));
      node.replaceWith(frag);
    }
  }
  prepareMarkdownLinks(el);
  for (const a of el.querySelectorAll('a[href]')) {
    // 角标样式认的是"链接文字就是 [12]",落盘写进去的和刚补上的一视同仁。
    if (/^\[\d{1,3}\]$/.test((a.textContent || '').trim())) a.classList.add('daily-ref');
  }
}
