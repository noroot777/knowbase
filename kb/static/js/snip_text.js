// ── 收藏片段的「文本坐标系」:一份正文 → 可匹配的全文 + 回 DOM 的映射 ─────────
// 从 snip.js 里分出来(那份连着 UI 和网络已经顶到行数上限),这一份只管文本:
// 压平、半个字、显示分隔、坐标系、锚点规范化和定位搜索。`snip.js` 与
// `kb/web/pages.py` 的内联副本都按这一份的口径算。**index.html 里排在 snip.js
// 之前。**
//
// 这里的不变量有两条,写在一起才说得清:
//   1 **落库的 `quote` 等于用户在屏幕上选中的那份文字**(只许裁首尾空白)——
//     包括**元素产生的换行**:`<br>`、段落/列表/表格边界在 DOM 文本节点里一个
//     字符都不占,而用户看到的是分行的。拿文本节点串当原文存,两行当场接成一行
//     (审查实测:问答回答里 `第一行<br>第二行`,`Selection.toString()` 是
//     `第一行\n第二行`、`Range.toString()` 是 `第一行第二行`,落库的是后者)。
//   2 **匹配坐标系按同一套显示分隔规则建**,再压平空白。两条规则同源,所以
//     「`quote` 压平一遍 = 坐标系里那一段」照旧成立 —— 否则保真修好了,回跳却
//     再也找不到。
// 口径只有一份:**显示分隔一律过 `snipBreaksOf()`**(活文档里问计算样式、离线才
// 退回标签名名单),坐标系和取原文两处都用它。
//
// **这两条不变量的优先级不一样,别倒过来。** 第 1 条是内容本身,错了不可恢复
// (库里那几个字再也找不回来);第 2 条是能不能点回去,对不上的代价只是一句
// 「可能定位不到」。所以**坐标系对不上的时候放弃定位,不是改内容去迁就坐标**
// (`snipSpec` 的 `locatable` 那一行)。

// 前后文锚点各取多少字:**从 30 起,不唯一就加长**。同一段话在一篇里出现两次
// 时,它两头 30 字也可能一字不差(重复的段落、模板说明、重复的代码块)—— 那时
// 「整段锚点命中了」证明不了位置,而正文再往外一点就不一样了。长到唯一为止,
// 回跳才落得回用户选的那一处。三档都不唯一就用最短那档:那是正文自己分不出的
// 两处,回跳那边会如实说没找到(见 snipSeek),不许静默挑第一处。
// 阅读页那份内联副本(`kb/web/pages.py` 的 `_DOC_SNIP_JS`)是同一个阶梯。
const SNIP_CTX_STEPS = [30, 80, 200];

// 定位时一侧的候选数到这个数就不往下数了:判据只问「是不是正好一处」,数到
// 第二处就已经定论。**数不完的那一侧一律当说不清**(见 snipSeek)—— 截断过的
// 集合拿去取交集会让交集偏小,而偏小正好是误采信的方向。
const SNIP_MAX_HITS = 64;

// ── 坐标系 ────────────────────────────────────────────────────────────────────
// `SNIP_WS` 和 `snipCollapse` 认的是同一个字符类(JS 的 `\s`,含 NBSP),所以
// 逐字走和整串替换压出来的结果逐位相同 —— 两套规则会让采集和回跳差一两个空格。
const SNIP_WS = /\s/;
function snipCollapse(text) {
  return String(text == null ? '' : text).replace(/\s+/g, ' ');
}

// ── 不许切开一个字 ────────────────────────────────────────────────────────────
// JS 的字符串按 UTF-16 存:emoji 一类字占两个单元(代理对),按单元 `slice` 会把
// 它切成两半。**孤立的那一半不是合法字符** —— Python 的 str 装得下它、UTF-8 编
// 不出来,于是 sqlite3 在 INSERT 那一句当场抛 UnicodeEncodeError,整条 POST 变成
// 500(审查实测:正文里一个 emoji 后面隔着 29 个字母,选中再后面那句普通中文,
// 日报和独立阅读页都收藏失败 —— 而用户选的那几个字里一个 emoji 都没有,是**前后文
// 窗口**正好切在那个 emoji 中间)。
//
// 所以**每一处会进库的截断都得过这一道**:前后文窗口(`snipContext`)、标题上限
// (`postSnip`、`chat.js` 的提问标题)、阅读页那份内联副本(`kb/web/pages.py` 的
// `whole`/`contextOf`)。只削掉边上那半个字,正文里合法的字一个不动 —— 替换合法
// 原文不是修法,那动的是用户收下来的引文。后端还有一道:非法字符串如实拒成 400
// (`kb/web/favorites_api.py` 的 `_LONE_SURROGATE`),不等到 INSERT 抛 500。
const SNIP_LOW_HALF = /^[\uDC00-\uDFFF]/;    // 代理对的后一半(它的前一半被切掉了)
const SNIP_HIGH_HALF = /[\uD800-\uDBFF]$/;   // 代理对的前一半(它的后一半被切掉了)

/** 掐掉两头那半个字(截断只会在边上留下半个)。*/
function snipWhole(text) {
  let s = String(text == null ? '' : text);
  if (SNIP_LOW_HALF.test(s)) s = s.slice(1);
  if (SNIP_HIGH_HALF.test(s)) s = s.slice(0, -1);
  return s;
}

/** 按长度上限截断,**不切开字符**。给要进库的快照字段用。*/
function snipClip(text, max) {
  return snipWhole(String(text == null ? '' : text).slice(0, max));
}

// ── 显示分隔:DOM 里不占字符、屏幕上却换了行的那些边界 ────────────────────────
// **屏幕上分不分行,只有浏览器说得准** —— 那是 CSS 的事,不是标签名的事。正文
// 里的 HTML 是内容自带的(聚合源、RSS 正文、净化后留下的行内样式),
// `<span style="display:block">` 在屏幕上就是两行,而标签名名单里没有 SPAN:
// 按名单判就会把那两行接成一行(审查实测:两段 `display:block` 的 span,浏览器
// 选区是 `第一行\n第二行`,按名单拼出来的是 `第一行第二行`)。
//
// 所以**活文档里一律问 `getComputedStyle`**:`display` 不是 inline 那一族就是
// 一处显示分隔。名单只做**离线退路** —— 核验那一侧(`snipRenderedText`)在
// `DOMParser` 造的文档上跑,那里压根没有布局,`display` 问不出来。两条路都通向
// 同一个判据函数(`snipBreaksOf`),**不许一处按样式、另一处按名单各判一遍**。
//
// 离线退路的能力边界要说清:它只认 markdown 渲染结果里的那些块级标签,**认不出
// CSS 造成的分隔**。所以核验那一头只拿它做压平比较(换行个数无关),而**采集那
// 一头不靠它定保真** —— 保真走屏幕选区(见 `snipShown`),对不上就如实放弃定位。
//
// 名单按「markdown 渲染结果 + 正文容器可能的壳」取。漏一个的后果是离线那头少
// 一处分隔(压平之后无害),多一个只是多一个空格 —— 所以**名单宁可宽**。
const SNIP_BREAKS = new Set([
  'BR', 'P', 'DIV', 'PRE', 'BLOCKQUOTE', 'HR',
  'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
  'UL', 'OL', 'LI', 'DL', 'DT', 'DD',
  'TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'TD', 'TH', 'CAPTION',
  'SECTION', 'ARTICLE', 'HEADER', 'FOOTER', 'ASIDE', 'NAV', 'MAIN',
  'FIGURE', 'FIGCAPTION', 'DETAILS', 'SUMMARY', 'ADDRESS', 'FIELDSET',
]);

// `display` 这几个值在屏幕上**不**换行;其余(block、list-item、table-*、flex、
// grid、flow-root…)都换。`inline-*` 一族(inline-block/inline-flex)算行内。
const SNIP_INLINE_DISPLAY = new Set(['inline', 'contents', 'ruby', 'ruby-text', 'ruby-base']);

/** 这个元素的计算样式(拿不到就回空串)。活文档之外没有布局,问不出来。*/
function snipDisplayOf(el) {
  const win = el.ownerDocument && el.ownerDocument.defaultView;
  if (!win || !win.getComputedStyle || !el.isConnected) return '';
  try { return win.getComputedStyle(el).display || ''; } catch { return ''; }
}

/** 这个元素的边界在屏幕上是不是一处换行。**活文档里按计算样式**(CSS 才是
 *  屏幕上那份排版的依据),拿不到计算样式才退回标签名名单。*/
function snipBreaksOf(el) {
  if (!el || el.nodeType !== 1) return false;
  if (el.tagName === 'BR' || el.tagName === 'HR') return true;
  const d = snipDisplayOf(el);
  if (d) return !(SNIP_INLINE_DISPLAY.has(d) || d.startsWith('inline-'));
  return SNIP_BREAKS.has(el.tagName);
}

/** 屏幕上压根看不见的那一棵子树(`display:none`、`hidden` 属性)。选区不会把它
 *  的字交出来,所以坐标系和取原文也都要跳过它 —— 一头收进去、一头没有,`quote`
 *  压平之后就对不上坐标系,白白多一句「可能定位不到」。*/
function snipHidden(el) {
  return !!el && el.nodeType === 1 && snipDisplayOf(el) === 'none';
}

/** 一棵 DOM 子树 → **屏幕上那份文字**(不压平:真换行、真缩进原样留着)。
 *
 *  文本节点取 `nodeValue` 原文,`SNIP_BREAKS` 里那些元素的**进入和离开各算一处
 *  换行**,**空元素只算一处**(`<br>`、`<hr>` 没有"里面",进入和离开是同一个
 *  位置)—— `<p>A</p><p>B</p>` 于是是 `A\n\nB`、`A<br>B` 是 `A\nB`,两样都和
 *  `Selection.toString()` 逐字相同。空元素那一条不写的话 `<br>` 会多出一个换行,
 *  保真就差了一个字(压平之后匹配照旧,所以只有逐字判据看得见)。
 *
 *  **只用它取原文,别拿它当匹配文本** —— 匹配一律过 `snipCollapse`。
 *
 *  **离线那一头用它**(`snipRenderedText` 在 DOMParser 的文档上算「目标页会看到
 *  的文本」):那里没有布局,只能按标签名判,所以它认不出 CSS 造成的分隔。活文档
 *  上取范围内的文字走 `snipRangeText`(按计算样式判,还能跳过看不见的子树)。*/
function snipFragText(node, skip) {
  let out = '';
  const walk = parent => {
    for (const n of parent.childNodes) {
      if (n.nodeType === 3) { out += n.nodeValue; continue; }
      if (n.nodeType !== 1) continue;
      if (skip && n.matches && n.matches(skip)) continue;
      if (snipHidden(n)) continue;
      const br = snipBreaksOf(n);
      if (br) out += '\n';
      walk(n);
      if (br && n.childNodes.length) out += '\n';   // 空元素只算一处
    }
  };
  walk(node);
  return out;
}

/** 一个 Range → **屏幕上那一段文字**,在**活文档**上算(所以 `getComputedStyle`
 *  答得出 `display`,CSS 造成的分隔也认得出)。
 *
 *  **这是退路,不是主路**:保真那一头先问浏览器自己(`snipShown` 里的
 *  `Selection.toString()`)—— 选区就是用户看到的那份文字,元素边界算几个换行、
 *  `<pre>` 里的缩进怎么留,全是 CSS 的规则。只有选区用不上(程序派的范围、
 *  选区已经没了)才走到这里。
 *
 *  不用 `cloneContents()`:克隆出来的片段脱离了文档,计算样式一问就是空的 ——
 *  那正好把 CSS 造成的分隔丢回标签名名单。*/
function snipRangeText(range, skip) {
  if (!range) return '';
  const root = range.commonAncestorContainer;
  if (root.nodeType === 3) {
    return String(root.nodeValue).slice(range.startOffset, range.endOffset);
  }
  let out = '';
  const walk = parent => {
    for (const n of parent.childNodes) {
      let touches = false;
      try { touches = range.intersectsNode(n); } catch { touches = false; }
      if (!touches) continue;
      if (n.nodeType === 3) {
        const v = String(n.nodeValue);
        const from = n === range.startContainer ? range.startOffset : 0;
        const to = n === range.endContainer ? range.endOffset : v.length;
        out += v.slice(from, to);
        continue;
      }
      if (n.nodeType !== 1) continue;
      if (skip && n.matches && n.matches(skip)) continue;
      if (snipHidden(n)) continue;
      const br = snipBreaksOf(n);
      if (br) out += '\n';
      walk(n);
      if (br && n.childNodes.length) out += '\n';   // 空元素只算一处
    }
  };
  walk(root);
  return out;
}

/** 这一段里有没有碰到 `skip` 的子树(整段落在它里面也算)。
 *  只给 `snipShown` 用:它要据此决定「浏览器那份文字能不能用」。*/
function snipSkipsIn(range, skip) {
  if (!range || !skip) return false;
  const node = range.commonAncestorContainer;
  const el = node.nodeType === 1 ? node : node.parentElement;
  if (!el) return false;
  if (el.closest && el.closest(skip)) return true;
  if (!el.querySelectorAll) return false;
  for (const n of el.querySelectorAll(skip)) {
    try { if (range.intersectsNode(n)) return true; } catch { return true; }
  }
  return false;
}

/** 一个 Range → **屏幕上那份文字**。
 *
 *  **先问浏览器,而且判据是结构性的**:当前选区**覆盖**这一段时就用
 *  `Selection.toString()` —— 它是显示文本的权威答案。判「覆盖」比的是**边界点**,
 *  不是文字:拿我们自己拼出来的文本去跟选区比、不一样就改用自己那份,等于用一套
 *  认不出 CSS 的规范文本**否决**用户真正选中的原文(审查实测:两段
 *  `display:block` 的 span,屏幕上两行,落库的却是接成一行的那一份)。
 *
 *  选区两头可能比这一段多出几个空白(`snipSpec` 把它们挪进了前后文)和半个字,
 *  所以调用方照旧要 `trim()` + `snipWhole()`。选区用不上(程序派的范围、滚动中
 *  选区没了)才退回 `snipRangeText`。
 *
 *  **`skip` 子树一旦被选区碰到,浏览器那条路就不能用了** —— 它不认识 `skip`,
 *  给的那份文字里带着那几个字。那些字是界面文字、不是原文:搜索卡和订阅列表在
 *  正文被截断处补的那个「…」就住在 `.snip-more` 里,存进引文的话点回去必然定位
 *  不到(而面板上那条看着好好的)。这时退回 `snipRangeText` —— 它逐节点走,跳得
 *  掉;丢掉的只是「CSS 造成的分隔」那一点精度,而那一点在界面文字旁边无关紧要。*/
function snipShown(range, skip) {
  const sel = typeof getSelection === 'function' ? getSelection() : null;
  if (sel && !sel.isCollapsed && sel.rangeCount && !snipSkipsIn(range, skip)) {
    let covers = false;
    try {
      const live = sel.getRangeAt(0);
      covers = live.compareBoundaryPoints(Range.START_TO_START, range) <= 0
        && live.compareBoundaryPoints(Range.END_TO_END, range) >= 0;
    } catch { covers = false; }
    if (covers) {
      const t = sel.toString();
      if (t && t.trim()) return t;
    }
  }
  return snipRangeText(range, skip);
}


// 容器内所有 text node 串成一条**压平空白**的全文,并记下每个字出自哪个节点的
// 哪一位:选区偏移、锚点截取、回跳定位共用这一个坐标系。
//   text  压平之后的全文
//   map   text[i] 出自 map[i].node 的第 map[i].off 位(一段空白只留第一个字)
//   nodes 每个 text node 开始时 text 已有多长,给 snipOffsetAt 定位用
//
// `skip` 是一条 CSS 选择器:命中它的那些子树**不进这条全文**。界面文字住在正文
// 容器里是常态(问答气泡下面的模型名和「3 小时前」、引用块那一段「来源 N 篇」),
// 而**它们不是正文**:相对时间过一会儿就变成「5 小时前」,被采进前后文锚点的
// 那一条收藏从此对不上;「来源 N 篇」那一块是按 refs 现拼的。**采集和回跳要传
// 同一个 skip** —— 一头跳过一头不跳,采到的"唯一"在另一头就不唯一了。
// **分隔位**(`map[i].sep`)是元素边界插出来的那一格,它没有自己的 text node
// (`node` 为 null):`snipOffsetAt` 只会落在真节点上,`snipOffsetRange` 两头
// 也只取真节点 —— 取原文走 `snipFragText`,不靠这张表还原。
function snipTextIndex(root, skip) {
  const map = [], nodes = [];
  let text = '', space = false;
  // 一处显示分隔:**占一个空格位**。前面已经是空白时不另占位置,只把那一位标成
  // 分隔 —— 不然 `A <br> B` 在坐标系里是两个空格、`quote` 压平只有一个,匹配当场
  // 对不上(坐标系「一段连续空白只占一位」这条不许破)。
  const sep = () => {
    if (!text) return;                     // 开头那几处分隔不占位
    if (space) { if (map.length) map[map.length - 1].sep = true; return; }
    map.push({ node: null, off: 0, sep: true });
    text += ' ';
    space = true;
  };
  const walk = parent => {
    for (const n of parent.childNodes) {
      if (n.nodeType === 1) {
        if (skip && n.matches && n.matches(skip)) continue;   // 整棵子树不进全文
        if (snipHidden(n)) continue;          // 屏幕上看不见的字也不进全文
        const br = snipBreaksOf(n);
        if (br) sep();
        walk(n);
        if (br) sep();
        continue;
      }
      if (n.nodeType !== 3) continue;
      const v = n.nodeValue;
      nodes.push({ node: n, from: text.length });
      for (let i = 0; i < v.length; i++) {
        const ws = SNIP_WS.test(v[i]);
        if (ws && space) continue;         // 同一段空白里的第二个字:不占位置
        map.push({ node: n, off: i });
        text += ws ? ' ' : v[i];
        space = ws;
      }
    }
  };
  walk(root);
  return { map, nodes, text };
}

// 所以起点落在压掉的那几个空白里时,它和终点算出来的是同一套偏移。
function snipOffsetAt(idx, node, offset) {
  const it = idx.nodes.find(x => x.node === node);
  if (!it) return -1;
  let i = it.from;
  while (i < idx.map.length && idx.map[i].node === node && idx.map[i].off < offset) i++;
  return i;
}

// 全文偏移 [start, end) → DOM Range。end-1 那个字就是选区最后一个字:压掉的
// 空白没有自己的位置,而引文两头的空白在 snipSpec 里已经挪走了。
function snipOffsetRange(idx, start, end) {
  // 两头往里收过分隔位:那一格是元素边界插出来的,没有自己的 text node
  // (`setStart(null, …)` 当场抛)。收完仍是同一段文字 —— 分隔位在压平全文里
  // 就是一个空格,而引文两头的空白在 `snipSpec` 里已经挪进前后文了。
  let a = start, b = end;
  while (a < b && idx.map[a] && !idx.map[a].node) a++;
  while (b > a && idx.map[b - 1] && !idx.map[b - 1].node) b--;
  const first = idx.map[a], last = idx.map[b - 1];
  if (!first || !last || b <= a) return null;
  const range = document.createRange();
  range.setStart(first.node, first.off);
  range.setEnd(last.node, last.off + 1);
  return range;
}

/** [a,b) 这一段的前后文:按 SNIP_CTX_STEPS 一档档加长,取**第一档能让回跳落回
 *  这一处的**;都做不到就回最短那档(回跳那边会如实说没找到)。
 *
 *  判据直接走 `snipSeek` —— 回跳用的就是它。自己另写一遍"唯一吗"的话,采集这头
 *  算出"唯一",回跳那头按另一套规则(候选集合 + 外侧空白)可能落在别处,而两处的
 *  文字一模一样,谁都看不出来。`exact` = 这条锚点按回跳的口径落回的正是这一处。*/
function snipContext(text, a, b) {
  const quote = text.slice(a, b);
  // 采集这一刻正文就在手上,所以裸引文那一档**现算**唯一性交给 `snipSeek`
  // (回跳那一刻正文可能已经改过,那时按库里存的 `quote_unique` 判,见 snipSeek)
  const u = snipAll(text, quote).length === 1 ? 1 : 0;
  let first = null;
  for (const n of SNIP_CTX_STEPS) {
    // 窗口两头都可能切在一个字中间 —— 削掉那半个(见 snipWhole)。削完仍是
    // 正文里连着的一段,所以下面那道 `snipSeek` 判据照旧成立。
    const ctx = { prefix: snipWhole(text.slice(Math.max(0, a - n), a)),
                  suffix: snipWhole(text.slice(b, b + n)) };
    // `same: true` 是事实而不是假设:这份 text 正是这一刻采集的那一份
    const hit = snipSeek(text, { q: quote, p: ctx.prefix, s: ctx.suffix, u, same: true });
    if (hit && hit[0] === a) return { ...ctx, exact: true };
    if (!first) first = { ...ctx, exact: false };
    // 两头都顶到正文尽头了,再加长一个字也不会变
    if (a - n <= 0 && b + n >= text.length) break;
  }
  return first;
}

/** 锚点 {q,p,s} 收成规范形态:空白压平、**外侧**空白去掉、引文两头的空白挪进
 *  前后文(接缝上只留一个)。**读的时候也过这一道** —— 库里还有阅读页早先存下
 *  的、没压过的锚点,而规范形态再压一次不变,所以两种都认。口径同后端 `_anchor`。
 *
 *  只去外侧那一头:**接缝那一头的空白是位置信息**(引文首尾的空白挪过来的,
 *  去掉整段锚点就拼不回原文),而外侧那一头什么也不说明,还最容易在另一种渲染
 *  里差一个 —— 正文末尾那一段,在订阅阅读器的 textContent 里后面跟着一个换行,
 *  渲染成 HTML 之后那儿什么都没有,于是整段锚点差一个空格就对不上(审查复现那
 *  一段正文就卡在这上面)。*/
function snipAnchor(spec) {
  let p = snipCollapse(spec && spec.p).replace(/^ +/, '');
  let q = snipCollapse(spec && spec.q);
  let sfx = snipCollapse(spec && spec.s).replace(/ +$/, '');
  if (q.startsWith(' ') && !p.endsWith(' ')) p += ' ';
  if (q.endsWith(' ') && !sfx.startsWith(' ')) sfx = ' ' + sfx;
  return { p, q: q.trim(), s: sfx };
}

/** 在**压平过的**全文里找这条锚点,回 [start, end];位置说不清就回 null。
 *
 *  **按「候选集合」算,不按「档位顺序」挑。** 前文那一边(`p+q`)和后文那一边
 *  (`q+s`)各自给出它认的**全部**位置,两个集合各有两种空法:
 *    - 0 个 = 这一边的证据在这份文本里**用不上**。同一条锚点可能在另一种渲染
 *      里对不上一边:订阅阅读器摊的是 md 原文(`## 标题` 带着井号),`/doc/…`
 *      把它渲染成 `<h2>`(井号没了)—— 前文跨过一个标题就整段失配,而后文那
 *      一边分毫不差。那时只听还说得上话的那一边。
 *    - 多个 = 这一边**自己定不了位**,可它仍然把范围缩到了那几处。
 *  **这两件事不许压成同一个值。** 压成同一个哨兵(原来那个 -1)的话,后一件的
 *  证据就丢了:前文明明说「只可能是第一处或第二处」,后文唯一指向第三处 ——
 *  丢掉前文那个集合之后只剩一个「唯一命中」,于是静默高亮一处前文已经明确排除
 *  掉的地方(审查实测:三处一字不差的重复句,收藏第二处、只改写后文,app 里
 *  高亮落在偏移 1015 而收藏的是 912,独立阅读页 429 而收藏的是 326)。
 *
 *  所以:**两边都还有候选就取交集**(交集正是整段 `p+q+s` 命中的那几处 ——
 *  `p+q` 在 x 处命中且 `q+s` 在 x 处命中,等价于 `p+q+s` 在 x-|p| 处命中,所以
 *  不必再单列「整段」那一档),一边用不上就只听另一边,两边都用不上才退到裸
 *  引文。**最后一律要求只剩一处**:剩两处是正文自己分不出的两处,剩零处是两边
 *  的证据互相矛盾 —— 两种都如实说没找到。锚点还在、位置说不清,比挑一处诚实:
 *  一字不差重复的段落、模板说明、重复的代码块都长这样,静默挑一处换来的是一个
 *  落在错地方的高亮,而两处的文字一模一样,用户从页面上看不出来。
 *
 *  **弱档(只听一侧、退到裸引文)要先证明「要搜的这份语料就是采集时那一份」**
 *  —— `spec.same`。两件事在这儿分得清:一侧**天生没有**(引文顶在正文开头或
 *  末尾,采集那一刻就没取到前文/后文)照旧只听另一侧;一侧**有证据、却在这份
 *  文本里一处也对不上**,说明这份语料已经不是采集时那一份了,那时另一侧的
 *  「唯一命中」证明不了它就是原处:三处一字不差的重复句,收藏第二处,后来只把
 *  第二处的后文改掉 —— 前文那一边仍然唯一地指向……第二处的前一处,于是静默跳到
 *  一处早该排除的地方(审查用生产函数实测:收藏偏移 912,改稿后回查 1015)。
 *  所以**除非能证明是同一份语料,一侧失配就如实说位置说不清**。
 *  能证明的只有这两种,别处一律按"不能"办:
 *    - **绑了文档的收藏**:库里记着采集那一刻核对过的正文版本
 *      (`favorites.doc_version`),和阅读页此刻那一篇的版本号相等 = 文件没动过,
 *      那一侧失配只能是**两种渲染摊出来的文本不一样**(md 原文 `## 标题` vs
 *      渲染后的 `<h2>`)—— 弱档正是为这件事存在的。
 *    - **采集和回跳摊的是同一个单元**:问答那一条消息(落库之后不会再改,身份
 *      就是 `favorites.origin_msg`)。日报正文、订阅正文不在其中 —— 两头是同一
 *      个渲染器,一侧对不上就只能是正文自己变了。
 *
 *  **裸引文那一档还要看 `spec.u`:采集那一刻这段引文在那一篇里只有一处吗。**
 *  「现在只剩一处」不等于「这一处就是当时那一处」:收藏那一刻有两处、收的是第
 *  二处,后来第二处被删掉,两侧上下文于是全失配,而第一处还在、按裸引文找正好
 *  唯一 —— 采信它等于把另一处同句冒充成原位置,静默跳过去(审查用生产代码实测:
 *  收藏偏移 153,删掉第二段之后回查到 45)。所以**采集时不唯一(或者老数据没记
 *  下)就不许走这一档**,如实说没找到;采集时本来就唯一的,剩下这一处必是它。
 *  唯一性由采集那头算、随收藏落库(`favorites.quote_unique`),不在回跳这头
 *  现猜 —— 回跳看到的正文已经是改过的那一份了。**这一档同样要 `same`**:
 *  「两侧上下文全失配」本身就说明正文变过,那时连"当时唯一"也只在当时成立
 *  (审查实测:收藏第二处、原处被整段删掉,裸引文正好唯一命中第一处 —— 一段
 *  用户从没收藏过的文字被当成他收藏的那一段高亮出来)。
 *  口径同 `kb/web/pages.py` 的 `seek`/`hitsOf`,两头各一份,改一处要改两处。*/
function snipSeek(text, spec) {
  const a = snipAnchor(spec);
  if (!a.q) return null;
  const same = !!(spec && spec.same);
  // 一侧的三种状态**不许压成两种**:`null` = 这条锚点天生没有这一侧;`[]` =
  // 有这一侧、在这份文本里一处也对不上;非空 = 它认的那几处。
  const byP = a.p ? snipAll(text, a.p + a.q).map(at => at + a.p.length) : null;
  const byS = a.s ? snipAll(text, a.q + a.s) : null;   // q+s 的起点就是引文的起点
  // 「有证据却一处也对不上」= 这份语料不是采集时那一份,除非证明过(见上)
  if (((byP && !byP.length) || (byS && !byS.length)) && !same) return null;
  const hitP = byP || [], hitS = byS || [];
  const bare = same && Number(spec && spec.u) === 1 ? snipAll(text, a.q) : [];
  const cand = hitP.length && hitS.length ? hitP.filter(at => hitS.includes(at))
    : hitP.length ? hitP
      : hitS.length ? hitS
        : bare;
  // 数不完的那一边也算说不清(见 SNIP_MAX_HITS):截断过的集合取交集只会偏小
  const over = xs => xs.length > SNIP_MAX_HITS;
  if (over(hitP) || over(hitS) || over(cand)) return null;
  return cand.length === 1 ? [cand[0], cand[0] + a.q.length] : null;
}

/** `needle` 在 text 里出现的**全部**起点(升序);空 needle 回空数组。
 *  多于 `SNIP_MAX_HITS` 处时只回前 `SNIP_MAX_HITS + 1` 个 —— 多出来那一个就是
 *  「数不完」的信号(调用方按 `length > SNIP_MAX_HITS` 认)。*/
function snipAll(text, needle) {
  const out = [];
  if (!needle) return out;
  for (let at = text.indexOf(needle); at >= 0; at = text.indexOf(needle, at + 1)) {
    out.push(at);
    if (out.length > SNIP_MAX_HITS) break;
  }
  return out;
}
/** 收藏条目 → locateSnip 认的 spec。f 是 /api/favorites 给的那一行。
 *  `u` 是采集那一刻的唯一性(库里的 `quote_unique`;老数据没有这一列 → 留空,
 *  `snipSeek` 按「说不清」处理,不走裸引文那一档)。*/
function snipSpecOf(f) {
  const u = f.quote_unique;
  return { q: f.quote || '', p: f.prefix || '', s: f.suffix || '',
           ...(u === 1 || u === 0 ? { u } : {}) };
}
