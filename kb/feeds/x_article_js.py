# -*- coding: utf-8 -*-
"""读一次 X 文章页的那段浏览器脚本(`x_article.browser_fetch` 的后路用它)。

**单独一个文件只有一个理由:行数。** 它是一段 160 多行的 JS 源码 —— 装在
`x_article.py` 里会把那个文件顶到 700 行硬上限之外,而它恰好是里面最独立的一块:
**纯数据**(一个字符串常量),没有模块级可变状态,也没有任何测试打桩它(dom 那组
只读 `x_article._READ_JS` 把它丢给真浏览器 eval)。口径同 `scripts/wiki/taxo_tables.py`
那次拆分 —— **被测试改写的模块变量不能这么搬**(`API` / `PAGE_WAIT` /
`BROWSER_FALLBACK` / `MAX_CHARS` 都被 `patch.object(x_article, ...)` 改过,搬走之后
那些打桩只会改到一份副本),所以它们一个都没动,仍然待在 `x_article` 里。

`x_article` 把它绑回自己的模块全局(`_READ_JS`),所以外面照旧按老名字读它。
"""

# 读一次文章页。三件事一起回答,因为它们在同一个 DOM 快照里才自洽:
# ①**我们还在文章页上吗**(`at`),还是被 X 弹回了推文页(`back`);
# ②正文块(`blocks`);③标题(`title`)。
#
# **`back` 是「这条没有长文」的判据,而且它不押任何 CSS 选择器** —— 实测一条纯图
# 推文的 `/article/<id>` 会被 X 自己 302 回 `/status/<id>`(2026-10-09),那是它的
# 路由行为,比任何 testid 都稳。有长文的那条则老老实实停在 `/article/` 上。
#
# 正文容器认 `[data-contents="true"]` 里的 `[data-block="true"]`(Draft.js 的块),
# 所以**段落边界是按块拿的、不是切 innerText** —— 实测 47 块、2752 字纯正文,
# 比整页 innerText 少掉 234 字的导航、作者简介和「升级 Premium」。**块内按子节点
# 顺序拼**,理由见 `textOf` 上面那段:行内表情是 `<img>`,一块里既有文字又有图
# 是常态。
#
# 标题取 read view 里排除正文之后**字号最大的那个叶子**(实测标题 34px、其余
# ≤15px),拿不到按 read view 的第一行 —— 实测两者是同一个值,互为冗余。
READ_JS = r'''(() => {
  const path = location.pathname;
  const rich = document.querySelector('[data-testid="twitterArticleRichTextView"]');
  const read = document.querySelector('[data-testid="twitterArticleReadView"]');
  const root = document.querySelector('[data-contents="true"]');
  // 预格式化那几种标签里的空格、制表符和换行是**内容**,一个都不许折叠:一段
  // Python 示例的四格/八格缩进被压成一格之后就是无效代码了 —— 照着复制当场
  // `IndentationError`,而我们报的是成功、正文照样落库(`DO NOTHING` 之下永久
  // 改不回来)。判据两道、任一成立即可:计算样式的 `white-space` 以 `pre` 开头
  // (页面 CSS 说了话),或者标签本身是 `pre`/`code`/`samp`/`kbd`(CSS 没加载
  // 时 UA 默认样式也是 `pre`)。
  const PRE_WS = /^(pre|pre-wrap|pre-line|break-spaces)/;
  const preish = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'pre' || tag === 'code' || tag === 'samp' || tag === 'kbd') return true;
    return PRE_WS.test(getComputedStyle(el).whiteSpace || '');
  };
  // 一块的内容按**子节点顺序**拼出来。`<img>` 分两种,判据是 **alt 的内容**:
  // alt **整串都是表情字符**的是行内表情(X 把 emoji 渲染成 twemoji 图片,alt
  // 就是那个字符本身),按 alt 还原;**其余一律是真·配图**,写成 markdown 图片
  // 并把 alt 留在说明位上。
  //
  // **不许拿「有没有 alt」当判据**:给配图配一句描述是无障碍的常规做法
  // (`alt="系统架构示意图"`),按「非空 alt = 表情」会把图片地址整个丢掉,而
  // 剩下那句描述让正文看着非空 —— `browser_fetch` 那道「正文空不空」的判据
  // 发现不了图没了,于是一篇缺图的正文照样落库、从此改不回来。反过来万一 X
  // 哪天把 emoji 的 alt 换成 `:smile:` 这种短代码,我们只是多写一条图片链接,
  // 方向上安全。`<figure>` 里的那张一律按配图办(结构说了话)。
  //
  // **也不许写成「这一块里有 img 就只要图片」**:段落里插一个表情图是极常见的
  // 形态(emoji 全是这样),那一版会把**整段文字丢掉**只留一张小图,而且照样
  // 报成功。也不能用 `innerText`:它压根不包含 img 的 alt,表情会静默消失。
  const EMOJI_CORE = /[\p{Extended_Pictographic}\u20e3\u{1F1E6}-\u{1F1FF}]/u;
  const EMOJI_ONLY =
    /^[\p{Extended_Pictographic}\p{Emoji_Component}\u200d\ufe0f\u20e3]+$/u;
  const textOf = (node, pre) => {
    let s = '';
    for (const n of node.childNodes) {
      if (n.nodeType === 3) {
        let t = n.nodeValue;
        if (!pre) {
          // 非预格式化的那部分照 HTML 自己的空白折叠规则走(源码里的换行和缩进
          // 不是内容),`<br>` 刚插下的那个换行后面不再留一个行首空格。
          t = t.replace(/[\t\n\r\f ]+/g, ' ');
          if (/\n$/.test(s)) t = t.replace(/^ /, '');
        }
        s += t;
        continue;
      }
      if (n.nodeType !== 1) continue;
      if (getComputedStyle(n).display === 'none') continue;
      const tag = n.tagName.toLowerCase();
      if (tag === 'br') { s += '\n'; continue; }
      if (tag === 'img') {
        const alt = (n.getAttribute('alt') || '').trim();
        const emoji = alt && EMOJI_CORE.test(alt) && EMOJI_ONLY.test(alt)
                      && !n.closest('figure');
        if (emoji) { s += alt; continue; }
        const cap = alt.replace(/[\[\]]/g, ' ').trim();
        s += n.src ? '![' + cap + '](' + n.src + ')' : cap;
        continue;
      }
      s += textOf(n, pre || preish(n));
    }
    return s;
  };
  // **预格式化的块要写成 markdown 代码围栏,不能当普通段落交出去。** 读出来的
  // 字符串保真只是半截:裸代码写进 md 之后,阅读页按 markdown 段落渲染,HTML
  // 的空白折叠把换行和缩进一起吃掉 —— 页面上和复制出来都是
  // `def answer(): if True: return 42`,`compile()` 当场 `invalid syntax`
  // (2026-10-09 审查实测,走的是真导出 + 真 `/doc/` 阅读页 + 真选区复制)。
  // markdown 里保住空白只有围栏这一条路:原始 HTML 在阅读页是按**字面文字**
  // 显示的(见 `pages.py` 的 `_DOC_LITERAL_JS`),写 `<pre>` 进去没有用。
  //
  // **判据只认标签,不用计算样式** —— 这跟上面 `preish` 那两道分工不同:
  // 「页面不折叠这一段的空白」证明不了「这一段是代码」。Draft.js 官方默认样式
  // 给**每一个**块都加了 `white-space: pre-wrap`,按它写围栏会把整篇文章每一段
  // 都包成代码块,那是不可逆的毁容;漏判只是回到现状(那一段的缩进在阅读页被
  // 折叠)。两个方向差着一个量级,所以这里问的是「这一块的全部文字是不是都装在
  // 一个 `pre`/`code`/`samp`/`kbd` 里」:块本身或祖先是(真实 Draft.js 代码块
  // 就是 `<pre>` 包着多个 `data-block`),或者块里唯一那个预格式化容器装着这
  // 一块的全部文字。**段落里插一个行内 `<code>` 时它必然为假**(还有别的文字
  // 在外面),所以不会把一整段正文包成代码。
  const codeish = (el) => {
    if (el.closest('pre, code, samp, kbd')) return true;
    const inner = el.querySelector('pre, code, samp, kbd');
    if (!inner) return false;
    const all = (el.textContent || '').replace(/\s+/g, '');
    return !!all && all === (inner.textContent || '').replace(/\s+/g, '');
  };
  const blocks = [];
  // 真实 Draft.js 的代码块是**一行一个 block**(`blockRenderMap` 把相邻的几个
  // 包进同一个 `<pre>`),所以相邻的预格式化块要合并成**一个**围栏 —— 每行各发
  // 一个围栏的话,复制出来是一堆分散的小代码块。「一个块里装多行」走同一条路。
  let pend = [];
  // 围栏内容**一律原样,一个字节都不改**,口径同 `_code_body`:只在**整组边界**
  // 削**完全空**的行。真实 Draft.js 一行一个块,所以「每块收尾」就等于「每行
  // 收尾」—— 而行尾那几个空格可能正是多行字符串、提示词模板、定宽文本的数据,
  // 删掉之后代码照旧 `compile()` 得过而里面的 `assert` 当场失败(2026-10-09
  // 第 4 轮审查实测)。纯空格的行同样可能是数据,所以只削完全空的。
  // **「空行」= 空串,或者整行恰好只由零宽占位构成**(Draft.js 的空块占位是
  // `&#8203;`,外加 BOM)。清它**只在整组边界发生** —— 组**中间**那一行零宽
  // 字符可能正是三引号字符串里的数据,无条件当占位符清掉的话代码照旧
  // `compile()` 得过而**值变了**(2026-10-10 第 2 轮审查实测:`before` / 一行
  // U+200B / 一行 U+FEFF / `after` 这份有效的多行字符串,接口那条路原样保留两个
  // 码点,这条路把它们删掉了)。**位置就是这道判据的证据**:组边界上它只能是
  // 占位符(真要在代码首尾放一行零宽,那一行在任何渲染里都看不出区别),组
  // 中间它只能是内容。
  const blankRow = (r) => r === '' || /^[\u200b\ufeff]+$/.test(r);
  const codeBody = (lines) => {
    const rows = lines.join('\n').split('\n');
    let a = 0, b = rows.length;
    while (a < b && blankRow(rows[a])) a++;
    while (b > a && blankRow(rows[b - 1])) b--;
    const body = rows.slice(a, b).join('\n');
    return body.trim() ? body : '';
  };
  const flush = () => {
    if (!pend.length) return;
    const body = codeBody(pend);
    pend = [];
    if (!body) return;
    let n = 3;
    for (const t of body.match(/`{3,}/g) || []) n = Math.max(n, t.length + 1);
    const bar = '`'.repeat(n);
    blocks.push(bar + '\n' + body + '\n' + bar);
  };
  if (root) for (const b of root.querySelectorAll('[data-block="true"]')) {
    const isCode = codeish(b);
    const text = textOf(b, preish(b));
    if (isCode) {
      // **代码块里一个字节都不改。** 不换行空格(U+00A0)、零宽空格(U+200B)
      // 和 BOM(U+FEFF)在一段代码里是**数据** —— 字符串字面量、定宽对齐、
      // 从别处粘来的样本都可能带着它们,换成普通空格或删掉之后代码照旧
      // `compile()` 得过而**值变了**(2026-10-09 第 1 轮审查实测:接口那条路
      // 原样给出 `[97,160,98,8203,99,65279,100]`,这条路给的是
      // `[97,32,98,99,100]`)。口径同上面那条「行尾空白是数据」:**每一行都
      // 原样攒着**,一个字节都不判、不改。
      //
      // **占位空块的处理整个挪去了 `codeBody`(整组边界那一步)。** 在这里逐块
      // 判「整行是不是只有零宽」会连**组中间**的那种一起清掉,而那一行多半是
      // 三引号字符串里的数据 —— 清掉之后代码照旧编译得过而值变了(见
      // `blankRow` 上面那段实测)。位置是唯一分得开它们的证据,而位置只有
      // 攒完一整组才知道。
      pend.push(text);
      continue;
    }
    // 非代码段照旧归一:零宽占位要清(否则多出一个空段),nbsp 当空格 ——
    // 散文里它是排版产物,不是数据。**ZWJ / ZWNJ 一个都不许动**,它们是组合
    // 表情的一部分,删掉之后一个组合表情当场裂成几个。
    const raw = text.replace(/[\u200b\ufeff]/g, '').replace(/\u00a0/g, ' ');
    flush();
    const txt = raw.trim();
    if (txt) blocks.push(txt);
  }
  flush();
  let title = '', big = 0;
  if (read) {
    for (const e of read.querySelectorAll('*')) {
      if (e.children.length) continue;
      if (rich && rich.contains(e)) continue;
      const t = (e.innerText || '').trim();
      if (!t || t.length > 300) continue;
      const fs = parseFloat(getComputedStyle(e).fontSize) || 0;
      if (fs > big) { big = fs; title = t; }
    }
    if (!title) title = (read.innerText || '').split('\n')[0].trim();
  }
  return JSON.stringify({
    at: /\/article\/\d+\/?$/.test(path),
    back: /\/status\/\d+\/?$/.test(path),
    path: path, blocks: blocks, title: title,
  });
})()'''
