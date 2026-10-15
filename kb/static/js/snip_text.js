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
//     再也找不到(`locatable` 当场变 false,用户收到一句"可能定位不到")。
// 口径只有一份:`SNIP_BREAKS` 同时管「坐标系里插不插分隔」和「取原文时插不插
// 换行」,两处都过 `snipBreaks()`。

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
// 判据按**标签名**,不按计算样式:核验那一侧(`snipRenderedText`)在一个
// `DOMParser` 造的离线文档上跑,那里压根没有布局,`getComputedStyle` 答不出
// `display`。两侧口径必须同一份 —— 一侧按样式、一侧按标签名的话,同一句话在
// 面板上和在核验里分隔数不同,`quote` 压平之后就对不上核验文本,落库那一刻会
// 多出一句"可能定位不到"。
//
// 名单按「markdown 渲染结果 + 正文容器可能的壳」取:块级、列表、表格、以及
// `<br>`。漏了某个标签的后果是那一处的换行照旧丢掉(保真缺一块),多算一个的
// 后果只是坐标系里多一个空格、而 `quote` 里多一个换行 —— 两头同源,匹配照旧
// 成立,所以**名单宁可宽**。
const SNIP_BREAKS = new Set([
  'BR', 'P', 'DIV', 'PRE', 'BLOCKQUOTE', 'HR',
  'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
  'UL', 'OL', 'LI', 'DL', 'DT', 'DD',
  'TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'TD', 'TH', 'CAPTION',
  'SECTION', 'ARTICLE', 'HEADER', 'FOOTER', 'ASIDE', 'NAV', 'MAIN',
  'FIGURE', 'FIGCAPTION', 'DETAILS', 'SUMMARY', 'ADDRESS', 'FIELDSET',
]);

/** 这个元素的边界在屏幕上是不是一处换行。*/
function snipBreaks(el) {
  return !!el && el.nodeType === 1 && SNIP_BREAKS.has(el.tagName);
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
 *  给 `snipSpec` 取 `quote`(对选区 `cloneContents()` 的那份副本跑)、给
 *  `snipRenderedText` 算「目标页会看到的那份文本」两处共用。*/
function snipFragText(node, skip) {
  let out = '';
  const walk = parent => {
    for (const n of parent.childNodes) {
      if (n.nodeType === 3) { out += n.nodeValue; continue; }
      if (n.nodeType !== 1) continue;
      if (skip && n.matches && n.matches(skip)) continue;
      const br = snipBreaks(n);
      if (br) out += '\n';
      walk(n);
      if (br && n.childNodes.length) out += '\n';   // 空元素只算一处
    }
  };
  walk(node);
  return out;
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
        const br = snipBreaks(n);
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
  let first = null;
  for (const n of SNIP_CTX_STEPS) {
    // 窗口两头都可能切在一个字中间 —— 削掉那半个(见 snipWhole)。削完仍是
    // 正文里连着的一段,所以下面那道 `snipSeek` 判据照旧成立。
    const ctx = { prefix: snipWhole(text.slice(Math.max(0, a - n), a)),
                  suffix: snipWhole(text.slice(b, b + n)) };
    const hit = snipSeek(text, { q: quote, p: ctx.prefix, s: ctx.suffix });
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
 *  口径同 `kb/web/pages.py` 的 `seek`/`hitsOf`,两头各一份,改一处要改两处。*/
function snipSeek(text, spec) {
  const a = snipAnchor(spec);
  if (!a.q) return null;
  const byP = a.p ? snipAll(text, a.p + a.q).map(at => at + a.p.length) : [];
  const byS = a.s ? snipAll(text, a.q + a.s) : [];   // q+s 的起点就是引文的起点
  const cand = byP.length && byS.length ? byP.filter(at => byS.includes(at))
    : byP.length ? byP
      : byS.length ? byS
        : snipAll(text, a.q);
  // 数不完的那一边也算说不清(见 SNIP_MAX_HITS):截断过的集合取交集只会偏小
  const over = xs => xs.length > SNIP_MAX_HITS;
  if (over(byP) || over(byS) || over(cand)) return null;
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
/** 收藏条目 → locateSnip 认的 spec。f 是 /api/favorites 给的那一行。*/
function snipSpecOf(f) {
  return { q: f.quote || '', p: f.prefix || '', s: f.suffix || '' };
}
