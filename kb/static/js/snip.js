// ── 选中即收藏:一处实现,挂在所有能展示正文的容器上 ────────────────────────
// 「☆ 收藏此段」原来只长在 `/doc/…` 阅读页里(`kb/web/pages.py` 的
// `_DOC_SNIP_JS`,那是脱离 kb-web 也要能用的独立产物,所以它自带一份内联副本)。
// 日报正文、问答回答、订阅阅读器、图鉴条目各渲染一份正文,却都收藏不了 ——
// 这个文件是给它们用的那一份,四处共用同一个浮动按钮、同一套锚点口径。
//
// 挂一次就够:各面板重渲染换的是容器**里面**的 innerHTML/textContent,容器元素
// 本身不变,监听跟着元素走。
//
// 锚点口径必须和阅读页逐位对齐 —— 同一条收藏可能在这边选、在那边点回去:
//   quote  选中的原文,**按原文存**(只去掉首尾空白)
//   prefix 它前面那几十个字(压平过的匹配表示)
//   suffix 它后面那几十个字(同上)
// 回跳时先按 prefix+quote+suffix 整段找(见 snipSeek)。
//
// **「匹配表示」不等于「收藏原文」,这两件事分开。** 匹配坐标系只有一个:压平
// 空白之后的全文 —— 一段连续空白(空格、换行、制表、NBSP)一律当一个空格,采集
// 和回跳都在这套文本上算偏移。不压不行:同一份正文在两头的空白本来就不一样 ——
// 订阅阅读器摊的是 `textContent`(段落之间两个换行),阅读页把同一份 md 渲染成
// 一串 <p>(串出来只剩一个换行),前后文锚点一碰到段落边界就对不上。
// **可是压平过的那一份不许进库**:收藏四行 Python 代码时,存进去的会是接成一行
// 的 `if ready: run_task() else: stop_task()`,换行和缩进再也找不回来(审查实测,
// 日报正文和独立阅读页都中)。所以 `quote` 原样存,压平只在**读的时候**做
// (`snipAnchor`、后端 `_anchor`),两边都压一次,结果逐位相同。
// **「原样」包括元素产生的那几处换行**(`<br>`、段落/列表/表格边界):它们在文本
// 节点里一个字符都不占,照文本节点串存就是把两行接成一行。
//
// 文本那一半(压平、半个字、显示分隔、坐标系、锚点规范化、定位搜索)在
// `kb/static/js/snip_text.js` —— index.html 里排在本文件之前。本文件只管 UI、
// 采集入口、来源判定、核验和网络。

const SNIP_MIN = 4;          // 选不到这个字数不弹按钮(误触、双击单词)
// 后端 `SnippetRequest` 对这两个快照字段有长度上限,超了整条 POST 被拒成 422 ——
// 而订阅/图鉴的标题和链接是人家内容自带的,长了不是用户的错。**标题裁尾巴**
// (它只用来在列表里指代这一条),**链接不裁** —— 半截 URL 点过去是另一个地方,
// 超长就不送,回跳退回按 origin 走各自面板。`origin_ref` 不在这儿管:它是
// 会话号、日期、32 位摘要,够不着上限,裁了反而把回跳的落点裁没了。
const SNIP_MAX_TITLE = 300;
const SNIP_MAX_URL = 4096;
// 片段来源。和后端 `kb/web/favorites_api.py` 的 `_ORIGINS` 是同一份名单,
// 加一种来源要两边一起加(后端那边会把不认识的 origin 拒成 400)。
const SNIP_ORIGINS = ['doc', 'daily', 'chat', 'feed'];
const SNIP_LABEL = { doc: '知识库文章', daily: '每日日报', chat: '知识问答', feed: '订阅内容' };

const snipZones = [];        // [{el, describe}] —— describe() 回这一段的来源
let snipBtn = null;
let snipRange = null;        // 按钮当前对着的选区(点下去时选区可能已被清掉)
let snipZone = null;

/** 注册一个可收藏的正文容器。
 *  describe(range) 在**点收藏那一刻**调用,回
 *  {path, origin, origin_ref, origin_title, origin_url},或回 null/undefined
 *  表示这一段此刻不能收藏(正文还没加载好、选到的是空态文案之类)。
 *  **给了 `path` 就得一起给 `doc_version`**(这一段正文是哪一版,后端落库前按盘上
 *  此刻那一份再核一次;缺了后端按 400 拒)—— 要么来源自己知道(图鉴条目从接口
 *  读、阅读页从渲染那一刻带),要么由下面那道核对带出来。
 *  现算而不是注册时算:容器是常驻的,里面那篇文章会换;收到 range 是因为一个
 *  容器里可能并排放着好几段各有出处的正文(问答的多条回答、图鉴的多条记录)。
 *
 *  **可以划定这一段正文自己的范围**,前后文锚点就只从那里面采:
 *  - `root`:这一段正文的容器(默认是注册的那个容器)。**绑了文档的那一类必须
 *    给** —— 一个容器里并排着的不只正文:图鉴抽屉里每条记录下面跟着「原文 /
 *    Obsidian / 在阅读器打开」三颗按钮和小节标题,采进后文锚点之后,那条收藏
 *    带着一段**目标文档里压根不存在**的上下文(审查实测:payload 的 suffix 是
 *    `原文 Obsidian 在阅读器打开 就这个工具提问`)。
 *  - `skip`:`root` 里要跳过的子树(CSS 选择器),给那些**住在正文里的界面
 *    文字**用(问答气泡的模型名/相对时间、引用块)。回跳那一头要传同一个
 *    (`consumeSnipJump` 的第四个参数),见 `snipTextIndex`。
 *
 *  **容器只是那一篇的一截时要声明 `partial: true`**(列表卡片摊的是正文开头那
 *  380 字、搜索卡摊的是命中那一段):唯一性于是不按这一截断言 —— 切片里只有一处
 *  证明不了整篇里只有一处(见 `snipSpec` 的 `unique`)。
 *
 *  还可以带两样东西,都是给「绑了文档、却不一定定位得回去」那一类用的:
 *  - `fallback`(同结构、不再带 fallback):主选项被后端判成「文档不在索引里」
 *    时改用它。专门给订阅内容 —— md 已经落盘、索引那一步还没跑的那几分钟里,
 *    `path` 是真的、documents 里却还没有行(见 saveSnip)。
 *  - `docText()`:回 `{text, version, reason}` —— `text` 是「点回去那一头会看到的
 *    那份文本」、`version` 是那一篇此刻的正文版本号、读不到时 `text` 为 null 且
 *    `reason` 说清是**确定没有**还是**不知道**。**可以是 Promise**(要去读服务端
 *    那一篇的 `snipDocText` 就是异步的)。**绑了文档的那一类一律要给**:核不过时
 *    有 `fallback` 的改用它、没有的照存但当场说清点回去可能定位不到;核过了就把
 *    那一版的版本号挂到来源上(见 snipVerified)。 */
function attachSnip(el, describe) {
  if (!el || snipZones.some(z => z.el === el)) return;
  snipZones.push({ el, describe });
}

function snipButton() {
  if (snipBtn) return snipBtn;
  snipBtn = document.createElement('button');
  snipBtn.id = 'snipbtn';
  snipBtn.type = 'button';
  snipBtn.innerHTML = '☆ 收藏此段';
  snipBtn.hidden = true;
  // 别让按下按钮本身清掉选区 —— 清了就没有 quote 可收了
  snipBtn.addEventListener('mousedown', e => e.preventDefault());
  snipBtn.addEventListener('click', saveSnip);
  document.body.appendChild(snipBtn);
  return snipBtn;
}

function hideSnipBtn() {
  if (snipBtn) snipBtn.hidden = true;
  snipRange = null;
  snipZone = null;
}

// 选区落在哪个已注册容器里。`commonAncestorContainer` 是文本节点时 contains()
// 照样认(Node.contains 走的是节点树,不只元素)。
function snipZoneOf(range) {
  return snipZones.find(z => z.el.contains(range.commonAncestorContainer)) || null;
}

function showSnipBtn() {
  const sel = getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return hideSnipBtn();
  const range = sel.getRangeAt(0);
  const zone = snipZoneOf(range);
  if (!zone) return hideSnipBtn();
  // 字数判据**只有一份**:按压平过的那一份算(`saveSnip` 里那一道同口径)。
  // 两处不一样的话,「两个字 + 中间几个换行」这种选区会弹出按钮、点下去
  // 却静静收起 —— 又是一颗点了什么也不会发生的按钮。
  if (snipCollapse(sel.toString()).trim().length < SNIP_MIN) return hideSnipBtn();
  // **这一段到底收不收得下,现在就问清楚。** 一个注册过的容器里并不是每一处都
  // 是正文:问答空态那三颗建议按钮和回答同住 `#chat-msgs`,图鉴的小节标题和条目
  // 正文同住 `#td-entries`,会话还没落库时整块回答都没有落点。按钮照弹的话,
  // 用户选中、点下去,换来的是一句「这一段现在收藏不了」—— 一颗点了会失败的
  // 按钮比没有按钮更糟。describe 是纯查询,显示时问一次、点下去再问一次都安全。
  if (!snipSourceOf(zone, range)) return hideSnipBtn();
  // 容器自己滚出视口时不弹:按钮是 fixed 的,会飘在别的面板上面
  const box = zone.el.getBoundingClientRect();
  const r = range.getBoundingClientRect();
  if (r.bottom < box.top - 4 || r.top > box.bottom + 4) return hideSnipBtn();
  snipRange = range.cloneRange();      // 选区随后可能被点击清掉,留一份
  snipZone = zone;
  const btn = snipButton();
  btn.hidden = false;
  const w = btn.offsetWidth, h = btn.offsetHeight;
  btn.style.left = Math.max(8, Math.min(innerWidth - w - 8,
    r.left + r.width / 2 - w / 2)) + 'px';
  // 选区贴着窗口顶时翻到下面去,否则按钮会被裁出屏幕
  btn.style.top = (r.top > h + 14 ? r.top - h - 8 : r.bottom + 8) + 'px';
}

/** 选区 → `{quote, prefix, suffix, locatable}`。
 *
 *  **`quote` 是用户在屏幕上选中的那份文字**(只去首尾空白)。**显示文本的权威是
 *  浏览器自己**(`Selection.toString()`):哪些元素边界算换行、算几个、`<pre>` 里
 *  的缩进怎么留,那套规则是 CSS 的,自己照标签名重写一遍只会在某一类节点上和它
 *  差一个字(`<p>A</p>` 和 `<p>B</p>` 之间 HTML 源里那个换行文本节点,渲染里看
 *  不见、文本节点串里在)。所以这一头**先问浏览器**,再用「压平一遍等于坐标系里
 *  那一段」确认它说的正是 [a,b) 这一段 —— 对不上(程序派的选区、滚动中选区没了)
 *  才退回 `snipFragText`(文本节点原文 + 元素边界补换行,可能比浏览器多一个换行,
 *  但不会丢)。两条路都比 `String(range)` 对:那是 Range 的字符串,`<br>` 和段落
 *  边界在它里面一个字符都不占,两行当场接成一行。
 *
 *  prefix/suffix 是**压平过的**匹配用上下文;不变量是「压平之后三段连起来仍是
 *  坐标系里连着的一段」,所以坐标系(`snipTextIndex`)和取原文过的是同一个
 *  `snipBreaksOf()`。
 *
 *  `locatable` = 这条锚点在它自己这一篇里唯一定位得到吗(收藏那一刻就知道,用来
 *  如实提示)。**它还要求屏幕上那份文字压平之后真的等于坐标系里那一段** ——
 *  坐标系认不出的那种显示分隔(某些 CSS 排版)会让两头差一个空格,那时锚点在这
 *  套坐标系里定不准。**那一刻放弃定位,不是改引文去迁就坐标系**:引文错了不可
 *  恢复,定位不到只是一句如实的提示。
 *
 *  `unique` = 采集那一刻这段引文在这一篇里只有一处吗。回跳时两侧上下文都失效、
 *  退到裸引文那一档时按它决定能不能采信(见 `snipSeek`)。**算得出来才敢说** ——
 *  来源声明了 `partial`(屏幕上这个容器只是那一篇的一截,比如列表卡片摊的正文
 *  开头)时一律按「不唯一」存:切片里只有一处证明不了整篇里只有一处,照它存下去
 *  等于给裸引文那一档发一张它不配的通行证,回跳那一刻会命中整篇里的另一处。
 *  绑了文档的那一类随后会按盘上那一篇重算一遍(见 `snipVerified`),那一份更准。
 *  端点不在 text node 上的少见情况(选到元素边界)锚点留空、靠全文搜索。
 *  同阅读页那份内联副本。*/
function snipSpec(zone, range, src) {
  const idx = snipTextIndex((src && src.root) || zone.el, src && src.skip);
  let a = snipOffsetAt(idx, range.startContainer, range.startOffset);
  let b = snipOffsetAt(idx, range.endContainer, range.endOffset);
  if (a < 0 || b < 0 || b <= a) {
    const bare = { quote: snipWhole(snipShown(range, src && src.skip).trim()),
                   prefix: '', suffix: '' };
    const uniq = !(src && src.partial)
      && snipAll(idx.text, snipCollapse(bare.quote).trim()).length === 1;
    return { ...bare, unique: uniq,
             locatable: !!snipSeek(idx.text,
               { ...snipSpecOf(bare), u: uniq ? 1 : 0, same: true }) };
  }
  // 引文两头的空白挪进前后文。**不能留到后端再 strip** —— 那边剥掉引文的首尾
  // 空白、前后文却照旧,整段锚点就拼不回原文了(定位于是退回只找引文)。
  while (a < b && idx.text[a] === ' ') a++;
  while (b > a && idx.text[b - 1] === ' ') b--;
  // 端点落在一个字中间时往里收一格:引文和前后文的边界都得是整字,不然存进去
  // 的是半个字(见 snipWhole)。鼠标选不出这种端点,程序派的选区能。
  if (a < b && SNIP_LOW_HALF.test(idx.text[a])) a++;
  if (b > a && SNIP_HIGH_HALF.test(idx.text[b - 1])) b--;
  const ctx = snipContext(idx.text, a, b);
  // 原文那一头:[a,b) 这一段在 DOM 里的范围(`snipOffsetRange`)→ 屏幕上那份文字
  // (`snipShown`,它先问浏览器)。
  const dom = snipOffsetRange(idx, a, b);
  const seg = idx.text.slice(a, b);
  const quote = snipWhole((dom ? snipShown(dom, src && src.skip) : seg).trim());
  // **坐标系认不认得这一段**:屏幕上那份文字压平一遍,要等于坐标系里 [a,b) 那
  // 一段。对不上说明这一处的显示分隔坐标系没算出来(CSS 造成的那种),锚点在
  // 这套坐标系里就定不准 —— 引文照原样存,定位如实放弃。
  const mapped = snipCollapse(quote).trim() === snipCollapse(seg).trim();
  return {
    quote, prefix: ctx.prefix, suffix: ctx.suffix,
    unique: !(src && src.partial)
      && snipAll(idx.text, snipCollapse(quote).trim()).length === 1,
    locatable: mapped && !!ctx.exact,
  };
}

async function saveSnip() {
  if (!snipRange || !snipZone) return hideSnipBtn();
  const zone = snipZone, range = snipRange;
  // 按钮弹出来那一刻问过一次,这里再问一次 —— 中间可能换过文章(订阅阅读器的
  // 上一篇/下一篇)、会话可能刚落库。拿不到就静静收起:弹按钮那一步已经挡掉了
  // 「这儿压根不能收」的情形,走到这里只剩下竞态,不值得再弹一句提示。
  const src0 = snipSourceOf(zone, range);
  if (!src0) return hideSnipBtn();
  const spec0 = snipSpec(zone, range, src0);
  // 字数判据落在**压平过的那一份**上:quote 现在是原文,多行选区里的换行和缩进
  // 都算字,按它判的话「只选中四个空格」也能过关。
  if (snipCollapse(spec0.quote).trim().length < SNIP_MIN) return hideSnipBtn();
  const btn = snipButton();
  btn.innerHTML = '收藏中…';
  btn.disabled = true;
  // 核对要读服务端那一篇(`snipDocText`),所以先把按钮改成"收藏中"再等它
  const { src, reason, fell, fix } = await snipVerified(src0, spec0);
  // 核过了的话,唯一性和定位已经按盘上那一篇重算过(`fix`)—— 用那一份,它算的
  // 语料正是回跳那一头会看到的语料。
  const spec = { ...spec0, ...(fix || {}) };
  // **落库那个「定得住吗」按核对的结果算**(`fix.anchored`),没有核对这一步的
  // 来源(日报、问答、订阅列表卡)按屏幕那一份算 —— 那两头是同一个渲染器,
  // 屏幕上定得住就是回跳那一头定得住。见 `postSnip` 的 `anchor_exact`。
  const anchored = fix && 'anchored' in fix ? !!fix.anchored : !!spec.locatable;
  // 「点回去会怎么走」那一句:**凡是让它不按预期走的事,一件都不许瞒** ——
  // 不说的话用户只能到点回去那一刻才发现。攒成一串而不是挑一条:这几件会同时
  // 成立(定不准的锚点在盘上那篇里自然也核不上),挑一条说就把另一条瞒了。
  // `eff` 是最终真存下去的那个来源 —— 下面 404 那条退路会换掉它。
  // 退回 feed 的三种理由各有各的话。**不许共用一句** —— 「还没进检索索引」是
  // 一个结论,而"这一刻读不出来"只是一次失败:说成前者,用户会以为等索引跑完
  // 就好了(审查提的 N1)。
  const NOT_INDEXED = '这篇还没进检索索引,点回去会打开站外原文';
  const UNREADABLE = '这一刻读不出盘上那一篇、没法核对,点回去会打开站外原文';
  const STALE = '盘上那一篇刚换了一版正文,点回去会打开站外原文';
  const detailOf = (eff, reasonNote) => [
    reasonNote,
    // 前后文已经长到 200 字还是分不出两处(整篇就是重复的)。存照旧存 ——
    // 引文本身留着才是收藏的主要用处 —— 但点回去会是一句「没找到」。
    spec.locatable ? '' : '这一篇里有几处一模一样的文字,点回去可能定位不到这一处',
    // 超长链接没送出去(见 SNIP_MAX_URL)。**没有 path 的那一类才说**:那时
    // 链接是回原文的唯一一条路,丢了就只能回到来源面板;绑了文档的那一类回跳
    // 本来走阅读页,说它反而是噪音。
    !eff.path && snipUrlDropped(eff)
      ? '这条来源链接太长没能一起存下,点回去只能回到来源面板' : '',
  ].filter(Boolean).join(';') || '在「我的收藏 · 待试」里回看';
  const REASON_NOTE = {
    absent: NOT_INDEXED,
    unreadable: UNREADABLE,
    mismatch: '这段话在盘上那篇里核对不上(正文可能刚更新过),点回去会打开站外原文',
  };
  // **核不过、却没有退路可退的那一类(图鉴条目)说的是另外一句话。** 共用上面
  // 那三句就会出现"点回去会打开站外原文"—— 可它绑的还是那一篇文档,点回去打开
  // 的正是阅读页。这几句里唯一要紧的那半句是**「这一处定位不到」**。
  const STAY_NOTE = {
    absent: '这篇还没进检索索引,点回去可能打不开',
    unreadable: '这一刻读不出盘上那一篇、没法核对,点回去可能定位不到这一处',
    mismatch: '盘上那一篇里这段话跟这儿显示的不一样(正文里有 HTML 标签之类),'
      + '点回去可能定位不到这一处',
  };
  let detail = detailOf(src, (fell ? REASON_NOTE : STAY_NOTE)[reason] || '');
  try {
    // **绑了文档、却拿不出核对过的那一版正文时,一行都不送。** 后端那道必答校验
    // 本身是对的,可一条 POST 撞上它,用户看到的是「收藏失败:绑文档的收藏必须带
    // doc_version(核对过的正文版本)」—— 一句他无法处置的话,而真正的原因
    // (那一篇这一刻读不出来 / 已经不在索引里了)一个字都没说。所以在这儿拦住,
    // **并且说出真正的原因**。这是一道**通用的带**:新接上来的入口忘了带版本号
    // 时,坏的也是这一句话,而不是一个必填字段错误。
    // **不许反过来把 path 丢掉、偷偷存成一条只有引文的收藏** —— 那是把「能回到
    // 原文」静默降级,只有用户能做这个决定(口径同后端那段保存契约:它故意不替
    // 调用方降级)。带 `fallback` 的来源上面那道已经退过了,走不到这儿。
    const NO_VERSION = {
      absent: '这一篇已经不在检索索引里了,先没收下来(存下去会是一条点回去找不到原文的收藏)',
      unreadable: '这一刻读不出盘上那一篇、没法核对,先没收下来 —— 过一会儿再收一次',
    };
    if (src.path && !src.doc_version) {
      throw new Error(NO_VERSION[reason] || '没核出这一篇的正文版本,先没收下来');
    }
    let r = await postSnip(src, spec, anchored);
    // 后端拒绝绑这篇文档的三种理由,这里各退一次 —— 它**故意不替我们降级**:
    // 把一条本该能定位回原文的收藏静默换成只有引文的那一类,是调用方该自己做
    // 的决定。退了就如实说退的是哪一种(文案见上面那三句)。
    //   404 这篇不在索引里(订阅内容刚导出、索引那一步还没跑)
    //   409 核对完到真写进库这一下,盘上那篇又换了一版(重译写回、正文补抓)
    //   503 盘上那篇这一刻读不出来,后端没法核对
    // **这是第二道带子,别删**:带 `docText` 的来源在上面那道核对里就退了,
    // 可核对和 POST 是两个请求,中间那一下索引行照样会没、正文照样会换(审查
    // 实测:核对读到的是旧译文,POST 发出之前后台把那一篇重译重导出了,旧代码
    // 照旧落 origin=doc 返回 200,点回去 0 个高亮);而不给 `docText`、只给
    // `fallback` 的来源(图鉴条目正文)只有这一道。
    const RETRY_NOTE = { 404: NOT_INDEXED, 409: STALE, 503: UNREADABLE };
    const retry = src.fallback ? RETRY_NOTE[r.status] : '';
    if (retry) {
      r = await postSnip(src.fallback, spec, anchored);
      detail = detailOf(src.fallback, retry);
    }
    // 没有退路的那几类(图鉴条目正文):409 说明面板上这份正文已经旧了。后端那
    // 句话只说"不是那一版",没说怎么办 —— **而能办的只有刷新**:同一个工具关掉
    // 重开直接用现成视图(`gallery.js` 的 `openToolDrawer` 里 `!changed &&
    // toolMeta` 那一句),版本号跟着那份视图一起还是旧的,说"重新打开这条记录"
    // 就是让用户再撞一次同一面墙。
    if (r.status === 409 && !src.fallback) {
      throw new Error('这一篇的正文刚更新过(面板上这份是旧的),刷新页面再收藏');
    }
    if (!r.ok) throw new Error(await apiFail(r, '收藏失败'));
    getSelection()?.removeAllRanges();
    toast('片段已收藏', { type: 'ok', key: 'snip', detail });
    refreshFavPanel();
  } catch (e) {
    toast('收藏失败', { detail: e.message });
  } finally {
    btn.disabled = false;
    btn.innerHTML = '☆ 收藏此段';
    hideSnipBtn();
  }
}

// describe 是各面板自己写的,抛异常不该把整条收藏链带崩(更不该让浮动按钮
// 卡在屏幕上)。口径只有这一处:显示和点击都过它。
function snipSourceOf(zone, range) {
  try { return zone.describe(range) || null; } catch { return null; }
}

/** 「这条锚点在点回去那一头找得到吗」—— 核不过就改用 `fallback`。
 *  回 `{src, reason}`:`reason` 是退的理由(`''` = 没退、`absent` = 那一篇**确定**
 *  不在、`unreadable` = 这一刻读不出来/不知道、`mismatch` = 读到了但这段话不在
 *  里面),`saveSnip` 据它说话。**后两种不许合成前一种**:把一条暂时的失败说成
 *  「这篇还没进检索索引」,是拿一个没证明过的结论告诉用户(审查提的 N1)。
 *
 *  核过了的话,**把核的是哪一版正文一起带出去**(`src.doc_version`):`postSnip`
 *  原样回传,后端落库前按盘上此刻那一份再算一次,不等就拒。核对和写入是两个
 *  请求,中间那一下正文照样会换一版 —— 没有这个共同事实的话,核对读到的是旧
 *  译文、写入时盘上已是新译文,收藏照旧落成 doc,点回去 0 个高亮(审查实测)。
 *
 *  **核对的对象是服务端那一篇,不是屏幕上这一份。** `docText()` 现读
 *  `/api/doc`(阅读页渲染的就是这个文件),所以这一道同时挡住三件事:
 *  - **屏幕上这份正文可能已经不是盘上那一篇了。** `path` 是上一次导出留下的,
 *    而译文重写回、正文补抓都保留它;`exported_hash === content_hash` 也证明
 *    不了 —— 那是**原文**的哈希,同一篇原文重译一遍它一个字节都不变(审查实测:
 *    后台重译重导出之后两个哈希仍相等,旧阅读器上收藏一句存成 doc,点回去是
 *    另一份译文、0 个高亮)。所以不拿客户端手里的字段推断远端那一篇,**直接
 *    去读那一篇**。
 *  - **md 落盘了、索引那一步还没跑**:`/api/doc` 对它判 404,这一道退一步。
 *  - **两头摊的不是同一种文本。** `/doc/…` 把 md **按 markdown 渲染**
 *    (`kb/web/pages.py` 的 `marked.parse`),而订阅阅读器摊的是同一份正文的
 *    `textContent` —— 正文里的 `**`、反引号、`[字](url)` 在那一头会消失或变形
 *    (审查实测:选中带 `**` 的一句存成 doc,点回去 0 个高亮、报「原文可能已
 *    更新」,而文件一个字没改)。空白差异不在射程内:两头都压平。
 *
 *  渲染走的是**阅读页那一条规则**(`snipRenderedText` 里那个独立 marked 实例),
 *  不是面板自己的 `mdHtml` —— 后者把正文里的原始 HTML 交给净化器变成元素,
 *  阅读页把它当字面文字显示,只差这一条就能让一句话在这边连着、在那边中间插着
 *  四个尖括号字:核照旧过、收藏落成 doc、点回去 0 个高亮(审查实测的图鉴那一例)。
 *
 *  **有没有 `fallback` 决定核不过时怎么收场,但两种都不许瞒着**:
 *  - 有(订阅阅读器):改用它(`fell: true`),回跳走站外原文。
 *  - 没有(图鉴条目):**照旧绑那一篇**(点回去至少打开的是对的那一篇文章),
 *    但 `fell: false` 要让 `saveSnip` 当场说一句「点回去可能定位不到这一处」。
 *    悄悄存成功是这一类里最坏的那一种:库里那条看着好好的,用户要到点回去
 *    那一刻才知道(审查实测:成功提示里一个字都没提)。
 *  **裸引文那一档在这一步有两道闸,缺一道就会把另一处同句冒充成原位置**:
 *  「屏幕上就有两处」的不许靠它核过(那一刻谁也说不出用户选的是第几处,而盘上只剩
 *  一处恰恰说明原处被删掉了);**面板那一版和盘上这一版不等的也不许**
 *  (`sameCorpus` —— 那是"正文已经换过一版"的正面证据)。两道闸都只关裸引文那一档:
 *  「只听另一侧」要照旧放行(那一档有真正的位置证据,盘上末尾补了一段的那一条就
 *  靠它,见 `tests/snip_probe_bind.mjs` 第 4c 格)。
 *
 *  **核过了才把新读到的那一版带出去;核不过就原样把面板那一版送上去。** 后端那道
 *  409 于是正好替我们把两件事分开:面板那一版和盘上这一版**不等** = 面板旧了(改稿
 *  很可能正是把那句话换掉的那一下)→ 409 → 一行不落、提示说"刷新再收藏";**相等**
 *  = 文件没动过,核不过只是两头的文本不是同一份(正文里那段原始 HTML)→ 照存,
 *  说清点回去可能定位不到。反过来把新版本号也带上的话,后面那道就永远判不出"面板
 *  旧了",改稿删掉那句话也照存(回归在 `tests/snip_probe_bind.mjs` 第 4 节)。*/
/** 「刚读到的这一份就是屏幕上那一份吗」——**核对这一步**的 `same`。
 *
 *  只有一种证明:**来源自己报得出它展示的是哪一版**,而且那个数和刚读到的相等。
 *  **那个数必须和它摊出来那份语料同源**,不是「响应那一刻读一遍盘」:
 *  - 图鉴条目接口随正文一起给(`kb/projects.py` 里 body 和 version 同出一次读);
 *  - 独立阅读页把它烤在自己那份 HTML 里(`DOC_VERSION`);
 *  - 订阅条目行摊的是 feeds.db 里那一行,所以按**那一行内容**算出「它导出成 md
 *    之后正文该是什么指纹」(`kb/feeds/exported.item_body_md` → `scanner.body_version_of`);
 *  - 搜索命中摊的是建索引那一刻切下来的 `best_chunk`,所以只在**建索引时那份文件
 *    和此刻盘上这一份逐字节相等**时才报(`kb/web/common.indexed_doc_version`)。
 *  拿读盘算出来的数冒充的那一版,被两条真实路径打穿:先改稿、后打开搜索/订阅,
 *  旧语料带着新文件的版本号,这一道必然判「相等」,弱档重新放行。
 *
 *  **「版本号不等」和「说不出版本」都算证明不了**,两种都不许当成同一份:
 *  - 不等是**正面证据**:这一篇在面板开着的这几分钟里换过一版,于是「此刻的文字
 *    在这儿正好命中」证明不了它就是用户选的那一处(审查实测:抽屉上两节各写着
 *    一句一模一样的、前文也一字不差,选的是第二节;盘上那一节随后连后文一起被
 *    换掉,**前文**那一侧于是唯一命中第一节 —— 落库返回 200,点回去高亮的是他
 *    从没选过的那一节)。
 *  - 说不出 = 手里压根没有可被反驳的事实。原来这一档按「两头渲染本来就不一样」
 *    默认同份,那是把**没有证据**当成了**证据**(审查实测:订阅阅读器那阵子不报
 *    版本号,盘上那一篇随后把原处那一段换掉、另一节新写了一句一模一样的,裸引文
 *    那一档正好命中它 —— 收藏照旧成功,点回去高亮的是改稿新写的那一节)。
 *
 *  证明不了的后果**只落在弱档上**(裸引文、只听一侧);整段锚点两侧都对得上的
 *  那一档照旧算 —— 那是锚点自己的位置证据,盘上末尾补了一段的那一条就靠它
 *  (回归在 `tests/snip_probe_bind.mjs` 第 4c 格)。*/
function sameCorpus(src, got) {
  if (!src.doc_version) return false;
  return !!(got && got.version && got.version === src.doc_version);
}

async function snipVerified(src, spec) {
  if (typeof src.docText !== 'function') return { src, reason: '', fell: false };
  let got = null;
  // 抛异常是**不知道**,不是"确定没有"
  try { got = await src.docText(); } catch { got = null; }
  // **采集那一刻「这条锚点定不定得住」也得随 spec 进核对这一步。** 这件事采集阶段
  // 挂在 `locatable` 上、落库那头叫 `anchor_exact`,而 `snipSpecOf` 只认库里那个
  // 列名 —— 不在这儿转一下,它就在采集到核验之间静静丢掉,`snipSeek` 本来有的那道
  // `e=0` 拒绝(记着「定不住」的哪一档都不给用)于是压根没机会生效,核对按「盘上
  // 现在只剩一处」把它重算成「定得住」,点回去高亮的是用户从没选过的那一处。
  // **这一道在「同一份语料」上一分钱不要**:语料没变过的话那两处都还在,两侧各命
  // 中两处、取交集还是两处,`snipSeek` 自己就判说不清 —— 真正被它拦住的只有「原处
  // 没了、剩下一处别处」。盘上末尾补了一段那一条采集时本来就定得住,`stuck` 是
  // false,照旧走它那一档。
  // **`partial` 的来源不算**(列表卡、搜索卡只摊那一篇的一截):切片里分不出两处
  // 可能只是前后文窗口被切片边界截断了,那是**未知**,不是「已证明分不出」——
  // 整篇那一份下面会重算一遍,那一份才作数。
  // 事故形态与实测证据见 `docs/decisions/fav-snippets.md`。
  const stuck = !src.partial && !spec.locatable;
  const reason = !got || got.text == null
    ? ((got && got.reason) || 'unreadable')
    // spec 和收藏行的字段名一样,所以同一个 snipSpecOf 就能转。
    // **这几个数都不许在这儿写死。**
    // `u` 按屏幕那一份算(`spec.unique`):锚点是从屏幕那一份上采的,屏幕上就有
    //   两处时谁也说不出用户选的是第几处,而盘上这一篇里只剩一处恰恰说明原处被
    //   删掉了 —— 采信它就是把另一处同句冒充成原位置(审查实测:旧抽屉上选的是
    //   第二节,盘上那一节已被删掉,高亮落在第一节)。`partial` 的来源(列表卡、
    //   搜索卡只摊那一篇的一截)`spec.unique` 本来就是 0,同口径。
    // `same` 按**证明得出来的**算(`sameCorpus`),不许写死 true。一侧对不上在
    //   这一步确实常见(两头的渲染不一样),可「常见」证明不了「这两份是同一份」:
    //   版本号不等、或者来源压根说不出版本的,只听另一侧那个唯一命中就是审查实测
    //   那两条 —— 一条高亮落在他从没选过的那一节,一条落在改稿新写的那一节
    //   (见 `sameCorpus`)。证明不了就一律判 `mismatch`,那一头收场有三种(退路、
    //   409、照存并当场说清定位不到),没有一种是悄悄存成功。
    // `e` 按采集那一刻算出来的那个事实传(上面那个 `stuck`),**不许让它缺席** ——
    //   缺席在 `snipSeek` 那头是「不知道当时定不定得住」(老收藏才这样),而这儿
    //   明明知道。
    : (snipSeek(got.text, { ...snipSpecOf(spec), u: spec.unique ? 1 : 0,
                            ...(stuck ? { e: 0 } : {}),
                            same: sameCorpus(src, got) }) ? '' : 'mismatch');
  if (!reason) {
    // 核过了,顺手把**唯一性和定位**按盘上那一篇重算一遍 —— 上面那两个数是按屏幕
    // 上那个容器算的,而**容器未必是整篇**(列表卡片摊的是正文开头那一截)。切片里
    // 「只有一处」不代表整篇里只有一处;定位同理,要按回跳那一头真正的语料算。
    // 这一份比 `snipSpec` 那一份准,`saveSnip` 用它替掉。
    const flat = snipCollapse(spec.quote).trim();
    const uniq = snipAll(got.text, flat).length === 1;
    // `u` 用的是**会落库的那一个**(上面这个 `uniq`),不是核对那一步的 0:这一问
    // 是「回跳那一刻定得到吗」,那一头拿的就是库里这几个数。`same` 在这一问上是
    // 事实:落库带的是**刚读到的这一版**(下面那一句),回跳那一刻 `v` 就等于
    // 那一页的 `DOC_VERSION` —— 除非那一篇又被换了一版,而那时这个数本来也不该
    // 替它回答。
    const locatable = !!snipSeek(got.text,
      { ...snipSpecOf(spec), u: uniq ? 1 : 0, same: true });
    const fix = { unique: uniq, locatable, anchored: locatable };
    return { src: { ...src, doc_version: got.version }, reason: '', fell: false, fix };
  }
  // **核不过 = 这条锚点在回跳那一头定不住**,这件事要跟着收藏落库
  // (`anchor_exact`)—— 不落的话库里那条带着屏幕那一份算出来的「定得住」,而
  // 屏幕上明明刚说过一句「点回去可能定位不到这一处」:同一件事两处口径不一样,
  // 点回去于是可能落在别处一句同样的文字上。**和 `locatable` 分开两个字段**:
  // 那一个只管屏幕上那句提示(说的是「这一篇里有几处一模一样的文字」),
  // 核不过的原因通常不是重复,两句不许混成一句。
  if (src.fallback) return { src: src.fallback, reason, fell: true };
  // **核不过、又没有退路可退,而这个来源自己说不出它展示的是哪一版** —— 刚读到的
  // 那一版就是它唯一能说的版本,记下来。绑了文档却交不出版本号的话后端按 400 拒
  // (那一道是对的:没有这个数就没有共同事实可绑),于是用户得到一句「绑文档的
  // 收藏必须带 doc_version」—— 一句他无法处置的话,而这段话本来是存得下的
  // (搜索卡那一头摊的是命中那一篇的切片,`kb/chunker.py` 把 `*` 剥了却留着
  // `~~`,于是一句带删除线的话在卡片上和那一页上就是两份文本,核必然不过)。
  // **反过来,来源自带版本号的一律不许替换**(图鉴条目带的是面板上那一份的版本):
  // 那个数和盘上此刻**不等**正是「面板旧了」的唯一信号,后端那道 409 全靠它,
  // 替成新的之后改稿删掉那句话也照存(回归在 `tests/snip_probe_bind.mjs`)。
  const fix = got && got.text != null ? { anchored: false } : null;
  if (!src.doc_version && got && got.version) {
    return { src: { ...src, doc_version: got.version }, reason, fell: false, fix };
  }
  return { src, reason, fell: false, fix };
}

/** 服务端那一篇文档 → `{text, version, reason}`。
 *  `text` 是「点回去那一页会看到的那份文本」(已压平),`version` 是那一篇
 *  **正文**此刻的版本号(后端 `scanner.body_version`,落库时拿它核对)。
 *
 *  读不到时 `text` 为 null,**理由分两种**:`absent` = 确定不在(404:还没进
 *  索引、文件已经不在了),`unreadable` = 这一刻读不出来、不知道(请求抛了、
 *  5xx、JSON 坏了、服务端没报版本号)。调用方据此退一步,不许把"读不到"当成
 *  "核过了",也不许把"不知道"说成"确定没有"。
 *
 *  **版本号缺了按 `unreadable` 办**:没有这个数就没有共同事实可绑,绑上去就是
 *  一条核对不了的收藏(后端那一头也会拒,见 `favorites_api` 的保存契约)。 */
async function snipDocText(path) {
  const no = reason => ({ text: null, version: '', reason });
  let r = null;
  try { r = await fetch('/api/doc?path=' + encodeURIComponent(path)); }
  catch { return no('unreadable'); }
  if (r.status === 404) return no('absent');
  if (!r.ok) return no('unreadable');
  let d = null;
  try { d = await r.json(); } catch { return no('unreadable'); }
  const version = String(d.version || '');
  if (!version) return no('unreadable');
  // frontmatter 剥掉:阅读页渲染前剥的就是这一段(`kb/web/pages.py` 的
  // `doc_page`,同一条正则),不剥的话标题那几行也进了比对语料。
  const body = String(d.content || '').replace(/^---\n[\s\S]*?\n---\n/, '');
  return { text: snipRenderedText(body), version, reason: '' };
}

/** 一份 md 正文 → 「阅读页那一头会看到的那份文本」(已压平)。
 *  走 DOMParser 而不是往页面里塞一个 div:那是个惰性文档,不会去加载正文里的
 *  图片,也不会把一棵临时 DOM 挂进文档树。
 *
 *  **渲染器必须是阅读页那一个,不是面板自己的 `mdHtml`。** 两者只差一条规则:
 *  正文里的原始 HTML,`mdHtml` 交给 DOMPurify(于是 `<strong>x</strong>` 变成
 *  一个粗体元素、标签本身看不见),阅读页把它转义成**字面文字**(`<strong>`
 *  四个尖括号字连着显示,见 `kb/web/pages.py` 的 `_DOC_LITERAL_JS` ——
 *  那是为了让正文里的 `List<T>`、`<dependency>` 这类示例文字别被净化器吃掉)。
 *  **这一条之差就是"核过了却定位不回去"**:用户在图鉴抽屉里选中一句跨过
 *  `<strong>` 的话,屏幕上和 `mdHtml` 文本里它都是连着的一句,而阅读页那一头
 *  中间插着四个尖括号字 —— 核对照旧通过、收藏落成 `origin=doc` 返回 200,点
 *  回去 0 个高亮,而文件一个字都没改(审查实测)。所以这里用**和阅读页同一条
 *  规则**的一个**独立 marked 实例**:`marked.use()` 是全局的,拿它改会把问答
 *  回答、日报正文、条目正文一起改掉(`core.js` 的 `mdHtml` 那条契约
 *  不许动 —— 日报出处裁决器按它逐位对齐)。
 *  **`html(t)` 那三行是 `_DOC_LITERAL_JS` 的副本,改一处要改两处。**
 *  两个库没加载出来时退回原文本身 —— 那一头同样会退成纯文本显示(`MD_OK`)。*/
let _snipMd = null;
function snipDocRenderer() {
  if (_snipMd) return _snipMd;
  if (!window.marked || !marked.Marked) return null;
  _snipMd = new marked.Marked({ renderer: { html(t) {
    // v12 给的是字符串,v13 起给 token 对象;拿错了会抛异常、正文整个空白
    const s = typeof t === 'string' ? t : (t && t.text) || '';
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  } } });
  return _snipMd;
}

function snipRenderedText(md) {
  const src = String(md == null ? '' : md);
  const md1 = snipDocRenderer();
  // 失败关闭同 `mdHtml`:净化器没加载出来就别放未净化的 HTML 进 DOMParser
  const html = md1 && window.DOMPurify ? DOMPurify.sanitize(md1.parse(src)) : null;
  if (html == null) return snipCollapse(src);
  const doc = new DOMParser().parseFromString(html, 'text/html');
  // **`textContent` 不行**:元素产生的那几处换行它一个都不给,而 `quote` 里带着
  // 它们(见 `snipSpec`)—— 核验于是会把一条好收藏判成"两头文本不一样",当场多说
  // 一句"点回去可能定位不到"。走同一份 `SNIP_BREAKS`,压平之后两头才对得上。
  return snipCollapse(snipFragText(doc.body));
}

/** 这条来源链接超长、于是没送(见 SNIP_MAX_URL)。**判据只有一份**:送不送和
 *  要不要告诉用户是同一件事,`saveSnip` 据它补一句提示。 */
function snipUrlDropped(src) {
  return String(src.origin_url || '').length > SNIP_MAX_URL;
}

function postSnip(src, spec, anchored) {
  return fetch('/api/favorites', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: src.path || '', quote: spec.quote,
      // **绑了文档就必须说清绑的是哪一版正文。** 后端落库前按盘上此刻那一份
      // 再算一次,不等就拒(409)—— 核对完到真写进库这一下,重译写回/正文补抓
      // 照样会把那一篇换成另一版。没有 path 的那几类(daily/chat/feed)不绑
      // 文档,这个字段空着。口径在 `kb/web/favorites_api.py` 的保存契约。
      doc_version: src.doc_version || '',
      prefix: spec.prefix, suffix: spec.suffix, note: src.note || '',
      // **采集那一刻这段引文在那一篇里是不是只有一处。** 回跳那一刻两侧上下文都
      // 失效时按它决定能不能采信裸引文那一档 —— 当时就有两处的话,「现在只剩一处」
      // 证明不了这一处就是当时那一处(见 snip_text.js 的 `snipSeek`)。
      quote_unique: spec.unique ? 1 : 0,
      // **采集那一刻这条锚点定不定得住。** 定不住的(这一篇里有几处连前后文都
      // 一样、或者核对读到的那一份里压根对不上)往后哪一档都不给用 —— 回跳那一
      // 刻的「正好剩一处」只说明别处被删掉了,采信它就是把一处从没被收藏过的
      // 文字高亮出来(见 snip_text.js 的 `snipSeek`)。
      anchor_exact: anchored ? 1 : 0,
      origin: src.origin, origin_ref: src.origin_ref || '',
      // **问答那一类:这段话出自哪一条已经落库的消息。** 后端据它核验归属 ——
      // 会话号非空只说明那个对话存在,不说明这一轮写进去了(见 chat.js 的
      // `SNIP_CID`、`kb/web/favorites_api.py` 的问答归属核验)。
      origin_msg: src.origin_msg || 0,
      // 标题裁尾巴也不许切开一个字(见 snipWhole)
      origin_title: snipClip(src.origin_title, SNIP_MAX_TITLE),
      origin_url: snipUrlDropped(src) ? '' : (src.origin_url || ''),
    }),
  });
}

// 收藏面板可能正开着(侧栏切过去不卸载它),顺手刷一下
function refreshFavPanel() {
  if (typeof loadFavPanel === 'function' && $('favpanel').style.display !== 'none') {
    loadFavPanel();
  }
}

/** range 落在容器里哪一段上。给 describe 用:一个容器里并排放着多段正文时,
 *  按 selector 往上找那一段的宿主元素。range 的端点可能是文本节点。 */
/** 这一刻有没有一段**非折叠选区**落在 el 里面。给「拖选不是点击」那类守卫用:
 *  列表卡片整块可点(点开右侧阅读器),而卡片里现在能选正文收藏 —— 一次拖选松手
 *  时浏览器照样派一个 click,不挡住的话那次打开会把选区连同浮动按钮一起带走。
 *  真的单击时 mousedown 已经把选区折叠掉了,所以这道守卫不影响点开。*/
function snipSelectionIn(el) {
  const sel = typeof getSelection === 'function' ? getSelection() : null;
  if (!el || !sel || sel.isCollapsed || !sel.rangeCount) return false;
  return el.contains(sel.getRangeAt(0).commonAncestorContainer);
}

function snipHostOf(range, selector) {
  const node = range.commonAncestorContainer;
  const el = node.nodeType === 1 ? node : node.parentElement;
  return el ? el.closest(selector) : null;
}

// 「选完了」这件事三种输入方式各报一次,**一处也不许少**:鼠标是 mouseup、
// 触摸是 touchend(手机上长按选词没有 mouseup)、键盘和辅助技术是 keyup
// (Shift+方向键选字,从头到尾不会有 mouseup/touchend —— 少了这一处,只用键盘的
// 人压根看不到那颗按钮)。都要等浏览器把选区结算完,所以统一延一拍。
// `selectionchange` 只管**收起**:它在拖选途中一直在响,拿它弹按钮的话按钮会跟着
// 每个字闪一下。
const snipSettle = e => {
  if (snipBtn && snipBtn.contains(e.target)) return;
  setTimeout(showSnipBtn, 10);
};
document.addEventListener('mouseup', snipSettle);
document.addEventListener('touchend', snipSettle);
document.addEventListener('keyup', e => {
  // 只认真正会改选区的那几个键 —— 别的键一律不惊动按钮(Escape 另有收起那一道)
  if (e.key === 'Shift' || e.key.startsWith('Arrow') || e.key === 'Home'
      || e.key === 'End' || e.key === 'PageUp' || e.key === 'PageDown'
      || (e.key === 'a' && (e.ctrlKey || e.metaKey))) snipSettle(e);
});
document.addEventListener('selectionchange', () => {
  const sel = getSelection();
  if (!sel || sel.isCollapsed) hideSnipBtn();
});
// 滚动时按钮会留在原地、指向别处的文字 —— 跟着选区重算位置
addEventListener('scroll', () => { if (snipBtn && !snipBtn.hidden) showSnipBtn(); }, true);
addEventListener('resize', () => { if (snipBtn && !snipBtn.hidden) hideSnipBtn(); });
addEventListener('keydown', e => { if (e.key === 'Escape') hideSnipBtn(); });

// ── 回跳:在某个容器里找到那段话,高亮 + 滚到眼前 ─────────────────────────
// 高亮走 CSS Custom Highlight API(`::highlight(kb-quote)` 在 snip.css 里),
// 它不动 DOM —— 包一层 <mark> 的话,那几个面板下一次重渲染就把标记连正文一起
// 换掉了,而且会打断 `dailyRefMap` 那类按节点结构找东西的代码。
// 浏览器没有这个 API 时只滚动定位,不高亮(同阅读页的守卫)。
function clearSnipHighlight() {
  if (window.CSS && CSS.highlights) CSS.highlights.delete('kb-quote');
}

/** 在 el 里定位 spec={q,p,s}。找到回 true。
 *  `skip` 要和采集那一头传的是同一条选择器(见 `snipTextIndex`)。**这一道是冗余带**:
 *  下面按内容找(不按采集时的偏移找),所以多出来的那几段界面文字现在影响不到结果,定点
 *  注入打不红它(决策文档里那条 G11)。留着是因为**两头同一个坐标系**是这条链的前提 ——
 *  哪天定位改回按偏移走,它就是唯一那道带子。*/
function locateSnip(el, spec, skip) {
  clearSnipHighlight();
  if (!el || !spec || !spec.q) return false;
  const idx = snipTextIndex(el, skip);
  const hit = snipSeek(idx.text, spec);
  if (!hit) return false;
  const range = snipOffsetRange(idx, hit[0], hit[1]);
  if (!range) return false;
  if (window.Highlight && window.CSS && CSS.highlights) {
    CSS.highlights.set('kb-quote', new Highlight(range));
  }
  scrollSnipRange(range);
  return true;
}
